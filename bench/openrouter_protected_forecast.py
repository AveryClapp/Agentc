"""Label-free resource scenarios, never capacity certificates or spending guarantees.

The caller independently audits receipts and enforces original campaign/family
accounting. This module only renders public byte envelopes and estimates cost.
It takes task/receipt values, never opens labels/credentials, and never dispatches
provider requests. Source and offline materializer files are hashed for provenance.
"""

import hashlib
import re
from collections import defaultdict
from copy import deepcopy
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from bench import openrouter_protected_fixture as fixture
from bench import openrouter_rules_live as live
from bench import openrouter_typed_factorial as materializer
from bench import typed_relational_workload as typed
from bench.openrouter_joint_study import restrict_call
from bench.openrouter_matrix import ROOT, file_hash, load_module
from bench.openrouter_pilot import (
    PilotError,
    canonical,
    digest,
    make_request,
    money,
    upper_cost,
    validate_schema_endpoint,
)

POLICY = "protected-public-envelope-max-effective-rate-105pct-256-v1"
MODELS = (live.SOURCE_MODEL, live.TARGET_MODEL)
MAX_TOKENS, PLAN_PAIRS, SITE_ATTEMPTS = 256, 20, 60
DRIFT = Decimal("1.05")
PLANS = {
    "filter": (
        ("CC", MODELS[0], "ContextCompress"),
        ("MD", MODELS[1], None),
        ("CC+MD", MODELS[1], "ContextCompress"),
    ),
    "synthesize": (("MD", MODELS[1], None),),
    "answer": (
        ("SD", MODELS[0], "StateDrop"),
        ("MD", MODELS[1], None),
        ("SD+MD", MODELS[1], "StateDrop"),
    ),
}
PAIR_PREMISES = [
    "Seven stable exact plan identities; no new versions or resets.",
    "Evidence target20, retained paired window50, exploration60/site, one candidate/arrival.",
    "63 sequential source workflows within6h and one24h evidence/budget window.",
    "Every successful continuing probe appends exactly one authoritative pair, including semantic disagreements.",
    "First failed or ambiguous attempt permanently stops the entire protocol; no retry/resume.",
    "The driver independently verifies the actual native/coordinator contract; this arithmetic does not prove these premises.",
]
SOURCE_FILES = (
    "bench/openrouter_protected_forecast.py",
    "bench/openrouter_protected_fixture.py",
    "bench/typed_relational_workload.py",
    "bench/structured_relational_workload.py",
    "bench/openrouter_typed_factorial.py",
    "bench/openrouter_joint_study.py",
    "bench/openrouter_rules_live.py",
    "bench/openrouter_pilot.py",
    "python/agentc/_attention.py",
)


def _require(condition, message):
    if not condition:
        raise PilotError(message)


def _file_digest(value):
    return hashlib.sha256(canonical(value) + b"\n").hexdigest()


def _key(row):
    return tuple(row[k] for k in ("phase", "arm", "task_id", "workflow_stage"))


def normalize_reference(manifest, calls, intents, *, artifact_hashes):
    """Pin and normalize ALL historical metadata; never select favorable repeats.

    Original artifacts must use the repo's canonical JSON plus newline format.
    Payloads are reconstructed using their own original frozen enrollment.
    No answer text, correctness label, or score is inspected.
    """
    _require(
        set(artifact_hashes) == {"manifest.json", "calls.json", "intents.json"},
        "historical evidence needs all three externally pinned raw hashes",
    )
    for name, value in (
        ("manifest.json", manifest),
        ("calls.json", calls),
        ("intents.json", intents),
    ):
        _require(
            artifact_hashes[name] == _file_digest(value),
            "historical raw artifact hash mismatch",
        )
    _require(
        manifest["workload"] == typed.WORKLOAD
        and manifest["prompts"] == typed.prompts(),
        "historical public prompt contract differs",
    )
    _require(
        len(calls) == len(intents) and bool(calls),
        "historical calls/intents incomplete",
    )
    normalized = []
    for call, intent in zip(calls, intents, strict=True):
        _require(
            _key(call) == _key(intent), "historical receipt/intention order differs"
        )
        original = intent["original_call"]
        payload = live.payload_for(original, manifest)
        _require(
            call["request_sha256"] == digest(payload)
            and call["model"] == payload["model"],
            "historical request cannot be reconstructed exactly",
        )
        normalized.append(
            {
                **{k: intent[k] for k in ("phase", "arm", "task_id", "workflow_stage")},
                "id": call["id"],
                "payload": payload,
            }
        )
    return {
        "calls": deepcopy(calls),
        "intents": normalized,
        "reference_manifest_sha256": digest(manifest),
        "reference_artifact_hashes": deepcopy(artifact_hashes),
    }


def _measurements(calls, intents, dispatch, pilot_ids):
    _require(len(calls) == len(intents) and bool(calls), "usage evidence is incomplete")
    samples, seen, ids = defaultdict(list), set(), set()
    for call, intent in zip(calls, intents, strict=True):
        key = _key(call)
        stage, model, payload = call["workflow_stage"], call["model"], intent["payload"]
        _require(
            _key(intent) == key
            and key not in seen
            and call["id"] == intent["id"]
            and call["id"] not in ids,
            "usage evidence has duplicate or mismatched identities",
        )
        seen.add(key)
        ids.add(call["id"])
        _require(
            stage in typed.STAGES
            and model in MODELS
            and call["finish_reason"] == "stop",
            "usage evidence has unsupported model/stage/status",
        )
        cap = payload["max_tokens"]
        _require(
            type(cap) is int and cap in (256, 512),
            "unsupported historical or pilot output cap",
        )
        expected = make_request(
            model,
            [dispatch["endpoints"][model]["tag"]],
            payload["messages"],
            max_tokens=cap,
            response_format=typed.schema_for(stage),
        )
        _require(
            canonical(payload) == canonical(expected),
            "usage payload escaped the frozen typed transport",
        )
        usage = call["usage"]
        _require(
            type(usage["prompt_tokens"]) is int
            and usage["prompt_tokens"] > 0
            and type(usage["completion_tokens"]) is int
            and 0 <= usage["completion_tokens"] <= cap,
            "invalid billed usage",
        )
        money(call["cost_usd"])
        if pilot_ids is not None:
            _require(
                call["phase"] == "pilot"
                and call["arm"] == ("S0" if model == MODELS[0] else "H0")
                and call["task_id"] in pilot_ids
                and cap == MAX_TOKENS,
                "fresh pilot is not the frozen6x2x3 baseline schedule",
            )
        size = len(canonical(payload))
        samples[(model, stage)].append(
            {
                "task_id": call["task_id"],
                "arm": call["arm"],
                "id": call["id"],
                "payload_sha256": digest(payload),
                "payload_utf8_bytes": size,
                "billed_input_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "effective_rate": str(Decimal(usage["prompt_tokens"]) / size),
            }
        )
    result = {}
    for model in MODELS:
        for stage in typed.STAGES:
            values = samples[(model, stage)]
            covered = {row["task_id"] for row in values}
            _require(
                len(covered) >= 6,
                "need at least six distinct public tasks per model/stage",
            )
            if pilot_ids is not None:
                _require(
                    len(values) == 6 and covered == set(pilot_ids),
                    "fresh pilot does not cover all six tasks exactly once",
                )
            result[(model, stage)] = {
                "model": model,
                "stage": stage,
                "samples": values,
                "max_effective_rate": str(
                    max(Decimal(v["effective_rate"]) for v in values)
                ),
            }
    return result


def _histories(task, stage):
    if stage == "filter":
        return [{}]
    # JSON-escaped UTF8 contribution, not character count or semantic relevance.
    paragraphs = sorted(
        task["meta"]["paragraphs"],
        key=lambda p: (
            -len(canonical(p["title"] + "\n" + "\n".join(p["sentences"]))),
            p["title"],
        ),
    )[:3]
    selected = typed.application_text(
        canonical({"result": sorted(p["title"] for p in paragraphs)}).decode(),
        stage="filter",
    )
    valid = {"filter": selected}
    if stage == "answer":
        # Largest accepted widths; deliberately no arithmetic/gold oracle.
        synthesis = {
            "sku": "K999",
            "vendor": "V999",
            "qty": 999,
            "unit": 9999,
            "total": 9999999,
            "region": "US",
        }
        valid["synthesize"] = typed.application_text(
            canonical({"result": synthesis}).decode(), stage="synthesize"
        )
        _require(
            valid["synthesize"] != typed.ERROR, "synthetic synthesis domain changed"
        )
    return [valid, {key: typed.ERROR for key in valid}]


def _envelopes(bundle, dispatch, bridge):
    attention = load_module(
        "protected_forecast_attention", ROOT / "python/agentc/_attention.py"
    )
    records = []
    for task in bundle["tasks"]:
        for stage in typed.STAGES:
            variants = []
            for history in _histories(task, stage):
                original = restrict_call(
                    typed.call(
                        task,
                        stage,
                        history,
                        attention,
                        model=MODELS[0],
                        run_identity="public-resource-envelope",
                        prompts=typed.prompts(),
                    ),
                    stage,
                )
                original["parameters"]["max_output_tokens"] = MAX_TOKENS
                rule = materializer.RULES.get(stage)
                rewritten, proof = materializer.materialize(original, rule, bridge)
                _require(
                    "input_tokens_upper_bound"
                    not in original["parameters"]["extra"]["agentc_route_context"],
                    "byte envelope must not become a routing capacity claim",
                )
                variants.append(
                    (
                        original,
                        rewritten,
                        proof,
                        not history
                        or all(value != typed.ERROR for value in history.values()),
                    )
                )
            for model in MODELS:
                for rewrites in (False, True):
                    candidates = []
                    for original, rewritten, proof, valid in variants:
                        call = deepcopy(rewritten if rewrites else original)
                        call["model"] = model
                        payload = live.payload_for(call, dispatch)
                        candidates.append(
                            {
                                "payload_utf8_bytes": len(canonical(payload)),
                                "payload_sha256": digest(payload),
                                "valid_synthetic_history": valid,
                                "materialization": proof if rewrites else None,
                                "allowance_usd": str(upper_cost(payload)),
                            }
                        )
                    largest = max(candidates, key=lambda row: row["payload_utf8_bytes"])
                    records.append(
                        {
                            "task_id": task["task_id"],
                            "stage": stage,
                            "model": model,
                            "rewrites": rewrites,
                            **largest,
                            "history_variants": candidates,
                        }
                    )
    return records


def _sum(rows):
    return sum((money(row["cost_usd"]) for row in rows), Decimal(0))


def _forecast(bundle, calls, intents, enrollment, materializer_path, *, reference):
    fixture.validate_bundle(bundle)
    dispatch = {
        "workload": typed.WORKLOAD,
        "endpoints": enrollment["endpoints"],
        "catalog": enrollment["catalog"],
    }
    _require(
        set(dispatch["endpoints"]) == set(MODELS),
        "forecast needs the two frozen endpoints",
    )
    for model in MODELS:
        validate_schema_endpoint(model, dispatch["endpoints"][model])
    bridge = Path(materializer_path)
    _require(bridge.is_file(), "frozen materializer unavailable")
    bridge_sha256 = file_hash(bridge)
    fresh = reference is None
    measured = _measurements(
        calls, intents, dispatch, bundle["selection"]["pilot_ids"] if fresh else None
    )
    envelopes = _envelopes(bundle, dispatch, bridge)
    _require(file_hash(bridge) == bridge_sha256, "materializer changed during forecast")
    for row in envelopes:
        rate = money(measured[(row["model"], row["stage"])]["max_effective_rate"])
        tokens = int(
            (row["payload_utf8_bytes"] * rate * DRIFT).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        row.update(
            estimated_input_tokens=tokens,
            output_tokens=MAX_TOKENS,
            cost_usd=str(
                live.nominal_uncached_cost(
                    dispatch,
                    row["model"],
                    {"prompt_tokens": tokens, "completion_tokens": MAX_TOKENS},
                )
            ),
        )
    indexed = {
        (r["task_id"], r["stage"], r["model"], r["rewrites"]): r for r in envelopes
    }
    study_ids, pilot_ids = (
        bundle["selection"]["study_ids"],
        bundle["selection"]["pilot_ids"],
    )
    source = [
        indexed[(task_id, stage, MODELS[0], False)]
        for task_id in study_ids
        for stage in typed.STAGES
    ]
    hr = [
        indexed[(task_id, stage, MODELS[1], True)]
        for task_id in study_ids
        for stage in typed.STAGES
    ]
    pilot = [
        indexed[(task_id, stage, model, False)]
        for task_id in pilot_ids
        for model in MODELS
        for stage in typed.STAGES
    ]
    plan_costs, generic = [], []
    for stage, plans in PLANS.items():
        by_task = defaultdict(list)
        for name, model, rule in plans:
            candidates = [
                indexed[(task_id, stage, model, rule is not None)]
                for task_id in study_ids
            ]
            chosen = sorted(
                candidates, key=lambda row: (-money(row["cost_usd"]), row["task_id"])
            )[:PLAN_PAIRS]
            plan_costs.append(
                {
                    "stage": stage,
                    "plan": name,
                    "model": model,
                    "max_completed_pairs": PLAN_PAIRS,
                    "selected_cost_envelope_ids": [r["task_id"] for r in chosen],
                    "cost_usd": str(_sum(chosen)),
                }
            )
            for row in candidates:
                by_task[row["task_id"]].append(row)
        expensive = [
            max(values, key=lambda row: money(row["cost_usd"]))
            for values in by_task.values()
        ]
        chosen = sorted(
            expensive, key=lambda row: (-money(row["cost_usd"]), row["task_id"])
        )[:SITE_ATTEMPTS]
        generic.append(
            {
                "stage": stage,
                "max_attempts": SITE_ATTEMPTS,
                "cost_usd": str(_sum(chosen)),
            }
        )
    source_cost, hr_cost = _sum(source), _sum(hr)
    training = source_cost + _sum(plan_costs)
    largest_pair = max(
        money(indexed[(task_id, stage, MODELS[0], False)]["allowance_usd"])
        + money(indexed[(task_id, stage, model, rule is not None)]["allowance_usd"])
        for task_id in study_ids
        for stage, plans in PLANS.items()
        for _, model, rule in plans
    )
    return {
        "policy": POLICY,
        "evidence_kind": "fresh_pilot" if fresh else "historical_pre_pilot",
        "training_usd": str(training),
        "HR_usd": str(hr_cost),
        "source_usd": str(source_cost),
        "candidate_usd": str(_sum(plan_costs)),
        "training_plus_largest_pair_reserve_usd": str(training + largest_pair),
        "largest_pair_allowance_usd": str(largest_pair),
        "prospective_pilot_usd": str(_sum(pilot)),
        "observed_evidence_cost_usd": str(
            sum((money(row["cost_usd"]) for row in calls), Decimal(0))
        ),
        "training_and_HR_usd": str(training + hr_cost),
        "scenarios": {
            "seven_plans_twenty_completed_pairs": {
                "probes": 140,
                "training_usd": str(training),
                "plans": plan_costs,
                "premises": PAIR_PREMISES,
            },
            "generic_sixty_attempts_per_site": {
                "attempts": 180,
                "training_usd": str(source_cost + _sum(generic)),
                "sites": generic,
                "scope": "Generic native budget; not a successful completed coordinator trajectory. Failed-attempt charge remains covered by unchanged full-context reservations, not this empirical cost.",
            },
        },
        "public_envelopes": envelopes,
        "measurements": list(measured.values()),
        "provenance": {
            "selection_sha256": bundle["selection"]["selection_sha256"],
            "public_sha256": bundle["provenance"]["public_sha256"],
            "enrollment_sha256": digest(enrollment),
            "calls_sha256": digest(calls),
            "intents_sha256": digest(intents),
            "reference": reference,
            "materializer_sha256": bridge_sha256,
            "source_files": {path: file_hash(ROOT / path) for path in SOURCE_FILES},
        },
        "assumptions": {
            "input_rate_drift_multiplier": str(DRIFT),
            "output_tokens_each": MAX_TOKENS,
            "byte_scope": "Exact client JSON UTF8 only; includes schema representation but excludes opaque provider-added instructions.",
            "history_scope": "Largest three publicly rendered documents; maximal schema-valid synthesis widths or invalid own-history state, whichever is larger after actual rule materialization.",
            "input_estimate": "Each model/stage's maximum observed billed-input/byte ratio times105%; schema/tokenizer overhead is not separately identifiable and may not scale linearly.",
            "placement": "Top20 task cost envelopes independently per declared plan; generic scenario top60 per-site maxima. No favorable outcome or actual task selection.",
            "pair_headroom": "Adding the full pair reserve to all projected training charges conservatively includes that pair's projected charge too; the driver must still check every actual reservation.",
            "rewrite_credit": "Only actual native deletion-only CC/SD proposals on public byte envelopes; not planner admission or quality evidence.",
            "cache_credit": False,
            "uses_labels": False,
            "provider_capacity_bound": False,
            "spending_guarantee": False,
            "adaptive_savings_claim": False,
        },
    }


def forecast(bundle, pilot_calls, pilot_intents, frozen_enrollment, materializer_path):
    """Fresh audited36-call pilot only; caller applies current financial gates."""
    return _forecast(
        bundle,
        pilot_calls,
        pilot_intents,
        frozen_enrollment,
        materializer_path,
        reference=None,
    )


def prepilot_forecast(
    bundle,
    reference_calls,
    reference_intents,
    frozen_enrollment,
    materializer_path,
    *,
    reference_manifest_sha256,
    reference_artifact_hashes,
):
    """Historical metadata scenario, not fresh pilot evidence or paid approval."""
    _require(
        isinstance(reference_manifest_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", reference_manifest_sha256)
        and set(reference_artifact_hashes)
        == {"manifest.json", "calls.json", "intents.json"}
        and all(
            isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
            for v in reference_artifact_hashes.values()
        ),
        "historical reference pins are incomplete",
    )
    return _forecast(
        bundle,
        reference_calls,
        reference_intents,
        frozen_enrollment,
        materializer_path,
        reference={
            "manifest_sha256": reference_manifest_sha256,
            "artifact_hashes": deepcopy(reference_artifact_hashes),
            "selection": "All supplied model/stage measurements including every source repeat; caller pins original raw bytes through normalize_reference, no accuracy filtering.",
        },
    )
