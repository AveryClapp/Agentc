"""Public-fact analysis only: no requests, ledger access, or runtime feedback."""

from copy import deepcopy
import json
import shutil

import pytest

from bench import openrouter_pilot as pilot
from bench import openrouter_typed_error_trace as trace

SOURCE = trace.study.ROOT / "bench/repro/openrouter-typed-factorial-2026-09-05"
SHA = "4e30338f35205059a2b3d5b32fe41ee2b771b4f35eeea18bc194b5730b2ac148"


def task(index=0):
    return json.loads((SOURCE / "fixture.json").read_text())[index]


def rows(values):
    return {
        stage: {
            "id": "synthetic-" + stage,
            "generation_id": "synthetic-gen-" + stage,
            "answer": json.dumps({"result": value}),
            "finish_reason": "stop",
        }
        for stage, value in values.items()
    }


def test_public_facts_ignore_labels_and_reconstruct_all_fixture_answers():
    for item in json.loads((SOURCE / "fixture.json").read_text()):
        before = deepcopy(item)
        solved = trace.public_facts(item)
        assert solved["approval"]["approver"] == item["expected"]
        item["expected"] = "POISON-EVALUATOR-ONLY"
        item["labels"] = {"filter": "POISON"}
        assert trace.public_facts(item) == solved
        assert len(solved["join_witness"]) == 3
        assert len(solved["filter"]) == 3
        assert (
            solved["synthesis"]["total"]
            == solved["synthesis"]["qty"] * solved["synthesis"]["unit"]
        )
        assert before["prompt"] == item["prompt"]


@pytest.mark.parametrize(
    "position, tier", [(0, "low"), (1, "middle"), (2, "middle"), (3, "high")]
)
def test_inclusive_public_policy_bounds(position, tier):
    facts = trace.public_facts(task())
    policy = facts["policy"]["EU"]
    total = [policy["low"], policy["low"] + 1, policy["high"], policy["high"] + 1][
        position
    ]
    value = trace._policy(facts["policy"], {"region": "EU", "total": total})
    assert value["tier"] == tier


def test_wrong_join_propagation_differs_from_compensating_policy_error():
    item = task(18)
    facts = trace.public_facts(item)
    values = {
        "filter": facts["filter"],
        "synthesize": {**facts["synthesis"], "unit": 11, "total": 220},
        "answer": "M56",
    }
    propagated = trace.trace_cell(item, facts, rows(values))
    assert facts["synthesis"]["unit"] == 57 and facts["synthesis"]["total"] == 1140
    assert propagated["classification"] == "propagated_upstream_error"
    assert propagated["answer_matches_actual_synthesis_policy"] is True
    assert propagated["synthesis_differing_fields"] == ["unit", "total"]
    assert (
        "vendor=V759|unit=11"
        in propagated["actual_unit_matches_selected_offer_rows"][0]["text"]
    )
    values["answer"] = "M80"
    compensated = trace.trace_cell(item, facts, rows(values))
    assert compensated["classification"] == "final_correct_with_stage_inconsistency"
    assert compensated["correct_against_full_fixture"]["answer"] is True
    assert compensated["answer_matches_actual_synthesis_policy"] is False


def test_invalid_upstream_remains_a_failure_and_is_not_repaired():
    item = task()
    facts = trace.public_facts(item)
    values = {
        "filter": None,
        "synthesize": facts["synthesis"],
        "answer": facts["approval"]["approver"],
    }
    result = trace.trace_cell(item, facts, rows(values))
    assert result["classification"] == "invalid_application_path"
    assert result["correct_against_full_fixture"]["answer"] is False
    assert result["synthesis_required_by_actual_selection"]["value"] is None


def test_complete_primary_audit_without_network_ledger_or_input_mutation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("post-hoc audit attempted network/ledger/dispatch")

    monkeypatch.setattr(pilot, "request_json", forbidden)
    monkeypatch.setattr(pilot.Ledger, "read", forbidden)
    monkeypatch.setattr(pilot.Ledger, "locked", forbidden)
    monkeypatch.setattr(trace.study, "validate", forbidden)
    before = {name: trace.file_hash(SOURCE / name) for name in trace.FILES}
    report = trace.audit(SOURCE, SHA)
    assert report["source_artifact_sha256"] == before
    assert {name: trace.file_hash(SOURCE / name) for name in trace.FILES} == before
    assert len(report["tasks"]) == 32
    assert all(set(t["cells"]) == set(trace.study.ARMS) for t in report["tasks"])
    assert [
        report["by_arm"][a]["correct_by_stage"]["answer"] for a in trace.study.ARMS
    ] == [30, 29, 25, 31]
    assert report["tasks_with_final_errors"] == [5, 6, 8, 9, 13, 17, 19, 31]
    assert all(
        all(checks.values())
        for item in report["tasks"]
        for checks in item["within_model_input_pair_checks"].values()
    )
    policy_errors = [
        cell
        for item in report["tasks"]
        for cell in item["cells"].values()
        if not cell["answer_matches_actual_synthesis_policy"]
    ]
    assert len(policy_errors) == 13
    assert all(
        cell["chosen_policy_rows"]
        == [{"region": cell["values"]["synthesize"]["region"], "tier": "middle"}]
        for cell in policy_errors
    )


def test_saved_output_tampering_rejected_before_analysis(tmp_path):
    for name in trace.FILES:
        shutil.copyfile(SOURCE / name, tmp_path / name)
    calls = json.loads((tmp_path / "calls.json").read_text())
    calls[0]["answer"] = '{"result":null}'
    (tmp_path / "calls.json").write_text(json.dumps(calls))
    with pytest.raises(pilot.PilotError, match="digests"):
        trace.audit(tmp_path, SHA)
