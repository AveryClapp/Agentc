"""Prospective public QA pilot and fixed factorial; no adaptive/native admission.

Acquisition and its resource gate never open labels. Every phase is one-shot,
including crashes before its first attempt. Only a complete primary can open the
separate labels file for offline, whole-task paired evaluation.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import itertools
import json
import math
import os
from pathlib import Path

from bench import openrouter_joint_analysis as analysis
from bench import openrouter_rules_live as live
from bench import openrouter_structured_study as finance_helpers
from bench import openrouter_typed_factorial as materializer
from bench.openrouter_attempts import accounting
from bench.openrouter_contract import endpoints
from bench.openrouter_groups import validate_completion
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

KIND = "public-hotpot-model-sd-v1"
WORKLOAD = "typed_public_research_v1"
PILOT_COUNT, PRIMARY_COUNT = 6, 32
PILOT_CAP, PILOT_PROGRESS = Decimal("7"), Decimal("0.75")
FAMILY_CAP, PRIMARY_PROGRESS = Decimal("14"), Decimal("7")
ARMS = {
    prefix + suffix: {"model": model, "cc": cc, "sd": sd}
    for prefix, model in (("S", live.SOURCE_MODEL), ("H", live.TARGET_MODEL))
    for suffix, cc, sd in (
        ("0", False, False),
        ("R", True, True),
    )
}
BASELINE_ARMS = ("S0", "H0")
FOUR_ARMS = ("S0", "SR", "H0", "HR")
PUBLIC_FILES = ("selection.json", "public.json", "provenance.json", "coverage.json")
PHASE_FILES = ("manifest.json", "intents.json", "calls.json", "summary.json")
ROLLING_TELEMETRY = {
    "latency_last_30m",
    "throughput_last_30m",
    "perf_last_30m_by_workload",
    "uptime_last_1d",
    "uptime_last_30m",
    "uptime_last_5m",
}
FAILURE_POLICY = (
    "invalid=count-loss+contagious-null;provider-failure=stop;no-retry;no-resume-v1"
)
FORECAST_POLICY = "observed-endpoint-max-pilot-effective-rate-history-and-512-output-v1"
SCOPE_DECISION = {
    "reason": "Prepaid public native preflight: CC zero of 38; preserve IDs, raw contexts, and native thresholds.",
    "primary_arms": list(FOUR_ARMS),
    "attempted_factors": ["model", "cc_sd_bundle"],
    "operational_intervention": "model x answer StateDrop; filter ContextCompress is a recorded no-op",
    "individual_cc_identified": False,
    "cc_sd_interaction_identified": False,
    "accuracy_used": False,
    "resampling_or_padding": False,
}
LIMITATIONS = [
    "Public train-split development tasks, one realization per cell; no heldout or learned-policy claim.",
    "The pilot gate uses format and resources only; no labels or answer-accuracy threshold.",
    "Input overhead is not separately identifiable; billed-input/byte rates are empirical heuristics.",
    "Forecasts are neither provider context certificates nor guaranteed spending bounds.",
    "Ledger full-context allowances, the original campaign cap, and all prior holds remain authoritative.",
    "Provider failure or any budget stop leaves a partial artifact with no efficacy contrast.",
    "All arms keep their own histories; invalid paths remain losses even after plausible later answers.",
    "Native CC/SD materialization is not native planning, routing, learning, or semantic guard admission.",
    "No cache savings or rewrite savings are credited in the prospective forecast.",
    "CC has zero public preflight applicability; four cells test model x attempted bundle, operationally model x SD, not CC or CC x SD.",
    "Latency is observational; compilation, caching, provider load and generation randomness are uncontrolled.",
]


def read(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PilotError("duplicate artifact JSON key")
            result[key] = value
        return result

    def invalid(_):
        raise PilotError("nonfinite artifact JSON number")

    return json.loads(
        Path(path).read_text(), object_pairs_hook=unique, parse_constant=invalid
    )


def adapter():
    return live.workload_adapter({"workload": WORKLOAD})


def load_bundle(directory):
    from bench.build_typed_hotpot_fixture import load_bundle as public_bundle

    bundle = public_bundle(Path(directory))
    selection = bundle["selection"]
    if (
        len(selection["pilot_ids"]) != PILOT_COUNT
        or len(selection["primary_ids"]) != PRIMARY_COUNT
    ):
        raise PilotError("public experiment requires six pilot and 32 primary IDs")
    return bundle


def sources():
    result = live.sources(WORKLOAD)
    paths = [
        ROOT / "bench/openrouter_public_factorial.py",
        ROOT / "bench/openrouter_typed_factorial.py",
        ROOT / "Cargo.toml",
        ROOT / "Cargo.lock",
        ROOT / materializer.BRIDGE_SOURCE,
    ]
    for crate in ("agentc-core", "agentc-embed", "agentc-memo", "agentc-optimizer"):
        base = ROOT / "crates" / crate
        paths.append(base / "Cargo.toml")
        paths.extend(sorted((base / "src").rglob("*.rs")))
    result.update({str(p.relative_to(ROOT)): file_hash(p) for p in paths})
    return result


def selected_tasks(bundle, phase):
    ids = bundle["selection"][phase + "_ids"]
    indexed = {task["task_id"]: task for task in bundle["tasks"]}
    return [indexed[task_id] for task_id in ids]


def schedule(bundle, phase, arms):
    return [
        row
        for task in selected_tasks(bundle, phase)
        for row in sorted(
            ({"task_id": task["task_id"], "phase": phase, "arm": arm} for arm in arms),
            key=lambda row: digest([KIND, digest(bundle["selection"]), row]),
        )
    ]


def stable_endpoint(endpoint):
    return {k: v for k, v in endpoint.items() if k not in ROLLING_TELEMETRY}


def endpoint_snapshot(key):
    selected = {
        model: endpoint
        for model, endpoint in endpoints(key).items()
        if model in {live.SOURCE_MODEL, live.TARGET_MODEL}
    }
    if set(selected) != {live.SOURCE_MODEL, live.TARGET_MODEL}:
        raise PilotError("public experiment lacks both frozen model endpoints")
    for model, endpoint in selected.items():
        validate_schema_endpoint(model, endpoint)
    return selected


def stage_id(manifest):
    # Independent of output directories, manifest timestamps, or rule versions.
    return "public-factorial-" + manifest["phase"] + "-" + manifest["selection_sha256"]


def phase_claim_path(ledger_path, manifest):
    path = Path(ledger_path).resolve(strict=True)
    return path.with_name(path.name + "." + stage_id(manifest) + ".claim")


def claim_phase(ledger_path, manifest):
    path = phase_claim_path(ledger_path, manifest)
    value = claim_value(manifest)
    try:
        with path.open("x") as handle:
            handle.write(canonical(value).decode() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except FileExistsError:
        raise PilotError(
            "one-shot selection/phase already claimed; audit only"
        ) from None
    return value


def claim_value(manifest):
    return {
        "kind": KIND,
        "phase": manifest["phase"],
        "selection_sha256": manifest["selection_sha256"],
        "manifest_sha256": digest(manifest),
        "campaign_ledger": manifest["campaign_ledger"],
    }


class PinnedLedger(Ledger):
    """Check current endpoint facts/prices before persisting a new reservation."""

    def __init__(self, path, key, manifest):
        super().__init__(path, key)
        self.manifest = manifest

    def append(self, handle, event):
        if event["event"] == "reserve":
            model = event["request"]["model"]
            if event["stage"] != stage_id(self.manifest) or canonical(
                stable_endpoint(event.get("schema_endpoint_observation", {}))
            ) != canonical(stable_endpoint(self.manifest["endpoints"][model])):
                raise PilotError(
                    "public phase endpoint identity, capabilities, or prices changed"
                )
        return super().append(handle, event)


def schema_catalog(selected):
    # Only a fixed-dispatch capability table. It supplies no capacity bound or
    # native planner evidence; payload_for uses these structured-output facts.
    return {
        "kind": "fixed-dispatch-only-v1",
        "endpoint_sha256": digest(selected),
        "targets": [
            {"model_id": model, "capabilities": {"structured_outputs": True}}
            for model in sorted(selected)
        ],
    }


def coverage_evidence(args, bundle):
    """Pin the reviewed, label-blind zero-CC mechanics decision, not quality."""
    report = read(Path(args.bundle) / "coverage.json")
    source_paths = {
        "builder": "bench/build_typed_hotpot_fixture.py",
        "adapter": "bench/typed_public_workload.py",
        "attention": "python/agentc/_attention.py",
        "bridge": materializer.BRIDGE_SOURCE,
    }
    expected = {
        "kind": "hotpot-public-rule-mechanics-v1",
        "source_sha256": {
            key: file_hash(ROOT / path) for key, path in source_paths.items()
        },
        "bridge_sha256": file_hash(args.bridge),
        "public_sha256": file_hash(Path(args.bundle) / "public.json"),
        "selection_sha256": digest(bundle["selection"]),
        "contract_sha256": adapter().contract_hash(),
        "tasks": PILOT_COUNT + PRIMARY_COUNT,
        "provider_calls": 0,
        "quality_evaluations": 0,
        "gold_access": False,
        "native_admission": False,
        "feedback_provided": False,
    }
    if canonical({key: report.get(key) for key in expected}) != canonical(expected):
        raise PilotError(
            "public coverage report source, fixture, or no-network contract changed"
        )
    models = {live.SOURCE_MODEL, live.TARGET_MODEL}
    if len(report["models"]) != 2 or set(report["models"]) != models:
        raise PilotError("public coverage model set changed")
    cohorts = {
        "CC_actual_public": (
            "ContextCompress",
            "filter",
            False,
            {"not_applicable", "no_proposal"},
        ),
        "SD_synthetic_history": ("StateDrop", "answer", True, {None}),
        "SD_invalid_synthetic_history": (
            "StateDrop",
            "answer",
            False,
            {"ambiguous_provenance"},
        ),
    }
    expected_keys = {
        (task["task_id"], model, cohort)
        for task in bundle["tasks"]
        for model in models
        for cohort in cohorts
    }
    seen, counts = set(), {model: Counter() for model in models}
    for row in report["records"]:
        key = (row["task_id"], row["model"], row["cohort"])
        if key in seen or key not in expected_keys:
            raise PilotError("public coverage record identity changed")
        seen.add(key)
        rule, stage, applied, reasons = cohorts[row["cohort"]]
        if (
            row["rule"] != rule
            or row["stage"] != stage
            or row["applied"] is not applied
            or row["abstention_reason"] not in reasons
            or (
                not applied
                and row["original_call_sha256"] != row["materialized_call_sha256"]
            )
        ):
            raise PilotError(
                "public coverage no longer supports zero CC and synthetic SD mechanics"
            )
        counts[row["model"]][
            row["cohort"] + "/" + ("applied" if applied else row["abstention_reason"])
        ] += 1
    if seen != expected_keys or canonical(report["summary"]) != canonical(counts):
        raise PilotError("public coverage missing records or inconsistent counts")
    return {
        "canonical_sha256": digest(report),
        "summary": report["summary"],
        "tasks": report["tasks"],
    }


def base_contract(args, bundle, binding, phase, arms, selected, pilot=None):
    workload = adapter()
    pilot_known = money(pilot["known_cost_usd"]) if pilot else Decimal(0)
    return {
        "kind": KIND,
        "workload": WORKLOAD,
        "phase": phase,
        "source_files": sources(),
        "bridge_sha256": file_hash(args.bridge),
        "bundle_files": {
            name: file_hash(Path(args.bundle) / name) for name in PUBLIC_FILES
        },
        "public_bundle_sha256": digest(bundle),
        "selection_sha256": digest(bundle["selection"]),
        "fixture_provenance": bundle["provenance"],
        "dataset_source": bundle["source"],
        "structural_coverage": coverage_evidence(args, bundle),
        "schedule": schedule(bundle, phase, arms),
        "arms": {arm: ARMS[arm] for arm in arms},
        "prompts": workload.prompts(),
        "response_formats": {s: workload.schema_for(s) for s in live.STAGES},
        "format_variant": workload.FORMAT_VARIANT,
        "contract_sha256": workload.contract_hash(),
        "max_tokens": 512,
        "endpoints": selected,
        "catalog": schema_catalog(selected),
        "campaign_ledger": binding,
        "public_family_cap_usd": str(FAMILY_CAP),
        "stage_cap_usd": str(
            PILOT_CAP if phase == "pilot" else FAMILY_CAP - pilot_known
        ),
        "known_progress_stop_usd": str(
            PILOT_PROGRESS if phase == "pilot" else PRIMARY_PROGRESS
        ),
        "pilot_evidence": pilot,
        "failure_policy": FAILURE_POLICY,
        "forecast_policy": FORECAST_POLICY,
        "scope_decision": SCOPE_DECISION,
        "native_execution_allowed": False,
        "paper_evidence": False,
        "limitations": LIMITATIONS,
    }


def freeze(args, key, phase):
    if phase not in {"pilot", "primary"} or (args.output / "manifest.json").exists():
        raise PilotError("invalid phase or public manifest already frozen")
    bundle = load_bundle(args.bundle)
    binding, events, state = finance_helpers.typed_ledger_snapshot(args.ledger, key)
    if state["pending"] or state["failed_calls"] or state["unsafe_ids"]:
        raise PilotError("unresolved campaign accounting blocks public freeze")
    pilot, forecast = None, None
    arms = BASELINE_ARMS
    selected = endpoint_snapshot(key)
    if phase == "primary":
        pilot = verified_pilot(args, key, bundle)
        forecast = resource_forecast(
            bundle, pilot.pop("calls"), pilot.pop("intents"), selected
        )
        arms = choose_arms(forecast)
    manifest = {
        **base_contract(args, bundle, binding, phase, arms, selected, pilot),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "forecast": forecast,
    }
    sid = stage_id(manifest)
    if phase_claim_path(args.ledger, manifest).exists() or any(
        e.get("stage") == sid for e in events
    ):
        raise PilotError("one-shot public selection/phase already used")
    # Public raw inputs must fit the unchanged transport envelope before any
    # paid call. No selected task can be silently removed or redrawn.
    attention = load_module(
        "public_prepare_attention", ROOT / "python/agentc/_attention.py"
    )
    for task in selected_tasks(bundle, phase):
        for model in selected:
            call = adapter().call(
                task,
                "filter",
                {},
                attention,
                model=model,
                run_identity=sid,
                prompts=manifest["prompts"],
            )
            live.payload_for(call, manifest)
    write_json(args.output / "manifest.json", manifest, immutable=True)
    return {
        "manifest_sha256": digest(manifest),
        "phase": phase,
        "scheduled_calls": len(manifest["schedule"]) * 3,
        "arms": list(arms),
        "stage_cap_usd": manifest["stage_cap_usd"],
    }


def validate(args, manifest, key=None):
    if canonical(read(args.output / "manifest.json")) != canonical(manifest):
        raise PilotError("on-disk public manifest differs from validated object")
    bundle = load_bundle(args.bundle)
    binding, events, state = finance_helpers.typed_ledger_snapshot(args.ledger, key)
    phase = manifest["phase"]
    if phase not in {"pilot", "primary"}:
        raise PilotError("unknown public phase")
    pilot, forecast = None, None
    arms = BASELINE_ARMS
    selected = manifest["endpoints"]
    if set(selected) != {live.SOURCE_MODEL, live.TARGET_MODEL}:
        raise PilotError("public model coverage changed")
    for model, endpoint in selected.items():
        validate_schema_endpoint(model, endpoint)
    if phase == "primary":
        pilot = verified_pilot(args, key, bundle)
        forecast = resource_forecast(
            bundle, pilot.pop("calls"), pilot.pop("intents"), selected
        )
        arms = choose_arms(forecast)
    expected = base_contract(args, bundle, binding, phase, arms, selected, pilot)
    expected.update(created_at=manifest["created_at"], forecast=forecast)
    if digest(manifest) != args.manifest_sha256 or canonical(manifest) != canonical(
        expected
    ):
        raise PilotError(
            "public source, fixture, schema, budget, pilot or manifest changed"
        )
    return bundle, events, state


def intent_for(manifest, item, task, history, attention, bridge):
    arm = manifest["arms"][item["arm"]]
    stage = item["workflow_stage"]
    original = restrict_call(
        adapter().call(
            adapter().public_task(task),
            stage,
            history,
            attention,
            model=arm["model"],
            run_identity=stage_id(manifest) + "/" + item["arm"],
            prompts=manifest["prompts"],
        ),
        stage,
    )
    rule = (
        "ContextCompress"
        if stage == "filter" and arm["cc"]
        else ("StateDrop" if stage == "answer" and arm["sd"] else None)
    )
    selected, report = materializer.materialize(original, rule, bridge)
    # The bridge wrapper preserves deletion order; independently require exact
    # canonical non-message fields and remapped annotations, including types.
    rebuilt = deepcopy(original)
    kept = report["retained_indices"]
    rebuilt["messages"] = [original["messages"][i] for i in kept]
    rebuilt["input_deps"] = [original["input_deps"][i] for i in kept]
    for field in ("message_deps", "attention_scores"):
        if field in rebuilt["parameters"]["extra"]:
            rebuilt["parameters"]["extra"][field] = [
                original["parameters"]["extra"][field][i] for i in kept
            ]
    if (
        canonical(selected) != canonical(rebuilt)
        or "input_tokens_upper_bound"
        in selected["parameters"]["extra"]["agentc_route_context"]
    ):
        raise PilotError(
            "materializer changed an exact public request or invented capacity"
        )
    payload = live.payload_for(selected, manifest)
    core = {
        **item,
        "original_call": original,
        "selected_call": selected,
        "materialization": report,
        "payload": payload,
    }
    endpoint = manifest["endpoints"][arm["model"]]
    return {
        **core,
        "id": stage_id(manifest) + "-" + digest(item)[:24],
        "metadata": {
            **item,
            "scope": manifest["phase"],
            "manifest_sha256": digest(manifest),
            "selection_sha256": manifest["selection_sha256"],
            "decision_sha256": digest(core),
            "endpoint_sha256": digest(stable_endpoint(endpoint)),
            "dispatch_contract": {
                "provider_name": endpoint["provider_name"],
                "endpoint_model": endpoint["name"].split(" | ", 1)[1],
            },
        },
    }


def public_row(item, result, manifest):
    return {
        **materializer.public_row(item, result, manifest),
        "scope": manifest["phase"],
    }


def update_history(task, stage, history, result):
    history[stage] = adapter().application_text(
        result["answer"],
        result["finish_reason"],
        stage=stage,
        upstream_valid=adapter().history_valid(task, stage, history),
    )


def run(args, key, manifest):
    bundle, events, _ = validate(args, manifest, key)
    sid = stage_id(manifest)
    if any(e.get("stage") == sid for e in events) or any(
        (args.output / name).exists() for name in PHASE_FILES[1:]
    ):
        raise PilotError("one-shot public phase has prior evidence; never resume")
    claim_phase(args.ledger, manifest)
    ledger = PinnedLedger(args.ledger, key, manifest)
    indexed = {t["task_id"]: t for t in bundle["tasks"]}
    attention = load_module(
        "public_acquire_attention", ROOT / "python/agentc/_attention.py"
    )
    calls, intents, complete = [], [], False
    try:
        for workflow in manifest["schedule"]:
            task, history = indexed[workflow["task_id"]], {}
            for stage in live.STAGES:
                if sum((money(row["cost_usd"]) for row in calls), Decimal(0)) >= money(
                    manifest["known_progress_stop_usd"]
                ):
                    raise PilotError("prospective public known-charge progress stop")
                item = {**workflow, "workflow_stage": stage}
                intent = intent_for(
                    manifest, item, task, history, attention, args.bridge
                )
                intents.append(intent)
                write_json(args.output / "intents.json", intents)
                result = ledger.call(
                    key,
                    intent["id"],
                    sid,
                    money(manifest["stage_cap_usd"]),
                    intent["payload"],
                    intent["metadata"],
                )
                generation = result.get("generation_id")
                if (
                    not isinstance(generation, str)
                    or not generation
                    or any(r["generation_id"] == generation for r in calls)
                ):
                    raise PilotError("missing or duplicate public generation")
                calls.append(public_row(item, result, manifest))
                write_json(args.output / "calls.json", calls)
                update_history(task, stage, history, result)
        complete = True
    finally:
        write_json(args.output / "intents.json", intents)
        write_json(args.output / "calls.json", calls)
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


def reconstruct(manifest, bundle, intents, calls, events, bridge):
    steps = [
        {**w, "workflow_stage": stage}
        for w in manifest["schedule"]
        for stage in live.STAGES
    ]
    if (
        not isinstance(intents, list)
        or not isinstance(calls, list)
        or not len(calls) <= len(intents) <= min(len(steps), len(calls) + 1)
    ):
        raise PilotError("invalid public journal prefix")
    state = accounting(events)
    indexed = {t["task_id"]: t for t in bundle["tasks"]}
    attention = load_module(
        "public_replay_attention", ROOT / "python/agentc/_attention.py"
    )
    expected, history, seen = {}, {}, set()
    valid_by_call = {}
    for index, intent in enumerate(intents):
        item = steps[index]
        task = indexed[item["task_id"]]
        if item["workflow_stage"] == "filter":
            history = {}
        rebuilt = intent_for(manifest, item, task, history, attention, bridge)
        if canonical(intent) != canonical(rebuilt):
            raise PilotError(
                "public intent differs from exact own-history reconstruction"
            )
        expected[intent["id"]] = (intent["payload"], intent["metadata"])
        if index < len(calls):
            result = state["results"].get(intent["id"])
            if result is None or canonical(calls[index]) != canonical(
                public_row(item, result, manifest)
            ):
                raise PilotError("public result differs from authoritative ledger")
            analysis.validate_row(
                calls[index], intent["payload"], intent["metadata"], manifest
            )
            generation = result.get("generation_id")
            if not isinstance(generation, str) or not generation or generation in seen:
                raise PilotError("missing or duplicate public generation")
            seen.add(generation)
            update_history(task, item["workflow_stage"], history, result)
            valid_by_call[intent["id"]] = (
                adapter().parse_stage(
                    item["workflow_stage"], history[item["workflow_stage"]]
                )
                is not None
            )
    sid = stage_id(manifest)
    reserves = {
        key: reserve
        for key, reserve in state["reserves"].items()
        if reserve["stage"] == sid
    }
    if len({reserve["id"] for reserve in reserves.values()}) != len(reserves):
        raise PilotError("public phase contains a forbidden retry")
    pairs = {
        (reserve["id"], reserve.get("attempt_id")) for reserve in reserves.values()
    }
    if any(
        e["event"] == "response"
        and e.get("id", "").startswith(sid + "-")
        and (e["id"], e.get("attempt_id")) not in pairs
        for e in events
    ):
        raise PilotError("public phase has an orphan response receipt")
    for reserve in reserves.values():
        if reserve["id"] not in expected:
            raise PilotError("public paid attempt lacks a frozen intent")
        payload, metadata = expected[reserve["id"]]
        if (
            canonical(reserve["request"]) != canonical(payload)
            or canonical(reserve["metadata"]) != canonical(metadata)
            or reserve["fingerprint"]
            != digest({"payload": payload, "metadata": metadata, "stage": sid})
            or money(reserve["upper_cost_usd"]) != upper_cost(payload)
        ):
            raise PilotError(
                "public reservation differs from exact payload, metadata, or allowance"
            )
        endpoint = reserve.get("schema_endpoint_observation", {})
        validate_schema_endpoint(payload["model"], endpoint)
        if canonical(stable_endpoint(endpoint)) != canonical(
            stable_endpoint(manifest["endpoints"][payload["model"]])
        ):
            raise PilotError(
                "public reservation changed frozen endpoint prices or facts"
            )
        bodies = [
            e
            for e in events
            if e["event"] == "response"
            and e.get("id") == reserve["id"]
            and e.get("attempt_id") == reserve.get("attempt_id")
        ]
        result = state["results"].get(reserve["id"])
        if result:
            if len(bodies) != 1 or canonical(bodies[0]["latency_ms"]) != canonical(
                result["latency_ms"]
            ):
                raise PilotError("public source receipt count or timing changed")
            validate_completion(reserve, bodies[0]["response"], result)
    finance = analysis.ledger_report(events, sid, calls, expected)
    complete = (
        len(calls) == len(intents) == len(steps)
        and finance["snapshot_financially_complete"]
        and not finance["failed_provider_results"]
        and not any(t["stage"] == sid for t in state["terminals"].values())
    )
    return finance, complete, valid_by_call


def audit(args, manifest, key=None):
    bundle, events, _ = validate(args, manifest, key)
    if canonical(read(phase_claim_path(args.ledger, manifest))) != canonical(
        claim_value(manifest)
    ):
        raise PilotError(
            "public phase claim differs from selection, manifest, or campaign"
        )
    calls, intents, summary = (
        read(args.output / name)
        for name in ("calls.json", "intents.json", "summary.json")
    )
    finance, complete, validity = reconstruct(
        manifest, bundle, intents, calls, events, args.bridge
    )
    stage_snapshot = {
        "paid_stage_cost_usd": finance["known_stage_usd"],
        "retained_uncertainty_usd": finance["retained_uncertainty_usd"],
        "conservative_stage_commitment_usd": finance[
            "conservative_stage_commitment_usd"
        ],
        "ledger_completed_calls": len(calls)
        - len(finance["artifact_ids_missing_from_ledger"])
        + len(finance["unincorporated_completed_ids"]),
        "unincorporated_completed_ids": finance["unincorporated_completed_ids"],
        "artifact_ids_missing_from_ledger": finance["artifact_ids_missing_from_ledger"],
        "unresolved_stage_calls": finance["unresolved_stage_calls"],
    }
    expected_summary = {
        "kind": KIND,
        "manifest_sha256": digest(manifest),
        "calls_sha256": digest(calls),
        "intents_sha256": digest(intents),
        "completed_calls": len(calls),
        "schedule_complete": complete,
        "stage_accounting": stage_snapshot,
        "paper_evidence": False,
        "efficacy_claim": False,
    }
    if canonical(summary) != canonical(expected_summary):
        raise PilotError("public completion summary inconsistent")
    return {
        "manifest": manifest,
        "bundle": bundle,
        "calls": calls,
        "intents": intents,
        "finance": finance,
        "schedule_complete": complete,
        "valid_by_call": validity,
        "artifact_hashes": {
            name: file_hash(args.output / name) for name in PHASE_FILES
        },
    }


def verified_pilot(args, key, bundle):
    from types import SimpleNamespace

    pilot_manifest = read(args.pilot / "manifest.json")
    pilot_args = SimpleNamespace(
        **{
            **vars(args),
            "output": args.pilot,
            "manifest_sha256": args.pilot_manifest_sha256,
        }
    )
    if pilot_manifest.get("phase") != "pilot" or pilot_manifest.get(
        "selection_sha256"
    ) != digest(bundle["selection"]):
        raise PilotError("primary needs the exact disjoint public pilot")
    checked = audit(pilot_args, pilot_manifest, key)
    finance = checked["finance"]
    passed = (
        checked["schedule_complete"]
        and len(checked["calls"]) == PILOT_COUNT * 2 * 3
        and len(checked["valid_by_call"]) == PILOT_COUNT * 2 * 3
        and all(checked["valid_by_call"].values())
        and money(finance["known_stage_usd"]) <= PILOT_PROGRESS
        and money(finance["retained_uncertainty_usd"]) == 0
        and money(finance["pending_upper_bound_usd"]) == 0
        and not finance["additional_attempt_records"]
    )
    if not passed:
        raise PilotError(
            "public pilot failed structural/resource gate; no revised pilot or primary"
        )
    return {
        "manifest_sha256": digest(pilot_manifest),
        "artifact_hashes": checked["artifact_hashes"],
        "known_cost_usd": finance["known_stage_usd"],
        "structural_resource_gate": True,
        "accuracy_used": False,
        "calls": checked["calls"],
        "intents": checked["intents"],
    }


def payload_bytes(payload):
    return len(canonical(payload))


def history_bytes(history):
    return {
        stage: len((adapter().parse_stage(stage, text) or "").encode("utf-8"))
        for stage, text in history.items()
    }


def resource_forecast(bundle, pilot_calls, pilot_intents, selected):
    """A frozen planning heuristic including observed serving/tokenizer overhead.

    Ratios do not identify true schema overhead or certify context fit. Rewrites
    and caching earn no forecast credit. The universal price-cap variants are
    reported separately; scope selection uses the frozen observed endpoint rates.
    """
    indexed = {task["task_id"]: task for task in bundle["tasks"]}
    samples, histories, history = defaultdict(list), defaultdict(list), {}
    for call, intent in zip(pilot_calls, pilot_intents, strict=True):
        stage, model = call["workflow_stage"], call["model"]
        if stage == "filter":
            history = {}
        size = payload_bytes(intent["payload"])
        samples[(model, stage)].append(
            {
                "task_id": call["task_id"],
                "payload_utf8_bytes": size,
                "message_utf8_bytes": sum(
                    len(m["content"].encode("utf-8"))
                    for m in intent["payload"]["messages"]
                ),
                "own_history_utf8_bytes": history_bytes(history),
                "billed_input_tokens": call["usage"]["prompt_tokens"],
                "output_tokens": call["usage"]["completion_tokens"],
                "effective_input_tokens_per_payload_byte": str(
                    Decimal(call["usage"]["prompt_tokens"]) / size
                ),
            }
        )
        histories[(model, stage)].append(deepcopy(history))
        update_history(indexed[call["task_id"]], stage, history, call)
    statistics = {}
    for model in sorted(selected):
        for stage in live.STAGES:
            values = samples[(model, stage)]
            if len(values) != PILOT_COUNT:
                raise PilotError(
                    "forecast requires complete pilot model/stage coverage"
                )
            rates = [
                Decimal(v["effective_input_tokens_per_payload_byte"]) for v in values
            ]
            statistics[(model, stage)] = {
                "model": model,
                "stage": stage,
                "samples": values,
                "mean_effective_rate": str(sum(rates) / len(rates)),
                "max_effective_rate": str(max(rates)),
                "mean_output_tokens": str(
                    sum(Decimal(v["output_tokens"]) for v in values) / len(values)
                ),
                "true_input_overhead_tokens": None,
            }
    attention = load_module(
        "public_forecast_attention", ROOT / "python/agentc/_attention.py"
    )
    dispatch = {
        "workload": WORKLOAD,
        "endpoints": selected,
        "catalog": schema_catalog(selected),
    }
    forecasts = []
    for task in selected_tasks(bundle, "primary"):
        for model in sorted(selected):
            for stage in live.STAGES:
                sizes = []
                for own_history in histories[(model, stage)]:
                    call = adapter().call(
                        task,
                        stage,
                        own_history,
                        attention,
                        model=model,
                        run_identity="forecast-only",
                        prompts=adapter().prompts(),
                    )
                    sizes.append(payload_bytes(live.payload_for(call, dispatch)))
                stats = statistics[(model, stage)]
                mean_bytes = Decimal(sum(sizes)) / len(sizes)
                expected_input = math.ceil(
                    mean_bytes * Decimal(stats["mean_effective_rate"])
                )
                conservative_input = math.ceil(
                    max(sizes) * Decimal(stats["max_effective_rate"])
                )
                expected_output = math.ceil(Decimal(stats["mean_output_tokens"]))
                costs = {}
                for variant, tokens_in, tokens_out in (
                    ("expected", expected_input, expected_output),
                    ("conservative", conservative_input, 512),
                ):
                    usage = {
                        "prompt_tokens": tokens_in,
                        "completion_tokens": tokens_out,
                    }
                    costs[variant + "_observed_usd"] = str(
                        live.nominal_uncached_cost(dispatch, model, usage)
                    )
                    costs[variant + "_universal_price_cap_usd"] = str(
                        Decimal(tokens_in) * Decimal("0.000006")
                        + Decimal(tokens_out) * Decimal("0.000030")
                    )
                forecasts.append(
                    {
                        "task_id": task["task_id"],
                        "model": model,
                        "stage": stage,
                        "mean_payload_utf8_bytes": str(mean_bytes),
                        "max_payload_utf8_bytes": max(sizes),
                        "expected_input_tokens": expected_input,
                        "conservative_input_tokens": conservative_input,
                        "expected_output_tokens": expected_output,
                        "conservative_output_tokens": 512,
                        **costs,
                    }
                )
    options = {}
    for name, arms in (("four", FOUR_ARMS),):
        repetitions = Counter(ARMS[arm]["model"] for arm in arms)
        totals = {
            field: str(
                sum(
                    (
                        money(row[field]) * repetitions[row["model"]]
                        for row in forecasts
                    ),
                    Decimal(0),
                )
            )
            for field in (
                "expected_observed_usd",
                "conservative_observed_usd",
                "expected_universal_price_cap_usd",
                "conservative_universal_price_cap_usd",
            )
        }
        options[name] = {
            "arms": list(arms),
            "scheduled_calls": PRIMARY_COUNT * len(arms) * 3,
            **totals,
        }
    return {
        "policy": FORECAST_POLICY,
        "selection_metric": "conservative_observed_usd",
        "primary_progress_ceiling_usd": str(PRIMARY_PROGRESS),
        "options": options,
        "pilot_model_stage_measurements": list(statistics.values()),
        "primary_baseline_forecasts": forecasts,
        "pilot_known_usd": str(
            sum((money(row["cost_usd"]) for row in pilot_calls), Decimal(0))
        ),
        "cache_credit": False,
        "rewrite_savings_credit": False,
        "uses_labels": False,
        "true_input_overhead_identifiable": False,
        "capacity_bound": False,
        "spending_guarantee": False,
        "history_method": "Each primary question with each same-model pilot own-history prefix; acquisition never reuses these histories.",
    }


def choose_arms(forecast):
    if (
        forecast["policy"] != FORECAST_POLICY
        or forecast["selection_metric"] != "conservative_observed_usd"
    ):
        raise PilotError("public forecast selection policy changed")
    if (
        money(forecast["options"]["four"]["conservative_observed_usd"])
        <= PRIMARY_PROGRESS
    ):
        return FOUR_ARMS
    raise PilotError(
        "fixed four-cell public factorial exceeds the frozen forecast gate"
    )


def resources(rows):
    return {
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


def cell_report(rows, task, label):
    history, valid = {}, {}
    for stage in live.STAGES:
        row = next(r for r in rows if r["workflow_stage"] == stage)
        update_history(task, stage, history, row)
        valid[stage] = adapter().parse_stage(stage, history[stage]) is not None
    score = adapter().score_answer(history["answer"], label["answer"], history=history)
    if not all(valid.values()):
        score = {"em": 0.0, "f1": 0.0}
    return {
        **score,
        "valid_by_stage": valid,
        "invalid_path": not all(valid.values()),
        **resources(rows),
    }


def contrasts(cells):
    """Factorial differences averaged over factors outside each contrast."""
    if set(cells) != set(FOUR_ARMS):
        raise PilotError("paired task has the wrong fixed cell coverage")
    factors = ("model", "rules_bundle")
    signs = {
        arm: {
            "model": 1 if ARMS[arm]["model"] == live.TARGET_MODEL else -1,
            "cc": 1 if ARMS[arm]["cc"] else -1,
            "sd": 1 if ARMS[arm]["sd"] else -1,
            "rules_bundle": 1 if ARMS[arm]["cc"] and ARMS[arm]["sd"] else -1,
        }
        for arm in cells
    }
    result = {}
    for size in range(1, len(factors) + 1):
        for selected in itertools.combinations(factors, size):
            weights = {
                arm: math.prod(signs[arm][factor] for factor in selected)
                for arm in cells
            }
            denominator = 2 ** (len(factors) - size)
            for metric in ("em", "f1", "cost_usd", "log_cost"):
                values = {
                    arm: float(
                        cell["cost_usd"] if metric == "log_cost" else cell[metric]
                    )
                    for arm, cell in cells.items()
                }
                if metric == "log_cost":
                    if any(value <= 0 for value in values.values()):
                        result["x".join(selected) + "/" + metric] = None
                        continue
                    values = {arm: math.log(value) for arm, value in values.items()}
                result["x".join(selected) + "/" + metric] = (
                    sum(weights[arm] * value for arm, value in values.items())
                    / denominator
                )
    return result


def analyze(args, manifest):
    checked = audit(args, manifest)
    applied_rules = {
        i["materialization"]["rule"]
        for i in checked["intents"]
        if i["materialization"]["applied"]
    }
    report = {
        "kind": KIND,
        "manifest_sha256": digest(manifest),
        "artifact_hashes": checked["artifact_hashes"],
        "schedule_complete": checked["schedule_complete"],
        "finance": checked["finance"],
        "paired_tasks": [],
        "descriptive_task_cluster_intervals": None,
        "labels_read": False,
        "paper_evidence": False,
        "efficacy_claim": False,
        "adaptive_policy_tested": False,
        "native_admission_tested": False,
        "applied_transformations": sorted(applied_rules),
        "resources": resources(checked["calls"]),
        "limitations": LIMITATIONS,
        "materialization_coverage": dict(
            Counter(
                (i["materialization"]["rule"] or "none")
                + "/"
                + str(i["materialization"]["applied"])
                for i in checked["intents"]
            )
        ),
        "materialization_abstentions": dict(
            Counter(
                i["materialization"]["abstention_reason"]
                for i in checked["intents"]
                if not i["materialization"]["applied"]
            )
        ),
        "application_invalid_calls": sum(
            not valid for valid in checked["valid_by_call"].values()
        ),
    }
    if checked["schedule_complete"] and manifest["phase"] == "primary":
        # This is the only label-file access in this module, after full public,
        # own-history, source, provider, and financial reconstruction.
        label_path = Path(args.bundle) / "labels.json"
        if file_hash(label_path) != checked["bundle"]["provenance"]["labels_sha256"]:
            raise PilotError("offline labels changed after public fixture freeze")
        labels = read(label_path)
        expected_ids = set(
            checked["bundle"]["selection"]["pilot_ids"]
            + checked["bundle"]["selection"]["primary_ids"]
        )
        if set(labels) != expected_ids or any(
            not isinstance(value.get("answer"), str) or not value["answer"]
            for value in labels.values()
        ):
            raise PilotError("offline labels do not cover the frozen selected IDs")
        for task in selected_tasks(checked["bundle"], "primary"):
            cells = {
                arm: cell_report(
                    [
                        row
                        for row in checked["calls"]
                        if row["task_id"] == task["task_id"] and row["arm"] == arm
                    ],
                    task,
                    labels[task["task_id"]],
                )
                for arm in manifest["arms"]
            }
            report["paired_tasks"].append(
                {
                    "task_id": task["task_id"],
                    "cells": cells,
                    "contrasts": contrasts(cells),
                }
            )
        if len(report["paired_tasks"]) != PRIMARY_COUNT:
            raise PilotError(
                "primary analysis requires all 32 independent task clusters"
            )
        metrics = []
        for task in report["paired_tasks"]:
            row = dict(task["contrasts"])
            for arm, cell in task["cells"].items():
                for field in (
                    "em",
                    "f1",
                    "cost_usd",
                    "nominal_uncached_cost_usd",
                    "input_tokens",
                    "output_tokens",
                    "cached_input_tokens",
                    "cache_unknown_calls",
                    "observational_latency_ms",
                ):
                    row[arm + "/" + field] = float(cell[field])
            metrics.append(row)
        finite_fields = [
            field
            for field in metrics[0]
            if all(row[field] is not None for row in metrics)
        ]
        report.update(
            labels_read=True,
            undefined_log_contrasts=[
                field for field in metrics[0] if field not in finite_fields
            ],
            descriptive_task_cluster_intervals=analysis.paired_bootstrap(
                [{field: row[field] for field in finite_fields} for row in metrics],
                seed=2026090504,
            ),
            scheduled_factorial_factors=["model", "rules_bundle"],
            operational_interpretation=SCOPE_DECISION["operational_intervention"],
            individual_cc_sd_identified=False,
            pilot_inclusive_known_usd=str(
                money(checked["finance"]["known_stage_usd"])
                + money(manifest["pilot_evidence"]["known_cost_usd"])
            ),
        )
    write_json(args.output / "analysis.json", report)
    return {
        "schedule_complete": report["schedule_complete"],
        "paired_tasks": len(report["paired_tasks"]),
        "labels_read": report["labels_read"],
        "finance": report["finance"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("prepare-pilot", "prepare-primary", "run", "audit", "analyze"),
    )
    for name in ("bundle", "ledger", "output", "bridge"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("env-file", "pilot"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--pilot-manifest-sha256")
    args = parser.parse_args()
    try:
        if args.command.startswith("prepare") or args.command == "run":
            if args.env_file is None:
                raise PilotError("acquisition requires its explicitly named key file")
            key = load_key(args.env_file)
        else:
            key = None
        if args.command.startswith("prepare"):
            result = freeze(args, key, args.command.removeprefix("prepare-"))
        else:
            manifest = read(args.output / "manifest.json")
            if args.command == "run":
                result = run(args, key, manifest)
            elif args.command == "analyze":
                result = analyze(args, manifest)
            else:
                checked = audit(args, manifest)
                result = {
                    "schedule_complete": checked["schedule_complete"],
                    "finance": checked["finance"],
                    "valid_calls": sum(checked["valid_by_call"].values()),
                    "labels_read": False,
                }
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError):
        print(
            "Public factorial stopped at validation, provider, or budget boundary; audit only."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
