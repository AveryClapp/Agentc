"""Read-only, fail-closed analysis of the frozen prospective input-rule study.

No native runtime, credentials, provider requests, or ledger writes are used.
Only a complete scheduled heldout can yield descriptive paired comparisons;
artifact-only reports cannot certify financial completeness. Hashes bind a
snapshot, not the absence of future provider adjustments or external spending.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import fcntl
import hashlib
import json
import math
from pathlib import Path
import random
from types import SimpleNamespace

from bench import openrouter_joint_study as study
from bench import openrouter_rules_live as live
from bench.openrouter_attempts import accounting
from bench.openrouter_matrix import ROOT, file_hash, load_module, score, write_json
from bench.openrouter_pilot import PilotError, digest, money, text_choice
from bench.openrouter_replay import lexical_divergence
from bench.openrouter_rules_comparison import load_tasks
from bench.openrouter_rules_diagnostics import analyze as plan_diagnostics

BOOTSTRAP_SEED = "joint-input-study-question-bootstrap-v1"
PHASES = ("calibration", "warmup", "training", "heldout")
SCOPES = ("calibration", "primary", "exploration", "shadow")
ITEM_KEYS = ("task_id", "phase", "arm", "workflow_stage")


def read_ledger_snapshot(path):
    """Read one consistent byte snapshot; never create, truncate, or wait on it."""
    with Path(path).open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PilotError("ledger is busy; capture a stable snapshot later") from exc
        try:
            raw = handle.read()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if (
        not events
        or not isinstance(events[0].get("key_id"), str)
        or not events[0]["key_id"]
        or len({e.get("key_id") for e in events}) != 1
    ):
        raise PilotError("ledger snapshot is empty or mixes key bindings")
    return events, hashlib.sha256(raw).hexdigest()


def metadata(item, scope, signature, payload, manifest, manifest_sha256=None):
    endpoint = manifest["endpoints"][payload["model"]]
    return {
        **item,
        "scope": scope,
        "manifest_sha256": manifest_sha256 or digest(manifest),
        "decision_sha256": digest(signature),
        "dispatch_contract": {
            "provider_name": endpoint["provider_name"],
            "endpoint_model": endpoint["name"].split(" | ", 1)[1],
        },
    }


def validate_row(row, payload, meta, manifest):
    endpoint = manifest["endpoints"][payload["model"]]
    if (
        row["model"] != payload["model"]
        or row["provider"] != endpoint["provider_name"]
        or row["fingerprint"]
        != digest({"payload": payload, "metadata": meta, "stage": row["stage"]})
        or not isinstance(row["answer"], str)
    ):
        raise PilotError("call dispatch, fingerprint, or answer attribution changed")
    usage = row["usage"]
    for name in ("prompt_tokens", "completion_tokens"):
        if type(usage.get(name)) is not int or usage[name] < 0:
            raise PilotError("invalid provider token accounting")
    if usage.get("is_byok") or money(row["cost_usd"]) != money(usage.get("cost")):
        raise PilotError("call billing differs from provider usage")
    cached = usage.get("prompt_tokens_details", {}).get("cached_tokens")
    if (
        cached is not None
        and (type(cached) is not int or not 0 <= cached <= usage["prompt_tokens"])
    ) or row["cached_input_tokens"] != cached:
        raise PilotError("cached-token accounting changed")
    nominal = (
        money(endpoint["pricing"]["prompt"]) * usage["prompt_tokens"]
        + money(endpoint["pricing"]["completion"]) * usage["completion_tokens"]
    )
    if money(row["nominal_uncached_cost_usd"]) != nominal:
        raise PilotError("nominal uncached cost differs from frozen prices")
    if (
        isinstance(row["latency_ms"], bool)
        or not math.isfinite(row["latency_ms"])
        or row["latency_ms"] < 0
    ):
        raise PilotError("invalid latency")
    router = row.get("router_metadata") or {}
    selected = [
        e
        for e in router.get("endpoints", {}).get("available", [])
        if e.get("selected") is True
    ]
    if (
        router.get("requested") != payload["model"]
        or router.get("attempt") != 1
        or router.get("is_byok") is not False
        or len(selected) != 1
        or selected[0].get("provider") != endpoint["provider_name"]
        or selected[0].get("model") != meta["dispatch_contract"]["endpoint_model"]
    ):
        raise PilotError("selected provider endpoint metadata changed")


def validate_journals(manifest, decisions, calls, intents, tasks, attention):
    manifest_hash = digest(manifest)
    stage_id = "rules-live-dev-v1-" + manifest_hash[:20]
    live.Acquisition.validate_journals(
        SimpleNamespace(
            manifest=manifest,
            decisions=decisions,
            calls=calls,
            intents=intents,
            stage=stage_id,
        )
    )
    by_id = {row["id"]: row for row in calls}
    calibration_steps = (
        sum(r["phase"] == "calibration" for r in manifest["schedule"]) * 3
    )
    selector = (
        live.static_selection(decisions, calls, tasks, manifest)
        if len(decisions) >= calibration_steps
        else None
    )
    histories, expected = {}, {}
    failures = []
    for index, intent in enumerate(intents):
        item = {k: intent[k] for k in ITEM_KEYS}
        arm, phase, task, stage = (
            item[k] for k in ("arm", "phase", "task_id", "workflow_stage")
        )
        model = (
            arm.removeprefix("calibration/")
            if phase == "calibration"
            else selector["model"]
            if arm == "sequential"
            else live.SOURCE_MODEL
        )
        history = histories.setdefault((task, arm), {})
        original = study.restrict_call(
            live.workflow_call(
                tasks[task],
                stage,
                history,
                attention,
                model=model,
                run_identity=stage_id + "/" + arm,
                prompts=manifest["prompts"],
            ),
            stage,
        )
        if original != intent["original_call"]:
            raise PilotError(
                "original request differs from this arm's own workflow history"
            )
        signature = intent["semantic_plan"]
        allowed_rules = set(
            filter(
                None,
                manifest["policies"]["original" if phase == "calibration" else arm][
                    "AGENTC_ENABLED_RULES"
                ].split(","),
            )
        )
        if (
            not set(signature["primary_rules"] + signature["candidate_rules"])
            <= allowed_rules
        ):
            raise PilotError("plan contains a rule disabled for this arm")
        if arm == "historical_rules" and signature["kind"] == "composed":
            raise PilotError("historical noncomposing arm contains a composed plan")
        if phase == "heldout" and signature["candidate"] is not None:
            raise PilotError("heldout contains forbidden exploration")
        payloads = {
            "primary": signature["primary"],
            "calibration": signature["primary"],
            "exploration": signature["candidate"],
            "shadow": live.payload_for(original, manifest),
        }
        scopes = ["calibration" if phase == "calibration" else "primary"]
        if signature["candidate"] is not None:
            scopes.append("exploration")
        elif (
            phase != "heldout"
            and signature["kind"] != "pass_through"
            and live.shadow_sample(
                manifest["shadow_seed"],
                arm + "/" + stage,
                task,
                float(
                    live.phase_settings(manifest, arm, phase)["AGENTC_OPTIMIZE_SHADOW"]
                ),
            )
        ):
            scopes.append("shadow")
        for scope in scopes:
            payload = payloads[scope]
            call_id = stage_id + "-" + digest([item, scope])[:24]
            if payload is None:
                continue
            expected[call_id] = (
                payload,
                metadata(item, scope, signature, payload, manifest, manifest_hash),
            )
            if call_id in by_id:
                row = by_id[call_id]
                validate_row(row, payload, expected[call_id][1], manifest)
                if row.get("finish_reason") not in {"stop", "length"}:
                    failures.append(
                        {"id": call_id, "finish_reason": row.get("finish_reason")}
                    )
        primary_id = (
            stage_id
            + "-"
            + digest([item, "calibration" if phase == "calibration" else "primary"])[
                :24
            ]
        )
        if primary_id in by_id:
            history[stage] = by_id[primary_id]["answer"]
        if index < len(decisions):
            decision = decisions[index]
            if (
                live.semantic_plan(original, decision["native_plan"], manifest)
                != signature
            ):
                raise PilotError("decision native plan differs from its intent")
            active = live.activation(original, decision["native_plan"])
            active["executed_on_provider"] = True
            if active != decision["activation"]:
                raise PilotError("decision activation differs from dispatched plan")
            feedback = decision["divergence_feedback"]
            ids = decision["incurred_ids"]
            if phase in {"calibration", "heldout"} and (
                feedback is not None or len(ids) != 1
            ):
                raise PilotError(
                    "calibration or heldout contains optimizer feedback/probes"
                )
            if len(ids) == 2:
                if feedback != lexical_divergence(
                    by_id[ids[0]]["answer"], by_id[ids[1]]["answer"]
                ):
                    raise PilotError("paired feedback differs from returned answers")
            elif feedback is not None:
                raise PilotError("unpaired decision contains divergence feedback")
    return selector, expected, failures


def ledger_report(events, stage_id, calls, expected):
    """Validate every stage attempt and keep failure known charge + full residual."""
    state = accounting(events)
    results = {k: v for k, v in state["results"].items() if v["stage"] == stage_id}
    reserves = {k: r for k, r in state["reserves"].items() if r["stage"] == stage_id}
    by_id = {r["id"]: r for r in calls}
    records, failures = [], []
    responses = defaultdict(list)
    for event in events:
        if event["event"] == "response":
            responses[(event["id"], event.get("attempt_id"))].append(event)
    for key, reserve in reserves.items():
        if reserve["id"] not in expected:
            raise PilotError(
                "ledger stage attempt lies outside the validated intent prefix"
            )
        payload, meta = expected[reserve["id"]]
        if (
            reserve["request"] != payload
            or reserve["metadata"] != meta
            or reserve["fingerprint"]
            != digest({"payload": payload, "metadata": meta, "stage": stage_id})
        ):
            raise PilotError("ledger attempt differs from its frozen request")
        terminal = state["terminals"].get(key)
        if terminal:
            known = money(terminal.get("reported_cost_usd") or 0)
            records.append(
                {
                    **{k: meta[k] for k in ITEM_KEYS},
                    "scope": meta["scope"],
                    "kind": "failed_attempt",
                    "id": reserve["id"],
                    "attempt_id": reserve.get("attempt_id"),
                    "known_usd": str(known),
                    "uncertainty_usd": str(
                        max(Decimal(0), money(reserve["upper_cost_usd"]) - known)
                    ),
                    "pending_usd": "0",
                    "reported_cost_known": terminal.get("reported_cost_usd")
                    is not None,
                }
            )
        elif reserve in state["pending"]:
            records.append(
                {
                    **{k: meta[k] for k in ITEM_KEYS},
                    "scope": meta["scope"],
                    "kind": "pending_attempt",
                    "id": reserve["id"],
                    "attempt_id": reserve.get("attempt_id"),
                    "known_usd": "0",
                    "uncertainty_usd": "0",
                    "pending_usd": str(money(reserve["upper_cost_usd"])),
                }
            )
    for call_id, result in results.items():
        matching = [
            r
            for k, r in reserves.items()
            if k not in state["terminals"]
            and r["id"] == call_id
            and r.get("attempt_id") == result.get("attempt_id")
        ]
        if len(matching) != 1 or not result.get("attempt_id"):
            raise PilotError("stage result has no unique current attempt")
        payload, meta = expected[call_id]
        if result["metadata"] != meta:
            raise PilotError("ledger result metadata differs from intent")
        if call_id in by_id:
            public = {
                k: v for k, v in result.items() if k not in {"key_id", "at", "metadata"}
            }
            if any(by_id[call_id].get(k) != v for k, v in public.items()):
                raise PilotError("artifact result differs from authoritative ledger")
        bodies = responses[(call_id, result["attempt_id"])]
        if len(bodies) != 1:
            raise PilotError("stage result lacks one durable response")
        body = bodies[0]["response"]
        try:
            choice = text_choice(body)
            if (
                choice["message"]["content"] != result["answer"]
                or choice["finish_reason"] != result["finish_reason"]
                or body.get("usage") != result["usage"]
                or body.get("id") != result["generation_id"]
                or body.get("model") != result["model"]
                or body.get("provider") != result["provider"]
                or body.get("openrouter_metadata") != result["router_metadata"]
            ):
                raise PilotError("durable response differs from result")
        except PilotError:
            failures.append(
                {"id": call_id, "reason": "invalid durable provider response"}
            )
        if call_id not in by_id:
            records.append(
                {
                    **{k: meta[k] for k in ITEM_KEYS},
                    "scope": meta["scope"],
                    "kind": "unincorporated_result",
                    "id": call_id,
                    "known_usd": str(money(result["cost_usd"])),
                    "uncertainty_usd": "0",
                    "pending_usd": "0",
                }
            )
    pending = [e for e in state["pending"] if e["stage"] == stage_id]
    known = state["known_by_stage"].get(stage_id, Decimal(0))
    holds = state["holds_by_stage"].get(stage_id, Decimal(0))
    pending_cost = sum((money(r["upper_cost_usd"]) for r in pending), Decimal(0))
    missing, extra = (
        sorted(set(by_id) - set(results)),
        sorted(set(results) - set(by_id)),
    )
    unresolved = sorted(
        {r["id"] for r in pending}
        | ((state["failed_calls"] | state["unsafe_ids"]) & set(expected))
    )
    return {
        "snapshot_financially_complete": not (missing or extra or unresolved),
        "artifact_ids_missing_from_ledger": missing,
        "unincorporated_completed_ids": extra,
        "unresolved_stage_calls": unresolved,
        "failed_provider_results": failures,
        "known_stage_usd": str(known),
        "retained_uncertainty_usd": str(holds),
        "pending_upper_bound_usd": str(pending_cost),
        "conservative_stage_commitment_usd": str(known + holds + pending_cost),
        "campaign_known_usd": str(state["known_usd"]),
        "campaign_retained_uncertainty_usd": str(state["holds_usd"]),
        "campaign_pending_upper_bound_usd": str(state["pending_usd"]),
        "campaign_conservative_commitment_usd": str(
            state["known_usd"] + state["holds_usd"] + state["pending_usd"]
        ),
        "additional_attempt_records": records,
    }


def costs(calls, finance):
    records = [
        {
            **{k: r[k] for k in ITEM_KEYS},
            "scope": r["scope"],
            "id": r["id"],
            "kind": "artifact_result",
            "known_usd": r["cost_usd"],
            "uncertainty_usd": "0",
            "pending_usd": "0",
            "nominal_usd": r["nominal_uncached_cost_usd"],
        }
        for r in calls
    ]
    records += finance["additional_attempt_records"] if finance else []
    reports = []
    for arm in live.ARMS:
        charged = [
            r
            for r in records
            if r["arm"] == arm or (arm == "sequential" and r["phase"] == "calibration")
        ]

        def total(rows):
            values = {
                k: sum((money(r.get(k, "0")) for r in rows), Decimal(0))
                for k in ("known_usd", "uncertainty_usd", "pending_usd", "nominal_usd")
            }
            return {
                **{k: str(v) for k, v in values.items()},
                "conservative_commitment_usd": str(
                    sum(
                        values[k]
                        for k in ("known_usd", "uncertainty_usd", "pending_usd")
                    )
                ),
                "successful_nominal_missing_attempts": sum(
                    r["kind"] != "artifact_result" for r in rows
                ),
                "records": len(rows),
            }

        reports.append(
            {
                "arm": arm,
                "all_phases": total(charged),
                "setup": total([r for r in charged if r["phase"] != "heldout"]),
                "heldout": total([r for r in charged if r["phase"] == "heldout"]),
                "by_phase_and_scope": [
                    {
                        "phase": phase,
                        "scope": scope,
                        **total(
                            [
                                r
                                for r in charged
                                if r["phase"] == phase and r["scope"] == scope
                            ]
                        ),
                    }
                    for phase in PHASES
                    for scope in SCOPES
                ],
                "cached_input_tokens_observed": sum(
                    r["cached_input_tokens"] or 0
                    for r in calls
                    if r["arm"] == arm
                    or (arm == "sequential" and r["phase"] == "calibration")
                ),
                "cache_accounting_missing_results": sum(
                    r["cached_input_tokens"] is None
                    for r in calls
                    if r["arm"] == arm
                    or (arm == "sequential" and r["phase"] == "calibration")
                ),
            }
        )
    return reports


def paired_bootstrap(rows, repetitions=10000, seed=BOOTSTRAP_SEED):
    """Resample entire matched questions, never arms/stages/calls separately."""
    if not rows or type(repetitions) is not int or repetitions < 100:
        raise PilotError(
            "paired bootstrap requires questions and at least100 replicates"
        )
    keys, n = tuple(rows[0]), len(rows)
    if any(
        tuple(row) != keys or any(not math.isfinite(v) for v in row.values())
        for row in rows
    ):
        raise PilotError("bootstrap rows differ or contain non-finite values")
    rng, distributions = random.Random(seed), {k: [] for k in keys}
    for _ in range(repetitions):
        sample = [rows[rng.randrange(n)] for _ in range(n)]
        for key in keys:
            distributions[key].append(sum(row[key] for row in sample) / n)

    def quantile(values, probability):
        values = sorted(values)
        position = (len(values) - 1) * probability
        low = int(position)
        return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (
            position - low
        )

    return {
        k: {
            "mean": sum(r[k] for r in rows) / n,
            "descriptive_95_percentile_interval": [
                quantile(values, 0.025),
                quantile(values, 0.975),
            ],
        }
        for k, values in distributions.items()
    }


def admission_counts(decisions):
    """Check evidence chronology, without pretending to reconstruct native guards."""
    pairs, groups = Counter(), {}
    for decision in decisions:
        signature = decision["semantic_plan"]
        identity = signature["primary_identity"]
        key = (decision["arm"], decision["workflow_stage"], digest(identity))
        selected_rules = set(signature["primary_rules"])
        if (
            decision["arm"] == "joint"
            and "ModelDowngrade" in selected_rules
            and selected_rules & {"ContextCompress", "StateDrop"}
            and pairs[key] < 20
        ):
            raise PilotError(
                "selected joint plan lacks20 earlier exact source comparisons"
            )
        count_key = (decision["phase"], *key, signature["kind"])
        if count_key not in groups:
            groups[count_key] = {
                "phase": decision["phase"],
                "arm": decision["arm"],
                "workflow_stage": decision["workflow_stage"],
                "identity": identity,
                "native_identity_available": set(identity.get("key", {}))
                == {"call_site_version", "execution_plan_id"},
                "kind": signature["kind"],
                "rules": signature["primary_rules"],
                "model": signature["primary"]["model"],
                "max_tokens": signature["primary"]["max_tokens"],
                "earlier_recorded_pairs_at_first_selection": pairs[key],
                "count": 0,
            }
        groups[count_key]["count"] += 1
        if decision["divergence_feedback"] is not None:
            paired_identity = (
                signature["candidate_identity"]
                if signature["candidate"] is not None
                else identity
            )
            pairs[
                (decision["arm"], decision["workflow_stage"], digest(paired_identity))
            ] += 1
    return [groups[key] for key in sorted(groups)]


def analyze(
    manifest,
    decisions,
    calls,
    intents,
    fixture,
    *,
    expected_manifest_sha256,
    gate=None,
    static=None,
    summary=None,
    ledger_events=None,
    ledger_sha256=None,
    source_root=ROOT,
    repetitions=10000,
):
    if digest(manifest) != expected_manifest_sha256:
        raise PilotError("manifest differs from externally frozen hash")
    study.validate_manifest(manifest)
    if manifest.get("scoring") != "unchanged_normalized_raw_answer_EM_F1":
        raise PilotError("scoring contract changed")
    if manifest["source_files"] != live.sources():
        raise PilotError("imported acquisition dependencies differ from frozen source")
    for path, expected_hash in manifest["source_files"].items():
        if file_hash(Path(source_root) / path) != expected_hash:
            raise PilotError(
                "analysis dependency differs from frozen acquisition source"
            )
    tasks = load_tasks(manifest, fixture)
    attention = load_module(
        "joint_analysis_attention", Path(source_root) / "python/agentc/_attention.py"
    )
    selector, expected, failures = validate_journals(
        manifest, decisions, calls, intents, tasks, attention
    )
    primary_counts = admission_counts(decisions)
    reasons = []
    if selector is not None and static != selector:
        reasons.append("missing_or_changed_frozen_static_selection")
    training_count = (
        sum(
            r["phase"] in {"calibration", "warmup", "training"}
            for r in manifest["schedule"]
        )
        * 3
    )
    reconstructed_gate = None
    failed_ids = {failure["id"] for failure in failures}
    training_failure = any(
        row["id"] in failed_ids and row["phase"] in {"warmup", "training"}
        for row in calls
    )
    if len(decisions) >= training_count and not training_failure:
        reconstructed_gate = study.admission_gate(manifest, decisions, calls)
        if gate != reconstructed_gate or gate.get("proceed_to_heldout") is not True:
            reasons.append("missing_changed_or_failed_training_gate")
    else:
        reasons.append("training_incomplete_or_invalid")
    scheduled = len(manifest["schedule"]) * 3
    complete = len(decisions) == len(intents) == scheduled and {
        i for d in decisions for i in d["incurred_ids"]
    } == {r["id"] for r in calls}
    if not complete:
        reasons.append("scheduled_workflows_incomplete")
    if (
        summary is None
        or summary.get("schedule_complete") is not True
        or summary.get("manifest_sha256") != digest(manifest)
        or summary.get("decisions_sha256") != digest(decisions)
        or summary.get("calls_sha256") != digest(calls)
        or summary.get("training_gate") != gate
    ):
        reasons.append("missing_or_inconsistent_completion_summary")
    finance = None
    if ledger_events is not None:
        if not isinstance(ledger_sha256, str) or len(ledger_sha256) != 64:
            raise PilotError("ledger snapshot requires its byte SHA256")
        finance = ledger_report(
            ledger_events, "rules-live-dev-v1-" + digest(manifest)[:20], calls, expected
        )
        finance.update(
            ledger_sha256=ledger_sha256, ledger_events_sha256=digest(ledger_events)
        )
        failures += finance["failed_provider_results"]
        if not finance["snapshot_financially_complete"]:
            reasons.append("ledger_accounting_incomplete")
    else:
        reasons.append("artifact_only_financial_completeness_unknown")
    if failures:
        reasons.append("provider_failure_contaminates_results")
    reports = costs(calls, finance)
    diagnostics = plan_diagnostics(manifest, decisions, calls, intents)
    paired, comparisons = [], []
    heldout_ids = sorted(
        {r["task_id"] for r in manifest["schedule"] if r["phase"] == "heldout"}
    )
    if not reasons:
        by_id = {r["id"]: r for r in calls}
        grouped = defaultdict(dict)
        for d in decisions:
            if d["phase"] == "heldout":
                grouped[(d["task_id"], d["arm"])][d["workflow_stage"]] = d
        for task in heldout_ids:
            arms = {}
            for arm in live.ARMS:
                stages = grouped[(task, arm)]
                if set(stages) != set(live.STAGES):
                    raise PilotError(
                        "complete heldout is not question-paired across every arm"
                    )
                rows = [by_id[d["primary_id"]] for d in stages.values()]
                arms[arm] = {
                    **score(
                        by_id[stages["answer"]["primary_id"]]["answer"],
                        tasks[task]["expected"],
                    ),
                    "primary_billed_usd": float(
                        sum((money(r["cost_usd"]) for r in rows), Decimal(0))
                    ),
                    "primary_nominal_usd": float(
                        sum(
                            (money(r["nominal_uncached_cost_usd"]) for r in rows),
                            Decimal(0),
                        )
                    ),
                }
            paired.append({"task_id": task, "arms": arms})
        for control in live.ARMS:
            if control == "joint":
                continue
            rows = [
                {
                    k: p["arms"]["joint"][k] - p["arms"][control][k]
                    for k in ("em", "f1", "primary_billed_usd", "primary_nominal_usd")
                }
                for p in paired
            ]
            joint_cost, control_cost = (
                next(r for r in reports if r["arm"] == a)["all_phases"]
                for a in ("joint", control)
            )
            comparisons.append(
                {
                    "joint_minus": control,
                    "questions": len(rows),
                    "effects": paired_bootstrap(rows, repetitions),
                    "f1_decreased_questions": sum(r["f1"] < 0 for r in rows),
                    "em_lost_questions": sum(r["em"] < 0 for r in rows),
                    "setup_inclusive_known_cost_difference_usd": str(
                        money(joint_cost["known_usd"])
                        - money(control_cost["known_usd"])
                    ),
                    "setup_inclusive_cost_difference_bounds_usd": [
                        str(
                            money(joint_cost["known_usd"])
                            - money(control_cost["conservative_commitment_usd"])
                        ),
                        str(
                            money(joint_cost["conservative_commitment_usd"])
                            - money(control_cost["known_usd"])
                        ),
                    ],
                }
            )
    return {
        "kind": "prospective_joint_input_rules_read_only_analysis",
        "paper_evidence": False,
        "efficacy_claim": False,
        "comparison_available": not reasons,
        "suppression_reasons": reasons,
        "manifest_sha256": digest(manifest),
        "decisions_sha256": digest(decisions),
        "calls_sha256": digest(calls),
        "intents_sha256": digest(intents),
        "fixture_sha256": file_hash(fixture),
        "source_files": manifest["source_files"],
        "native_sha256": manifest["native_sha256"],
        "analysis_source_sha256": file_hash(Path(__file__)),
        "scheduled_heldout_questions": len(heldout_ids),
        "analysis_dependency_sha256": {
            path: file_hash(ROOT / path)
            for path in (
                "bench/openrouter_rules_comparison.py",
                "bench/openrouter_rules_diagnostics.py",
                "bench/openrouter_rules_validity.py",
            )
        },
        "scheduled_decisions": scheduled,
        "recorded_decisions": len(decisions),
        "recorded_calls": len(calls),
        "completed_workflows_by_phase_arm": dict(
            Counter(
                d["phase"] + "/" + d["arm"]
                for d in decisions
                if d["workflow_stage"] == "answer"
            )
        ),
        "failed_provider_results": failures,
        "training_gate_reconstructed": reconstructed_gate,
        "static_selection_verified": selector is not None and static == selector,
        "financial_snapshot": finance,
        "costs_by_arm": reports,
        "exact_plan_feedback": diagnostics["plans"],
        "primary_exact_plan_counts": primary_counts,
        "paired_questions": paired,
        "comparisons": comparisons,
        "bootstrap": {
            "unit": "whole matched question",
            "seed": BOOTSTRAP_SEED,
            "replicates": repetitions,
            "confidence": 0.95,
        },
        "effect_units": {
            "em": "fraction; joint minus control",
            "f1": "fraction; joint minus control",
            "primary_billed_usd": "USD per complete heldout workflow; joint minus control",
            "primary_nominal_usd": "USD per complete heldout workflow; joint minus control",
        },
        "limitations": [
            *manifest["limitations"],
            "No efficacy conclusion follows automatically from comparison availability; this is descriptive development evidence.",
            "Intervals resample heldout questions conditional on this trained controller; they exclude training variability and multiplicity correction.",
            "Cost intervals concern successful heldout primary calls only; fixed setup and all failed-attempt costs/holds are reported separately and in setup-inclusive bounds.",
            "Nominal repricing covers successful artifact calls only; failed attempts have known billed charges and full residual bounds, not invented token costs.",
            "Billed cost and latency are noncausal under interleaved providers and implicit cache warming; no latency speedup claim is computed.",
            "Financial completeness is relative to the supplied consistent ledger snapshot; it cannot certify absence of later settlements or external charges.",
            "Exact-plan pair counts describe recorded feedback, not a reconstructed native rolling guard or safety guarantee.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("artifacts", "fixture", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:

        def read(name, default=None):
            path = args.artifacts / name
            return json.loads(path.read_text()) if path.exists() else default

        manifest = read("manifest.json")
        events, ledger_hash = (
            read_ledger_snapshot(args.ledger) if args.ledger else (None, None)
        )
        report = analyze(
            manifest,
            read("decisions.json", []),
            read("calls.json", []),
            read("intents.json", []),
            args.fixture,
            expected_manifest_sha256=args.manifest_sha256,
            gate=read("training-gate.json"),
            static=read("static-selection.json"),
            summary=read("summary.json"),
            ledger_events=events,
            ledger_sha256=ledger_hash,
            source_root=args.source_root,
        )
        write_json(args.output, report, immutable=True)
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in (
                        "comparison_available",
                        "suppression_reasons",
                        "recorded_calls",
                    )
                }
            )
        )
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError):
        # Private ledger bodies may contain sensitive provider error details.
        print(
            "Joint analysis rejected inconsistent inputs; no efficacy report written."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
