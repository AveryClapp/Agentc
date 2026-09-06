"""One-shot protected native learning protocol; UNKNOWN stays source-visible.

The generated distribution is developmental, not a held-out task benchmark.
Native probes never become workflow history. A failed phase cannot be resumed,
and an incomplete acquisition supports no workflow efficacy calculation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
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
    "Six pilot and two study inputs previously appeared in offline fake-provider unit tests; no unseen-input claim.",
    "UNKNOWN primary always uses the exact Sonnet source; no adaptive cost-win claim.",
    "Twenty exact-plan pairs are an evidence floor, not a two-percent damage certificate.",
    "Native source/probe block precedes fixed HR; timing and cache are confounded.",
    "All fees, failed attempts, probes and retained uncertainty remain charged.",
    "Only full paired workflow blocks permit descriptive source-versus-HR scoring.",
]


def exposure_disclosure(selection):
    """Additive correction; never rewrite the already frozen selection/artifacts."""
    affected = fixture.ids_for(fixture.SEED, 8)
    require(
        affected == selection["pilot_ids"] + selection["study_ids"][:2],
        "offline exposure inventory differs from preserved prospective IDs",
    )
    return {
        "kind": "protected-generated-offline-exposure-disclosure-v1",
        "bug": "bd-9hjr",
        "selection_sha256": selection["selection_sha256"],
        "reviewed_checkpoint": "8956a352afe71eca315876365eb80a2eec91b15e",
        "materialization_commit": "c506794bc30a9a55b2ae3479889d861b1eeda0e5",
        "historical_test_source": "tests/test_openrouter_protected_pair.py",
        "first_introducing_commit": "11f0f55b00051083e981a93f51203f211998a6e1",
        "first_introducing_commit_time": "2026-09-05T20:52:43-04:00",
        "reviewed_historical_commit": "e20f4f52baefe90f6ffbd52238f1dbdbcfef4c89",
        "reviewed_historical_test_sha256": "bb19243dc7ea52b8810c09d610eb8036225e4f07eb509281e42e1a0b2d25ab2c",
        "offline_generated_ids": affected,
        "affected_pilot_ids": selection["pilot_ids"],
        "affected_study_ids": selection["study_ids"][:2],
        "exposure": "Before the ID-only checkpoint, inherited unit tests generated these eight public contexts and their internal expected scalars. Fake response constants, not expected scalars, drove assertions and native feedback.",
        "scope": "Offline tests only: real transport forbidden, constant fake responses, temporary ledger/native stores. No provider-response, gold-feedback, accuracy-selection, or production-profile exposure was found in independent source review.",
        "corrected_claim": "One preserved development draw with prospectively acquired real-model responses; not unseen-input or held-out evidence. The earlier checkpoint is not proof that no overlapping task bodies had ever been generated.",
        "prospective_ids_and_raw_artifacts_preserved": True,
        "resampling": False,
        "unseen_input_claim": False,
        "future_test_only_seed": -2026090601,
    }


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
    return pilot.money(manifest["family_cap_usd"]) - committed


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


def prepilot_gate(manifest, state):
    clean_finance(state)
    report = manifest["prepilot_forecast"]
    available = min(
        pilot.money(financial_state(state)["remaining_usd"]),
        pilot.money(manifest["family_cap_usd"]),
    )
    pilot_cost = pilot.money(report["prospective_pilot_usd"])
    training = pilot.money(report["training_usd"])
    require(
        pilot_cost + training + pilot.money(report["largest_pair_allowance_usd"])
        <= available,
        "prepilot forecast plus full pair reserve exceeds authorized remaining funds",
    )
    require(
        pilot_cost + training + pilot.money(report["HR_usd"]) <= available,
        "prepilot full protocol forecast exceeds authorized remaining funds",
    )


def validate_controls(report, frozen):
    """Integrity preflight; independent review still verifies the actual run."""
    require(
        report["kind"] == "protected-native-reachability-controls-v1"
        and report["native_sha256"] == frozen["native_sha256"],
        "native control binary differs from enrollment",
    )
    for field in (
        "reference_primary_only",
        "no_seeded_profiles",
        "fake_provider",
        "typed_zero_loss",
        "other_numeric_defaults_unchanged",
        "complete",
    ):
        require(
            report[field] is True, "native controls lack a required verified premise"
        )
    require(
        type(report["provider_calls"]) is int and report["provider_calls"] == 0,
        "offline controls cannot contain provider calls",
    )
    require(
        report["source_files"] == pair.source_files()
        and report["source_files_sha256"] == pilot.digest(report["source_files"]),
        "native control source closure changed",
    )
    for name, allowance, probes in (("control", 20, 60), ("treatment", 60, 140)):
        row = report[name]
        require(
            pilot.canonical(
                {
                    key: row[key]
                    for key in (
                        "allowance",
                        "output_cap",
                        "source_workflows",
                        "source_calls",
                        "probes",
                    )
                }
            )
            == pilot.canonical(
                {
                    "allowance": allowance,
                    "output_cap": 256,
                    "source_workflows": 63,
                    "source_calls": 189,
                    "probes": probes,
                }
            ),
            "native control trajectory changed",
        )
    require(
        report["test_files"]
        and all(
            file_hash(ROOT / name) == expected
            for name, expected in report["test_files"].items()
        ),
        "native control test source changed",
    )


def manifest_core(
    bundle,
    binding,
    frozen,
    native_store,
    materializer_sha256,
    initial_events,
    prepilot,
    controls,
    created_at,
):
    fixture.validate_bundle(bundle)
    state = accounting(initial_events)
    clean_finance(state)
    remaining = pilot.money(financial_state(state)["remaining_usd"])
    require(0 < remaining <= pilot.HARD_CAP, "invalid original campaign remainder")
    expected = pair.enrollment(
        frozen["catalog"],
        frozen["endpoints"],
        frozen["native_sha256"],
        max_calls_per_site=60,
        max_output_tokens=256,
    )
    require(
        pilot.canonical(frozen) == pilot.canonical(expected),
        "native experiment enrollment changed",
    )
    validate_controls(controls, frozen)
    require(
        prepilot["evidence_kind"] == "historical_pre_pilot"
        and prepilot["provenance"]["enrollment_sha256"] == pilot.digest(frozen)
        and prepilot["provenance"]["selection_sha256"]
        == bundle["selection"]["selection_sha256"]
        and prepilot["provenance"]["materializer_sha256"] == materializer_sha256,
        "prospective resource evidence belongs to another frozen protocol",
    )
    result = {
        "kind": KIND,
        "created_at": created_at,
        "source_files": sources(),
        "selection_sha256": bundle["selection"]["selection_sha256"],
        "public_bundle_sha256": pilot.digest(bundle),
        "fixture_exposure": exposure_disclosure(bundle["selection"]),
        "fixture_exposure_sha256": pilot.digest(
            exposure_disclosure(bundle["selection"])
        ),
        "bundle_files": {
            "selection.json": bundle["provenance"]["selection_file_sha256"],
            "public.json": bundle["provenance"]["public_sha256"],
            "provenance.json": hashlib.sha256(
                pilot.canonical(bundle["provenance"]) + b"\n"
            ).hexdigest(),
        },
        "schedule": schedules(bundle),
        "enrollment": frozen,
        "campaign_ledger": binding,
        "native_store": str(Path(native_store).resolve()),
        "materializer_sha256": materializer_sha256,
        "family_cap_usd": str(remaining),
        "initial_ledger_prefix": {
            "count": len(initial_events),
            "sha256": pilot.digest(initial_events),
        },
        "initial_financial_snapshot": financial_state(state),
        "prepilot_forecast": prepilot,
        "structural_controls": controls,
        "structural_controls_sha256": pilot.digest(controls),
        "protocol": {
            "pilot_tasks": 6,
            "study_tasks": 63,
            "pilot_calls": 36,
            "source_calls": 189,
            "completed_probe_ceiling": 140,
            "HR_calls": 189,
            "max_tokens": 256,
            "max_calls_per_site": 60,
            "per_plan_evidence_target": 20,
            "max_phase_seconds": MAX_SECONDS,
            "pilot_cap_usd": str(PILOT_CAP),
            "pilot_known_progress_stop_usd": str(PILOT_PROGRESS_STOP),
            "phase_order": ["pilot", "study:source-and-probes", "study:HR"],
            "pilot_gate": "all36-valid-non-null-nontruncated;format-and-resources-only",
            "study_invalid": "charged-loss-and-own-source-contagious-null;native-guards-unchanged",
            "failure_policy": "first-provider-or-protocol-failure=permanent-phase-stop;no-retry-resume-reseed",
            "source_visible": True,
            "primary_promotion": False,
            "gold_before_complete_protocol": False,
        },
        "limitations": LIMITATIONS,
        "efficacy_claim": False,
        "paper_evidence": False,
    }
    prepilot_gate(result, state)
    return result


def validate_manifest(manifest, bundle, events, bridge, materializer, expected_sha256):
    require(
        pilot.digest(manifest) == expected_sha256, "protected manifest hash changed"
    )
    prefix = manifest["initial_ledger_prefix"]
    require(
        type(prefix["count"]) is int
        and 0 < prefix["count"] <= len(events)
        and pilot.digest(events[: prefix["count"]]) == prefix["sha256"],
        "original campaign prefix changed since prospective freeze",
    )
    binding = manifest["campaign_ledger"]
    require(
        events[0]["event"] == "origin"
        and pilot.digest(events[0]) == binding["origin_event_sha256"]
        and all(event.get("key_id") == events[0].get("key_id") for event in events),
        "protected original campaign key or origin changed",
    )
    require(
        str(bridge.store) == manifest["native_store"]
        and bridge.sha256 == manifest["enrollment"]["native_sha256"]
        and file_hash(materializer) == manifest["materializer_sha256"],
        "protected frozen binary or store changed",
    )
    bridge.validate()
    expected = manifest_core(
        bundle,
        binding,
        manifest["enrollment"],
        bridge.store,
        manifest["materializer_sha256"],
        events[: prefix["count"]],
        manifest["prepilot_forecast"],
        manifest["structural_controls"],
        manifest["created_at"],
    )
    require(
        pilot.canonical(expected) == pilot.canonical(manifest),
        "protected source or protocol contract changed",
    )


def freeze_manifest(
    output,
    bundle,
    binding,
    frozen,
    native_store,
    materializer,
    events,
    prepilot,
    controls,
):
    require(
        not Path(native_store).exists(), "prospective native profile store is not empty"
    )
    result = manifest_core(
        bundle,
        binding,
        frozen,
        native_store,
        file_hash(materializer),
        events,
        prepilot,
        controls,
        datetime.now(timezone.utc).isoformat(),
    )
    require(
        not any(
            e.get("event") == CLAIM_EVENT
            and e.get("selection_sha256") == result["selection_sha256"]
            for e in events
        ),
        "this selection already has a claimed protocol",
    )
    write_json(Path(output) / "manifest.json", result, immutable=True)
    return result


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
            require(
                pilot.canonical(gate)
                == pilot.canonical(
                    resource_gate(
                        manifest, state, gate["forecast"], gate["pilot_evidence"]
                    )
                ),
                "study financial gate changed before its atomic phase claim",
            )
        else:
            require(gate is None, "pilot cannot have a post-pilot gate")
            prepilot_gate(manifest, state)
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


def audit_records(manifest, bundle, phase, intents, events, bridge, materializer):
    from bench import openrouter_protected_audit as audit

    calls, protected = extract_records(manifest, phase, intents, events)
    return audit.audit_phase(
        manifest, bundle, phase, intents, calls, protected, events, bridge, materializer
    )


def prepare_study_gate(output, manifest, bundle, events, bridge, materializer):
    from bench import openrouter_protected_forecast as forecast

    directory = Path(output) / "pilot"
    summary = fixture.read(directory / "summary.json")
    require(
        summary["manifest_sha256"] == pilot.digest(manifest)
        and summary["schedule_complete"] is True,
        "study requires the complete frozen pilot artifact",
    )
    for name in ("intents.json", "calls.json", "protected-events.json"):
        require(
            file_hash(directory / name) == summary["artifact_files"][name],
            "pilot artifact bytes changed",
        )
    intents = fixture.read(directory / "intents.json")
    calls, protected = extract_records(manifest, "pilot", intents, events)
    require(
        pilot.canonical(calls)
        == pilot.canonical(fixture.read(directory / "calls.json"))
        and pilot.canonical(protected)
        == pilot.canonical(fixture.read(directory / "protected-events.json")),
        "pilot artifact differs from original campaign receipts",
    )
    audit = audit_records(
        manifest, bundle, "pilot", intents, events, bridge, materializer
    )
    require(
        audit["pilot_format_gate"] is True, "pilot format gate failed; no continuation"
    )
    evidence = {
        "manifest_sha256": pilot.digest(manifest),
        "selection_sha256": manifest["selection_sha256"],
        "calls_sha256": pilot.digest(calls),
        "intents_sha256": pilot.digest(intents),
        "known_cost_usd": audit["finance"]["known_stage_usd"],
    }
    estimate = forecast.forecast(
        bundle, calls, intents, manifest["enrollment"], materializer
    )
    return resource_gate(manifest, accounting(events), estimate, evidence)


def run_phase(
    output,
    manifest,
    bundle,
    phase,
    ledger,
    key,
    bridge,
    materializer,
    *,
    expected_manifest_sha256,
    gate=None,
):
    """Validate exact source/manifest and the pilot before claiming any work."""
    _, initial_events, _ = finance.typed_ledger_snapshot(ledger.path, key)
    validate_manifest(
        manifest, bundle, initial_events, bridge, materializer, expected_manifest_sha256
    )
    if phase == "study":
        expected_gate = prepare_study_gate(
            output, manifest, bundle, initial_events, bridge, materializer
        )
        require(
            pilot.canonical(gate) == pilot.canonical(expected_gate),
            "study gate is not the exact independently audited pilot forecast",
        )
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
        _, events, _ = finance.typed_ledger_snapshot(ledger.path, key)
        audit = audit_records(
            manifest, bundle, phase, intents, events, bridge, materializer
        )
        require(
            audit["schedule_complete"] is True,
            "executed phase lacks complete audited evidence",
        )
        if phase == "pilot":
            require(audit["pilot_format_gate"] is True, "pilot format gate failed")
        write_json(directory / "audit.json", audit, immutable=True)
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
