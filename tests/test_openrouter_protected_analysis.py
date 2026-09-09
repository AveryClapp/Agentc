"""Synthetic artifacts only; no generated answers, campaign ledger or provider."""

from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from bench import openrouter_protected_analysis as analysis
from bench import openrouter_protected_audit as audit
from bench import openrouter_protected_study as study
from bench import typed_relational_workload as typed
from bench.openrouter_pilot import canonical, digest, money, PilotError
from test_openrouter_protected_audit import public_bundle
from test_openrouter_protected_pair import SYNTH


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical(value) + b"\n"
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def completed(tmp_path, monkeypatch, request):
    output, public = tmp_path / "protocol", tmp_path / "public"
    bundle = public_bundle()  # Real ID formula, literal synthetic public bodies.
    selection = bundle["selection"]
    labels = {task["task_id"]: {"expected": "M01"} for task in bundle["tasks"]}
    bundle["provenance"]["labels_sha256"] = write(public / "labels.json", labels)
    for name, value in (
        ("selection.json", selection),
        ("provenance.json", bundle["provenance"]),
        ("public.json", bundle["tasks"]),
    ):
        write(public / name, value)
    manifest = {
        "kind": study.KIND,
        "selection_sha256": selection["selection_sha256"],
        "public_bundle_sha256": digest(bundle),
        "schedule": study.schedules(bundle),
        "fixture_exposure": study.exposure_disclosure(selection),
        "fixture_exposure_sha256": digest(study.exposure_disclosure(selection)),
        "protocol": {"per_plan_evidence_target": 20},
        "limitations": list(study.LIMITATIONS),
    }
    raw_hash = write(output / "manifest.json", manifest)
    monkeypatch.setattr(analysis, "MANIFEST_FILE_SHA256", raw_hash)
    monkeypatch.setattr(analysis, "MANIFEST_SHA256", digest(manifest))
    monkeypatch.setattr(
        analysis,
        "PROTOCOL",
        {
            **analysis.PROTOCOL,
            "manifest_sha256": digest(manifest),
            "manifest_file_sha256": raw_hash,
        },
    )
    events = [{"event": "origin", "key_id": "synthetic-key-digest", "at": "synthetic"}]
    hashes, audits, calls, intents, checks = {}, {}, {}, {}, []
    bridge, materializer = object(), object()
    ids = selection["study_ids"]

    def result(item, probe=False):
        step, arm, task = item["workflow_stage"], item["arm"], item["task_id"]
        value = (
            ["D01", "D02", "D03"]
            if step == "filter"
            else SYNTH
            if step == "synthesize"
            else "M01"
        )
        if item["phase"] == "study":
            if step == "answer" and (
                (arm == "source" and task == ids[0]) or (arm == "HR" and task == ids[1])
            ):
                value = "M02"
            if step == "filter" and arm == "HR" and task == ids[2]:
                value = None
            if probe and task in ids[18:20]:
                value = ["D04", "D05", "D06"]
        cost = ".0005" if probe else ".002" if arm == "source" else ".001"
        if (
            getattr(request, "param", None) == "zero_source"
            and arm == "source"
            and not probe
        ):
            cost = "0"
        row = {
            **item,
            "id": study.stage_id(manifest, item["phase"]) + "/" + digest([item, probe]),
            "event": "result",
            "stage": study.stage_id(manifest, item["phase"]),
            "fingerprint": digest(item),
            "answer": json.dumps({"result": value}),
            "finish_reason": "stop",
            "cost_usd": cost,
            "usage": {"cost": cost, "prompt_tokens": 400, "completion_tokens": 40},
            "key_id": events[0]["key_id"],
            "at": "synthetic",
        }
        if probe:
            row["arm"] = "probe"
        events.append(row)
        return {key: value for key, value in row.items() if key not in {"key_id", "at"}}

    for phase in study.PHASES:
        calls[phase], intents[phase] = [], []
        for item in manifest["schedule"][phase]:
            calls[phase].append(result(item))
            if phase == "pilot" or item["arm"] == "HR":
                intents[phase].append({**item, "id": calls[phase][-1]["id"]})
            if (
                phase == "study"
                and item["arm"] == "source"
                and item["workflow_stage"] == "filter"
                and item["task_id"] in ids[:20]
            ):
                calls[phase].append(result(item, probe=True))
        for name, value in (
            ("calls.json", calls[phase]),
            ("intents.json", intents[phase]),
            ("protected-events.json", []),
        ):
            hashes[phase + "/" + name] = write(output / phase / name, value)
        state = analysis.accounting(events)
        finance = audit._financial_report(state, study.stage_id(manifest, phase))
        learning = {
            "exact_plan_profiles": []
            if phase == "pilot"
            else [
                {
                    "key": {
                        "call_site_version": "synthetic-filter",
                        "plan": "synthetic-exact-plan",
                    },
                    "runtime_version": "synthetic-runtime",
                    "divergence_threshold": 0.03,
                    "workflow_stage": "filter",
                    "paired_observations": 20,
                    "disagreements": 2,
                    "sequences": list(range(1, 21)),
                }
            ],
            "ambiguous_native_claim_ids": [],
            "incomplete_pair_ids": [],
            "learning_evidence_reported": True,
            "current_guard_state_assessed": False,
            "admission_assessed": False,
            "unknown_primary_remains_source": True,
        }
        audits[phase] = {
            "kind": "protected-study-readonly-audit-v1",
            "phase": phase,
            "manifest_sha256": digest(manifest),
            "ledger_event_count": len(events),
            "ledger_events_sha256": digest(events),
            "schedule_complete": True,
            "integrity_verified": True,
            "labels_opened": False,
            "pilot_format_gate": phase == "pilot",
            "completed_scheduled_steps": len(manifest["schedule"][phase]),
            "finance": finance,
            "learning": learning,
            "calls_sha256": digest(calls[phase]),
            "intents_sha256": digest(intents[phase]),
            "protected_events_sha256": digest([]),
        }
        hashes[phase + "/audit.json"] = write(
            output / phase / "audit.json", audits[phase]
        )
        summary = {
            "kind": study.KIND,
            "phase": phase,
            "manifest_sha256": digest(manifest),
            "schedule_complete": True,
            "completed_calls": len(calls[phase]),
            "artifact_files": {
                name: hashes[phase + "/" + name]
                for name in ("intents.json", "calls.json", "protected-events.json")
            },
            "known_cost_usd": finance["known_stage_usd"],
            "retained_uncertainty_usd": "0",
            "pending_upper_usd": "0",
            "financial_snapshot": study.financial_state(state),
            "error_type": None,
            "efficacy_claim": False,
            "paper_evidence": False,
            "labels_opened": False,
        }
        hashes[phase + "/summary.json"] = write(
            output / phase / "summary.json", summary
        )

    def independent(
        man,
        pub,
        phase,
        ins,
        receipts,
        native,
        prefix,
        actual_bridge,
        actual_materializer,
    ):
        checks.append(phase)
        assert (
            man == manifest
            and pub == bundle
            and ins == intents[phase]
            and receipts == calls[phase]
            and native == []
        )
        assert actual_bridge is bridge and actual_materializer is materializer
        assert digest(prefix) == audits[phase]["ledger_events_sha256"]
        return deepcopy(audits[phase])

    # Separately verified native replay is the one mocked seam: these are
    # analysis-unit fixtures, never fabricated native execution evidence.
    monkeypatch.setattr(analysis.audit, "audit_phase", independent)

    def forbidden(*args, **kwargs):
        pytest.fail("analysis invoked a generator, ledger, key or provider")

    monkeypatch.setattr(study.pilot.Ledger, "__init__", forbidden)
    for name in ("account", "load_key", "request_json"):
        monkeypatch.setattr(study.pilot, name, forbidden)
    monkeypatch.setattr(typed, "make_tasks", forbidden)
    monkeypatch.setattr(analysis.fixture.generator, "make_tasks", forbidden)
    return dict(
        output=output,
        public=public,
        manifest=manifest,
        bundle=bundle,
        events=events,
        hashes=hashes,
        audits=audits,
        calls=calls,
        intents=intents,
        checks=checks,
        bridge=bridge,
        materializer=materializer,
    )


def run(data, monkeypatch, *, allow_labels):
    original, labels_opened = Path.read_bytes, []
    original_open = Path.open
    before = {
        str(path): original(path)
        for directory in (data["output"], data["public"])
        for path in directory.rglob("*.json")
    }
    events = deepcopy(data["events"])

    def guarded_open(path, mode="r", *args, **kwargs):
        assert not any(flag in mode for flag in "wa+"), "analysis artifact write"
        if path.name == "labels.json":
            assert allow_labels, "labels opened before complete evidence gate"
            assert data["checks"] == ["pilot", "study"]
            labels_opened.append(str(path))
        return original_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", guarded_open)
        patch.setattr(
            Path, "write_bytes", lambda *args: pytest.fail("analysis artifact write")
        )
        patch.setattr(
            Path, "write_text", lambda *args: pytest.fail("analysis artifact write")
        )
        if not allow_labels:
            patch.setattr(
                typed,
                "score_answer",
                lambda *args, **kwargs: pytest.fail("premature scoring"),
            )
        report = analysis.analyze(
            data["output"],
            data["public"],
            data["events"],
            data["bridge"],
            data["materializer"],
            artifact_sha256=data["hashes"],
        )
    assert data["events"] == events
    assert all(original(Path(path)) == raw for path, raw in before.items())
    assert labels_opened == [str(data["public"] / "labels.json")]
    return report


def change_artifact(data, phase, name, mutate):
    path = data["output"] / phase / name
    value = json.loads(path.read_bytes())
    mutate(value)
    data["hashes"][phase + "/" + name] = write(path, value)


def test_label_gate_also_guards_read_text(completed, monkeypatch):
    def premature_bundle_read(*args, **kwargs):
        (completed["public"] / "labels.json").read_text()
        pytest.fail("premature label access escaped the guard")

    monkeypatch.setattr(analysis.fixture, "load_bundle", premature_bundle_read)
    with pytest.raises(
        AssertionError, match="labels opened before complete evidence gate"
    ):
        run(completed, monkeypatch, allow_labels=False)


def test_complete_analysis_keeps_all63_and_separates_probes(completed, monkeypatch):
    report = run(completed, monkeypatch, allow_labels=True)
    assert report["paired_task_count"] == 63
    assert [row["task_id"] for row in report["paired_tasks"]] == completed["bundle"][
        "selection"
    ]["study_ids"]
    assert report["accuracy_totals"] == {
        "source": {"correct": 62, "tasks": 63},
        "HR": {"correct": 61, "tasks": 63},
    }
    assert report["quality_transitions"] == {"HR_gain": 1, "HR_loss": 2, "tie": 60}
    assert report["call_counts"] == {"pilot": 36, "source": 189, "HR": 189, "probe": 20}
    assert money(report["costs"]["all_acquisition_usd"]) == Decimal(".613")
    assert money(report["costs"]["source_learning_block_usd"]) == Decimal(".388")
    assert report["costs"]["fixed_HR_to_source_workflow_cost_ratio"] == 0.5
    profile = report["native_learning"]["profiles"][0]
    assert profile["paired_observations"] == 20 and profile["disagreements"] == 2
    assert profile["count_floor_met"] and profile["pairs_short_of_floor"] == 0
    assert report["native_learning"]["current_guard_or_admission_assessed"] is False
    assert report["fixture_exposure"] == completed["manifest"]["fixture_exposure"]
    assert (
        report["pilot_accuracy_reported"]
        is report["adaptive_savings_claim"]
        is report["efficacy_claim"]
        is False
    )
    assert report["descriptive_paired_intervals"]["HR_minus_source_accuracy"][
        "mean"
    ] == pytest.approx(-1 / 63)
    assert report["analysis_protocol"]["bootstrap_seed"] == 2026090605
    assert report["analysis_protocol"]["replicates"] == 10000


@pytest.mark.parametrize("phase", ["pilot", "study"])
@pytest.mark.parametrize(
    "problem",
    [
        "incomplete",
        "failure",
        "ambiguous",
        "pending",
        "failed_hold",
        "false_gate",
        "audit_hash",
        "summary_cost",
    ],
)
def test_bad_evidence_never_opens_labels(completed, monkeypatch, phase, problem):
    if problem in {"incomplete", "failure", "summary_cost"}:
        key, value = {
            "incomplete": ("schedule_complete", False),
            "failure": ("error_type", "TimeoutError"),
            "summary_cost": ("known_cost_usd", "0"),
        }[problem]
        change_artifact(
            completed, phase, "summary.json", lambda row: row.update({key: value})
        )
    elif problem == "audit_hash":
        completed["hashes"][phase + "/audit.json"] = "0" * 64
    else:
        target = completed["audits"][phase]
        if problem == "ambiguous":
            target["learning"]["ambiguous_native_claim_ids"] = ["unacknowledged"]
        elif problem == "pending":
            target["finance"]["pending_ids"] = ["pending"]
        elif problem == "failed_hold":
            target["finance"]["failed_attempt_ids"] = ["failed"]
            target["finance"]["retained_uncertainty_usd"] = "6.00768"
        else:
            target["pilot_format_gate"] = False
            if phase == "study":
                target["schedule_complete"] = False
        completed["hashes"][phase + "/audit.json"] = write(
            completed["output"] / phase / "audit.json", target
        )
    with pytest.raises(PilotError):
        run(completed, monkeypatch, allow_labels=False)


@pytest.mark.parametrize("kind", ["reserve", "response", "claim", "native"])
def test_later_same_phase_activity_rejected(completed, monkeypatch, kind):
    sid = study.stage_id(completed["manifest"], "study")
    row = {"event": kind, "key_id": completed["events"][0]["key_id"]}
    if kind == "reserve":
        row["stage"] = sid
    elif kind == "response":
        row["id"] = completed["calls"]["study"][0]["id"]
    elif kind == "claim":
        row.update(
            event=study.CLAIM_EVENT,
            selection_sha256=completed["manifest"]["selection_sha256"],
            phase="study",
        )
    else:
        row.update(event=study.pair.EVENT, binding={"run_id": sid})
    completed["events"].append(row)
    with pytest.raises(PilotError, match="later activity"):
        run(completed, monkeypatch, allow_labels=False)


@pytest.mark.parametrize(
    "mutation",
    [
        "manifest_bytes",
        "missing_external_hash",
        "extra_external_hash",
        "prefix",
        "mixed_key",
        "changed_saved_audit",
        "raw_call_bytes",
    ],
)
def test_provenance_tamper_before_labels(completed, monkeypatch, mutation):
    if mutation == "manifest_bytes":
        path = completed["output"] / "manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "missing_external_hash":
        completed["hashes"].pop("study/audit.json")
    elif mutation == "extra_external_hash":
        completed["hashes"]["other.json"] = "0" * 64
    elif mutation == "prefix":
        completed["events"][1]["answer"] = "changed"
    elif mutation == "mixed_key":
        completed["events"][-1]["key_id"] = "other"
    elif mutation == "changed_saved_audit":
        change_artifact(
            completed, "study", "audit.json", lambda row: row.update(extra="unverified")
        )
    else:
        path = completed["output"] / "study/calls.json"
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(PilotError):
        run(completed, monkeypatch, allow_labels=False)


def test_label_hash_checked_before_scoring(completed, monkeypatch):
    (completed["public"] / "labels.json").write_bytes(b"changed label bytes")
    monkeypatch.setattr(
        typed,
        "score_answer",
        lambda *args, **kwargs: pytest.fail("unverified label scored"),
    )
    with pytest.raises(PilotError, match="raw artifact"):
        run(completed, monkeypatch, allow_labels=True)


def test_other_stage_cost_remains_separate(completed, monkeypatch):
    completed["events"].append(
        {
            "event": "result",
            "id": "other",
            "stage": "other",
            "fingerprint": "other",
            "cost_usd": "1",
            "key_id": completed["events"][0]["key_id"],
        }
    )
    report = run(completed, monkeypatch, allow_labels=True)
    assert money(report["costs"]["all_acquisition_usd"]) == Decimal(".613")
    assert money(report["original_current_snapshot"]["known_usd"]) == Decimal("1.613")
    assert (
        report["audited_prefixes"]["study"]["event_count"]
        == len(completed["events"]) - 1
    )


def test_bootstrap_uses_whole_tasks_and_predeclared_seed(completed, monkeypatch):
    actual, captured = analysis.bootstrap.paired_bootstrap, []

    def paired(rows, repetitions, seed):
        captured.append((deepcopy(rows), repetitions, seed))
        return actual(rows, repetitions=repetitions, seed=seed)

    monkeypatch.setattr(analysis.bootstrap, "paired_bootstrap", paired)
    report = run(completed, monkeypatch, allow_labels=True)
    rows, repetitions, seed = captured[0]
    assert len(rows) == 63 and repetitions == 10000 and seed == 2026090605
    assert [row["HR_minus_source_accuracy"] for row in rows[:3]] == [1, -1, -1]
    assert report["descriptive_paired_intervals"] == actual(
        rows, repetitions=10000, seed=2026090605
    )


@pytest.mark.parametrize("completed", ["zero_source"], indirect=True)
def test_zero_source_cost_is_null_ratio_without_excluding_tasks(completed, monkeypatch):
    report = run(completed, monkeypatch, allow_labels=True)
    assert report["costs"]["fixed_HR_to_source_workflow_cost_ratio"] is None
    assert money(report["costs"]["source_workflows_usd"]) == 0
    assert report["paired_task_count"] == len(report["paired_tasks"]) == 63
    assert report["descriptive_paired_intervals"]["HR_minus_source_cost_usd"][
        "mean"
    ] == pytest.approx(0.003)


@pytest.mark.parametrize(
    "raw", [b'{"expected":"M01","expected":"M02"}', b'{"expected":NaN}']
)
def test_ambiguous_json_is_rejected(raw):
    with pytest.raises(PilotError):
        analysis._json(raw)


def test_independent_audit_exception_cannot_be_replaced_by_saved_complete_flags(
    completed, monkeypatch
):
    def reject(*args):
        raise PilotError("independent native evidence failed")

    monkeypatch.setattr(analysis.audit, "audit_phase", reject)
    with pytest.raises(PilotError, match="independent native"):
        run(completed, monkeypatch, allow_labels=False)
