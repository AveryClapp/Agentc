"""Invented rows only: never open the FinQA corpus or paid experiment labels."""

from copy import deepcopy
import json

import pytest

from bench import finqa_workload as w


def task():
    return {
        "id": "INVENTED/1900/page_1.pdf-0",
        "pre_text": ["Synthetic revenue notes."],
        "post_text": ["Synthetic unrelated notes."],
        "table": [
            ["measure", "1900", "1899"],
            ["revenue", "9", "4"],
            ["expenses", "$2", "$1"],
        ],
        "qa": {"question": "What is the revenue difference?", "exe_ans": "POISON_GOLD"},
        "model_input": "POISON_RETRIEVER",
        "gold_inds": {"POISON": True},
    }


def raw(value):
    return json.dumps({"result": value})


def history(program=None, evidence=None):
    return {
        "retrieve": raw(["table:1"] if evidence is None else evidence),
        "program": raw(
            ["subtract(", "9", "4", ")", "EOF"] if program is None else program
        ),
    }


class Attention:
    @staticmethod
    def compute_attention_scores(messages, _):
        return [1.0] * len(messages), [1] * len(messages)


def call(stage="retrieve", prior=None, item=None, run="unit/arm-A"):
    return w.call(
        task() if item is None else item,
        stage,
        {} if prior is None else prior,
        Attention(),
        model="fake-model",
        run_identity=run,
    )


def test_public_projection_does_not_access_labels_or_retriever_fields():
    class QuestionOnly(dict):
        def __getitem__(self, key):
            assert key == "question", "label access"
            return super().__getitem__(key)

        def __iter__(self):
            raise AssertionError("must not copy all QA fields")

        def __deepcopy__(self, memo):
            raise AssertionError("must not copy QA container")

    item = task()
    item["qa"] = QuestionOnly(question="Synthetic question", exe_ans=object())
    public = w.public_task(item)
    assert public["qa"] == {"question": "Synthetic question"}
    assert set(public) == {"id", "qa", "pre_text", "post_text", "table"}
    assert "POISON" not in json.dumps(call(item=item))
    public["table"][1][1] = "changed"
    assert item["table"][1][1] == "9"


@pytest.mark.parametrize(
    "mutation",
    [
        {"id": "../../secrets"},
        {"qa": {}},
        {"table": []},
        {"table": [["short"]]},
        {"table": [["a", 2]]},
        {"pre_text": "not-array"},
        {"post_text": [False]},
        {"qa": {"question": "\ud800"}},
        {"qa": {"question": " "}},
    ],
)
def test_invalid_public_input(mutation):
    item = task() | mutation
    with pytest.raises(w.ContractError):
        w.public_task(item)


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "prose",
        "{}",
        "[]",
        "null",
        '{"result":null}',
        '{"result":true}',
        '{"result":[]}',
        '{"result":[1]}',
        '{"result":[" "]}',
        '{"result":["table:1"],"result":["pre:0"]}',
        '{"result":["table:1"],"extra":1}',
        '{"result":NaN}',
        '{"result":["table:1"]} trailing',
        '{"result":["\\ud800"]}',
        "[" * 2000,
        "x" * (w.MAX_RAW_BYTES + 1),
    ],
)
def test_invalid_json_never_becomes_application_state(invalid):
    for stage in w.STAGES:
        assert w.parse_stage(task(), stage, invalid) is None


@pytest.mark.parametrize("finish", ["length", "error", "content_filter", None, ""])
def test_non_normal_finish_invalid(finish):
    assert (
        w.parse_stage(task(), "retrieve", raw(["table:1"]), finish_reason=finish)
        is None
    )


def test_application_state_preserves_stop_and_upstream_failure():
    item = task()
    value = raw(["table:1", "pre:0"])
    state = w.application_text(item, "retrieve", value)
    assert state == w.canonical({"result": ["pre:0", "table:1"]})
    for options in (
        {"finish_reason": "length"},
        {"upstream_valid": False},
        {"upstream_valid": 1},
    ):
        assert (
            w.application_text(item, "retrieve", value, **options) == '{"result":null}'
        )
    clipped = w.application_text(item, "retrieve", value, finish_reason="length")
    assert w.execute(item, history() | {"retrieve": clipped}) is None


def test_retrieval_uses_real_set_of_public_ids():
    assert w.parse_stage(task(), "retrieve", raw(["table:1", "pre:0"])) == [
        "pre:0",
        "table:1",
    ]
    for invalid in (["table:0"], ["table:7"], ["gold:0"], ["table:1", "table:1"]):
        assert w.parse_stage(task(), "retrieve", raw(invalid)) is None


def test_two_stages_keep_only_selected_evidence_and_all_live_state_reads():
    retrieve = call()
    program = call("program", {"retrieve": raw(["table:1"])})
    encoded = json.dumps(program)
    assert "revenue" in encoded
    assert "expenses" not in encoded and "unrelated" not in encoded
    assert w.STAGES == ("retrieve", "program")
    for request in (retrieve, program):
        extra = request["parameters"]["extra"]
        assert len(extra["message_deps"]) == len(request["messages"])
        assert "input_tokens_upper_bound" not in extra["agentc_route_context"]
        assert "input_tokens_upper_bound_basis" not in extra["agentc_route_context"]
        states = {d["key"] for d in extra["message_deps"] if d["kind"] == "state"}
        assert states == set(extra["window_state_reads"])
    assert call()["trace_id"] != call(run="unit/arm-B")["trace_id"]
    assert call()["call_site_id"] == call(run="unit/arm-B")["call_site_id"]
    assert retrieve["call_site_id"] != program["call_site_id"]


def test_invalid_upstream_is_contagious_and_question_preserved():
    prior = {"retrieve": raw(["invented:999"])}
    request = call("program", prior)
    assert request["parameters"]["extra"]["finqa_contract"]["upstream_valid"] is False
    assert "ERROR_INPUT" in json.dumps(request)
    assert "revenue" not in json.dumps(request["messages"][1:-1])
    assert w.execute(task(), history() | prior) is None


def test_selected_evidence_rendering_preserves_source_order_not_lexical_ids():
    request = call(
        "program", {"retrieve": raw(["post:0", "table:2", "pre:0", "table:1"])}
    )
    evidence = [
        message["content"].split("\n")[0] for message in request["messages"][2:-1]
    ]
    assert evidence == ["pre:0", "table:1", "table:2", "post:0"]


@pytest.mark.parametrize(
    "stage,prior", [("unknown", {}), ("program", {}), ("retrieve", {"retrieve": "x"})]
)
def test_wrong_stage_history_rejected(stage, prior):
    with pytest.raises(w.ContractError):
        call(stage, prior)


@pytest.mark.parametrize(
    "op,a,b,expected",
    [
        ("add", "3", "2", 5.0),
        ("subtract", "3", "2", 1.0),
        ("multiply", "3", "2", 6.0),
        ("divide", "1", "3", 0.33333),
        ("exp", "3", "2", 9.0),
        ("greater", "3", "2", "yes"),
        ("greater", "2", "3", "no"),
        ("add", "const_m1", "const_100", 99.0),
        ("add", "1,000", "50%", 1000.5),
    ],
)
def test_scalar_operations(op, a, b, expected):
    assert w.execute(task(), history([op + "(", a, b, ")", "EOF"])) == expected


@pytest.mark.parametrize(
    "op,expected",
    [("table_max", 9), ("table_min", 4), ("table_sum", 13), ("table_average", 6.5)],
)
def test_all_table_operations(op, expected):
    assert (
        w.execute(task(), history([op + "(", "revenue", "none", ")", "EOF"]))
        == expected
    )


def test_backreferences_round_only_final_result():
    tokens = ["divide(", "1", "3", ")", "multiply(", "#0", "3", ")", "EOF"]
    assert w.execute(task(), history(tokens)) == 1.0


@pytest.mark.parametrize(
    "tokens",
    [
        ["EOF"],
        ["subtract(", "9", "4", ")"],
        ["unknown(", "9", "4", ")", "EOF"],
        ["add(", "#0", "1", ")", "EOF"],
        ["add(", "#-1", "1", ")", "EOF"],
        ["divide(", "1", "0", ")", "EOF"],
        ["exp(", "1e308", "100", ")", "EOF"],
        ["exp(", "-1", "0.5", ")", "EOF"],
        ["add(", "nan", "1", ")", "EOF"],
        ["add(", "inf", "1", ")", "EOF"],
        ["multiply(", "1e308", "1e308", ")", "EOF"],
        ["greater(", "2", "1", ")", "add(", "#0", "1", ")", "EOF"],
        ["table_sum(", "revenue", "0", ")", "EOF"],
        ["add(", "__import__('os').system('false')", "1", ")", "EOF"],
        ["add(", "1", "1", ")"] * (w.MAX_STEPS + 1) + ["EOF"],
    ],
)
def test_invalid_or_unbounded_program_is_loss(tokens):
    assert w.execute(task(), history(tokens)) is None


def test_table_execution_uses_public_table_like_upstream_but_rejects_ambiguous_labels():
    tokens = ["table_sum(", "expenses", "none", ")", "EOF"]
    assert w.execute(task(), history(tokens)) == 3.0
    assert w.execute(task(), history(tokens, ["table:2"])) == 3.0
    item = task()
    item["table"].append(["expenses", "8", "9"])
    assert w.execute(item, history(tokens, ["table:2", "table:3"])) is None


def test_execution_metric_exact_denominator_and_no_tolerant_number_matching():
    item = task()
    task_id = item["id"]
    result = w.score_inventory([item], {task_id: history()}, {task_id: 5})
    assert result["accuracy"] == 1.0 and result["total"] == 1
    assert (
        w.score_inventory([item], {task_id: history()}, {task_id: 5.00001})["correct"]
        == 0
    )
    broken = history() | {"retrieve": raw(None)}
    assert w.score_inventory([item], {task_id: broken}, {task_id: 5})["correct"] == 0
    for gold in (True, "5", float("nan"), float("inf"), None):
        with pytest.raises(w.ContractError):
            w.score_inventory([item], {task_id: history()}, {task_id: gold})
    for tasks, histories, labels in (
        [[], {}, {}],
        [[item], {}, {task_id: 5}],
        [[item], {task_id: history()}, {}],
        [[item, deepcopy(item)], {task_id: history()}, {task_id: 5}],
    ):
        with pytest.raises(w.ContractError):
            w.score_inventory(tasks, histories, labels)
