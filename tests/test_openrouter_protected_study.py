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
    gate = {"passed": True, "fixture_sha256": "synthetic"}
    result = s.claim_phase(ledger, manifest, "study", gate)
    assert result["gate"] == gate and result["gate_sha256"] == s.pilot.digest(gate)
    assert result["stage_cap_usd"] == "15.4"


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
    assert gate["available_usd"] == "15.4"
    with pytest.raises(s.pilot.PilotError, match="do not fit"):
        s.resource_gate(manifest, state, {**report, "training_usd": "3.4"}, {})
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
        )
        assert report["schedule_complete"] is True
        assert s.fixture.read(tmp_path / "output/pilot/summary.json") == report
    assert len([e for e in read(ledger) if e["event"] == s.CLAIM_EVENT]) == 1
