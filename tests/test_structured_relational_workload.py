"""No-network contract tests; reference solver exists only in this test module."""

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from bench import structured_relational_workload as workload
from bench.openrouter_joint_study import restrict_call
from bench.openrouter_matrix import PROTOCOL
from bench.openrouter_pilot import PilotError


def attention():
    return SimpleNamespace(
        compute_attention_scores=lambda messages, _: ([0.5] * len(messages), [])
    )


def reference(task):
    """Independent evaluator oracle. Never imported by acquisition/controller."""
    tenant, req = re.search(
        r"Tenant (\w+); requisition (R\d{3})", task["prompt"]
    ).groups()
    paragraphs = [
        p
        for p in task["meta"]["paragraphs"]
        if p["sentences"][0].startswith(f"tenant={tenant}|")
    ]
    tables = {}
    for paragraph in paragraphs:
        kind = paragraph["sentences"][0].split("table=")[1]
        tables[kind] = [
            dict(field.split("=") for field in row.split("|"))
            for row in paragraph["sentences"][1:]
        ]
    request = next(row for row in tables["REQUESTS"] if row["req"] == req)
    catalog = next(row for row in tables["CATALOG"] if row["sku"] == request["sku"])
    offer = next(row for row in tables["OFFERS"] if row["vendor"] == catalog["vendor"])
    total = int(request["qty"]) * int(offer["unit"])
    policy = re.search(
        rf"{request['region']}: total<=(\d+)->(M\d+); \d+<total<=(\d+)->(M\d+); total>\d+->(M\d+)",
        task["prompt"],
    )
    low, first, high, middle, last = policy.groups()
    answer = first if total <= int(low) else middle if total <= int(high) else last
    return {
        "filter": "|".join(sorted(p["title"] for p in paragraphs)),
        "synthesize": f"sku={request['sku']}|vendor={catalog['vendor']}|qty={request['qty']}|unit={offer['unit']}|total={total}|region={request['region']}",
        "answer": answer,
    }


def build(task, stage, history, **kwargs):
    return workload.call(
        task,
        stage,
        history,
        attention(),
        model=kwargs.pop("model", "source"),
        run_identity=kwargs.pop("run_identity", "test"),
        **kwargs,
    )


def test_generator_determinism_prefix_separation_and_solved_public_relations():
    tasks = workload.make_tasks(73519, 131)
    assert tasks == workload.make_tasks(73519, 131)
    assert tasks[:6] == workload.make_tasks(73519, 6)
    assert not {t["task_id"] for t in tasks} & {
        t["task_id"] for t in workload.make_tasks(73520, 6)
    }
    assert len({t["task_id"] for t in tasks}) == 131
    assert len({t["expected"] for t in tasks}) > 30
    for task in tasks:
        history = reference(task)
        assert history["answer"] == task["expected"]
        assert workload.score_answer(
            history["answer"], task["expected"], history=history
        ) == {"em": 1.0, "f1": 1.0}
        assert len(task["meta"]["paragraphs"]) == 20
        assert set(task) == {"task_id", "prompt", "meta", "expected"}
        assert set(task["meta"]) == {"paragraphs"}
    assert workload.make_tasks(1, 0) == []


@pytest.mark.parametrize(
    "seed,count", [(True, 1), (1, True), (1, -1), ("1", 2), (1, 1.5)]
)
def test_generator_invalid_allocation(seed, count):
    with pytest.raises(PilotError):
        workload.make_tasks(seed, count)


@pytest.mark.parametrize(
    "stage,text",
    [
        ("filter", "D01|D03|D20"),
        ("synthesize", "sku=K100|vendor=V999|qty=7|unit=13|total=91|region=EU"),
        ("answer", "M01"),
        ("answer", "M99"),
    ],
)
def test_grammar_valid(stage, text):
    assert workload.parse(stage, text) is not None


@pytest.mark.parametrize(
    "stage,text",
    [
        ("filter", "D03|D01|D20"),
        ("filter", "D01|D01|D20"),
        ("filter", "D00|D01|D20"),
        ("filter", "D01|D02|D21"),
        ("filter", "D01|D02"),
        ("filter", "D01|D02|D03|D04"),
        ("filter", "d01|D02|D03"),
        ("filter", "D01 |D02|D03"),
        ("synthesize", "sku=K100|vendor=V999|qty=07|unit=13|total=91|region=EU"),
        ("synthesize", "sku=K100|vendor=V999|qty=7|unit=13|total=91.0|region=EU"),
        ("synthesize", "sku=K100|vendor=V999|qty=7|unit=13|total=91|region=CA"),
        ("synthesize", "vendor=V999|sku=K100|qty=7|unit=13|total=91|region=EU"),
        ("answer", "M00"),
        ("answer", "M001"),
        ("answer", "m01"),
        ("answer", "Ｍ01"),
        ("answer", "M01\n"),
        ("answer", "M01\r"),
        ("answer", " M01"),
        ("answer", "M01 "),
        ("answer", "M01."),
        ("answer", "`M01`"),
        ("answer", "M01\x00"),
        ("answer", "The answer is M01"),
        ("answer", "M01\nM02"),
        ("answer", None),
        ("filter", workload.ERROR),
        ("synthesize", workload.ERROR),
        ("answer", workload.ERROR),
    ],
)
def test_grammar_rejects_without_repair(stage, text):
    assert workload.parse(stage, text) is None


def test_parser_is_not_an_arithmetic_or_selection_oracle():
    # Deliberately incorrect multiplication passes syntax; no hidden gold check.
    assert (
        workload.parse(
            "synthesize", "sku=K100|vendor=V999|qty=7|unit=13|total=999|region=EU"
        )["total"]
        == 999
    )
    task = workload.make_tasks(19, 1)[0]
    wrong = (
        "D01|D02|D03" if reference(task)["filter"] != "D01|D02|D03" else "D04|D05|D06"
    )
    assert workload.history_valid(task, "synthesize", {"filter": wrong})


def test_public_seam_strips_gold_and_does_not_mutate_inputs():
    task = workload.make_tasks(20, 1)[0]
    history = reference(task)
    poisoned = deepcopy(task)
    poisoned["expected"] = "UNIQUE_GOLD_SENTINEL"
    poisoned["solution"] = "UNIQUE_GOLD_SENTINEL"
    poisoned["meta"]["supporting_ids"] = "UNIQUE_GOLD_SENTINEL"
    for paragraph in poisoned["meta"]["paragraphs"]:
        paragraph["supporting"] = "UNIQUE_GOLD_SENTINEL"
    before = deepcopy(poisoned)
    for i, stage in enumerate(workload.STAGES):
        prior = {s: history[s] for s in workload.STAGES[:i]}
        assert build(task, stage, prior) == build(poisoned, stage, prior)
        assert "UNIQUE_GOLD_SENTINEL" not in json.dumps(build(poisoned, stage, prior))
    assert poisoned == before


def test_three_stage_own_history_and_state_drop_contract():
    task = workload.make_tasks(21, 1)[0]
    history = reference(task)
    synth = build(task, "synthesize", {"filter": history["filter"]})
    answer = build(task, "answer", {k: history[k] for k in ("filter", "synthesize")})
    assert synth["messages"][1]["content"] == answer["messages"][1]["content"]
    assert len(answer["messages"][1]["content"]) > 1700
    assert answer["messages"][2]["content"] == history["synthesize"]
    assert answer["input_deps"][1] == {"kind": "state", "key": "selected_records"}
    assert answer["parameters"]["extra"]["window_state_reads"] == ["synthesis"]
    assert synth["parameters"]["extra"]["window_state_reads"] == ["selected_records"]
    changed_history = {
        "filter": "D01|D02|D03",
        "synthesize": "sku=K999|vendor=V999|qty=9|unit=9|total=81|region=US",
    }
    changed = build(task, "answer", changed_history)
    assert changed["messages"][2]["content"] == changed_history["synthesize"]
    assert changed["messages"][2] != answer["messages"][2]
    ids = workload.parse("filter", changed_history["filter"])
    for paragraph in task["meta"]["paragraphs"]:
        if paragraph["title"] in ids:
            assert (
                "\n".join(paragraph["sentences"]) in changed["messages"][1]["content"]
            )


def test_invalid_upstream_contagious_but_not_crashing_or_gold_repaired():
    task = workload.make_tasks(22, 1)[0]
    good = reference(task)
    synth = build(task, "synthesize", {"filter": "D03|D02|D01"})
    assert synth["messages"][1]["content"] == workload.ERROR_INPUT
    assert (
        synth["parameters"]["extra"]["structured_relational_contract"]["upstream_valid"]
        is False
    )
    for history in (
        {"filter": workload.ERROR, "synthesize": good["synthesize"]},
        {"filter": good["filter"], "synthesize": workload.ERROR},
    ):
        answer = build(task, "answer", history)
        assert answer["messages"][2]["content"] == workload.ERROR_INPUT
        assert workload.ERROR_INPUT in answer["messages"][-1]["content"]
        assert not workload.history_valid(task, "answer", history)
        # A lucky final guess cannot recover a malformed application history.
        assert workload.score_answer(
            task["expected"], task["expected"], history=history
        ) == {"em": 0.0, "f1": 0.0}


@pytest.mark.parametrize(
    "stage,history",
    [
        ("filter", {"filter": "D01|D02|D03"}),
        ("answer", {}),
        ("synthesize", {"filter": 1}),
        ("not_a_stage", {}),
    ],
)
def test_exact_preceding_history_is_required(stage, history):
    with pytest.raises(PilotError):
        build(workload.make_tasks(23, 1)[0], stage, history)


def test_shape_and_plan_identity_stable_across_cases_models_and_histories():
    sites = {stage: set() for stage in workload.STAGES}
    for task in workload.make_tasks(24, 12):
        reference_history = reference(task)
        trace_ids, spans = set(), set()
        for i, stage in enumerate(workload.STAGES):
            history = {s: reference_history[s] for s in workload.STAGES[:i]}
            call = build(task, stage, history)
            assert (
                len(call["messages"])
                == {"filter": 22, "synthesize": 3, "answer": 4}[stage]
            )
            assert call["messages"][-1]["content"] == "Question: " + task["prompt"]
            assert call["input_deps"] == call["parameters"]["extra"]["message_deps"]
            assert call["parameters"]["max_output_tokens"] == 512
            assert (
                call["parameters"]["extra"]["agentc_route_context"]["provider_protocol"]
                == PROTOCOL
            )
            assert (
                call["parameters"]["extra"]["agentc_route_context"][
                    "structured_outputs"
                ]
                is False
            )
            assert (
                build(task, stage, history, model="target")["call_site_id"]
                == call["call_site_id"]
            )
            sites[stage].add(call["call_site_id"])
            trace_ids.add(call["trace_id"])
            spans.add(call["span_id"])
            bounded = restrict_call(call, stage)
            assert ("attention_scores" in bounded["parameters"]["extra"]) == (
                stage == "filter"
            )
        assert len(trace_ids) == 1 and len(spans) == 3
    assert all(len(values) == 1 for values in sites.values())
    assert len(set.union(*sites.values())) == 3


def test_namespace_binds_actual_prompt_source_and_feedback_contract(monkeypatch):
    original = workload.contract_hash()
    values = workload.prompts()
    values["ANSWER_SYSTEM"] += " Another actual instruction."
    assert workload.contract_hash(values) != original
    task = workload.make_tasks(25, 1)[0]
    assert (
        workload.contract_hash(values)
        in build(task, "filter", {}, prompts=values)["call_site_id"]
    )
    assert workload.prompts()["ANSWER_SYSTEM"] != values["ANSWER_SYSTEM"]
    original_bytes = Path(workload.__file__).read_bytes()
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: original_bytes + b"\n# changed parser"
    )
    assert workload.contract_hash() != original
    monkeypatch.setattr(Path, "read_bytes", lambda self: original_bytes)
    monkeypatch.setattr(workload, "FEEDBACK_CONTRACT", "different-comparator")
    assert workload.contract_hash() != original
    with pytest.raises(PilotError):
        workload.contract_hash({"FILTER_SYSTEM": "missing"})
    assert len(hashlib.sha256(original_bytes).hexdigest()) == 64


def test_real_attention_has_context_compression_opportunity_without_support_labels():
    path = Path(__file__).resolve().parents[1] / "python/agentc/_attention.py"
    spec = importlib.util.spec_from_file_location("structured_attention_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for task in workload.make_tasks(26, 6):
        call = workload.call(
            task, "filter", {}, module, model="source", run_identity="attention"
        )
        assert sum(len(m["content"].encode()) for m in call["messages"]) > 8192
        extra = call["parameters"]["extra"]
        relevant = set(reference(task)["filter"].split("|"))
        droppable = 0
        for message, score in zip(
            call["messages"][1:-1], extra["attention_scores"][1:-1]
        ):
            protected = any(
                token.lower() in message["content"].lower()
                for token in extra["follow_on_tokens"]
            )
            if message["content"].splitlines()[0] in relevant:
                assert protected
            if score <= 0.15 and not protected:
                droppable += 1
        assert droppable >= 7


def test_strict_scoring_no_alias_partial_normalization_or_inconsistent_history():
    assert workload.score_answer("M01", "M01") == {"em": 1.0, "f1": 1.0}
    for output in ("M02", "m01", "M01\n", "M01.", "The answer is M01", workload.ERROR):
        assert workload.score_answer(output, "M01") == {"em": 0.0, "f1": 0.0}
    history = {
        "filter": "D01|D02|D03",
        "synthesize": "sku=K100|vendor=V999|qty=7|unit=13|total=91|region=EU",
        "answer": "M02",
    }
    assert workload.score_answer("M01", "M01", history=history) == {
        "em": 0.0,
        "f1": 0.0,
    }
    with pytest.raises(PilotError):
        workload.score_answer("M01", "M01", history={})
