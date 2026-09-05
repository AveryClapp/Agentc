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
                {"type": "string", "pattern": "^M[0-9]{2}$"}, {"type": "null"}
            ]}},
        },
    },
}
METADATA = {"dispatch_contract": {
    "provider_name": "Anthropic", "endpoint_model": pilot.SCHEMA_ENDPOINTS[MODEL][0]
}}


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
    assert payload == {k: v for k, v in request().items() if k != "response_format"}
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
    {"type": "string", "enum": ["A", "A"]},
    {"anyOf": [{"type": "string"}, {"type": "integer"}]},
    {"type": "object", "properties": {"a": {"type": "string"}}, "required": [], "additionalProperties": False},
])
def test_unsupported_schema_rejected(node):
    value = deepcopy(FORMAT)
    value["json_schema"]["schema"]["properties"]["result"] = node
    with pytest.raises(pilot.PilotError):
        pilot.validate_response_format(value)


@pytest.mark.parametrize("change", [
    {"context_length": 1_000_001}, {"context_length": True},
    {"name": "Anthropic | changed"}, {"tag": "other"},
    {"supported_parameters": ["max_tokens", "temperature"]},
    {"pricing": {"prompt": "0.000007", "completion": "0.000015"}},
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
