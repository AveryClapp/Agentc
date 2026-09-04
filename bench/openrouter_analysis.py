"""Validate and describe a complete exploratory matrix without new API calls.

This post-hoc analysis does not select a policy, change the frozen EM/F1 scores,
or treat gold-token presence as semantic accuracy. Small-sample intervals are
descriptive Wilson intervals, not a safety certificate or a multiplicity-
adjusted statistical test.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from bench.openrouter_matrix import file_hash, normalize, score, write_json
from bench.openrouter_pilot import PilotError, digest, money


def gold_tokens_present(answer: str, expected: str) -> bool:
    """Diagnostic only: also matches quoted, contradicted, and negated answers."""
    words, gold = normalize(answer).split(), normalize(expected).split()
    return bool(gold) and any(words[i:i + len(gold)] == gold
                              for i in range(len(words) - len(gold) + 1))


def wilson(events: int, trials: int) -> list[float] | None:
    if not 0 <= events <= trials:
        raise PilotError("invalid binomial counts")
    if not trials:
        return None
    z = 1.959963984540054
    rate = events / trials
    scale = 1 + z * z / trials
    center = (rate + z * z / (2 * trials)) / scale
    half = z * math.sqrt(rate * (1 - rate) / trials + z * z / (4 * trials * trials)) / scale
    return [max(0.0, center - half), min(1.0, center + half)]


def validate(manifest: dict[str, Any], rows: list[dict[str, Any]],
             tasks: list[dict[str, Any]]) -> None:
    """Reject partial, duplicated, relabeled, or misattributed paired results."""
    if manifest.get("paper_evidence") is not False or manifest.get("kind") != "exploratory_factorial":
        raise PilotError("expected an exploratory matrix manifest")
    schedule = manifest["schedule"]
    if not schedule or len(schedule) != len(rows):
        raise PilotError("results are incomplete or have extra calls")
    task_map = {t["task_id"]: t for t in tasks}
    if len(task_map) != len(tasks):
        raise PilotError("fixture contains duplicate identities")
    identities, generations = set(), set()
    phase_tasks: dict[str, set[str]] = defaultdict(set)
    task_arms: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    models = set(manifest["endpoints"])
    stage = "matrix-v1-" + digest(manifest)[:20]
    for index, (item, row) in enumerate(zip(schedule, rows)):
        for field in ("task_id", "phase", "model", "provider_tag", "arm"):
            if row[field] != item[field]:
                raise PilotError("results do not follow the frozen schedule")
        identity = (row["phase"], row["task_id"], row["model"], row["arm"])
        if identity in identities or row["model"] not in models:
            raise PilotError("duplicate or unknown factorial arm")
        identities.add(identity)
        phase_tasks[row["phase"]].add(row["task_id"])
        task_arms[(row["phase"], row["task_id"])].add((row["model"], row["arm"]))
        if row["id"] != stage + f"-{index:04d}" or row["stage"] != stage:
            raise PilotError("result belongs to a different manifest or call")
        generation = row.get("generation_id")
        if not isinstance(generation, str) or not generation or generation in generations:
            raise PilotError("missing or duplicate provider generation")
        generations.add(generation)
        endpoint = manifest["endpoints"][row["model"]]
        if row["provider_tag"] != endpoint["tag"] or row["provider"] != endpoint["provider_name"]:
            raise PilotError("provider attribution differs from the frozen endpoint")
        if row.get("paper_evidence") is not False:
            raise PilotError("pilot row incorrectly claims paper evidence")
        if row["task_id"] not in task_map or row["expected"] != task_map[row["task_id"]]["expected"]:
            raise PilotError("result gold answer differs from the original fixture")
        for metric, value in score(row["answer"], row["expected"]).items():
            if not math.isclose(row[metric], value, rel_tol=0, abs_tol=1e-12):
                raise PilotError("saved score differs from frozen scoring")
        if money(row["cost_usd"]) != money(row["usage"]["cost"]):
            raise PilotError("result cost differs from provider usage")
        for field in ("prompt_tokens", "completion_tokens"):
            value = row["usage"][field]
            if type(value) is not int or value < 0:
                raise PilotError("invalid token accounting")
        kind = row["native_plan"]["kind"]
        if kind not in {"rewritten", "pass_through"} or (row["arm"] == "full" and kind != "pass_through"):
            raise PilotError("unexpected native plan for factorial arm")
    if set(phase_tasks) != {"warmup", "calibration", "holdout"}:
        raise PilotError("missing or unknown experiment phase")
    if sum(map(len, phase_tasks.values())) != len(set.union(*phase_tasks.values())):
        raise PilotError("questions overlap across phases")
    for phase, task_ids in phase_tasks.items():
        if len(task_ids) != manifest[phase + "_tasks"]:
            raise PilotError("phase task count differs from manifest")
        expected_arms = {(m, a) for m in models
                         for a in (["full"] if phase == "warmup" else ["full", "compress"])}
        if any(task_arms[(phase, task_id)] != expected_arms for task_id in task_ids):
            raise PilotError("phase is not a complete model by rewrite factorial")


def arm_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n": len(rows), "exact_matches": sum(int(r["em"]) for r in rows),
        "mean_f1": sum(r["f1"] for r in rows) / len(rows),
        "cost_usd": str(sum((money(r["cost_usd"]) for r in rows), Decimal(0))),
        "input_tokens": sum(r["usage"]["prompt_tokens"] for r in rows),
        "output_tokens": sum(r["usage"]["completion_tokens"] for r in rows),
        "truncated": sum(r["finish_reason"] == "length" for r in rows),
        "rewritten": sum(r["native_plan"]["kind"] == "rewritten" for r in rows),
        "nonexact_with_gold_tokens_diagnostic_only": sum(
            not r["em"] and gold_tokens_present(r["answer"], r["expected"]) for r in rows),
    }


def reduction(before: Decimal | int, after: Decimal | int) -> float | None:
    return float(1 - after / before) if before else None


def analyze(manifest: dict[str, Any], rows: list[dict[str, Any]],
            tasks: list[dict[str, Any]]) -> dict[str, Any]:
    validate(manifest, rows, tasks)
    groups: dict[tuple[str, str], dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    phase_costs: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        phase_costs[row["phase"]] += money(row["cost_usd"])
        if row["phase"] != "warmup":
            groups[(row["phase"], row["model"])][(row["task_id"], row["arm"])] = row
    paired = []
    for (phase, model), values in sorted(groups.items()):
        task_ids = sorted({task_id for task_id, _ in values})
        full = [values[(t, "full")] for t in task_ids]
        compressed = [values[(t, "compress")] for t in task_ids]
        transitions = Counter((int(a["em"]), int(b["em"])) for a, b in zip(full, compressed))
        before, after = arm_totals(full), arm_totals(compressed)
        loss_ids = [a["task_id"] for a, b in zip(full, compressed) if a["em"] and not b["em"]]
        gain_ids = [a["task_id"] for a, b in zip(full, compressed) if b["em"] and not a["em"]]
        paired.append({
            "phase": phase, "model": model, "paired_tasks": len(task_ids),
            "full": before, "compress": after,
            "input_token_reduction": reduction(before["input_tokens"], after["input_tokens"]),
            "cost_reduction": reduction(money(before["cost_usd"]), money(after["cost_usd"])),
            "strict_em_delta": (after["exact_matches"] - before["exact_matches"]) / len(task_ids),
            "strict_em_transitions": {"both_pass": transitions[(1, 1)], "loss": transitions[(1, 0)],
                                      "gain": transitions[(0, 1)], "both_fail": transitions[(0, 0)]},
            "loss_task_ids": loss_ids, "gain_task_ids": gain_ids,
            "loss_given_full_pass_wilson_95": wilson(len(loss_ids), before["exact_matches"]),
        })
    return {
        "schema_version": 1, "paper_evidence": False, "post_hoc_analysis": True,
        "manifest_sha256": digest(manifest), "results_sha256": digest(rows),
        "completed_calls": len(rows), "phase_cost_usd": {p: str(c) for p, c in sorted(phase_costs.items())},
        "total_matrix_cost_usd": str(sum(phase_costs.values(), Decimal(0))),
        "paired": paired,
        "limitations": manifest["limitations"] + [
            "Strict EM conflates answer correctness and answer-format compliance; token truncation can hide final answers.",
            "Gold-token presence is diagnostic only: it also accepts quoted, negated, and contradicted answers.",
            "Wilson intervals are descriptive, assume independent questions, and do not adjust for multiple comparisons.",
            "Zero observed loss is not proof of safety; undefined risk when no full-context answers pass is reported as null.",
            "No learned policy or policy choice is evaluated; calibration and holdout remain separate.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads((args.artifacts / "manifest.json").read_text())
        if file_hash(args.fixture) != manifest["fixture_sha256"]:
            raise PilotError("fixture hash differs from the frozen manifest")
        result = analyze(manifest, json.loads((args.artifacts / "results.json").read_text()),
                         json.loads(args.fixture.read_text()))
        write_json(args.artifacts / "paired_analysis.json", result, immutable=True)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Analysis stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
