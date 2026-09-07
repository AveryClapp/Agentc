"""Read-only receipt and native-evidence audit for the protected study.

Inputs are frozen public artifacts and an already-read original journal prefix.
This module never loads a key, opens a ledger, scores a label, dispatches a
provider request, or writes an artifact. An incomplete prefix is not efficacy
evidence, and an unverifiable native acknowledgement is never recovered.
"""

from collections import Counter
from decimal import Decimal
import json
import math

from bench import openrouter_groups as groups
from bench import openrouter_pilot as pilot
from bench.openrouter_attempts import accounting


def _require(condition, message):
    if not condition:
        raise pilot.PilotError(message)


def _same(left, right, message):
    _require(pilot.canonical(left) == pilot.canonical(right), message)


def _financial_report(state, stage):
    known = state["known_by_stage"].get(stage, Decimal(0))
    holds = state["holds_by_stage"].get(stage, Decimal(0))
    pending = [row for row in state["pending"] if row["stage"] == stage]
    pending_cost = sum(
        (pilot.money(row["upper_cost_usd"]) for row in pending), Decimal(0)
    )
    return {
        "known_stage_usd": str(known),
        "retained_uncertainty_usd": str(holds),
        "pending_upper_bound_usd": str(pending_cost),
        "stage_commitment_usd": str(known + holds + pending_cost),
        "campaign_known_usd": str(state["known_usd"]),
        "campaign_retained_uncertainty_usd": str(state["holds_usd"]),
        "campaign_pending_upper_bound_usd": str(state["pending_usd"]),
        "campaign_commitment_usd": str(
            state["known_usd"] + state["holds_usd"] + state["pending_usd"]
        ),
        "pending_ids": sorted(row["id"] for row in pending),
        "failed_attempt_ids": sorted(
            row["id"] for row in state["terminals"].values() if row["stage"] == stage
        ),
        "unsafe_failed_attempt_ids": sorted(
            row["id"]
            for row in state["terminals"].values()
            if row["stage"] == stage and row["id"] in state["unsafe_ids"]
        ),
    }


def _receipt_inventory(
    events, stage, specs, endpoints, stable_endpoint, ledger_binding
):
    """Check exact requests and raw receipts; accounting owns all liabilities."""
    state = accounting(events)
    _require(
        all(
            row.get("stage") == stage
            or (row["event"] == "response" and "stage" not in row)
            for row in events
            if row.get("id") in specs
        ),
        "protected call identity appears in another phase",
    )
    reserves = [row for row in state["reserves"].values() if row["stage"] == stage]
    ids = [row["id"] for row in reserves]
    _require(len(ids) == len(set(ids)), "protected phase retried a logical call")
    claims = {row["id"]: row for row in reserves}
    responses = {}
    positions = {id(row): index for index, row in enumerate(events)}
    for row in reserves:
        _require(row["id"] in specs, "protected reservation lacks an expected request")
        spec = specs[row["id"]]
        _same(row["request"], spec["payload"], "protected request changed")
        _same(row["metadata"], spec["metadata"], "protected request metadata changed")
        _require(
            row["fingerprint"]
            == pilot.digest(
                {
                    "payload": spec["payload"],
                    "metadata": spec["metadata"],
                    "stage": stage,
                }
            )
            and pilot.money(row["upper_cost_usd"]) == pilot.upper_cost(spec["payload"])
            and isinstance(row.get("attempt_id"), str)
            and bool(row["attempt_id"]),
            "protected reservation fingerprint, allowance or attempt changed",
        )
        model = spec["payload"]["model"]
        observation = row.get("schema_endpoint_observation", {})
        pilot.validate_schema_endpoint(model, observation)
        _same(
            stable_endpoint(observation),
            stable_endpoint(endpoints[model]),
            "protected endpoint identity, price or capability changed",
        )
    for row in events:
        if row["event"] == "budget_group" and row.get("stage") == stage:
            _require(
                row["ledger_binding"] == ledger_binding,
                "protected group changed original ledger",
            )
            for slot in row["slots"].values():
                _require(slot["id"] in specs, "protected group has an unknown slot")
                spec = specs[slot["id"]]
                _same(
                    slot["request"], spec["payload"], "protected group request changed"
                )
                _same(
                    slot["metadata"],
                    spec["metadata"],
                    "protected group metadata changed",
                )
                _require(
                    pilot.money(slot["upper_cost_usd"])
                    == pilot.upper_cost(spec["payload"]),
                    "protected group allowance changed",
                )
        elif row["event"] == "budget_group_release" and row.get("stage") == stage:
            raise pilot.PilotError(
                "protected protocol cannot release a group allowance"
            )
        elif row["event"] == "response" and (
            row.get("stage") == stage
            or row.get("id") in claims
            or str(row.get("id", "")).startswith(stage + "/")
        ):
            claim = claims.get(row.get("id"))
            _require(
                claim is not None
                and row.get("stage", stage) == stage
                and row.get("attempt_id") == claim["attempt_id"]
                and row["id"] not in responses
                and positions[id(claim)] < positions[id(row)],
                "protected raw response lacks one preceding exact attempt",
            )
            responses[row["id"]] = row
    generations = Counter(
        row.get("generation_id") for row in events if row["event"] == "result"
    )
    results = []
    for row in events:
        if row["event"] != "result" or row.get("stage") != stage:
            continue
        claim, raw = claims.get(row["id"]), responses.get(row["id"])
        _require(
            claim is not None
            and raw is not None
            and positions[id(raw)] < positions[id(row)],
            "protected result lacks its preceding exact raw receipt",
        )
        groups.validate_completion(claim, raw["response"], row)
        _require(
            type(row.get("latency_ms")) in (int, float)
            and math.isfinite(row["latency_ms"])
            and row["latency_ms"] >= 0,
            "protected result latency is invalid",
        )
        _same(
            raw.get("latency_ms"),
            row["latency_ms"],
            "protected receipt latency changed",
        )
        generation = row.get("generation_id")
        _require(
            isinstance(generation, str)
            and bool(generation)
            and generations[generation] == 1,
            "protected generation is missing or reused in the campaign",
        )
        results.append(row)
    return state, results


def _resources(rows):
    return {
        "successful_calls": len(rows),
        "known_successful_cost_usd": str(
            sum((pilot.money(row["cost_usd"]) for row in rows), Decimal(0))
        ),
        "prompt_tokens": sum(row["usage"]["prompt_tokens"] for row in rows),
        "completion_tokens": sum(row["usage"]["completion_tokens"] for row in rows),
        "observational_latency_ms": sum(row["latency_ms"] for row in rows),
    }


def _dispatch_budgets(events, stage, stage_cap, *, pilot_known_stop=None):
    """Use the one accounting implementation at every liability boundary."""
    for index, row in enumerate(events):
        if row.get("stage") != stage or row["event"] not in {"reserve", "budget_group"}:
            continue
        if pilot_known_stop is not None and row["event"] == "reserve":
            before = accounting(events[:index])
            _require(
                before["known_by_stage"].get(stage, Decimal(0)) < pilot_known_stop,
                "pilot dispatch exceeded the known-charge progress stop",
            )
        state = accounting(events[: index + 1])
        commitment = state["known_usd"] + state["holds_usd"] + state["pending_usd"]
        stage_commitment = (
            state["known_by_stage"].get(stage, Decimal(0))
            + state["holds_by_stage"].get(stage, Decimal(0))
            + sum(
                (
                    pilot.money(pending["upper_cost_usd"])
                    for pending in state["pending"]
                    if pending["stage"] == stage
                ),
                Decimal(0),
            )
        )
        _require(
            commitment <= pilot.HARD_CAP and stage_commitment <= stage_cap,
            "historical protected dispatch exceeded its full exposure ceiling",
        )


def _learning_report(pairs):
    ambiguous = sorted(
        key
        for key, rows in pairs.items()
        if rows[-1]["action"]
        in {"start", "observe_claim", "feedback_claim", "failure_claim"}
    )
    profiles = {}
    for rows in () if ambiguous else pairs.values():
        if rows[-1]["action"] != "complete":
            continue
        actions = {row["action"]: row for row in rows}
        if "feedback" not in actions:
            continue
        result = actions["feedback"]["reply"]["result"]
        token, threshold = result["observation"], result["divergence_threshold"]
        identity = {
            "key": token["key"],
            "runtime_version": token["runtime_version"],
            "divergence_threshold": threshold,
        }
        key = pilot.digest(identity)
        profile = profiles.setdefault(
            key,
            {
                **identity,
                "workflow_stage": rows[0]["workflow_stage"],
                "paired_observations": 0,
                "disagreements": 0,
                "sequences": [],
            },
        )
        _require(
            token["sequence"] not in profile["sequences"],
            "native feedback token reused",
        )
        profile["sequences"].append(token["sequence"])
        profile["paired_observations"] += 1
        profile["disagreements"] += actions["feedback_claim"]["divergence"] != 0
    return {
        "exact_plan_profiles": [profiles[key] for key in sorted(profiles)],
        "ambiguous_native_claim_ids": ambiguous,
        "learning_evidence_reported": not ambiguous,
        "count_scope": "verified completed coordinator stages, not a native database census",
        "incomplete_pair_ids": sorted(
            key for key, rows in pairs.items() if rows[-1]["action"] != "complete"
        ),
        "current_guard_state_assessed": False,
        "admission_assessed": False,
        "unknown_primary_remains_source": True,
        "paired_count_is_not_admission_or_damage_certificate": True,
    }


def _claim(manifest, phase, events, study, pair):
    stage = study.stage_id(manifest, phase)
    claims = [
        (index, row)
        for index, row in enumerate(events)
        if row["event"] == study.CLAIM_EVENT
        and row.get("selection_sha256") == manifest["selection_sha256"]
        and row.get("phase") == phase
    ]
    scoped = [
        (index, row)
        for index, row in enumerate(events)
        if row.get("stage") == stage
        or (
            row["event"] == pair.EVENT and row.get("binding", {}).get("run_id") == stage
        )
    ]
    _require(len(claims) <= 1, "protected phase was claimed more than once")
    if not claims:
        _require(not scoped, "protected phase evidence precedes its claim")
        return None, None
    index, claim = claims[0]
    before = accounting(events[:index])
    study.clean_finance(before)
    expected = {
        "event": study.CLAIM_EVENT,
        "action": "claim",
        "kind": study.KIND,
        "phase": phase,
        "stage": stage,
        "selection_sha256": manifest["selection_sha256"],
        "manifest_sha256": pilot.digest(manifest),
        "campaign_ledger": manifest["campaign_ledger"],
        "stage_cap_usd": str(study.stage_cap(manifest, phase, before)),
        "gate": claim.get("gate"),
        "gate_sha256": pilot.digest(claim["gate"])
        if claim.get("gate") is not None
        else None,
        "key_id": events[0]["key_id"],
        "at": claim.get("at"),
    }
    _same(claim, expected, "protected phase claim changed")
    _require(
        isinstance(claim["at"], str) and bool(claim["at"]), "phase claim lacks time"
    )
    _require(all(i >= index for i, _ in scoped), "phase work preceded its claim")
    _require(
        all(row.get("key_id") == events[0]["key_id"] for _, row in scoped),
        "protected phase changed original credential identity",
    )
    if phase == "pilot":
        _require(claim["gate"] is None, "pilot cannot use a post-pilot gate")
        study.prepilot_gate(manifest, before)
    else:
        _require(isinstance(claim["gate"], dict), "study lacks its resource gate")
    return index, claim


def _rebuild_pilot(manifest, bundle, events, materializer, study, typed, attention):
    """Study eligibility is rebuilt from receipts, never a saved gate boolean."""
    state = accounting(events)
    tasks = {task["task_id"]: task for task in bundle["tasks"]}
    intents, calls, history, workflow = [], [], {}, None
    for item in manifest["schedule"]["pilot"]:
        identity = (item["arm"], item["task_id"])
        if identity != workflow:
            history, workflow = {}, identity
        intent = study.fixed_intent(
            manifest, item, tasks[item["task_id"]], history, attention, materializer
        )
        result = state["results"].get(intent["id"])
        _require(result is not None, "study started without the complete pilot")
        intents.append(intent)
        calls.append(study.public_row(item, result))
        history[item["workflow_stage"]] = typed.application_text(
            result["answer"], result["finish_reason"], stage=item["workflow_stage"]
        )
    return intents, calls


def audit_phase(
    manifest,
    bundle,
    phase,
    intents,
    calls,
    protected_events,
    events,
    bridge,
    materializer,
    *,
    completed_batch=False,
):
    """Validate a complete or interrupted phase without scoring or mutating it.

    ``events`` must be an immutable, already-read original journal prefix; the
    caller owns byte-level journal loading. Native dependencies may only execute
    read-only checkpoint/verify operations. Integrity mismatches raise PilotError;
    an intact unfinished prefix returns ``schedule_complete=False``.

    ``completed_batch=True`` explicitly selects successor-source batch replay
    and requires a complete phase. It privately snapshots JSON inputs, retains
    every schedule/receipt/historical-budget check, and rejects mutation or
    native rollback before returning the same report as individual replay.
    No caller-provided cache or old-source compatibility mode is accepted.
    """
    from bench import openrouter_protected_pair as pair
    from bench import openrouter_protected_study as study
    from bench import typed_relational_workload as typed
    from bench.openrouter_matrix import ROOT, load_module

    _require(phase in study.PHASES, "unknown protected audit phase")
    _require(type(completed_batch) is bool, "completed_batch must be a boolean")
    _require(
        all(
            isinstance(value, list)
            for value in (intents, calls, protected_events, events)
        ),
        "protected audit inputs must be ordered arrays",
    )
    if completed_batch:
        caller_inputs = [manifest, bundle, intents, calls, protected_events, events]
        encoded = pilot.canonical(caller_inputs)
        input_sha256 = pilot.digest(caller_inputs)
        manifest, bundle, intents, calls, protected_events, events = json.loads(encoded)
    study.validate_manifest(
        manifest, bundle, events, bridge, materializer, pilot.digest(manifest)
    )
    frozen = manifest["enrollment"]
    ledger_binding = pilot.digest(manifest["campaign_ledger"]["resolved_path"])
    stage = study.stage_id(manifest, phase)
    claim_index, claim = _claim(manifest, phase, events, study, pair)
    expected_protected = [
        row
        for row in events
        if row["event"] == pair.EVENT and row.get("binding", {}).get("run_id") == stage
    ]
    _same(
        protected_events,
        expected_protected,
        "protected event artifact is not the exact journal subset",
    )
    _require(
        phase == "study" or not protected_events,
        "pilot cannot have native learning events",
    )
    attention = load_module(
        "protected_audit_attention", ROOT / "python/agentc/_attention.py"
    )

    if phase == "study" and claim is not None:
        prior_events = events[:claim_index]
        prior_intents, prior_calls = _rebuild_pilot(
            manifest, bundle, prior_events, materializer, study, typed, attention
        )
        prior = audit_phase(
            manifest,
            bundle,
            "pilot",
            prior_intents,
            prior_calls,
            [],
            prior_events,
            bridge,
            materializer,
        )
        _require(prior["pilot_format_gate"], "study pilot format gate failed")
        from bench import openrouter_protected_forecast as forecast

        estimate = forecast.forecast(
            bundle, prior_calls, prior_intents, frozen, materializer
        )
        evidence = {
            "manifest_sha256": pilot.digest(manifest),
            "selection_sha256": manifest["selection_sha256"],
            "calls_sha256": pilot.digest(prior_calls),
            "intents_sha256": pilot.digest(prior_intents),
            "known_cost_usd": prior["finance"]["known_stage_usd"],
        }
        _same(
            claim["gate"],
            study.resource_gate(manifest, accounting(prior_events), estimate, evidence),
            "study gate differs from exact pilot receipts and pre-claim finances",
        )

    native_pairs = {}
    completed_texts = None
    binding = None
    if phase == "study" and claim is not None:
        binding = {
            "kind": pair.KIND,
            "ledger": ledger_binding,
            "key_id": events[0]["key_id"],
            "store": manifest["native_store"],
            "enrollment_sha256": pilot.digest(frozen),
            "run_id": stage,
            "budget_stage": stage,
            "stage_cap_usd": claim["stage_cap_usd"],
            "experiment_policy": frozen["experiment_policy"],
        }
        validated = pair.audit_native_state(bridge, frozen, binding, events)
        ids = {row["pair_id"] for row in protected_events}
        native_pairs = {key: rows for key, rows in validated.items() if key in ids}
        _require(set(native_pairs) == ids, "native event inventory changed")
        if completed_batch:
            completed_texts = pair.audit_completed_many(bridge, frozen, binding, events)
            _require(
                set(completed_texts) == set(native_pairs),
                "completed batch changed the native pair inventory",
            )

    state = accounting(events)
    tasks = {task["task_id"]: task for task in bundle["tasks"]}
    specs, items, used_pairs = {}, {}, set()
    positions = {id(row): index for index, row in enumerate(events)}
    previous_end = claim_index if claim_index is not None else -1
    history, workflow, fixed_index, completed_steps = {}, None, 0, 0
    valid, invalid = Counter(), Counter()
    for item in manifest["schedule"][phase]:
        identity = (item["arm"], item["task_id"])
        if identity != workflow:
            history, workflow = {}, identity
        task, step = tasks[item["task_id"]], item["workflow_stage"]
        if item["arm"] == "source":
            pair_id = pilot.digest([pair.KIND, stage, task["task_id"], step])
            rows = native_pairs.get(pair_id)
            if rows is None:
                break
            used_pairs.add(pair_id)
            start = rows[0]
            _require(
                positions[id(start)] > previous_end,
                "native start violates the source-first frozen schedule",
            )
            _require(
                start["task_sha256"] == pilot.digest(task)
                and start["workflow_stage"] == step
                and start["task_id"] == task["task_id"],
                "native start changed scheduled public task",
            )
            _same(
                start["source_call"],
                pair.source_call(task, step, history, frozen, stage),
                "native source changed its exact own history",
            )
            for slot, arm in (("source", "source"), ("candidate", "probe")):
                call_id = pair_id + "/" + slot
                items[call_id] = {**item, "arm": arm, "pair_id": pair_id}
            if rows[-1]["action"] != "complete":
                break
            text = (
                completed_texts[pair_id]
                if completed_texts is not None
                else pair.audit_completed(bridge, frozen, binding, rows, events)
            )
            previous_end = positions[id(rows[-1])]
        else:
            if fixed_index == len(intents):
                break
            intent = study.fixed_intent(
                manifest, item, task, history, attention, materializer
            )
            _same(
                intents[fixed_index],
                intent,
                "fixed intent changed exact own history or materialization",
            )
            fixed_index += 1
            specs[intent["id"]] = {
                "payload": intent["payload"],
                "metadata": intent["metadata"],
            }
            items[intent["id"]] = item
            for reserve in state["reserves"].values():
                if reserve["id"] == intent["id"]:
                    _require(
                        positions[id(reserve)] > previous_end,
                        "fixed dispatch violates the frozen schedule",
                    )
            result = state["results"].get(intent["id"])
            if result is None:
                break
            text = typed.application_text(
                result["answer"], result["finish_reason"], stage=step
            )
            previous_end = positions[id(result)]
        history[step] = text
        (valid if typed.parse(step, text) is not None else invalid)[
            item["arm"] + "/" + step
        ] += 1
        completed_steps += 1
        if phase == "pilot" and typed.parse(step, text) is None:
            break
    _require(
        fixed_index == len(intents), "fixed intents continue beyond the executed prefix"
    )
    _require(
        used_pairs == set(native_pairs),
        "native starts continue beyond the executed prefix",
    )
    # Native specifications were independently reconstructed by the shared
    # native audit above; use its checked receipt inventory, not a second copy
    # of coordinator request-construction logic.
    for row in state["reserves"].values():
        if row["id"] in items and "pair_id" in items[row["id"]]:
            specs[row["id"]] = {"payload": row["request"], "metadata": row["metadata"]}
    for row in events:
        if row["event"] == "budget_group" and row.get("stage") == stage:
            for slot in row["slots"].values():
                _require(slot["id"] in items, "group slot has no scheduled native pair")
                specs[slot["id"]] = {
                    "payload": slot["request"],
                    "metadata": slot["metadata"],
                }
    state, results = _receipt_inventory(
        events, stage, specs, frozen["endpoints"], study.stable_endpoint, ledger_binding
    )
    _same(
        calls,
        [study.public_row(items[row["id"]], row) for row in results],
        "call artifact is not the exact ordered result inventory",
    )
    _require(
        claim is not None or not (calls or intents or protected_events),
        "unclaimed phase contains artifacts",
    )
    finance = _financial_report(state, stage)
    if claim is not None:
        _dispatch_budgets(
            events,
            stage,
            pilot.money(claim["stage_cap_usd"]),
            pilot_known_stop=study.PILOT_PROGRESS_STOP if phase == "pilot" else None,
        )
        _require(
            pilot.money(finance["stage_commitment_usd"])
            <= pilot.money(claim["stage_cap_usd"]),
            "protected phase exceeds its frozen financial ceiling",
        )
        _require(
            all(
                pilot.money(row["stage_cap_usd"]) == pilot.money(claim["stage_cap_usd"])
                for row in events
                if row["event"] == "budget_group" and row.get("stage") == stage
            ),
            "protected group changed its stage ceiling",
        )
    _require(
        pilot.money(finance["campaign_commitment_usd"]) <= pilot.HARD_CAP,
        "original campaign exceeds its full retained ceiling",
    )
    complete = (
        claim is not None
        and completed_steps == len(manifest["schedule"][phase])
        and not finance["pending_ids"]
        and not finance["failed_attempt_ids"]
        and (phase != "pilot" or not invalid)
    )
    learning = _learning_report(native_pairs)
    if completed_batch:
        _require(complete, "completed batch requires the complete phase schedule")
        if binding is not None:
            # Recheck the live reciprocal checkpoint after the entire phase,
            # including fixed HR materialization and every liability boundary.
            pair.audit_native_state(bridge, frozen, binding, events)
        study.validate_manifest(
            manifest, bundle, events, bridge, materializer, pilot.digest(manifest)
        )
        _require(
            pilot.digest(caller_inputs) == input_sha256
            and pilot.digest(
                [manifest, bundle, intents, calls, protected_events, events]
            )
            == input_sha256,
            "protected phase input changed during completed batch replay",
        )
    return {
        "kind": "protected-study-readonly-audit-v1",
        "phase": phase,
        "manifest_sha256": pilot.digest(manifest),
        "ledger_events_sha256": pilot.digest(events),
        "ledger_event_count": len(events),
        "started": claim is not None,
        "integrity_verified": not learning["ambiguous_native_claim_ids"],
        "known_receipt_and_checkpoint_integrity_verified": True,
        "schedule_complete": complete,
        "all_scheduled_steps_returned": completed_steps
        == len(manifest["schedule"][phase]),
        "completed_scheduled_steps": completed_steps,
        "successful_calls": len(results),
        "calls_sha256": pilot.digest(calls),
        "intents_sha256": pilot.digest(intents),
        "protected_events_sha256": pilot.digest(protected_events),
        "pilot_format_gate": complete and not invalid if phase == "pilot" else None,
        "valid_application_steps": dict(sorted(valid.items())),
        "invalid_application_steps": dict(sorted(invalid.items())),
        "finance": finance,
        "resources_by_arm": {
            arm: _resources([row for row in calls if row["arm"] == arm])
            for arm in sorted({row["arm"] for row in calls})
        },
        "learning": learning,
        "labels_opened": False,
        "efficacy_claim": False,
        "paper_evidence": False,
    }
