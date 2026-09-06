"""Protocol helpers and temporary ledgers only; paid transport is forbidden."""

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bench import openrouter_protected_study as s

KEY = "literal-fake-protected-study-key"


@pytest.fixture(autouse=True)
def no_paid_transport(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("paid transport or credentials are forbidden")

    monkeypatch.setattr(s.pilot, "request_json", fail)
    monkeypatch.setattr(s.pilot, "load_key", fail)
    monkeypatch.setattr(s.pilot, "account", fail)


@pytest.fixture
def setup(tmp_path):
    ledger = s.pilot.Ledger(tmp_path / "fake-ledger.jsonl", KEY)
    with ledger.locked() as handle:
        origin = ledger.append(handle, {"event": "origin", "usage_usd": "0"})
    bundle = {
        "selection": {
            "pilot_ids": [f"p{i}" for i in range(6)],
            "study_ids": [f"s{i}" for i in range(63)],
        }
    }
    manifest = {
        "kind": s.KIND,
        "family_cap_usd": "15.44903353",
        "prepilot_forecast": {
            "prospective_pilot_usd": "0.2",
            "training_usd": "3.0",
            "HR_usd": "0.4",
            "largest_pair_allowance_usd": "12.01536",
        },
        "selection_sha256": "a" * 64,
        "campaign_ledger": {
            "resolved_path": str(ledger.path.resolve()),
            "origin_event_sha256": s.pilot.digest(origin),
        },
        "schedule": s.schedules(bundle),
    }
    return ledger, manifest, bundle


def read(ledger):
    with ledger.locked() as handle:
        return ledger.read(handle)


def test_schedule_is_full_fixed_and_clustered(setup):
    _, manifest, _ = setup
    pilot, study = (manifest["schedule"][phase] for phase in s.PHASES)
    assert len(pilot) == 36 and len(study) == 378
    assert {row["arm"] for row in pilot} == {"S0", "H0"}
    assert all(row["arm"] == "source" for row in study[:189])
    assert all(row["arm"] == "HR" for row in study[189:])
    assert [r["task_id"] for r in study[:189]] == [r["task_id"] for r in study[189:]]
    for rows in (pilot, study):
        for index in range(0, len(rows), 3):
            assert [r["workflow_stage"] for r in rows[index : index + 3]] == list(
                s.typed.STAGES
            )
            assert len({(r["task_id"], r["arm"]) for r in rows[index : index + 3]}) == 1


def test_claim_is_durable_selection_wide_and_cannot_resume(setup):
    ledger, manifest, _ = setup
    claim = s.claim_phase(ledger, manifest, "pilot", None)
    assert claim == read(ledger)[-1]
    assert claim["gate"] is None and claim["gate_sha256"] is None
    assert claim["stage_cap_usd"] == "8"
    changed = {**manifest, "created_at": "different", "output": "copied-directory"}
    before = ledger.path.read_bytes()
    with pytest.raises(s.pilot.PilotError, match="already claimed"):
        s.claim_phase(ledger, changed, "pilot", None)
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("change", ["origin", "path"])
def test_claim_rejects_campaign_replacement(setup, change):
    ledger, manifest, _ = setup
    manifest = deepcopy(manifest)
    manifest["campaign_ledger"][
        "origin_event_sha256" if change == "origin" else "resolved_path"
    ] = "changed"
    before = ledger.path.read_bytes()
    with pytest.raises(s.pilot.PilotError, match="changed"):
        s.claim_phase(ledger, manifest, "pilot", None)
    assert ledger.path.read_bytes() == before


def test_study_requires_exact_pilot_claim_and_gate(setup):
    ledger, manifest, _ = setup
    with pytest.raises(s.pilot.PilotError, match="exact audited pilot"):
        s.claim_phase(ledger, manifest, "study", {"passed": True})
    s.claim_phase(ledger, manifest, "pilot", None)
    with pytest.raises(s.pilot.PilotError, match="exact audited pilot"):
        s.claim_phase(ledger, manifest, "study", None)
    gate = s.resource_gate(
        manifest,
        s.accounting(read(ledger)),
        {
            "training_usd": "3",
            "HR_usd": "0.4",
            "largest_pair_allowance_usd": "12.01536",
        },
        {},
    )
    result = s.claim_phase(ledger, manifest, "study", gate)
    assert result["gate"] == gate and result["gate_sha256"] == s.pilot.digest(gate)
    assert result["stage_cap_usd"] == "15.44903353"


def test_resource_gate_keeps_largest_simultaneous_pair_allowance(setup):
    ledger, manifest, _ = setup
    state = s.accounting(read(ledger))
    state.update(known_usd=Decimal("22.27594747"), holds_usd=Decimal("12.275019"))
    report = {
        "training_usd": "3.3",
        "HR_usd": "0.45",
        "largest_pair_allowance_usd": "12.01536",
    }
    gate = s.resource_gate(manifest, state, report, {"complete": True})
    assert gate["passed"] is True and gate["accuracy_used"] is False
    assert gate["available_usd"] == "15.44903353"
    with pytest.raises(s.pilot.PilotError, match="do not fit"):
        s.resource_gate(manifest, state, {**report, "training_usd": "3.5"}, {})
    state["pending"] = [{"stage": "old", "upper_cost_usd": "1"}]
    with pytest.raises(s.pilot.PilotError, match="unresolved"):
        s.resource_gate(manifest, state, report, {})


def test_rolling_telemetry_does_not_mask_endpoint_identity():
    endpoint = {
        "name": "provider/model",
        "pricing": {"prompt": "3"},
        "latency_last_30m": 5,
    }
    assert s.stable_endpoint(endpoint) == {
        "name": "provider/model",
        "pricing": {"prompt": "3"},
    }


def test_prepaid_rounding_is_not_extra_budget_or_a_weaker_reserve(setup):
    ledger, manifest, _ = setup
    state = s.accounting(read(ledger))
    state.update(known_usd=Decimal("22.27594747"), holds_usd=Decimal("12.275019"))
    manifest["prepilot_forecast"].update(
        prospective_pilot_usd="0.312176", training_usd="3.087615", HR_usd="0.472196"
    )
    s.prepilot_gate(manifest, state)
    with pytest.raises(s.pilot.PilotError, match="full pair reserve"):
        s.prepilot_gate({**manifest, "family_cap_usd": "15.4"}, state)
    state["holds_usd"] += Decimal("0.04")
    with pytest.raises(s.pilot.PilotError, match="full pair reserve"):
        s.prepilot_gate(manifest, state)


@pytest.fixture
def frozen_setup(setup, tmp_path, monkeypatch):
    ledger, simple, bundle = setup
    bundle.update(
        kind="synthetic",
        tasks=[],
        provenance={"selection_file_sha256": "s" * 64, "public_sha256": "p" * 64},
    )
    bundle["selection"]["selection_sha256"] = simple["selection_sha256"]
    monkeypatch.setattr(s.fixture, "validate_bundle", lambda bundle: None)
    monkeypatch.setattr(s, "sources", lambda: {"frozen-source": "fixed"})
    monkeypatch.setattr(s.pair, "source_files", lambda: {"native-source": "fixed"})
    frozen = {
        "catalog": {},
        "endpoints": {},
        "native_sha256": "b" * 64,
        "experiment_policy": {"max_calls_per_site": 60, "max_output_tokens": 256},
    }

    def enrollment(*args, **kwargs):
        assert kwargs == {"max_calls_per_site": 60, "max_output_tokens": 256}
        return deepcopy(frozen)

    monkeypatch.setattr(s.pair, "enrollment", enrollment)
    controls = {
        "kind": "protected-native-reachability-controls-v1",
        "native_sha256": frozen["native_sha256"],
        "source_files": s.pair.source_files(),
        "source_files_sha256": s.pilot.digest(s.pair.source_files()),
        "test_files": {
            "tests/test_openrouter_protected_study.py": s.file_hash(
                s.ROOT / "tests/test_openrouter_protected_study.py"
            )
        },
        "provider_calls": 0,
        **{
            key: True
            for key in (
                "reference_primary_only",
                "no_seeded_profiles",
                "fake_provider",
                "typed_zero_loss",
                "other_numeric_defaults_unchanged",
                "complete",
            )
        },
        **{
            name: {
                "allowance": allowance,
                "output_cap": 256,
                "source_workflows": 63,
                "source_calls": 189,
                "probes": probes,
            }
            for name, allowance, probes in (("control", 20, 60), ("treatment", 60, 140))
        },
    }
    materializer = tmp_path / "fake-materializer"
    materializer.write_bytes(b"not executable; never invoked")
    forecast = {
        **simple["prepilot_forecast"],
        "evidence_kind": "historical_pre_pilot",
        "provenance": {
            "enrollment_sha256": s.pilot.digest(frozen),
            "selection_sha256": simple["selection_sha256"],
            "materializer_sha256": s.file_hash(materializer),
        },
    }
    bridge = SimpleNamespace(
        store=(tmp_path / "new.sqlite").resolve(),
        sha256=frozen["native_sha256"],
        validate=lambda: None,
    )
    manifest = s.manifest_core(
        bundle,
        simple["campaign_ledger"],
        frozen,
        bridge.store,
        s.file_hash(materializer),
        read(ledger),
        forecast,
        controls,
        "2026-09-06T00:00:00Z",
    )
    return ledger, manifest, bundle, bridge, materializer


def test_frozen_manifest_binds_original_remaining_funds(frozen_setup):
    ledger, manifest, bundle, bridge, materializer = frozen_setup
    assert manifest["family_cap_usd"] == "50"
    assert manifest["protocol"]["primary_promotion"] is False
    s.validate_manifest(
        manifest, bundle, read(ledger), bridge, materializer, s.pilot.digest(manifest)
    )
    with ledger.locked() as handle:
        ledger.append(handle, {"event": "nonfinancial-audit-note"})
    # An intact old prefix remains valid after unrelated append-only events.
    s.validate_manifest(
        manifest, bundle, read(ledger), bridge, materializer, s.pilot.digest(manifest)
    )


@pytest.mark.parametrize(
    "change",
    ["family", "source", "cap", "schedule", "prefix", "control", "materializer"],
)
def test_manifest_contract_mutations_fail_even_with_rehashed_outer_object(
    frozen_setup, change
):
    ledger, manifest, bundle, bridge, materializer = frozen_setup
    manifest = deepcopy(manifest)
    if change == "family":
        manifest["family_cap_usd"] = "51"
    elif change == "source":
        manifest["source_files"] = {}
    elif change == "cap":
        manifest["protocol"]["max_tokens"] = 128
    elif change == "schedule":
        manifest["schedule"]["study"].reverse()
    elif change == "prefix":
        manifest["initial_ledger_prefix"]["sha256"] = "x" * 64
    elif change == "control":
        manifest["structural_controls"]["treatment"]["probes"] = 139
    else:
        materializer.write_bytes(b"changed")
    with pytest.raises(s.pilot.PilotError):
        s.validate_manifest(
            manifest,
            bundle,
            read(ledger),
            bridge,
            materializer,
            s.pilot.digest(manifest),
        )


def test_phase_snapshot_exports_only_selected_protocol(setup, tmp_path):
    ledger, manifest, _ = setup
    s.claim_phase(ledger, manifest, "pilot", None)
    report = s.save_phase(
        tmp_path / "out",
        manifest,
        "pilot",
        [],
        read(ledger),
        complete=False,
        error="SyntheticStop",
    )
    assert report["completed_calls"] == 0 and report["schedule_complete"] is False
    assert report["error_type"] == "SyntheticStop"
    assert report["efficacy_claim"] is False and report["labels_opened"] is False
    assert set(report["artifact_files"]) == {
        "intents.json",
        "calls.json",
        "protected-events.json",
    }


@pytest.mark.parametrize("failure", [False, True])
def test_runner_returns_terminal_summary_or_preserves_failed_claim(
    setup, tmp_path, monkeypatch, failure
):
    ledger, manifest, bundle = setup
    bundle["tasks"] = []
    manifest["schedule"] = {"pilot": [], "study": []}
    materializer = tmp_path / "fake-binary"
    materializer.write_bytes(b"not executable")
    bridge = SimpleNamespace(store=tmp_path / "native.sqlite")
    manifest.update(
        materializer_sha256=s.file_hash(materializer), native_store=str(bridge.store)
    )
    monkeypatch.setattr(s.fixture, "validate_bundle", lambda bundle: None)
    monkeypatch.setattr(s, "schedules", lambda bundle: manifest["schedule"])
    monkeypatch.setattr(s, "load_module", lambda *args: None)
    monkeypatch.setattr(s, "validate_manifest", lambda *args: None)
    monkeypatch.setattr(
        s,
        "audit_records",
        lambda *args: {"schedule_complete": True, "pilot_format_gate": True},
    )
    if failure:
        manifest["schedule"]["pilot"] = [
            {
                "arm": "S0",
                "task_id": "absent",
                "workflow_stage": "filter",
                "phase": "pilot",
            }
        ]
        with pytest.raises(KeyError):
            s.run_phase(
                tmp_path / "output",
                manifest,
                bundle,
                "pilot",
                ledger,
                KEY,
                bridge,
                materializer,
                expected_manifest_sha256=s.pilot.digest(manifest),
            )
        report = s.fixture.read(tmp_path / "output/pilot/summary.json")
        assert not report["schedule_complete"] and report["error_type"] == "KeyError"
    else:
        report = s.run_phase(
            tmp_path / "output",
            manifest,
            bundle,
            "pilot",
            ledger,
            KEY,
            bridge,
            materializer,
            expected_manifest_sha256=s.pilot.digest(manifest),
        )
        assert report["schedule_complete"] is True
        assert s.fixture.read(tmp_path / "output/pilot/summary.json") == report
    assert len([e for e in read(ledger) if e["event"] == s.CLAIM_EVENT]) == 1
