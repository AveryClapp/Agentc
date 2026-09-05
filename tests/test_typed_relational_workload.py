"""Typed application contract tests. Any reference solution is test-only."""

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from bench import structured_relational_workload as legacy
from bench import typed_relational_workload as workload
from bench.openrouter_joint_study import restrict_call
from bench.openrouter_pilot import PilotError

ROOT = Path(__file__).resolve().parents[1]
SYNTHESIS = {
    "sku": "K100",
    "vendor": "V200",
    "qty": 7,
    "unit": 13,
    "total": 91,
    "region": "EU",
}


def envelope(value):
    return json.dumps({"result": value})


def attention():
    path = ROOT / "python/agentc/_attention.py"
    spec = importlib.util.spec_from_file_location("typed_attention_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reference(task):
    """Independent public-data solver; never imported by the application module."""
    tenant, req = re.search(
        r"Tenant (\w+); requisition (R\d{3})", task["prompt"]
    ).groups()
    selected = [
        p
        for p in task["meta"]["paragraphs"]
        if p["sentences"][0].startswith(f"tenant={tenant}|")
    ]
    tables = {
        p["sentences"][0].split("table=")[1]: [
            dict(field.split("=") for field in row.split("|"))
            for row in p["sentences"][1:]
        ]
        for p in selected
    }
    req_row = next(row for row in tables["REQUESTS"] if row["req"] == req)
    cat_row = next(row for row in tables["CATALOG"] if row["sku"] == req_row["sku"])
    offer = next(row for row in tables["OFFERS"] if row["vendor"] == cat_row["vendor"])
    total = int(req_row["qty"]) * int(offer["unit"])
    low, first, high, middle, last = re.search(
        rf"{req_row['region']}: total<=(\d+)->(M\d+); \d+<total<=(\d+)->(M\d+); total>\d+->(M\d+)",
        task["prompt"],
    ).groups()
    return {
        "filter": envelope(sorted(p["title"] for p in selected)),
        "synthesize": envelope(
            {
                "sku": req_row["sku"],
                "vendor": cat_row["vendor"],
                "qty": int(req_row["qty"]),
                "unit": int(offer["unit"]),
                "total": total,
                "region": req_row["region"],
            }
        ),
        "answer": envelope(
            first if total <= int(low) else middle if total <= int(high) else last
        ),
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


def test_generator_is_unchanged_and_public_solution_is_evaluator_only():
    tasks = workload.make_tasks(8821, 31)
    assert workload.VERSION == legacy.VERSION
    assert tasks == legacy.make_tasks(8821, 31)
    assert tasks[:6] == workload.make_tasks(8821, 6)
    assert workload.make_tasks(2026090501, 6) == json.loads(
        (
            ROOT / "bench/repro/openrouter-structured-pilot-2026-09-05/fixture.json"
        ).read_text()
    )
    for task in tasks:
        history = reference(task)
        assert workload.parse_answer(history["answer"]) == task["expected"]
        assert workload.score_answer(
            history["answer"], task["expected"], history=history
        ) == {"em": 1.0, "f1": 1.0}


@pytest.mark.parametrize(
    "stage,value",
    [
        ("filter", ["D01", "D03", "D20"]),
        ("synthesize", SYNTHESIS),
        ("answer", "M01"),
        ("answer", "M99"),
    ],
)
def test_valid_envelopes_and_json_whitespace(stage, value):
    raw = " \n" + envelope(value) + "\t\r\n"
    parsed = workload.parse(stage, raw)
    assert parsed == (tuple(value) if stage == "filter" else value)
    canonical = workload.application_text(raw, stage=stage)
    assert canonical == workload.application_text(raw)
    assert canonical == workload.application_text(canonical)
    assert workload.application_disagreement(stage, raw, canonical) == 0.0
    assert workload.normalized_disagreement(raw, canonical) == 0.0


@pytest.mark.parametrize(
    "raw",
    [
        None,
        1,
        True,
        "",
        "null",
        "[]",
        '"M01"',
        '{"result":null}',
        '{"result":"M01","extra":1}',
        "{}",
        '{"result":"M01","result":"M02"}',
        '{"result":"M01","re\\u0073ult":"M01"}',
        '{"result":"M01"} trailing',
        'explanation {"result":"M01"}',
        '```json\n{"result":"M01"}\n```',
        '{"result":NaN}',
        '{"result":Infinity}',
        '{"result":-Infinity}',
        '{"result":1e309}',
        '{"result":true}',
        '{"result":01}',
        '{"result":"M01",}',
        '{"result":"Ｍ01"}',
        '{"result":"m01"}',
        '{"result":"M00"}',
        '{"result":"M100"}',
        '{"result":"M01\\n"}',
        '{"result":"M01."}',
        '{"result":" M01"}',
        '{"result":"M01","nested":{"a":1,"a":2}}',
        '{"result":' + "[" * 1100 + "0" + "]" * 1100 + "}",
        " " * 65537,
    ],
)
def test_invalid_raw_cannot_be_repaired_or_agree_as_success(raw):
    assert workload.application_text(raw) == workload.ERROR
    assert workload.normalized_disagreement(raw, raw) == 1.0
    for stage in workload.STAGES:
        assert workload.parse_stage(stage, raw) is None


@pytest.mark.parametrize(
    "stage,value",
    [("filter", ["D01", "D02", "D03"]), ("synthesize", SYNTHESIS), ("answer", "M01")],
)
@pytest.mark.parametrize(
    "reason",
    [
        "length",
        "content_filter",
        "refusal",
        "max_tokens",
        "error",
        "tool_calls",
        "end_turn",
        None,
        True,
    ],
)
def test_non_normal_finish_is_invalid_even_when_content_parses(stage, value, reason):
    raw = envelope(value)
    assert workload.application_text(raw, reason, stage=stage) == workload.ERROR
    assert workload.parse_stage(stage, raw, finish_reason=reason) is None
    assert (
        workload.application_disagreement(
            stage, raw, raw, reference_finish_reason=reason
        )
        == 1.0
    )
    assert (
        workload.application_disagreement(
            stage, raw, raw, candidate_finish_reason=reason
        )
        == 1.0
    )


@pytest.mark.parametrize(
    "value",
    [
        ["D01", "D03"],
        ["D01", "D02", "D03", "D04"],
        ["D03", "D01", "D02"],
        ["D01", "D01", "D03"],
        ["D00", "D01", "D03"],
        ["D01", "D03", "D21"],
        ["D01", "D03", "d20"],
        ["D01", "D03", 20],
        ["D01", "D03", []],
    ],
)
def test_filter_exact_public_grammar_length_and_order(value):
    assert workload.parse_filter(envelope(value)) is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("qty", 0),
        ("qty", 1000),
        ("qty", True),
        ("qty", 7.0),
        ("qty", "7"),
        ("unit", -1),
        ("unit", 10000),
        ("unit", False),
        ("total", 10000000),
        ("total", "91"),
        ("region", "eu"),
        ("region", ["EU"]),
        ("sku", "K12"),
        ("sku", "K100\n"),
        ("vendor", "v200"),
    ],
)
def test_synthesis_domain_and_exact_types_without_coercion(key, value):
    assert workload.parse_synthesis(envelope({**SYNTHESIS, key: value})) is None


def test_synthesis_fields_duplicates_and_arithmetic_not_an_oracle():
    assert workload.parse_synthesis(envelope({**SYNTHESIS, "extra": 1})) is None
    assert (
        workload.parse_synthesis(
            envelope({k: v for k, v in SYNTHESIS.items() if k != "unit"})
        )
        is None
    )
    raw = envelope(SYNTHESIS).replace('"qty": 7', '"qty": 7, "qty": 8')
    assert workload.parse_synthesis(raw) is None
    wrong = {**SYNTHESIS, "total": 999}
    assert workload.parse_synthesis(envelope(wrong)) == wrong
    assert (
        workload.application_disagreement(
            "synthesize", envelope(SYNTHESIS), envelope(wrong)
        )
        == 1.0
    )
    reordered = dict(reversed(list(SYNTHESIS.items())))
    assert (
        workload.application_disagreement(
            "synthesize", envelope(SYNTHESIS), envelope(reordered)
        )
        == 0.0
    )
    assert (
        workload.normalized_disagreement(
            envelope("M01"), envelope(["D01", "D02", "D03"])
        )
        == 1.0
    )


def test_public_only_call_seam_and_wrong_selection_remain_label_blind():
    task = workload.make_tasks(8822, 1)[0]
    history = reference(task)
    poisoned = deepcopy(task)
    poisoned["expected"] = "UNIQUE_GOLD_SENTINEL"
    poisoned["solution"] = "UNIQUE_GOLD_SENTINEL"
    poisoned["meta"]["supporting_ids"] = "UNIQUE_GOLD_SENTINEL"
    for p in poisoned["meta"]["paragraphs"]:
        p["supporting"] = "UNIQUE_GOLD_SENTINEL"
    before = deepcopy(poisoned)
    for i, stage in enumerate(workload.STAGES):
        prior = {s: history[s] for s in workload.STAGES[:i]}
        assert build(task, stage, prior) == build(poisoned, stage, prior)
        assert "UNIQUE_GOLD_SENTINEL" not in json.dumps(build(poisoned, stage, prior))
    assert poisoned == before
    wrong = (
        ["D01", "D02", "D03"]
        if workload.parse_filter(history["filter"]) != ("D01", "D02", "D03")
        else ["D04", "D05", "D06"]
    )
    assert workload.history_valid(task, "synthesize", {"filter": envelope(wrong)})


def test_invalid_upstream_is_contagious_and_lucky_answer_gets_no_credit():
    task = workload.make_tasks(8823, 1)[0]
    history = reference(task)
    for prior in (
        {"filter": workload.ERROR, "synthesize": history["synthesize"]},
        {"filter": history["filter"], "synthesize": workload.ERROR},
    ):
        assert not workload.history_valid(task, "answer", prior)
        answer = build(task, "answer", prior)
        assert answer["messages"][1]["content"] == workload.ERROR_INPUT
        assert answer["messages"][2]["content"] == workload.ERROR_INPUT
        assert workload.ERROR_INPUT in answer["messages"][-1]["content"]
        assert not answer["parameters"]["extra"]["typed_relational_contract"][
            "upstream_valid"
        ]
        assert workload.score_answer(
            envelope(task["expected"]), task["expected"], history=prior
        ) == {"em": 0.0, "f1": 0.0}
    assert (
        workload.application_disagreement(
            "answer", envelope("M01"), envelope("M01"), upstream_valid=False
        )
        == 1.0
    )
    assert (
        workload.application_disagreement(
            "answer", envelope("M01"), envelope("M01"), upstream_valid=1
        )
        == 1.0
    )


def test_shapes_state_dependencies_stage_locality_and_schema_preservation():
    sites = {stage: set() for stage in workload.STAGES}
    for task in workload.make_tasks(8824, 6):
        solved = reference(task)
        calls = {}
        for i, stage in enumerate(workload.STAGES):
            history = {s: solved[s] for s in workload.STAGES[:i]}
            call = build(task, stage, history)
            calls[stage] = call
            assert (
                len(call["messages"])
                == {"filter": 22, "synthesize": 3, "answer": 4}[stage]
            )
            assert call["input_deps"] == call["parameters"]["extra"]["message_deps"]
            extra = call["parameters"]["extra"]
            assert extra["response_format"] == workload.schema_for(stage)
            assert extra["agentc_route_context"]["structured_outputs"] is True
            assert "input_tokens_upper_bound" not in extra["agentc_route_context"]
            assert "input_tokens_upper_bound_basis" not in extra["agentc_route_context"]
            assert extra["typed_client_size_diagnostic"]["json_utf8_bytes"] > 0
            assert (
                "not a provider-context proof"
                in extra["typed_client_size_diagnostic"]["scope"]
            )
            assert "structured_relational_contract" not in extra
            assert (
                restrict_call(call, stage)["parameters"]["extra"]["response_format"]
                == extra["response_format"]
            )
            assert (
                call["call_site_id"]
                == build(task, stage, history, model="target", run_identity="another")[
                    "call_site_id"
                ]
            )
            assert call["call_site_id"].startswith(workload.WORKLOAD + "/")
            sites[stage].add(call["call_site_id"])
            assert "vertical bars" not in call["messages"][-1]["content"]
            assert "ASCII line" not in call["messages"][0]["content"]
            if stage != "answer":
                assert "Approval table" not in call["messages"][-1]["content"]
            if stage == "filter":
                assert "requisition R" not in call["messages"][-1]["content"]
        synth, answer = calls["synthesize"], calls["answer"]
        assert synth["messages"][1] == answer["messages"][1]
        assert len(answer["messages"][1]["content"]) > 1700
        assert answer["messages"][2]["content"] == workload.application_text(
            solved["synthesize"]
        )
        assert synth["parameters"]["extra"]["window_state_reads"] == [
            "selected_records"
        ]
        assert answer["parameters"]["extra"]["window_state_reads"] == ["synthesis"]
    assert all(len(v) == 1 for v in sites.values())


def test_real_attention_retains_three_records_and_can_drop_all_seventeen_distractors():
    for task in workload.make_tasks(8825, 12):
        call = build(task, "filter", {})
        extra = call["parameters"]["extra"]
        relevant = set(workload.parse_filter(reference(task)["filter"]))
        droppable = set()
        for message, score in zip(
            call["messages"][1:-1], extra["attention_scores"][1:-1]
        ):
            protected = any(
                t.lower() in message["content"].lower()
                for t in extra["follow_on_tokens"]
            )
            identifier = message["content"].splitlines()[0]
            if identifier in relevant:
                assert protected
            if score <= 0.15 and not protected:
                droppable.add(identifier)
        assert len(droppable) == 17
        assert not droppable & relevant


def test_schemas_are_small_supported_subset_nullable_and_independently_copied():
    def shape(value, depth=0):
        if isinstance(value, dict):
            descendants = [shape(v, depth + 1) for v in value.values()]
        elif isinstance(value, list):
            descendants = [shape(v, depth + 1) for v in value]
        else:
            descendants = []
        return 1 + sum(n for n, _ in descendants), max(
            [depth] + [d for _, d in descendants]
        )

    for stage in workload.STAGES:
        wrapper = workload.schema_for(stage)
        schema = wrapper["json_schema"]["schema"]
        assert len(json.dumps(wrapper).encode()) <= 8192
        nodes, depth = shape(schema)
        assert nodes <= 128 and depth <= 12
        assert (
            schema["required"] == ["result"] and schema["additionalProperties"] is False
        )
        assert schema["properties"]["result"]["anyOf"][1] == {"type": "null"}
        assert len(schema["properties"]["result"]["anyOf"]) == 2
        assert not any(
            key in json.dumps(schema)
            for key in [
                "minItems",
                "maxItems",
                "uniqueItems",
                "minimum",
                "maximum",
                "description",
                "$ref",
            ]
        )
        wrapper["json_schema"]["schema"]["required"].append("mutated")
        assert workload.schema_for(stage)["json_schema"]["schema"]["required"] == [
            "result"
        ]


def test_contract_identity_binds_actual_prompts_schema_comparator_and_sources(
    monkeypatch,
):
    base = workload.contract_hash()
    values = workload.prompts()
    values["ANSWER_SYSTEM"] += " Another instruction."
    assert workload.contract_hash(values) != base
    task = workload.make_tasks(8826, 1)[0]
    assert (
        workload.contract_hash(values)
        in build(task, "filter", {}, prompts=values)["call_site_id"]
    )
    original_read = Path.read_bytes
    for changed_path in (Path(workload.__file__), Path(legacy.__file__)):
        with monkeypatch.context() as scoped:
            scoped.setattr(
                Path,
                "read_bytes",
                lambda self: (
                    original_read(self) + (b" changed" if self == changed_path else b"")
                ),
            )
            assert workload.contract_hash() != base
    with monkeypatch.context() as scoped:
        scoped.setattr(workload, "FEEDBACK_CONTRACT", "changed")
        assert workload.contract_hash() != base
    with monkeypatch.context() as scoped:
        scoped.setitem(workload._RESULT_SCHEMAS, "answer", {"type": "string"})
        assert workload.contract_hash() != base
    assert workload.contract_hash() == base


def test_history_and_stage_interface_errors_are_explicit():
    task = workload.make_tasks(8827, 1)[0]
    for stage, history in [
        ("unknown", {}),
        ("answer", {}),
        ("filter", {"filter": workload.ERROR}),
        ("synthesize", {"filter": 1}),
    ]:
        with pytest.raises(PilotError):
            build(task, stage, history)
    with pytest.raises(PilotError):
        workload.schema_for("unknown")
    with pytest.raises(PilotError):
        workload.parse_stage("unknown", workload.ERROR)
    with pytest.raises(PilotError):
        workload.contract_hash({"FILTER_SYSTEM": "only one"})
    with pytest.raises(PilotError):
        workload.score_answer(envelope("M01"), "M01", history={})


def test_strict_scoring_uses_typed_value_and_complete_consistent_history():
    good = {
        "filter": envelope(["D01", "D02", "D03"]),
        "synthesize": envelope(SYNTHESIS),
    }
    assert workload.score_answer(' { "result" : "M01" } ', "M01", history=good) == {
        "em": 1.0,
        "f1": 1.0,
    }
    for text in [envelope("M02"), "M01", envelope("m01"), workload.ERROR]:
        assert workload.score_answer(text, "M01", history=good) == {
            "em": 0.0,
            "f1": 0.0,
        }
    assert workload.score_answer(
        envelope("M01"), "M01", history={**good, "answer": envelope("M02")}
    ) == {"em": 0.0, "f1": 0.0}


def test_native_rewrites_preserve_schema_when_existing_extension_is_supplied():
    """Opt-in existing binary only. This tests mechanics, never provider efficacy."""
    native_path = os.environ.get("AGENTC_TYPED_NATIVE_PATH")
    native_python = os.environ.get("AGENTC_TYPED_NATIVE_PYTHON")
    if not native_path or not native_python:
        pytest.skip("supply an existing compatible native binary/interpreter; no build")
    script = r"""
import json, os, tempfile
from pathlib import Path
from bench import typed_relational_workload as w
from bench import openrouter_joint_study as study
from bench.openrouter_matrix import load_module
native = load_module("_native", Path(os.environ["AGENTC_TYPED_NATIVE_PATH"]), native=True)
attention = load_module("typed_attention_native_test", Path("python/agentc/_attention.py"))
settings = study.policies()["historical_rules"]
# Hypothetical capability for this mechanics-only check, not a live catalog.
catalog = json.loads(Path("bench/repro/openrouter-structured-pilot-2026-09-05/manifest.json").read_text())["catalog"]
for target in catalog["targets"]:
    target["capabilities"]["structured_outputs"] = True
for key in list(os.environ):
    if key.startswith("AGENTC_"):
        os.environ.pop(key)
os.environ.update(settings)
rewritten = set()
with tempfile.TemporaryDirectory(prefix="agentc-typed-native-test-") as store:
    native.optimize_configure(store, catalog_json=json.dumps(catalog))
    try:
        for task in w.make_tasks(8828, 9):
            history = {"filter": '{"result":["D01","D02","D03"]}', "synthesize": '{"result":{"sku":"K100","vendor":"V200","qty":7,"unit":13,"total":91,"region":"EU"}}'}
            for stage in w.STAGES:
                prior = {s:history[s] for s in w.STAGES[:w.STAGES.index(stage)]}
                call = study.restrict_call(w.call(task, stage, prior, attention, model="anthropic/claude-sonnet-4.5", run_identity="synthetic-native-test"), stage)
                # Explicitly hypothetical capacity assumption for mechanics only;
                # production adapter deliberately omits an unproven input bound.
                assert "input_tokens_upper_bound" not in call["parameters"]["extra"]["agentc_route_context"]
                call["parameters"]["extra"]["agentc_route_context"]["input_tokens_upper_bound"] = 64000
                call["parameters"]["extra"]["agentc_route_context"]["input_tokens_upper_bound_basis"] = "synthetic-test-only-not-a-provider-proof"
                encoded = native.optimize_plan(json.dumps(call))
                plan = json.loads(encoded)
                selected = plan.get("call", call)
                assert selected["parameters"]["extra"]["response_format"] == call["parameters"]["extra"]["response_format"]
                assert selected["parameters"]["extra"]["agentc_route_context"]["structured_outputs"]
                if selected["messages"] != call["messages"]:
                    rewritten.add(stage)
                # Hypothetical measurements only, needed to traverse warmup.
                native.optimize_observe(encoded, json.dumps({"call_site_id":call["call_site_id"],"executed_model_id":selected["model"],"input_tokens":4000,"output_tokens":30,"latency_ms":100,"cost_usd":0.01,"output_is_structured":True,"output_is_short":True}))
        assert rewritten == {"filter","answer"}, rewritten
    finally:
        native.optimize_reset()
print(json.dumps({"rewritten_stages":sorted(rewritten),"schema_preserved":True,"provider_efficacy":False}))
"""
    result = subprocess.run(
        [native_python, "-c", script],
        cwd=ROOT,
        env={**os.environ, "PYTHONHASHSEED": "0"},
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["schema_preserved"]
