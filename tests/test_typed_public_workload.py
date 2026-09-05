"""Synthetic public-contract tests; never read a real QA question or label."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from bench import typed_public_workload as w
from bench.openrouter_matrix import load_module
from bench.openrouter_pilot import PilotError, validate_response_format
from bench.openrouter_rules_protocol import prompt_constants, workflow_call


def task():
    return {
        "task_id": "hotpot_" + "1" * 24,
        "prompt": "Where did Ada work?",
        "meta": {
            "paragraphs": [
                {"title": f"Document {i}", "sentences": [f"Synthetic paragraph {i}."]}
                for i in range(10)
            ]
        },
    }


def attention():
    return load_module("public_test_attention", w.ROOT / "python/agentc/_attention.py")


def envelope(value):
    return json.dumps({"result": value})


def build(stage="filter", history=None, item=None, **kwargs):
    return w.call(
        task() if item is None else item,
        stage,
        {} if history is None else history,
        attention(),
        model=kwargs.pop("model", "source"),
        run_identity="unit",
        **kwargs,
    )


@pytest.mark.parametrize("stage", w.STAGES)
def test_native_schema_subset_and_verbatim_application(stage):
    validate_response_format(w.schema_for(stage))
    schema = w.schema_for(stage)["json_schema"]["schema"]
    assert schema == {
        "type": "object",
        "properties": {"result": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        "required": ["result"],
        "additionalProperties": False,
    }
    value = ' \n A title: "quoted text" \t π \\ '
    raw = "\n" + envelope(value) + "\t"
    assert w.parse(stage, raw) == value
    state = w.application_text(raw, stage=stage)
    assert w.parse_stage(stage, state) == value
    assert w.application_text(state, stage=stage) == state


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "prose",
        "{}",
        "[]",
        "null",
        '{"result":1}',
        '{"result":true}',
        '{"result":false}',
        '{"result":[]}',
        '{"result":{}}',
        '{"result":null}',
        '{"result":""}',
        '{"result":" \\n\\t"}',
        '{"result":"a","result":"b"}',
        '{"result":"x","extra":1}',
        '{"result":"x"} trailing',
        '{"result":NaN}',
        '{"result":Infinity}',
        '{"result":-Infinity}',
        '{"result":"\\ud800"}',
        '```json\n{"result":"x"}\n```',
        '{"result":"x"}\n{"result":"y"}',
    ],
)
def test_invalid_output_fails_closed(raw):
    for stage in w.STAGES:
        assert w.parse_stage(stage, raw) is None
        assert w.application_text(raw, stage=stage) == w.ERROR


@pytest.mark.parametrize(
    "status",
    [
        "length",
        "refusal",
        "content_filter",
        "error",
        "tool_calls",
        "model_context_window_exceeded",
        "end_turn",
        None,
    ],
)
def test_non_normal_status_never_repaired(status):
    assert w.parse_stage("answer", envelope("valid"), finish_reason=status) is None
    assert w.application_text(envelope("valid"), status) == w.ERROR


def test_exact_byte_bound_and_nonfinite_nested_raw_limits():
    assert w.parse_stage("filter", envelope("π" * 4096)) == "π" * 4096
    assert w.parse_stage("filter", envelope("π" * 4097)) is None
    assert w.parse_stage("filter", " " * 65537 + envelope("x")) is None
    assert w.parse_stage("filter", '{"result":{"nested":NaN}}') is None
    assert (
        w.parse_stage("answer", envelope("The sentence is false."))
        == "The sentence is false."
    )


def test_label_blindness_and_ast_only_legacy_prompts():
    before = set(sys.modules)
    original = task()
    contaminated = deepcopy(original)
    contaminated["expected"] = {"secret": object()}
    contaminated["meta"].update(gold_answer=object(), type=object(), level=object())
    for paragraph in contaminated["meta"]["paragraphs"]:
        paragraph["supporting"] = object()
    assert w.public_task(contaminated) == original
    assert build(item=contaminated) == build(item=original)
    base = prompt_constants()
    assert all(w.prompts()[k].startswith(v) for k, v in base.items())
    assert "bench.agents.research_planner" not in set(sys.modules) - before


@pytest.mark.parametrize("field", ["prompt", "title", "sentence"])
def test_public_unpaired_surrogate_rejected_before_dispatch(field):
    item = task()
    if field == "prompt":
        item["prompt"] = "invalid \ud800"
    elif field == "title":
        item["meta"]["paragraphs"][0]["title"] = "invalid \ud800"
    else:
        item["meta"]["paragraphs"][0]["sentences"] = ["invalid \ud800"]
    with pytest.raises(PilotError):
        build(item=item)


def test_original_natural_workflow_topology_and_public_attention():
    history = {
        "filter": envelope("  Title: actual selected text \n"),
        "synthesize": envelope(" A draft answer. "),
    }
    for stage, count in zip(w.STAGES, (12, 3, 4)):
        prior = {key: history[key] for key in w.STAGES[: w.STAGES.index(stage)]}
        call = build(stage, prior)
        legacy = workflow_call(
            task(),
            stage,
            {key: w.parse_stage(key, value) for key, value in prior.items()},
            attention(),
            run_identity="unit",
        )
        assert call["messages"][1:] == legacy["messages"][1:]
        assert len(call["messages"]) == count
        assert call["input_deps"] == legacy["input_deps"]
        extra = call["parameters"]["extra"]
        assert (
            extra["window_state_reads"]
            == legacy["parameters"]["extra"]["window_state_reads"]
        )
        assert (
            extra["attention_scores"]
            == attention().compute_attention_scores(call["messages"], None)[0]
        )
        assert extra["agentc_route_context"]["structured_outputs"] is True
        assert "input_tokens_upper_bound" not in extra["agentc_route_context"]
        assert call["parameters"]["max_output_tokens"] == 512
        assert call["messages"][-1]["content"] == "Question: " + task()["prompt"]


def test_blank_input_sentences_preserve_exact_order_and_legacy_rendering():
    item = task()
    sentences = ["", "  \t", "Original sentence.", "\n", "", "Last sentence."]
    item["meta"]["paragraphs"][0]["sentences"] = sentences
    original = deepcopy(item)
    assert w.public_task(item) == original
    call = build(item=item)
    legacy = workflow_call(item, "filter", {}, attention(), run_identity="unit")
    assert item == original
    assert len(call["messages"]) == 12
    assert call["messages"][1:] == legacy["messages"][1:]
    assert call["messages"][1]["content"] == "Document 0\n" + " ".join(sentences)
    assert w.parse_stage("filter", envelope("  \t\n")) is None


def test_invalid_history_contagious_even_when_later_answer_is_plausible():
    history = {"filter": w.ERROR, "synthesize": envelope("plausible rescue")}
    assert not w.history_valid(task(), "answer", history)
    call = build("answer", history)
    assert (
        call["messages"][1]["content"]
        == call["messages"][2]["content"]
        == w.ERROR_INPUT
    )
    assert (
        call["parameters"]["extra"]["typed_public_contract"]["upstream_valid"] is False
    )
    assert w.application_text(envelope("correct"), upstream_valid=False) == w.ERROR
    assert w.score_answer(envelope("correct"), "correct", history=history) == {
        "em": 0,
        "f1": 0,
    }
    with pytest.raises(PilotError):
        build("answer", {"synthesize": envelope("x")})


def test_callsite_hash_changes_only_with_contract_not_labels_or_model():
    original = build()
    assert build(model="different")["call_site_id"] == original["call_site_id"]
    changed = w.prompts()
    changed["FILTER_SYSTEM"] += " change"
    assert build(prompts=changed)["call_site_id"] != original["call_site_id"]
    assert original["call_site_id"].startswith(w.WORKLOAD + "/")
    with pytest.raises(PilotError):
        w.contract_hash({})


@pytest.mark.parametrize(
    "prediction,gold,expected",
    [
        ("The Eiffel Tower.", "eiffel tower", {"em": 1, "f1": 1}),
        ("Ada Ada Lovelace", "Ada Lovelace", {"em": 0, "f1": 0.8}),
        ("yes", "YES!", {"em": 1, "f1": 1}),
        ("yes indeed", "yes", {"em": 0, "f1": 0}),
        ("no", "no answer", {"em": 0, "f1": 0}),
        ("the", "a", {"em": 1, "f1": 0}),
        ("Noanswer", "noanswer", {"em": 1, "f1": 1}),
    ],
)
def test_official_answer_metric_vectors(prediction, gold, expected):
    assert w.score_answer(envelope(prediction), gold) == expected


def test_pinned_official_evaluator_parity_when_supplied(monkeypatch):
    source = os.environ.get("AGENTC_HOTPOT_EVALUATOR")
    if not source:
        pytest.skip(
            "supply the verified official evaluator; never download in unit tests"
        )
    path = Path(source)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == w.EVALUATOR["sha256"]
    # The official file imports ujson for its CLI's file IO. Metric functions do
    # not use it; supply stdlib JSON for that unused import without a dependency.
    monkeypatch.setitem(sys.modules, "ujson", json)
    spec = importlib.util.spec_from_file_location("pinned_hotpot_metric", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    values = [
        "The Eiffel Tower.",
        "eiffel tower",
        "Ada Ada Lovelace",
        "Ada Lovelace",
        "yes",
        "YES!",
        "yes indeed",
        "no",
        "noanswer",
        "the",
        "a",
        "π",
        "one-two",
        "one two",
        "",
    ]
    for prediction in values:
        for gold in values:
            if not prediction:
                continue  # empty result is application-invalid, independent of metric
            score = w.score_answer(envelope(prediction), gold)
            assert score["em"] == module.exact_match_score(prediction, gold)
            assert score["f1"] == module.f1_score(prediction, gold)[0]


def test_actual_native_state_drop_and_ambiguous_provenance_when_supplied():
    binary = os.environ.get("AGENTC_TYPED_MATERIALIZER")
    if not binary:
        pytest.skip("supply frozen materializer; this test never builds")
    assert (
        hashlib.sha256(Path(binary).read_bytes()).hexdigest()
        == "b260a39b1baedbe0a4cc45895c6041ec5bfcfdbddb3c7f342c38ed3879fe0122"
    )
    for history, expected, reason in [
        (
            {
                "filter": envelope("selected passage"),
                "synthesize": envelope("draft answer"),
            },
            [0, 2, 3],
            None,
        ),
        (
            {"filter": w.ERROR, "synthesize": w.ERROR},
            [0, 1, 2, 3],
            "ambiguous_provenance",
        ),
    ]:
        call = build("answer", history)
        result = subprocess.run(
            [binary],
            input=json.dumps({"rule": "StateDrop", "call": call}),
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
        report = json.loads(result.stdout)
        assert report["retained_indices"] == expected
        assert report["abstention_reason"] == reason
        assert report["call"]["messages"] == [call["messages"][i] for i in expected]
        assert report["call"]["parameters"] == call["parameters"]
    call = build()
    result = subprocess.run(
        [binary],
        input=json.dumps({"rule": "ContextCompress", "call": call}),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    report = json.loads(result.stdout)
    assert report["retained_indices"] == list(range(12))
    assert report["abstention_reason"] == "not_applicable"


def test_actual_native_context_compress_preserves_schema_when_supplied():
    binary = os.environ.get("AGENTC_TYPED_MATERIALIZER")
    if not binary:
        pytest.skip("supply frozen materializer; this test never builds")
    assert (
        hashlib.sha256(Path(binary).read_bytes()).hexdigest()
        == "b260a39b1baedbe0a4cc45895c6041ec5bfcfdbddb3c7f342c38ed3879fe0122"
    )
    item = task()
    # Long synthetic fixture exercises mechanics only. Production sampling must
    # never pad or filter public contexts to make ContextCompress applicable.
    for index, paragraph in enumerate(item["meta"]["paragraphs"]):
        paragraph["sentences"] = [
            (
                "Ada work workplace. "
                if index < 2
                else "Volcano basalt mineral eruption. "
            )
            * 80
        ]
    item["meta"]["paragraphs"][0]["sentences"].extend(["", " \t\n"])
    call = build(item=item)
    legacy = workflow_call(item, "filter", {}, attention(), run_identity="unit")
    assert call["messages"][1:] == legacy["messages"][1:]
    result = subprocess.run(
        [binary],
        input=json.dumps({"rule": "ContextCompress", "call": call}),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    report = json.loads(result.stdout)
    assert report["applied"] is True
    indices = report["retained_indices"]
    assert len(indices) < 12 and indices[0] == 0 and indices[-1] == 11
    assert report["call"]["messages"] == [call["messages"][i] for i in indices]
    assert report["call"]["parameters"] == call["parameters"]
