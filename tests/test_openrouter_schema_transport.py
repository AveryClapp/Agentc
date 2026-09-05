"""No-network strict-schema transport, bounded reservation and replay checks."""
from copy import deepcopy
from decimal import Decimal
import json
from unittest.mock import patch

import pytest

from bench import openrouter_pilot as pilot


MODEL = "anthropic/claude-sonnet-4.5"
TARGET = "anthropic/claude-haiku-4.5"
FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "procurement_answer_v1", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False, "required": ["result"],
            "properties": {"result": {"anyOf": [
                {"type": "string", "pattern": "^M(0[1-9]|[1-9][0-9])$"}, {"type": "null"}
            ]}},
        },
    },
}
METADATA = {"dispatch_contract": {
    "provider_name": "Anthropic", "endpoint_model": pilot.SCHEMA_ENDPOINTS[MODEL][0]
}}
PASSED_MODERATION = {
    "type": "guardrail", "name": "moderation",
    "summary": "Content passed OpenAI moderation",
    "data": {"engine": "openai-moderations", "flagged": False,
             "moderator_ids": ["OpenAIOmniModerator"]},
}


def endpoint(model=MODEL):
    return {
        "tag": "anthropic", "provider_name": "Anthropic",
        "name": "Anthropic | " + pilot.SCHEMA_ENDPOINTS[model][0],
        "context_length": pilot.SCHEMA_ENDPOINTS[model][1],
        "supported_parameters": ["max_tokens", "temperature", "structured_outputs", "response_format"],
        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
    }


def request(model=MODEL, **kwargs):
    return pilot.make_request(model, ["anthropic"], [{"role": "user", "content": "A"}],
                              max_tokens=512, response_format=FORMAT, **kwargs)


def response():
    return {
        "id": "gen-test", "model": MODEL, "provider": "Anthropic",
        "usage": {"cost": 0.01, "prompt_tokens": 500, "completion_tokens": 10},
        "choices": [{"finish_reason": "stop", "native_finish_reason": "end_turn",
                     "message": {"content": '{"result":"M01"}'}}],
        "openrouter_metadata": {
            "requested": MODEL, "attempt": 1, "is_byok": False, "pipeline": [],
            "endpoints": {"available": [{"selected": True, "provider": "Anthropic",
                                         "model": pilot.SCHEMA_ENDPOINTS[MODEL][0]}]},
        },
    }


@pytest.fixture
def ledger(tmp_path):
    return pilot.Ledger(tmp_path / "ledger.jsonl", "fake")


def dispatch(ledger, payload=None, cap="8", metadata=None):
    return ledger.call("fake", "schema-1", "typed-pilot", Decimal(cap),
                       request() if payload is None else payload,
                       METADATA if metadata is None else metadata)


def test_legacy_request_and_bound_are_byte_identical():
    payload = pilot.make_request(MODEL, ["anthropic"], [{"role": "user", "content": "A"}], max_tokens=512)
    assert "response_format" not in payload
    typed = request()
    assert typed["provider"]["max_price"].pop("request") == 0
    assert payload == {k: v for k, v in typed.items() if k != "response_format"}
    expected = (Decimal(len(pilot.canonical(payload)) + 1024 + 64) * 6 + 512 * 30) / 1_000_000
    assert pilot.upper_cost(payload) == expected


@pytest.mark.parametrize("model,cost", [(MODEL, "6.01536"), (TARGET, "1.21536")])
def test_entire_context_reserved_independent_of_schema_size(model, cost):
    assert pilot.upper_cost(request(model)) == Decimal(cost)


def test_request_schema_detached_and_fingerprint_bound():
    payload = request()
    assert payload["response_format"] == FORMAT
    payload["response_format"]["json_schema"]["name"] = "different"
    assert FORMAT["json_schema"]["name"] == "procurement_answer_v1"
    assert pilot.digest(payload) != pilot.digest(request())


@pytest.mark.parametrize("cap", [True, False, 1.5, "512", None, 0, 2049])
def test_token_cap_requires_bounded_integer(cap):
    with pytest.raises(pilot.PilotError):
        pilot.make_request(MODEL, ["anthropic"], [{"role": "user", "content": "A"}], max_tokens=cap)


@pytest.mark.parametrize("model,providers,pool", [
    ("test/model", ["anthropic"], None), (MODEL, ["other"], None),
    ("openrouter/auto", ["anthropic"], [MODEL]),
])
def test_schema_has_no_router_or_other_provider(model, providers, pool):
    with pytest.raises(pilot.PilotError):
        pilot.make_request(model, providers, [{"role": "user", "content": "A"}],
                           response_format=FORMAT, allowed_models=pool)


@pytest.mark.parametrize("node", [
    {"type": "number"}, {"$ref": "https://example.invalid/schema"},
    {"type": "integer", "minimum": 1},
    {"type": "array", "items": {"type": "string"}, "minItems": 3},
    {"type": "string", "pattern": "not-anchored"},
    {"type": "string", "pattern": "^(?=M)M[0-9]{2}$"},
    {"type": "string", "pattern": r"^(M)\1$"},
    {"type": "string", "enum": ["A", "A"]},
    {"anyOf": [{"type": "string"}, {"type": "integer"}]},
    {"type": "object", "properties": {"a": {"type": "string"}}, "required": [], "additionalProperties": False},
])
def test_unsupported_schema_rejected(node):
    value = deepcopy(FORMAT)
    value["json_schema"]["schema"]["properties"]["result"] = node
    with pytest.raises(pilot.PilotError):
        pilot.validate_response_format(value)


def test_provider_union_parameter_limit():
    value = deepcopy(FORMAT)
    schema = value["json_schema"]["schema"]
    schema["properties"] = {"field_" + chr(97 + i): deepcopy(schema["properties"]["result"]) for i in range(17)}
    schema["required"] = list(schema["properties"])
    with pytest.raises(pilot.PilotError, match="union parameter"):
        pilot.validate_response_format(value)
    schema["properties"].pop(schema["required"].pop())
    pilot.validate_response_format(value)


def test_all_active_price_tiers_must_fit_reservation():
    value = endpoint()
    value["pricing"].update(input_cache_write="0.00000375", web_search="0.01", overrides=[
        {"min_prompt_tokens": 200000, "prompt": "0.000006", "completion": "0.0000225", "input_cache_write": "0.0000075"}
    ])
    pilot.validate_schema_endpoint(MODEL, value)
    value["pricing"]["overrides"][0]["prompt"] = "0.000007"
    with pytest.raises(pilot.PilotError, match="price tier"):
        pilot.validate_schema_endpoint(MODEL, value)
    value = endpoint()
    value["pricing"]["unknown_fee"] = "1"
    with pytest.raises(pilot.PilotError, match="unknown pricing"):
        pilot.validate_schema_endpoint(MODEL, value)


@pytest.mark.parametrize("change", [
    {"context_length": 1_000_001}, {"context_length": True},
    {"name": "Anthropic | changed"}, {"tag": "other"},
    {"supported_parameters": ["max_tokens", "temperature"]},
    {"pricing": {"prompt": "0.000007", "completion": "0.000015"}},
    {"pricing": {"prompt": "0.000003", "completion": "0.000015", "request": "10"}},
    {"pricing": {"prompt": "0.000003", "completion": "0.000015", "request": None}},
])
def test_endpoint_drift_rejected(change):
    with pytest.raises(pilot.PilotError):
        pilot.validate_schema_endpoint(MODEL, endpoint() | change)


def test_fresh_endpoint_observation_and_exact_replay(ledger):
    def network(path, key, payload=None):
        return response() if payload else {"data": {"endpoints": [endpoint()]}}
    with patch.object(pilot, "account", return_value={"usage": 0}), patch.object(pilot, "request_json", side_effect=network) as api:
        first = dispatch(ledger)
        replay = dispatch(ledger)
        assert all(replay[k] == v for k, v in first.items())
        assert api.call_count == 2  # one free capacity lookup, one paid completion
        changed = request()
        changed["response_format"]["json_schema"]["name"] = "other"
        with pytest.raises(pilot.PilotError, match="changed inputs"):
            dispatch(ledger, changed)
    events = [json.loads(line) for line in ledger.path.read_text().splitlines()]
    reserve = next(e for e in events if e["event"] == "reserve")
    assert reserve["upper_cost_usd"] == "6.01536"
    assert reserve["schema_endpoint_observation"] == endpoint()
    assert ledger.summary()["spent_usd"] == "0.01"


def test_original_stage_cap_rejects_before_network(ledger):
    with patch.object(pilot, "account") as account, patch.object(pilot, "request_json") as api:
        with pytest.raises(pilot.PilotError, match="reserved spending"):
            dispatch(ledger, cap="1")
        account.assert_not_called()
        api.assert_not_called()


def test_material_request_controls_cannot_be_added(ledger):
    with patch.object(pilot, "account") as account:
        payload = request() | {"plugins": [{"id": "response-healing"}]}
        with pytest.raises(pilot.PilotError, match="dispatch controls"):
            dispatch(ledger, payload)
        account.assert_not_called()


def test_changed_endpoint_blocks_paid_dispatch(ledger):
    with patch.object(pilot, "account", return_value={"usage": 0}), patch.object(
        pilot, "request_json", return_value={"data": {"endpoints": [endpoint() | {"context_length": 2_000_000}]}}
    ) as api:
        with pytest.raises(pilot.PilotError, match="endpoint changed"):
            dispatch(ledger)
        assert api.call_count == 1
        assert api.call_args.args[0].endswith("/endpoints")
    assert not any(json.loads(line)["event"] == "reserve" for line in ledger.path.read_text().splitlines())


@pytest.mark.parametrize("mode", ["refusal", "native_refusal", "healing", "missing_cost", "timeout"])
def test_failed_schema_attempt_keeps_entire_context_hold(ledger, mode):
    body = response()
    if mode == "refusal":
        body["choices"][0]["message"]["refusal"] = "declined"
    elif mode == "native_refusal":
        body["choices"][0]["native_finish_reason"] = "refusal"
    elif mode == "healing":
        body["openrouter_metadata"]["pipeline"] = [{"type": "response_healing"}]
    elif mode == "missing_cost":
        body["usage"].pop("cost")
    def network(path, key, payload=None):
        if not payload:
            return {"data": {"endpoints": [endpoint()]}}
        if mode == "timeout":
            raise pilot.PilotError("timeout")
        return body
    with patch.object(pilot, "account", return_value={"usage": 0}), patch.object(pilot, "request_json", side_effect=network):
        with pytest.raises(pilot.PilotError):
            dispatch(ledger)
    summary = ledger.summary()
    assert Decimal(summary["spent_usd"]) + Decimal(summary["retained_uncertainty_usd"]) == Decimal("6.01536")
    assert summary["completed_calls"] == 0
    assert summary["unresolved_calls"] == ["schema-1"]


def test_length_is_financial_receipt_not_an_application_success():
    body = response()
    body["choices"][0].update(finish_reason="length", native_finish_reason="max_tokens")
    assert pilot.schema_text_choice(body)["finish_reason"] == "length"
    body["choices"][0]["finish_reason"] = "stop"
    with pytest.raises(pilot.PilotError):
        pilot.schema_text_choice(body)


def passed_moderation_response():
    body = response()
    body["openrouter_metadata"]["pipeline"] = [deepcopy(PASSED_MODERATION)]
    body["usage"] = {"cost": "0.003945", "prompt_tokens": 1270, "completion_tokens": 9}
    return body


def test_exact_passed_moderation_is_diagnostic_and_never_stripped():
    body = passed_moderation_response()
    before = deepcopy(body)
    assert pilot.schema_text_choice(body) is body["choices"][0]
    assert body == before
    assert body["choices"][0]["message"]["content"] == '{"result":"M01"}'


def test_passed_moderation_actual_ledger_replay_and_billing(ledger):
    body = passed_moderation_response()

    def network(path, key, payload=None):
        return deepcopy(body) if payload else {"data": {"endpoints": [endpoint()]}}

    with patch.object(pilot, "account", return_value={"usage": 0}), patch.object(
        pilot, "request_json", side_effect=network
    ) as api:
        first = dispatch(ledger)
        before_replay = ledger.path.read_bytes()
        replay = dispatch(ledger)
        assert all(replay[k] == value for k, value in first.items())
        assert ledger.path.read_bytes() == before_replay
        assert api.call_count == 2
        assert first["router_metadata"]["pipeline"] == [PASSED_MODERATION]
        assert first["answer"] == body["choices"][0]["message"]["content"]
    events = [json.loads(line) for line in ledger.path.read_text().splitlines()]
    assert next(event for event in events if event["event"] == "response")["response"] == body
    assert sum(event["event"] == "reserve" for event in events) == 1
    assert sum(event["event"] == "result" for event in events) == 1
    assert not any(event["event"] == "attempt_failure" for event in events)
    summary = ledger.summary()
    assert summary["spent_usd"] == "0.003945"
    assert summary["retained_uncertainty_usd"] == "0"
    assert summary["completed_calls"] == 1 and not summary["unresolved_calls"]


def rejected_moderation_pipelines():
    variants = []
    for verdict in (True, 0, 0.0, "false", None):
        stage = deepcopy(PASSED_MODERATION)
        stage["data"]["flagged"] = verdict
        variants.append(pytest.param([stage], id="flagged-" + repr(verdict)))
    for key, value in (
        ("engine", "unknown"), ("action", "passed"), ("action", "blocked"),
        ("action", "redacted"), ("moderator_ids", []),
        ("moderator_ids", ["UnknownModerator"]),
        ("moderator_ids", ["OpenAIOmniModerator", "OpenAIOmniModerator"]),
        ("moderator_ids", "OpenAIOmniModerator"), ("new_field", False),
    ):
        stage = deepcopy(PASSED_MODERATION)
        stage["data"][key] = value
        variants.append(pytest.param([stage], id="data-" + key + "-" + repr(value)))
    for key, value in (
        ("type", "plugin"), ("name", "content-filter"),
        ("summary", "Content blocked by moderation"), ("summary", None),
        ("action", "passed"), ("guardrail_id", "new-policy"), ("data", None),
    ):
        variants.append(pytest.param([deepcopy(PASSED_MODERATION) | {key: value}], id="stage-" + key + "-" + repr(value)))
    for key in PASSED_MODERATION:
        stage = deepcopy(PASSED_MODERATION)
        del stage[key]
        variants.append(pytest.param([stage], id="missing-stage-" + key))
    for key in PASSED_MODERATION["data"]:
        stage = deepcopy(PASSED_MODERATION)
        del stage["data"][key]
        variants.append(pytest.param([stage], id="missing-data-" + key))
    for stage_type in ("response_healing", "context_compression", "plugin", "server_tools", "unknown"):
        variants.append(pytest.param([deepcopy(PASSED_MODERATION), {"type": stage_type}], id="extra-" + stage_type))
    variants.extend([
        pytest.param([deepcopy(PASSED_MODERATION)] * 2, id="duplicate-diagnostic"),
        pytest.param(PASSED_MODERATION, id="not-an-array"),
        pytest.param(None, id="null-pipeline"),
        pytest.param(False, id="boolean-pipeline"),
        pytest.param({}, id="empty-object-pipeline"),
        pytest.param([None], id="null-stage"),
    ])
    return variants


@pytest.mark.parametrize("pipeline", rejected_moderation_pipelines())
def test_unknown_flagged_or_mutating_pipeline_rejected_with_full_allowance(ledger, pipeline):
    body = passed_moderation_response()
    body["openrouter_metadata"]["pipeline"] = deepcopy(pipeline)
    before = deepcopy(body)
    with pytest.raises(pilot.PilotError):
        pilot.schema_text_choice(body)
    assert body == before

    def network(path, key, payload=None):
        return deepcopy(body) if payload else {"data": {"endpoints": [endpoint()]}}

    with patch.object(pilot, "account", return_value={"usage": 0}), patch.object(
        pilot, "request_json", side_effect=network
    ) as api:
        with pytest.raises(pilot.PilotError):
            dispatch(ledger)
        assert api.call_count == 2
        with pytest.raises(pilot.PilotError):
            dispatch(ledger)
        assert api.call_count == 2
    events = [json.loads(line) for line in ledger.path.read_text().splitlines()]
    assert next(event for event in events if event["event"] == "response")["response"] == body
    assert not any(event["event"] == "result" for event in events)
    failure = next(event for event in events if event["event"] == "attempt_failure")
    assert failure["budget_hold_usd"] == "6.01536"
    assert failure["reported_cost_usd"] == "0.003945"
    summary = ledger.summary()
    assert summary["spent_usd"] == "0.003945"
    assert summary["retained_uncertainty_usd"] == "6.011415"
    assert Decimal(summary["conservative_committed_usd"]) == Decimal("6.01536")
    assert summary["completed_calls"] == 0


@pytest.mark.parametrize("mutation", ["refusal", "native-refusal", "finish-filtered", "error", "truncation"])
def test_passed_moderation_does_not_override_other_status_checks(mutation):
    body = passed_moderation_response()
    choice = body["choices"][0]
    if mutation == "refusal":
        choice["message"]["refusal"] = "declined"
    elif mutation == "native-refusal":
        choice["native_finish_reason"] = "refusal"
    elif mutation == "finish-filtered":
        choice["finish_reason"] = "content_filter"
    elif mutation == "error":
        body["error"] = {"code": 403}
    else:
        choice.update(finish_reason="length", native_finish_reason="max_tokens")
        assert pilot.schema_text_choice(body)["finish_reason"] == "length"
        return  # Financial receipt only; the unchanged typed adapter rejects it.
    with pytest.raises(pilot.PilotError):
        pilot.schema_text_choice(body)
