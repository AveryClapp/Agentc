"""Development-only paired suffix replay and unchanged-source noise controls.

Uses every recorded training exploration pair, including zero disagreement.
No native feedback, policy selection, heldout inference, or guard changes.
The archived reference and candidate each get the same source-only suffix;
a third branch repeats the original source request and then that suffix.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import time

from bench import openrouter_joint_analysis as analysis
from bench import openrouter_rules_live as live
from bench.openrouter_attempts import accounting
from bench.openrouter_contract import endpoints
from bench.openrouter_matrix import ROOT, file_hash, load_module, score, write_json
from bench.openrouter_pilot import (
    Ledger,
    PilotError,
    ProviderFailure,
    digest,
    load_key,
    money,
)
from bench.openrouter_replay import lexical_divergence, public_task
from bench.openrouter_rules_protocol import STAGES, workflow_call

PREVIOUS_HASH = "dcb703739e460810825f0966c06371d2848a4d17346afbfea4e77c52a0548ace"
FILES = (
    "manifest.json",
    "calls.json",
    "decisions.json",
    "intents.json",
    "training-gate.json",
    "static-selection.json",
    "summary.json",
)
CAP = "3"
BRANCHES = ("reference", "candidate", "repeat")


def sources():
    paths = {
        **live.sources(),
        **{
            p: file_hash(ROOT / p)
            for p in (
                "bench/openrouter_outcome_diagnostic.py",
                "bench/openrouter_joint_analysis.py",
                "bench/openrouter_rules_comparison.py",
                "bench/openrouter_rules_diagnostics.py",
                "bench/openrouter_rules_validity.py",
            )
        },
    }
    return paths


def previous(args):
    data = {name: json.loads((args.previous / name).read_text()) for name in FILES}
    if digest(data["manifest.json"]) != PREVIOUS_HASH:
        raise PilotError("not the externally frozen completed training study")
    events, ledger_hash = analysis.read_ledger_snapshot(args.ledger)
    checked = analysis.analyze(
        data["manifest.json"],
        data["decisions.json"],
        data["calls.json"],
        data["intents.json"],
        args.fixture,
        expected_manifest_sha256=PREVIOUS_HASH,
        gate=data["training-gate.json"],
        static=data["static-selection.json"],
        summary=data["summary.json"],
        ledger_events=events,
        ledger_sha256=ledger_hash,
    )
    validate_previous_report(data, checked)
    data["verified_known_stage_usd"] = checked["financial_snapshot"]["known_stage_usd"]
    return data


def validate_previous_report(data, checked):
    summary = data["summary.json"]
    finance = checked["financial_snapshot"]
    if (
        checked["failed_provider_results"]
        or checked["training_gate_reconstructed"] is None
        or checked["training_gate_reconstructed"]["proceed_to_heldout"]
        or not finance["snapshot_financially_complete"]
        or not checked["static_selection_verified"]
        or any(d["phase"] == "heldout" for d in data["decisions.json"])
        or summary["manifest_sha256"] != PREVIOUS_HASH
        or summary["calls_sha256"] != digest(data["calls.json"])
        or summary["decisions_sha256"] != digest(data["decisions.json"])
        or summary["completed_calls"] != len(data["calls.json"])
        or summary["completed_decisions"] != len(data["decisions.json"])
        or summary["training_gate"] != checked["training_gate_reconstructed"]
        or data["training-gate.json"] != checked["training_gate_reconstructed"]
        or money(summary["cost_usd"]) != money(finance["known_stage_usd"])
        or money(summary["artifact_cost_usd"])
        != sum((money(r["cost_usd"]) for r in data["calls.json"]), Decimal(0))
    ):
        raise PilotError("previous study is not a valid failed-admission training run")


def pairs_for(data):
    calls = {r["id"]: r for r in data["calls.json"]}
    history = {}
    pairs = []
    for d in data["decisions.json"]:
        key = (d["phase"], d["arm"], d["task_id"])
        prior = history.setdefault(key, {})
        stage = d["workflow_stage"]
        if set(prior) != set(STAGES[: STAGES.index(stage)]):
            raise PilotError("previous decisions are not ordered own-history workflows")
        signature = d["semantic_plan"]
        if d["phase"] == "training" and signature["candidate"] is not None:
            if (
                d["divergence_feedback"] is None
                or signature["kind"] != "pass_through"
                or signature["primary"]["model"] != live.SOURCE_MODEL
                or len(d["incurred_ids"]) != 2
            ):
                raise PilotError(
                    "diagnostic requires completed exact source-paired probes"
                )
            candidate = calls[d["incurred_ids"][1]]
            reference = calls[d["primary_id"]]
            pair = {
                "pair_id": digest([d["task_id"], d["arm"], stage, d["primary_id"]])[
                    :24
                ],
                "task_id": d["task_id"],
                "arm": d["arm"],
                "workflow_stage": stage,
                "history": deepcopy(prior),
                "reference_id": reference["id"],
                "candidate_id": candidate["id"],
                "reference_output": reference["answer"],
                "candidate_output": candidate["answer"],
                "reference_payload": signature["primary"],
                "candidate_payload_sha256": digest(signature["candidate"]),
                "candidate_identity": signature["candidate_identity"],
                "candidate_rules": signature["candidate_rules"],
                "lexical_divergence": d["divergence_feedback"],
            }
            pairs.append(pair)
        prior[stage] = calls[d["primary_id"]]["answer"]
    if not pairs or len({p["pair_id"] for p in pairs}) != len(pairs):
        raise PilotError("missing or duplicated training pairs")
    return sorted(pairs, key=lambda p: digest(["suffix-pair-order-v1", p["pair_id"]]))


def schedule_for(pairs):
    schedule = []
    for pair in pairs:
        branches = sorted(
            BRANCHES,
            key=lambda b: digest(["suffix-branch-order-v1", pair["pair_id"], b]),
        )
        start = STAGES.index(pair["workflow_stage"])
        for branch in branches:
            for stage in STAGES[start + (branch != "repeat") :]:
                schedule.append(
                    {
                        "pair_id": pair["pair_id"],
                        "branch": branch,
                        "task_id": pair["task_id"],
                        "workflow_stage": stage,
                    }
                )
    return schedule


def prepare(args, key):
    if (args.output / "manifest.json").exists():
        raise PilotError("diagnostic manifest is already frozen")
    data = previous(args)
    pairs = pairs_for(data)
    current = endpoints(key)[live.SOURCE_MODEL]
    old = data["manifest.json"]["endpoints"][live.SOURCE_MODEL]
    if any(current[k] != old[k] for k in ("tag", "provider_name", "name")):
        raise PilotError("source endpoint identity changed since original study")
    manifest = {
        "kind": "paired_source_suffix_development_v1",
        "paper_evidence": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_files": sources(),
        "previous_manifest_sha256": PREVIOUS_HASH,
        "previous_files": {n: file_hash(args.previous / n) for n in FILES},
        "fixture_sha256": file_hash(args.fixture),
        "pairs": pairs,
        "schedule": schedule_for(pairs),
        "stage_cap_usd": CAP,
        "source_model": live.SOURCE_MODEL,
        "endpoints": {live.SOURCE_MODEL: current},
        "prompts": data["manifest.json"]["prompts"],
        "scoring": "unchanged_normalized_raw_answer_EM_F1",
        "limitations": [
            "All observed training probes, adaptively selected under the original lexical guard; not a random workload sample.",
            "Archived candidate/reference outputs followed by new source-only suffixes; not an on-policy joint-system trial.",
            "Repeat branch measures source-call plus suffix variability; archived-reference suffix is also stochastic.",
            "Gold is used only in offline diagnostics, never to dispatch, choose pairs, change guards or train a controller.",
            "Question overlap across arms means pairs are dependent; no independent-pair confidence interval is reported.",
            "Endpoint aliases and time changes are not controlled by temperature zero or provider pinning.",
            "No efficacy or safety claim; heldout questions remain untouched and old failed gate remains failed.",
            "Archive acquisition costs are sunk but not free; incremental and previous full-stage costs are reported separately.",
        ],
    }
    write_json(args.output / "manifest.json", manifest, immutable=True)
    return {
        "manifest_sha256": digest(manifest),
        "pairs": len(pairs),
        "scheduled_calls": len(manifest["schedule"]),
        "stage_cap_usd": CAP,
    }


def validate_manifest(manifest, args, data):
    if (
        digest(manifest) != args.manifest_sha256
        or manifest["source_files"] != sources()
        or manifest["stage_cap_usd"] != CAP
        or manifest["source_model"] != live.SOURCE_MODEL
        or manifest["previous_manifest_sha256"] != PREVIOUS_HASH
        or manifest["previous_files"]
        != {n: file_hash(args.previous / n) for n in FILES}
        or manifest["fixture_sha256"] != file_hash(args.fixture)
        or manifest["pairs"] != pairs_for(data)
        or manifest["schedule"] != schedule_for(manifest["pairs"])
    ):
        raise PilotError(
            "diagnostic source, archive, contract or frozen manifest changed"
        )


def retry_call(ledger, key, call_id, stage, payload, meta):
    while True:
        try:
            return ledger.call(key, call_id, stage, money(CAP), payload, meta)
        except ProviderFailure as exc:
            if not exc.details.get("retryable"):
                raise
            delay = max(
                5.0,
                exc.details.get("retry_not_before_epoch", time.time()) - time.time(),
                exc.details.get("retry_after_seconds", 0.0),
            )
            print(
                json.dumps({"retry_wait_seconds": delay, "call_id": call_id}),
                flush=True,
            )
            while delay > 0:
                pause = min(30, delay)
                time.sleep(pause)
                delay -= pause


def request_for(item, pair, history, task, attention, manifest):
    if item["branch"] == "repeat" and item["workflow_stage"] == pair["workflow_stage"]:
        return deepcopy(pair["reference_payload"])
    call = workflow_call(
        public_task(task),
        item["workflow_stage"],
        history,
        attention,
        model=live.SOURCE_MODEL,
        prompts=manifest["prompts"],
        run_identity="outcome-diagnostic/" + pair["pair_id"] + "/" + item["branch"],
    )
    return live.payload_for(call, manifest)


def acquire(args, manifest, data, ledger, key, attention):
    path = args.output / "calls.json"
    saved = json.loads(path.read_text()) if path.exists() else []
    if not isinstance(saved, list) or len(saved) > len(manifest["schedule"]):
        raise PilotError("invalid diagnostic journal length")
    if args.max_calls is not None and (
        type(args.max_calls) is not int
        or args.max_calls < 1
        or args.max_calls < len(saved)
    ):
        raise PilotError("requested call cap precedes saved evidence or is invalid")
    tasks = {
        t["task_id"]: public_task(t)
        for t in json.loads(args.fixture.read_text())
        if t["task_id"] in {p["task_id"] for p in manifest["pairs"]}
    }
    pairs = {p["pair_id"]: p for p in manifest["pairs"]}
    histories = {}
    for pair in pairs.values():
        for branch in BRANCHES:
            h = deepcopy(pair["history"])
            if branch != "repeat":
                h[pair["workflow_stage"]] = pair[branch + "_output"]
            histories[(pair["pair_id"], branch)] = h
    stage = "outcome-diagnostic-v1-" + digest(manifest)[:20]
    rows, generations = [], set()
    for index, item in enumerate(manifest["schedule"]):
        if args.max_calls is not None and index >= args.max_calls:
            break
        pair = pairs[item["pair_id"]]
        history = histories[(item["pair_id"], item["branch"])]
        payload = request_for(
            item, pair, history, tasks[item["task_id"]], attention, manifest
        )
        endpoint = manifest["endpoints"][live.SOURCE_MODEL]
        meta = {
            **item,
            "phase": "development",
            "manifest_sha256": digest(manifest),
            "dispatch_contract": {
                "provider_name": endpoint["provider_name"],
                "endpoint_model": endpoint["name"].split(" | ", 1)[1],
            },
        }
        call_id = stage + "-" + digest(item)[:24]
        # Cached Ledger.call validates the exact request/metadata and its real
        # successful attempt before any future scheduled dispatch can occur.
        result = retry_call(ledger, key, call_id, stage, payload, meta)
        row = {
            **item,
            **{
                k: v for k, v in result.items() if k not in {"key_id", "metadata", "at"}
            },
            "request_sha256": digest(payload),
            "cached_input_tokens": result["usage"]
            .get("prompt_tokens_details", {})
            .get("cached_tokens"),
            "nominal_uncached_cost_usd": str(
                money(endpoint["pricing"]["prompt"]) * result["usage"]["prompt_tokens"]
                + money(endpoint["pricing"]["completion"])
                * result["usage"]["completion_tokens"]
            ),
        }
        analysis.validate_row(row, payload, meta, manifest)
        generation = row.get("generation_id")
        if (
            not isinstance(generation, str)
            or not generation
            or generation in generations
        ):
            raise PilotError("missing or repeated generation identity")
        generations.add(generation)
        if index < len(saved) and saved[index] != row:
            raise PilotError("saved diagnostic result differs from immutable ledger")
        rows.append(row)
        history[item["workflow_stage"]] = row["answer"]
        if index >= len(saved):
            write_json(path, rows)
            print(
                json.dumps(
                    {
                        "completed_calls": len(rows),
                        "scheduled_calls": len(manifest["schedule"]),
                        "branch": item["branch"],
                        "site": item["workflow_stage"],
                        "cost_usd": row["cost_usd"],
                    }
                ),
                flush=True,
            )
    complete = len(rows) == len(manifest["schedule"])
    comparisons = []
    # Suppress favorable partial subsets, even for this diagnostic screen.
    if complete:
        gold = {
            t["task_id"]: t["expected"]
            for t in json.loads(args.fixture.read_text())
            if t["task_id"] in tasks
        }
        for pair in manifest["pairs"]:
            outputs = {b: histories[(pair["pair_id"], b)]["answer"] for b in BRANCHES}
            comparisons.append(
                {
                    "pair_id": pair["pair_id"],
                    "task_id": pair["task_id"],
                    "arm": pair["arm"],
                    "workflow_stage": pair["workflow_stage"],
                    "candidate_rules": pair["candidate_rules"],
                    "candidate_identity": pair["candidate_identity"],
                    "callsite_lexical_divergence": pair["lexical_divergence"],
                    "candidate_endpoint_disagreement": 1
                    - score(outputs["candidate"], outputs["reference"])["f1"],
                    "repeat_endpoint_disagreement": 1
                    - score(outputs["repeat"], outputs["reference"])["f1"],
                    "scores": {
                        b: score(a, gold[pair["task_id"]]) for b, a in outputs.items()
                    },
                    "repeat_callsite_lexical_divergence": lexical_divergence(
                        histories[(pair["pair_id"], "repeat")][pair["workflow_stage"]],
                        pair["reference_output"],
                    ),
                }
            )
    with ledger.locked() as handle:
        financial = accounting(ledger.read(handle))
    paid = financial["known_by_stage"].get(stage, Decimal(0))
    holds = financial["holds_by_stage"].get(stage, Decimal(0))
    summary = {
        "kind": manifest["kind"],
        "paper_evidence": False,
        "efficacy_claim": False,
        "manifest_sha256": digest(manifest),
        "calls_sha256": digest(rows),
        "schedule_complete": complete,
        "completed_calls": len(rows),
        "scheduled_calls": len(manifest["schedule"]),
        "scheduled_pairs": len(manifest["pairs"]),
        "paired_comparisons": comparisons,
        "incremental_paid_usd": str(paid),
        "incremental_retained_uncertainty_usd": str(holds),
        "previous_stage_paid_usd": data["verified_known_stage_usd"],
        "archive_and_diagnostic_known_usd": str(
            paid + money(data["verified_known_stage_usd"])
        ),
        "incremental_artifact_paid_usd": str(
            sum((money(r["cost_usd"]) for r in rows), Decimal(0))
        ),
        "incremental_nominal_usd": str(
            sum((money(r["nominal_uncached_cost_usd"]) for r in rows), Decimal(0))
        ),
        "pairs_by_site": dict(Counter(p["workflow_stage"] for p in manifest["pairs"])),
        "campaign": ledger.summary(),
        "limitations": manifest["limitations"],
    }
    write_json(args.output / "summary.json", summary)
    return {k: v for k, v in summary.items() if k != "paired_comparisons"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    for name in ("env-file", "ledger", "fixture", "previous", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--max-calls", type=int)
    args = parser.parse_args()
    try:
        key = load_key(args.env_file)
        if args.command == "prepare":
            report = prepare(args, key)
        else:
            manifest = json.loads((args.output / "manifest.json").read_text())
            data = previous(args)
            validate_manifest(manifest, args, data)
            attention = load_module(
                "diagnostic_attention", ROOT / "python/agentc/_attention.py"
            )
            report = acquire(
                args, manifest, data, Ledger(args.ledger, key), key, attention
            )
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError):
        print(
            "Diagnostic stopped: validation, transport or budget boundary; inspect sanitized ledger/status."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
