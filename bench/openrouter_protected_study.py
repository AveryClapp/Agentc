"""One-shot protected native learning protocol; UNKNOWN stays source-visible.

The generated distribution is developmental, not a held-out task benchmark.
Native probes never become workflow history. A failed phase cannot be resumed,
and an incomplete acquisition supports no workflow efficacy calculation.
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import time

from bench import openrouter_pilot as pilot
from bench import openrouter_protected_fixture as fixture
from bench import openrouter_protected_pair as pair
from bench import openrouter_rules_live as live
from bench import openrouter_structured_study as finance
from bench import openrouter_typed_factorial as fixed
from bench import typed_relational_workload as typed
from bench.openrouter_attempts import accounting
from bench.openrouter_joint_study import restrict_call
from bench.openrouter_matrix import ROOT, file_hash, load_module, write_json

KIND = "protected-native-learning-study-v1"
CLAIM_EVENT = "protected_study_phase"
FAMILY_CAP = Decimal("15.4")
PILOT_CAP = Decimal("8")
PILOT_PROGRESS_STOP = Decimal("0.6")
MAX_SECONDS = 6 * 60 * 60
PHASES = ("pilot", "study")
FIXED_ARMS = {"S0": live.SOURCE_MODEL, "H0": live.TARGET_MODEL, "HR": live.TARGET_MODEL}
PUBLIC_FILES = ("selection.json", "public.json", "provenance.json")
ROLLING_TELEMETRY = {
    "latency_last_30m",
    "throughput_last_30m",
    "perf_last_30m_by_workload",
    "uptime_last_1d",
    "uptime_last_30m",
    "uptime_last_5m",
}
LIMITATIONS = [
    "Fresh generated development tasks; not an independent public benchmark.",
    "UNKNOWN primary always uses the exact Sonnet source; no adaptive cost-win claim.",
    "Twenty exact-plan pairs are an evidence floor, not a two-percent damage certificate.",
    "Native source/probe block precedes fixed HR; timing and cache are confounded.",
    "All fees, failed attempts, probes and retained uncertainty remain charged.",
    "Only full paired workflow blocks permit descriptive source-versus-HR scoring.",
]


def require(condition, message):
    if not condition:
        raise pilot.PilotError(message)


def sources():
    paths = (
        *fixture.SOURCE_FILES,
        "bench/openrouter_protected_study.py",
        "bench/openrouter_protected_forecast.py",
        "bench/openrouter_protected_audit.py",
    )
    return {**pair.source_files(), **{name: file_hash(ROOT / name) for name in paths}}


def stage_id(manifest, phase):
    require(phase in PHASES, "unknown protected phase")
    return "protected-learning-" + phase + "-" + manifest["selection_sha256"]


def schedules(bundle):
    selection = bundle["selection"]
    workflows = {
        "pilot": [
            row
            for task_id in selection["pilot_ids"]
            for row in sorted(
                ({"task_id": task_id, "arm": arm} for arm in ("S0", "H0")),
                key=lambda row: pilot.digest([KIND, row]),
            )
        ],
        "study": [
            {"task_id": task_id, "arm": arm}
            for arm in ("source", "HR")
            for task_id in selection["study_ids"]
        ],
    }
    return {
        phase: [
            {"phase": phase, **row, "workflow_stage": stage}
            for row in rows
            for stage in typed.STAGES
        ]
        for phase, rows in workflows.items()
    }


def stable_endpoint(endpoint):
    return {
        key: value for key, value in endpoint.items() if key not in ROLLING_TELEMETRY
    }


def stage_cap(manifest, phase, state):
    if phase == "pilot":
        return PILOT_CAP
    sid = stage_id(manifest, "pilot")
    committed = state["known_by_stage"].get(sid, Decimal(0)) + state[
        "holds_by_stage"
    ].get(sid, Decimal(0))
    require(
        not any(row["stage"] == sid for row in state["pending"]),
        "pilot has pending allowances",
    )
    return FAMILY_CAP - committed


def financial_state(state):
    return {
        "known_usd": str(state["known_usd"]),
        "retained_uncertainty_usd": str(state["holds_usd"]),
        "pending_usd": str(state["pending_usd"]),
        "remaining_usd": str(
            pilot.HARD_CAP
            - state["known_usd"]
            - state["holds_usd"]
            - state["pending_usd"]
        ),
    }


def clean_finance(state):
    require(
        not state["pending"] and not state["failed_calls"] and not state["unsafe_ids"],
        "unresolved campaign accounting blocks protected work",
    )


def claim_phase(ledger, manifest, phase, gate):
    """Campaign-wide atomic one-shot claim, including a crash before dispatch."""
    sid = stage_id(manifest, phase)
    with ledger.locked() as handle:
        events = pilot.Ledger.read(ledger, handle)
        state = accounting(events)
        clean_finance(state)
        require(
            events
            and pilot.digest(events[0])
            == manifest["campaign_ledger"]["origin_event_sha256"],
            "protected campaign origin changed",
        )
        require(
            str(ledger.path.resolve()) == manifest["campaign_ledger"]["resolved_path"],
            "protected campaign path changed",
        )
        require(
            not any(
                (
                    e.get("event") == CLAIM_EVENT
                    and e.get("selection_sha256") == manifest["selection_sha256"]
                    and e.get("phase") == phase
                )
                or e.get("stage") == sid
                or (
                    e.get("event") == pair.EVENT
                    and e.get("binding", {}).get("run_id") == sid
                )
                for e in events
            ),
            "one-shot selection/phase already claimed; audit only, never resume",
        )
        if phase == "study":
            claims = [
                e
                for e in events
                if e.get("event") == CLAIM_EVENT
                and e.get("stage") == stage_id(manifest, "pilot")
            ]
            require(
                len(claims) == 1
                and claims[0]["manifest_sha256"] == pilot.digest(manifest)
                and gate is not None,
                "study requires the exact audited pilot and resource gate",
            )
        else:
            require(gate is None, "pilot cannot have a post-pilot gate")
        return ledger.append(
            handle,
            {
                "event": CLAIM_EVENT,
                "action": "claim",
                "kind": KIND,
                "phase": phase,
                "stage": sid,
                "selection_sha256": manifest["selection_sha256"],
                "manifest_sha256": pilot.digest(manifest),
                "campaign_ledger": manifest["campaign_ledger"],
                "stage_cap_usd": str(stage_cap(manifest, phase, state)),
                "gate": gate,
                "gate_sha256": pilot.digest(gate) if gate is not None else None,
            },
        )


class PinnedLedger(pilot.Ledger):
    """Current endpoint identity and generation uniqueness before native feedback."""

    def __init__(self, path, key, manifest, phase):
        super().__init__(path, key)
        self.manifest, self.phase = manifest, phase

    def append(self, handle, event):
        if event["event"] == "reserve":
            model = event["request"]["model"]
            require(
                event["stage"] == stage_id(self.manifest, self.phase),
                "reservation changed protected phase",
            )
            require(
                pilot.canonical(
                    stable_endpoint(event.get("schema_endpoint_observation", {}))
                )
                == pilot.canonical(
                    stable_endpoint(self.manifest["enrollment"]["endpoints"][model])
                ),
                "protected endpoint identity, price, or capability changed",
            )
            require(
                event["request"]["max_tokens"] == 256,
                "protected request changed frozen output cap",
            )
        elif event["event"] == "result":
            generation = event.get("generation_id")
            events = pilot.Ledger.read(self, handle)
            require(
                isinstance(generation, str)
                and generation
                and not any(
                    row.get("event") == "result"
                    and row.get("generation_id") == generation
                    for row in events
                ),
                "protected generation is missing or was already observed",
            )
        return super().append(handle, event)


def fixed_intent(manifest, item, task, history, attention, materializer):
    require(item["arm"] in FIXED_ARMS, "unknown fixed protected arm")
    model = FIXED_ARMS[item["arm"]]
    original = restrict_call(
        typed.call(
            task,
            item["workflow_stage"],
            history,
            attention,
            model=model,
            run_identity=stage_id(manifest, item["phase"]) + "/" + item["arm"],
        ),
        item["workflow_stage"],
    )
    original["parameters"]["max_output_tokens"] = 256
    selected, report = fixed.materialize(
        original,
        fixed.RULES.get(item["workflow_stage"]) if item["arm"] == "HR" else None,
        materializer,
    )
    payload = live.payload_for(selected, manifest["enrollment"])
    core = {
        **item,
        "original_call": original,
        "selected_call": selected,
        "materialization": report,
        "payload": payload,
    }
    endpoint = manifest["enrollment"]["endpoints"][model]
    return {
        **core,
        "id": stage_id(manifest, item["phase"]) + "/fixed/" + pilot.digest(item),
        "metadata": {
            **item,
            "manifest_sha256": pilot.digest(manifest),
            "decision_sha256": pilot.digest(core),
            "dispatch_contract": {
                "provider_name": endpoint["provider_name"],
                "endpoint_model": endpoint["name"].split(" | ", 1)[1],
            },
        },
    }


def public_row(item, result):
    return {
        **item,
        **{
            key: value
            for key, value in result.items()
            if key not in {"key_id", "at", "metadata"}
        },
    }


def extract_records(manifest, phase, intents, events):
    """Export this protocol's exact receipt subset, never the private campaign."""
    sid = stage_id(manifest, phase)
    protected = [
        e
        for e in events
        if e["event"] == pair.EVENT and e.get("binding", {}).get("run_id") == sid
    ]
    starts = {e["pair_id"]: e for e in protected if e["action"] == "start"}
    items = {
        intent["id"]: {
            k: intent[k] for k in ("phase", "arm", "task_id", "workflow_stage")
        }
        for intent in intents
    }
    for pair_id, start in starts.items():
        for slot, arm in (("source", "source"), ("candidate", "probe")):
            items[pair_id + "/" + slot] = {
                "phase": phase,
                "arm": arm,
                "task_id": start["task_id"],
                "workflow_stage": start["workflow_stage"],
                "pair_id": pair_id,
            }
    calls = []
    for event in events:
        if event["event"] == "result" and event.get("stage") == sid:
            require(
                event["id"] in items,
                "durable result lacks its protected intent or native start",
            )
            calls.append(public_row(items[event["id"]], event))
    return calls, protected


def resource_gate(manifest, state, forecast, pilot_evidence):
    """A forecast is empirical; actual full-context reservations remain decisive."""
    clean_finance(state)
    available = min(
        pilot.money(financial_state(state)["remaining_usd"]),
        stage_cap(manifest, "study", state),
    )
    training = pilot.money(forecast["training_usd"])
    baseline = pilot.money(forecast["HR_usd"])
    reserve = pilot.money(forecast["largest_pair_allowance_usd"])
    require(
        training + reserve <= available and training + baseline <= available,
        "prospective learning and largest pair reserve do not fit remaining budget",
    )
    return {
        "kind": "protected-post-pilot-resource-gate-v1",
        "forecast": forecast,
        "pilot_evidence": pilot_evidence,
        "financial_snapshot": financial_state(state),
        "study_cap_usd": str(stage_cap(manifest, "study", state)),
        "available_usd": str(available),
        "accuracy_used": False,
        "passed": True,
    }


def save_phase(output, manifest, phase, intents, events, *, complete, error=None):
    calls, protected = extract_records(manifest, phase, intents, events)
    directory = Path(output) / phase
    for name, value in (
        ("intents.json", intents),
        ("calls.json", calls),
        ("protected-events.json", protected),
    ):
        write_json(directory / name, value)
    sid, state = stage_id(manifest, phase), accounting(events)
    summary = {
        "kind": KIND,
        "phase": phase,
        "manifest_sha256": pilot.digest(manifest),
        "schedule_complete": complete,
        "completed_calls": len(calls),
        "artifact_files": {
            name: file_hash(directory / name)
            for name in ("intents.json", "calls.json", "protected-events.json")
        },
        "known_cost_usd": str(state["known_by_stage"].get(sid, Decimal(0))),
        "retained_uncertainty_usd": str(state["holds_by_stage"].get(sid, Decimal(0))),
        "pending_upper_usd": str(
            sum(
                (
                    pilot.money(row["upper_cost_usd"])
                    for row in state["pending"]
                    if row["stage"] == sid
                ),
                Decimal(0),
            )
        ),
        "financial_snapshot": financial_state(state),
        "error_type": error,
        "efficacy_claim": False,
        "paper_evidence": False,
        "labels_opened": False,
    }
    write_json(directory / "summary.json", summary)
    return summary


def run_phase(
    output, manifest, bundle, phase, ledger, key, bridge, materializer, *, gate=None
):
    """Caller verifies frozen source/manifest and pilot audit before this entry."""
    fixture.validate_bundle(bundle)
    require(manifest["schedule"] == schedules(bundle), "protected schedule changed")
    require(
        file_hash(materializer) == manifest["materializer_sha256"],
        "fixed materializer changed",
    )
    require(
        str(bridge.store) == manifest["native_store"], "protected native store changed"
    )
    require(not bridge.store.exists(), "prospective native store must start empty")
    directory = Path(output) / phase
    require(not directory.exists(), "phase artifact already exists; audit only")
    claim = claim_phase(ledger, manifest, phase, gate)
    intents, complete, error = [], False, None
    started = time.monotonic()
    tasks = {task["task_id"]: task for task in bundle["tasks"]}
    attention = load_module(
        "protected_study_attention", ROOT / "python/agentc/_attention.py"
    )
    coordinator = (
        pair.Coordinator(
            ledger,
            key,
            bridge,
            manifest["enrollment"],
            run_id=stage_id(manifest, phase),
            budget_stage=stage_id(manifest, phase),
            stage_cap=claim["stage_cap_usd"],
        )
        if phase == "study"
        else None
    )
    history, workflow = {}, None
    try:
        for item in manifest["schedule"][phase]:
            require(
                0 <= time.monotonic() - started < MAX_SECONDS,
                "one-shot phase exceeded six-hour deadline",
            )
            identity = (item["arm"], item["task_id"])
            if workflow != identity:
                history, workflow = {}, identity
            task, stage = tasks[item["task_id"]], item["workflow_stage"]
            if item["arm"] == "source":
                coordinator.execute(task, stage)
            else:
                if phase == "pilot":
                    _, _, state = finance.typed_ledger_snapshot(ledger.path, key)
                    require(
                        state["known_by_stage"].get(
                            stage_id(manifest, phase), Decimal(0)
                        )
                        < PILOT_PROGRESS_STOP,
                        "pilot exceeded prospective known-charge stop",
                    )
                intent = fixed_intent(
                    manifest, item, task, history, attention, materializer
                )
                intents.append(intent)
                write_json(directory / "intents.json", intents)
                result = ledger.call(
                    key,
                    intent["id"],
                    stage_id(manifest, phase),
                    pilot.money(claim["stage_cap_usd"]),
                    intent["payload"],
                    intent["metadata"],
                )
                history[stage] = typed.application_text(
                    result["answer"], result["finish_reason"], stage=stage
                )
                if phase == "pilot":
                    require(
                        result["finish_reason"] not in {"length", "content_filter"}
                        and typed.parse(stage, history[stage]) is not None,
                        "prospective pilot has an invalid application completion",
                    )
            _, events, _ = finance.typed_ledger_snapshot(ledger.path, key)
            summary = save_phase(
                output, manifest, phase, intents, events, complete=False
            )
            print(
                json.dumps(
                    {
                        "phase": phase,
                        "completed_calls": summary["completed_calls"],
                        "known_cost_usd": summary["known_cost_usd"],
                    }
                ),
                flush=True,
            )
        complete = True
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        _, events, _ = finance.typed_ledger_snapshot(ledger.path, key)
        summary = save_phase(
            output, manifest, phase, intents, events, complete=complete, error=error
        )
    return summary
