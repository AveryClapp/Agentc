"""One predeclared full factorial repeat; repeated development, not confirmation.

Reuse the unchanged frozen acquisition engine. Bind the complete primary raw
evidence and one campaign-wide repeat claim; never selectively repeat outcomes.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import copy, deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path

from bench import openrouter_typed_factorial as study
from bench.openrouter_matrix import file_hash, write_json
from bench.openrouter_pilot import PilotError, digest, load_key, money

KIND = "typed-factorial-matched-repeat-v1"
PRIMARY_FILES = (
    "manifest.json",
    "fixture.json",
    "calls.json",
    "intents.json",
    "summary.json",
)
LIMITATIONS = [
    "Exactly one full repeat of the same32 generated development tasks; not fresh confirmation or a heldout evaluation.",
    "All four cells repeat in the original order in a later time block; provider/time effects are not separated from stochastic variation.",
    "Each cell uses its own newly produced history; no original outputs or gold answers are fed into repeated calls.",
    "Workflow reproducibility is not fixed-request model repeatability: earlier output differences can change downstream payloads.",
    "Resampling units are the32 tasks, retaining both realizations and all four cells together; not64 independent tasks.",
    "No adaptive policy, native admission, risk certificate or control-loop amortization is tested.",
    "All costs remain charged, including the original feasibility pilot and every historical uncertainty hold.",
]


def primary_args(args):
    result = copy(args)
    result.output = args.primary
    result.manifest_sha256 = args.primary_manifest_sha256
    return result


def read(path):
    return json.loads(path.read_text())


def complete(report):
    finance = report["finance"]
    return (
        report.get("schedule_complete") is True
        and report.get("paper_evidence") is False
        and report.get("native_admission_tested") is False
        and report.get("efficacy_claim") is False
        and len(report["paired_tasks"]) == study.COUNT
        and finance.get("snapshot_financially_complete") is True
        and not finance.get("failed_provider_results")
        and not finance.get("additional_attempt_records")
        and money(finance["retained_uncertainty_usd"]) == 0
        and money(finance["pending_upper_bound_usd"]) == 0
    )


def reconstruct_primary(args, key):
    original_args = primary_args(args)
    manifest = read(args.primary / "manifest.json")
    study.analyze(original_args, key, manifest)
    report = read(args.primary / "analysis.json")
    if not complete(report):
        raise PilotError("matched repeat requires a complete verified primary")
    return manifest, report


def primary_hashes(args):
    return {name: file_hash(args.primary / name) for name in PRIMARY_FILES}


def same_setup(primary, repeated):
    # Only freeze/catalog observation times may refresh, not model versions,
    # actual endpoint facts, prices, capabilities, policy or request semantics.
    def timestamp(value):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise PilotError("repeat freeze requires UTC observation times")
        return parsed

    def normalized(manifest):
        value = deepcopy(manifest)
        created = timestamp(value.pop("created_at"))
        catalog = value["catalog"]
        observed_text = catalog.pop("observed_at_utc")
        observed = timestamp(observed_text)
        if observed > created:
            raise PilotError("catalog observation follows its freeze")
        for target in catalog["targets"]:
            for field in ("price", "provenance"):
                if target[field].pop("observed_at_utc") != observed_text:
                    raise PilotError("catalog observation timestamps are inconsistent")
        return value, created, observed

    left, created_left, observed_left = normalized(primary)
    right, created_right, observed_right = normalized(repeated)
    if (
        left != right
        or created_right <= created_left
        or observed_right < observed_left
        or study.stage_id(primary) == study.stage_id(repeated)
    ):
        raise PilotError("repeat setup or original-order contract changed")


def repeat_claim(args):
    ledger = args.ledger.resolve(strict=True)
    return ledger.with_name(
        ledger.name + ".matched-repeat-" + args.primary_manifest_sha256 + ".claim"
    )


def prepare(args, key):
    if repeat_claim(args).exists() or (args.output / "repeat_protocol.json").exists():
        raise PilotError("primary already has a matched repeat or protocol")
    primary, _ = reconstruct_primary(args, key)
    result = study.prepare(args, key)
    manifest = read(args.output / "manifest.json")
    same_setup(primary, manifest)
    protocol = {
        "kind": KIND,
        "primary": str(args.primary.resolve()),
        "primary_manifest_sha256": digest(primary),
        "primary_files": primary_hashes(args),
        "repeat_manifest_sha256": digest(manifest),
        "source_sha256": file_hash(Path(__file__)),
        "repeats": 1,
        "declared_before_primary_quality_inspection": "bd-323l.6.13.8.6.20.13; primary195/384calls",
        "limitations": LIMITATIONS,
    }
    write_json(args.output / "repeat_protocol.json", protocol, immutable=True)
    return {**result, "repeat_protocol_sha256": digest(protocol)}


def validate(args, key):
    protocol = read(args.output / "repeat_protocol.json")
    manifest = read(args.output / "manifest.json")
    expected = {
        "kind": KIND,
        "primary": str(args.primary.resolve()),
        "primary_manifest_sha256": args.primary_manifest_sha256,
        "primary_files": primary_hashes(args),
        "repeat_manifest_sha256": args.manifest_sha256,
        "source_sha256": file_hash(Path(__file__)),
        "repeats": 1,
        "declared_before_primary_quality_inspection": "bd-323l.6.13.8.6.20.13; primary195/384calls",
        "limitations": LIMITATIONS,
    }
    if protocol != expected or digest(protocol) != args.repeat_protocol_sha256:
        raise PilotError("repeat protocol, primary evidence or source changed")
    primary, report = reconstruct_primary(args, key)
    same_setup(primary, manifest)
    study.validate(args, manifest, key)
    return manifest, report


def run(args, key):
    manifest, _ = validate(args, key)
    # Full-primary identity, not the fresh repeat stage, prevents two different
    # repeat manifests or copied directories from scheduling another repetition.
    try:
        with repeat_claim(args).open("x") as handle:
            handle.write(args.repeat_protocol_sha256 + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise PilotError(
            "exact primary already has its one repeat; audit only"
        ) from None
    return study.run(args, key, manifest)


def compare(primary, repeated, pilot_cost):
    if not complete(primary) or not complete(repeated):
        raise PilotError("no repeat comparisons from a partial matrix")
    first = primary["paired_tasks"]
    second = repeated["paired_tasks"]
    if len({r["task_id"] for r in first}) != study.COUNT or [
        r["task_id"] for r in first
    ] != [r["task_id"] for r in second]:
        raise PilotError("repeat task identity or pairing changed")
    paired = []
    counts = {arm: Counter() for arm in study.ARMS}
    for left, right in zip(first, second):
        for row in (left, right):
            if set(row["cells"]) != set(study.ARMS) or any(
                row[k] != value for k, value in study.contrasts(row["cells"]).items()
            ):
                raise PilotError("repeat cells or interaction changed")
            if any(
                type(c["em"]) not in (int, float) or c["em"] not in (0, 1)
                for c in row["cells"].values()
            ):
                raise PilotError("repeat quality is not exact binary")
        for arm in study.ARMS:
            counts[arm][
                f"{int(left['cells'][arm]['em'])}{int(right['cells'][arm]['em'])}"
            ] += 1
        paired.append(
            {
                "task_id": left["task_id"],
                "primary": left,
                "repeat": right,
                "mean_quality_interaction": (
                    left["quality_interaction"] + right["quality_interaction"]
                )
                / 2,
                "quality_interaction_change": right["quality_interaction"]
                - left["quality_interaction"],
            }
        )
    cells = []
    for arm in study.ARMS:
        x, y = [r["cells"][arm] for r in first], [r["cells"][arm] for r in second]
        primary_cost = sum((money(c["cost_usd"]) for c in x), Decimal(0))
        repeat_cost = sum((money(c["cost_usd"]) for c in y), Decimal(0))
        cells.append(
            {
                "arm": arm,
                "tasks": study.COUNT,
                "realizations_per_task": 2,
                "quality_transitions_00_01_10_11": {
                    k: counts[arm][k] for k in ("00", "01", "10", "11")
                },
                "primary_exact": sum(int(c["em"]) for c in x),
                "repeat_exact": sum(int(c["em"]) for c in y),
                "quality_disagreements": counts[arm]["01"] + counts[arm]["10"],
                "primary_cost_usd": str(primary_cost),
                "repeat_cost_usd": str(repeat_cost),
                "primary_resources": resources(x),
                "repeat_resources": resources(y),
                "descriptive_task_cluster_intervals": study.analysis.paired_bootstrap(
                    [
                        {
                            "mean_em": (a["em"] + b["em"]) / 2,
                            "em_change": b["em"] - a["em"],
                        }
                        for a, b in zip(x, y)
                    ],
                    seed=991,
                ),
            }
        )
    known = money(primary["finance"]["known_stage_usd"]) + money(
        repeated["finance"]["known_stage_usd"]
    )
    return {
        "kind": KIND,
        "paper_evidence": False,
        "adaptive_policy_tested": False,
        "independent_tasks": study.COUNT,
        "cells": cells,
        "paired_tasks": paired,
        "descriptive_task_cluster_interactions": study.analysis.paired_bootstrap(
            [
                {
                    k: r[k]
                    for k in ("mean_quality_interaction", "quality_interaction_change")
                }
                for r in paired
            ],
            seed=991,
        ),
        "both_matrices_known_usd": str(known),
        "feasibility_pilot_known_usd": str(money(pilot_cost)),
        "matrices_plus_feasibility_known_usd": str(known + money(pilot_cost)),
        "limitations": LIMITATIONS
        + [
            "Degenerate bootstrap intervals do not imply certainty; two realizations do not isolate all provider randomness."
        ],
    }


def resources(cells):
    return {
        "nominal_uncached_cost_usd": str(
            sum((money(c["nominal_uncached_cost_usd"]) for c in cells), Decimal(0))
        ),
        **{
            key: sum(c[key] for c in cells)
            for key in (
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "cache_unknown_calls",
                "observational_latency_ms",
            )
        },
    }


def analyze(args, key):
    manifest, primary = validate(args, key)
    study.analyze(args, key, manifest)
    repeated = read(args.output / "analysis.json")
    first_generations = {r["generation_id"] for r in read(args.primary / "calls.json")}
    second_generations = {r["generation_id"] for r in read(args.output / "calls.json")}
    if first_generations & second_generations:
        raise PilotError("repeat reuses a primary provider generation")
    report = compare(primary, repeated, manifest["pilot_evidence"]["known_cost_usd"])
    report.update(
        {
            "repeat_protocol_sha256": args.repeat_protocol_sha256,
            "primary_analysis_sha256": digest(primary),
            "repeat_analysis_sha256": digest(repeated),
        }
    )
    write_json(args.output / "repeat_analysis.json", report)
    return {
        "complete_matrices": 2,
        "independent_tasks": study.COUNT,
        "paper_evidence": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "analyze"))
    for name in ("env-file", "ledger", "output", "pilot", "bridge", "primary"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("pilot-manifest-sha256", "primary-manifest-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--repeat-protocol-sha256")
    args = parser.parse_args()
    try:
        result = {"prepare": prepare, "run": run, "analyze": analyze}[args.command](
            args, load_key(args.env_file)
        )
        print(json.dumps(result, allow_nan=False, indent=2))
        return 0
    except (
        PilotError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        study.subprocess.TimeoutExpired,
    ):
        print(
            "Matched repeat stopped at evidence, protocol or provider boundary; no raw diagnostics."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
