"""Fixed model x native-rule materialization diagnostic, never planner admission.

One-shot acquisition: any prior intent or attempt makes this manifest read-only.
Invalid application outputs count as failures and propagate only within their arm.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import subprocess

from bench import openrouter_joint_analysis as analysis
from bench import openrouter_rules_live as live
from bench import openrouter_structured_study as feasibility
from bench import typed_relational_workload as typed
from bench.openrouter_contract import endpoints
from bench.openrouter_joint_study import restrict_call
from bench.openrouter_matrix import ROOT, file_hash, load_module, write_json
from bench.openrouter_pilot import (
    Ledger,
    PilotError,
    canonical,
    digest,
    load_key,
    money,
    upper_cost,
    validate_schema_endpoint,
)
from bench.openrouter_replay import public_task

KIND = "typed_fixed_factorial_opportunity_v1"
COUNT, SEED = 32, 2026090503
STAGE_CAP, KNOWN_PROGRESS_STOP = "9.5", "3"
ARMS = {
    "S0": (live.SOURCE_MODEL, False),
    "SR": (live.SOURCE_MODEL, True),
    "H0": (live.TARGET_MODEL, False),
    "HR": (live.TARGET_MODEL, True),
}
RULES = {"filter": "ContextCompress", "answer": "StateDrop"}
BRIDGE_SOURCE = "crates/agentc-optimizer/examples/typed_rule_materializer.rs"
FAILURE_POLICY = "application-invalid=count-loss-and-propagate-null;provider-failure=stop-no-retry;no-resume-v1"


def sources():
    result = live.sources(typed.WORKLOAD)
    paths = [
        ROOT / "bench/openrouter_typed_factorial.py",
        ROOT / "Cargo.toml",
        ROOT / "Cargo.lock",
    ]
    for crate in ("agentc-core", "agentc-embed", "agentc-memo", "agentc-optimizer"):
        base = ROOT / "crates" / crate
        paths.append(base / "Cargo.toml")
        paths.extend((base / "src").rglob("*.rs"))
    paths.append(ROOT / BRIDGE_SOURCE)
    result.update({str(p.relative_to(ROOT)): file_hash(p) for p in paths})
    return result


def schedule(tasks):
    return [
        row
        for task in tasks
        for row in sorted(
            (
                {"task_id": task["task_id"], "phase": "development", "arm": arm}
                for arm in ARMS
            ),
            key=lambda row: digest([KIND, SEED, row]),
        )
    ]


def materialize(call, rule, bridge):
    """Use actual rule proposals, not the optimizer or invented capacity evidence."""
    indices = list(range(len(call["messages"])))
    if rule is None:
        return deepcopy(call), {
            "rule": None,
            "applied": False,
            "retained_indices": indices,
            "materialization_only": True,
            "abstention_reason": "no_rule",
        }
    if rule not in RULES.values():
        raise PilotError("uncovered diagnostic rule")
    process = subprocess.run(
        [str(bridge)],
        input=canonical({"rule": rule, "call": call}),
        capture_output=True,
        timeout=10,
        check=False,
    )
    if process.returncode or len(process.stdout) > 2_000_000:
        raise PilotError("native materializer failed; no dispatch")
    report = json.loads(process.stdout)
    if set(report) != {
        "rule",
        "applied",
        "call",
        "retained_indices",
        "materialization_only",
        "abstention_reason",
    }:
        raise PilotError("materializer response shape changed")
    native = report.pop("call")
    kept = report["retained_indices"]
    if (
        report["rule"] != rule
        or report["materialization_only"] is not True
        or type(report["applied"]) is not bool
        or (
            report["abstention_reason"] is not None
            if report["applied"]
            else report["abstention_reason"]
            not in {
                "ambiguous_provenance",
                "not_applicable",
                "no_proposal",
                "safety_check_failed",
            }
        )
        or not isinstance(kept, list)
        or not kept
        or any(type(i) is not int or not 0 <= i < len(indices) for i in kept)
        or kept != sorted(set(kept))
        or report["applied"] != (kept != indices)
        or {k: v for k, v in native.items() if k != "messages"}
        != {k: v for k, v in call.items() if k != "messages"}
        or native["messages"] != [call["messages"][i] for i in kept]
    ):
        raise PilotError("native materializer escaped exact deletion-only contract")
    selected = deepcopy(call)
    selected["messages"] = deepcopy(native["messages"])
    for field in ("message_deps", "attention_scores"):
        values = selected["parameters"]["extra"].get(field)
        if values is not None:
            if len(values) != len(indices):
                raise PilotError("unaligned original annotation")
            selected["parameters"]["extra"][field] = [values[i] for i in kept]
    if len(selected["input_deps"]) != len(indices):
        raise PilotError("unaligned input dependencies")
    selected["input_deps"] = [selected["input_deps"][i] for i in kept]
    return selected, report


def stage_id(manifest):
    return "typed-factorial-v1-" + digest(manifest)[:20]


def intent_for(manifest, item, task, history, attention, bridge):
    model, rewrites = ARMS[item["arm"]]
    original = restrict_call(
        typed.call(
            public_task(task),
            item["workflow_stage"],
            history,
            attention,
            model=model,
            run_identity=stage_id(manifest) + "/" + item["arm"],
            prompts=manifest["prompts"],
        ),
        item["workflow_stage"],
    )
    selected, report = materialize(
        original, RULES.get(item["workflow_stage"]) if rewrites else None, bridge
    )
    payload = live.payload_for(selected, manifest)
    core = {
        **item,
        "original_call": original,
        "selected_call": selected,
        "materialization": report,
        "payload": payload,
    }
    endpoint = manifest["endpoints"][model]
    return {
        **core,
        "id": stage_id(manifest) + "-" + digest(item)[:24],
        "metadata": {
            **item,
            "scope": "primary",
            "manifest_sha256": digest(manifest),
            "decision_sha256": digest(core),
            "dispatch_contract": {
                "provider_name": endpoint["provider_name"],
                "endpoint_model": endpoint["name"].split(" | ", 1)[1],
            },
        },
    }


def pilot_evidence(args):
    data, finance = feasibility.verified_pilot(args)
    if data["manifest.json"]["workload"] != typed.WORKLOAD:
        raise PilotError("factorial requires typed feasibility")
    return {
        "path": str(args.pilot.resolve()),
        "hashes": {name: digest(value) for name, value in data.items()},
        "known_cost_usd": finance["known_stage_usd"],
    }


def prepare(args, key):
    if (args.output / "manifest.json").exists():
        raise PilotError("factorial manifest already frozen")
    pilot = pilot_evidence(args)
    prior = json.loads((args.pilot / "manifest.json").read_text())
    binding, _, _ = feasibility.typed_ledger_snapshot(args.ledger, key)
    tasks = typed.make_tasks(SEED, COUNT)
    exposed = {
        t["task_id"] for t in json.loads((args.pilot / "fixture.json").read_text())
    }
    # All earlier generated fixtures share the original six-task seed or the
    # protected study seed; compare IDs without reading protected task contents.
    protected_ids = {
        t["task_id"] for t in typed.make_tasks(feasibility.STUDY_SEED, 131)
    }
    if len({t["task_id"] for t in tasks}) != COUNT or {t["task_id"] for t in tasks} & (
        exposed | protected_ids
    ):
        raise PilotError("factorial tasks overlap development/protected IDs")
    selected = {
        m: e for m, e in endpoints(key).items() if m in {a[0] for a in ARMS.values()}
    }
    for model, endpoint in selected.items():
        validate_schema_endpoint(model, endpoint)
    catalog = feasibility.catalog_for(prior, selected)
    for target in catalog["targets"]:
        target["capabilities"]["structured_outputs"] = True
    manifest = {
        "kind": KIND,
        "workload": typed.WORKLOAD,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_files": sources(),
        "bridge_sha256": file_hash(args.bridge),
        "pilot_evidence": pilot,
        "generator_seed": SEED,
        "generator_count": COUNT,
        "schedule": schedule(tasks),
        "arms": {k: list(v) for k, v in ARMS.items()},
        "prompts": typed.prompts(),
        "response_formats": {s: typed.schema_for(s) for s in live.STAGES},
        "format_variant": typed.FORMAT_VARIANT,
        "feedback_contract": typed.FEEDBACK_CONTRACT,
        "catalog": catalog,
        "endpoints": selected,
        "campaign_ledger": binding,
        "stage_cap_usd": STAGE_CAP,
        "known_progress_stop_usd": KNOWN_PROGRESS_STOP,
        "failure_policy": FAILURE_POLICY,
        "native_execution_allowed": False,
        "paper_evidence": False,
        "limitations": [
            "Fixed native rule materialization is not native planner selection or admission.",
            "32 generated development tasks, one realization per cell; no efficacy or damage certificate.",
            "All four arms use their own histories, including invalid-output propagation; no oracle routing.",
            "Multiplicative token-price savings alone are not learned joint-system benefit.",
            "Latency is observational; provider caching and compilation are not controlled.",
            "All fees, failed calls and prior campaign uncertainty allowances remain charged.",
        ],
    }
    write_json(args.output / "fixture.json", tasks, immutable=True)
    manifest["fixture_sha256"] = file_hash(args.output / "fixture.json")
    write_json(args.output / "manifest.json", manifest, immutable=True)
    return {
        "manifest_sha256": digest(manifest),
        "scheduled_calls": COUNT * 12,
        "stage_cap_usd": STAGE_CAP,
    }


def validate(args, manifest, key):
    tasks = json.loads((args.output / "fixture.json").read_text())
    binding, events, state = feasibility.typed_ledger_snapshot(args.ledger, key)
    expected = {
        "kind": KIND,
        "workload": typed.WORKLOAD,
        "source_files": sources(),
        "bridge_sha256": file_hash(args.bridge),
        "generator_seed": SEED,
        "generator_count": COUNT,
        "schedule": schedule(tasks),
        "arms": {k: list(v) for k, v in ARMS.items()},
        "prompts": typed.prompts(),
        "response_formats": {s: typed.schema_for(s) for s in live.STAGES},
        "format_variant": typed.FORMAT_VARIANT,
        "feedback_contract": typed.FEEDBACK_CONTRACT,
        "campaign_ledger": binding,
        "stage_cap_usd": STAGE_CAP,
        "known_progress_stop_usd": KNOWN_PROGRESS_STOP,
        "failure_policy": FAILURE_POLICY,
        "native_execution_allowed": False,
        "paper_evidence": False,
        "fixture_sha256": file_hash(args.output / "fixture.json"),
        "pilot_evidence": pilot_evidence(args),
    }
    if (
        digest(manifest) != args.manifest_sha256
        or any(manifest.get(k) != v for k, v in expected.items())
        or tasks != typed.make_tasks(SEED, COUNT)
    ):
        raise PilotError("factorial source, binary, fixture, pilot or contract changed")
    if set(manifest["endpoints"]) != {a[0] for a in ARMS.values()}:
        raise PilotError("factorial model coverage changed")
    for model, endpoint in manifest["endpoints"].items():
        validate_schema_endpoint(model, endpoint)
    return tasks, events, state


def public_row(item, result, manifest):
    cached = result["usage"].get("prompt_tokens_details", {}).get("cached_tokens")
    if cached is not None and (
        type(cached) is not int or not 0 <= cached <= result["usage"]["prompt_tokens"]
    ):
        raise PilotError("invalid cache accounting")
    return {
        **item,
        **{k: v for k, v in result.items() if k not in {"at", "key_id", "metadata"}},
        "scope": "primary",
        "cached_input_tokens": cached,
        "nominal_uncached_cost_usd": str(
            live.nominal_uncached_cost(manifest, result["model"], result["usage"])
        ),
    }


def run(args, key, manifest):
    tasks, events, _ = validate(args, manifest, key)
    sid = stage_id(manifest)
    if any(e.get("stage") == sid for e in events) or any(
        (args.output / n).exists()
        for n in ("intents.json", "calls.json", "summary.json", "acquisition.claim")
    ):
        raise PilotError(
            "one-shot factorial has prior evidence; audit only, never resume"
        )
    # The claim must be campaign/stage-wide, not local to an output directory:
    # copied unacquired outputs can otherwise both pass a stale preflight.
    # Keep it permanently, including a crash before the first paid attempt.
    campaign_path = args.ledger.resolve(strict=True)
    claim = campaign_path.with_name(campaign_path.name + "." + sid + ".claim")
    try:
        with claim.open("x") as handle:
            handle.write(digest(manifest) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise PilotError(
            "one-shot campaign/stage already claimed; audit only"
        ) from None
    with (args.output / "acquisition.claim").open("x") as handle:
        handle.write(digest(manifest) + "\n")
    ledger, indexed = Ledger(args.ledger, key), {t["task_id"]: t for t in tasks}
    attention = load_module("factorial_attention", ROOT / "python/agentc/_attention.py")
    calls, intents, complete = [], [], False
    try:
        for workflow in manifest["schedule"]:
            history = {}
            for stage in live.STAGES:
                if sum((money(r["cost_usd"]) for r in calls), Decimal(0)) >= money(
                    KNOWN_PROGRESS_STOP
                ):
                    raise PilotError("prospective known-charge progress stop")
                item = {**workflow, "workflow_stage": stage}
                intent = intent_for(
                    manifest,
                    item,
                    indexed[item["task_id"]],
                    history,
                    attention,
                    args.bridge,
                )
                intents.append(intent)
                write_json(args.output / "intents.json", intents)
                result = ledger.call(
                    key,
                    intent["id"],
                    sid,
                    money(STAGE_CAP),
                    intent["payload"],
                    intent["metadata"],
                )
                generation = result.get("generation_id")
                if (
                    not isinstance(generation, str)
                    or not generation
                    or any(r["generation_id"] == generation for r in calls)
                ):
                    raise PilotError("missing or duplicated provider generation")
                calls.append(public_row(item, result, manifest))
                write_json(args.output / "calls.json", calls)
                history[stage] = typed.application_text(
                    result["answer"], result["finish_reason"], stage=stage
                )
                if len(calls) % 12 == 0:
                    print(
                        json.dumps(
                            {
                                "completed_calls": len(calls),
                                "scheduled_calls": COUNT * 12,
                            }
                        ),
                        flush=True,
                    )
        complete = True
    finally:
        write_json(args.output / "calls.json", calls)
        write_json(args.output / "intents.json", intents)
        write_json(
            args.output / "summary.json",
            {
                "kind": KIND,
                "manifest_sha256": digest(manifest),
                "calls_sha256": digest(calls),
                "intents_sha256": digest(intents),
                "completed_calls": len(calls),
                "schedule_complete": complete,
                "stage_accounting": live.stage_accounting(ledger, sid, calls),
                "paper_evidence": False,
                "efficacy_claim": False,
            },
        )
    return {"completed_calls": len(calls), "schedule_complete": complete}


def reconstruct(manifest, tasks, intents, calls, events, bridge):
    """Rebuild the scheduled prefix from public inputs and each arm's own state."""
    steps = [
        {**w, "workflow_stage": s} for w in manifest["schedule"] for s in live.STAGES
    ]
    if (
        not isinstance(intents, list)
        or not isinstance(calls, list)
        or not len(calls) <= len(intents) <= min(len(steps), len(calls) + 1)
    ):
        raise PilotError("invalid factorial journal prefix")
    from bench.openrouter_attempts import accounting

    state = accounting(events)
    indexed, expected, history = {t["task_id"]: t for t in tasks}, {}, {}
    attention = load_module(
        "factorial_replay_attention", ROOT / "python/agentc/_attention.py"
    )
    seen = set()
    for i, intent in enumerate(intents):
        item = steps[i]
        if item["workflow_stage"] == "filter":
            history = {}
        rebuilt = intent_for(
            manifest, item, indexed[item["task_id"]], history, attention, bridge
        )
        if intent != rebuilt:
            raise PilotError(
                "factorial intent differs from exact own-history reconstruction"
            )
        expected[intent["id"]] = (intent["payload"], intent["metadata"])
        if i < len(calls):
            result = state["results"].get(intent["id"])
            if result is None or calls[i] != public_row(item, result, manifest):
                raise PilotError("factorial result differs from authoritative ledger")
            analysis.validate_row(
                calls[i], intent["payload"], intent["metadata"], manifest
            )
            if (
                not isinstance(result.get("generation_id"), str)
                or not result["generation_id"]
                or result["generation_id"] in seen
            ):
                raise PilotError("missing or duplicate factorial generation")
            seen.add(result["generation_id"])
            history[item["workflow_stage"]] = typed.application_text(
                result["answer"], result["finish_reason"], stage=item["workflow_stage"]
            )
    sid = stage_id(manifest)
    for reserve in state["reserves"].values():
        if reserve["stage"] != sid:
            continue
        if reserve["id"] not in expected or money(
            reserve["upper_cost_usd"]
        ) != upper_cost(expected[reserve["id"]][0]):
            raise PilotError("factorial attempt has an unknown intent or allowance")
        validate_schema_endpoint(
            reserve["request"]["model"], reserve.get("schema_endpoint_observation", {})
        )
    finance = analysis.ledger_report(events, sid, calls, expected)
    # Raw receipt latency is not checked by the shared semantic/body checker.
    for event in events:
        if event["event"] == "response" and event.get("id", "").startswith(sid + "-"):
            matches = [
                r
                for r in state["reserves"].values()
                if r["id"] == event["id"]
                and r.get("attempt_id") == event.get("attempt_id")
            ]
            if len(matches) != 1:
                raise PilotError("factorial raw response lacks its exact reservation")
            result = state["results"].get(event["id"])
            if result and (
                event["latency_ms"] != result["latency_ms"]
                or money(result["cost_usd"]) > money(matches[0]["upper_cost_usd"])
            ):
                raise PilotError(
                    "factorial response timing or cost differs from allowance"
                )
    complete = (
        len(calls) == len(intents) == len(steps)
        and finance["snapshot_financially_complete"]
        and not finance["failed_provider_results"]
        and not any(t["stage"] == sid for t in state["terminals"].values())
    )
    return finance, complete


def contrasts(cells):
    """Within-task interaction, avoiding a price-multiplication synergy claim."""
    quality = (cells["HR"]["em"] - cells["H0"]["em"]) - (
        cells["SR"]["em"] - cells["S0"]["em"]
    )
    costs = {a: float(cells[a]["cost_usd"]) for a in ARMS}
    relative = (
        math.log(costs["HR"] / costs["H0"]) - math.log(costs["SR"] / costs["S0"])
        if all(c > 0 for c in costs.values())
        else None
    )
    return {
        "quality_interaction": quality,
        "log_cost_ratio_interaction": relative,
        "absolute_cost_interaction_usd": (costs["HR"] - costs["H0"])
        - (costs["SR"] - costs["S0"]),
    }


def cell_report(rows, task):
    history = {
        r["workflow_stage"]: typed.application_text(
            r["answer"], r["finish_reason"], stage=r["workflow_stage"]
        )
        for r in rows
    }
    return {
        "em": typed.score_answer(history["answer"], task["expected"], history=history)[
            "em"
        ],
        "valid_by_stage": {
            s: typed.parse_stage(s, text) is not None for s, text in history.items()
        },
        "cost_usd": str(sum((money(r["cost_usd"]) for r in rows), Decimal(0))),
        "nominal_uncached_cost_usd": str(
            sum((money(r["nominal_uncached_cost_usd"]) for r in rows), Decimal(0))
        ),
        "input_tokens": sum(r["usage"]["prompt_tokens"] for r in rows),
        "output_tokens": sum(r["usage"]["completion_tokens"] for r in rows),
        "cached_input_tokens": sum(r["cached_input_tokens"] or 0 for r in rows),
        "cache_unknown_calls": sum(r["cached_input_tokens"] is None for r in rows),
        "observational_latency_ms": sum(r["latency_ms"] for r in rows),
    }


def analyze(args, key, manifest):
    tasks, events, _ = validate(args, manifest, key)
    calls = json.loads((args.output / "calls.json").read_text())
    intents = json.loads((args.output / "intents.json").read_text())
    summary = json.loads((args.output / "summary.json").read_text())
    finance, complete = reconstruct(
        manifest, tasks, intents, calls, events, args.bridge
    )
    if (
        summary["manifest_sha256"] != digest(manifest)
        or summary["calls_sha256"] != digest(calls)
        or summary["intents_sha256"] != digest(intents)
        or summary["completed_calls"] != len(calls)
        or summary["schedule_complete"] != complete
    ):
        raise PilotError("factorial completion summary inconsistent")
    paired = []
    if complete:
        for task in tasks:
            cells = {
                arm: cell_report(
                    [
                        r
                        for r in calls
                        if r["task_id"] == task["task_id"] and r["arm"] == arm
                    ],
                    task,
                )
                for arm in ARMS
            }
            paired.append(
                {"task_id": task["task_id"], "cells": cells, **contrasts(cells)}
            )
    report = {
        "kind": KIND,
        "manifest_sha256": digest(manifest),
        "ledger_events_sha256": digest(events),
        "schedule_complete": complete,
        "finance": finance,
        "paired_tasks": paired,
        "materializations": dict(
            Counter(
                (i["materialization"]["rule"] or "none")
                for i in intents
                if i["materialization"]["applied"]
            )
        ),
        "materialization_abstentions": dict(
            Counter(
                i["materialization"]["abstention_reason"]
                for i in intents
                if not i["materialization"]["applied"]
            )
        ),
        "descriptive_quality_interval": analysis.paired_bootstrap(
            [{"quality_interaction": r["quality_interaction"]} for r in paired]
        )
        if paired
        else None,
        "zero_cost_tasks_excluded_from_log_contrast": [
            r["task_id"] for r in paired if r["log_cost_ratio_interaction"] is None
        ],
        "paper_evidence": False,
        "native_admission_tested": False,
        "efficacy_claim": False,
        "limitations": manifest["limitations"],
    }
    write_json(args.output / "analysis.json", report)
    return {
        "schedule_complete": complete,
        "paired_tasks": len(paired),
        "finance": finance,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "analyze"))
    for name in ("env-file", "ledger", "output", "pilot", "bridge"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--pilot-manifest-sha256", required=True)
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args()
    try:
        key = load_key(args.env_file)
        result = (
            prepare(args, key)
            if args.command == "prepare"
            else {"run": run, "analyze": analyze}[args.command](
                args, key, json.loads((args.output / "manifest.json").read_text())
            )
        )
        print(json.dumps(result, allow_nan=False, indent=2))
        return 0
    except (
        PilotError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.TimeoutExpired,
    ):
        print(
            "Factorial stopped at validation, provider or budget boundary; no raw error output."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
