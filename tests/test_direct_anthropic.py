from copy import deepcopy
from argparse import Namespace
from decimal import Decimal
import json

import pytest

from bench import direct_anthropic as direct
from bench.openrouter_pilot import Ledger, PilotError

MODEL = "anthropic/claude-sonnet-4.5"
KEY = "TEST_NOT_A_CREDENTIAL"


def original():
    return [{"role": "system", "content": "Exact system"}, {"role": "user", "content": "Passage"},
        {"role": "user", "content": "Question"}]


def response():
    return {"id": "msg_test", "model": direct.MODELS[MODEL]["snapshot"], "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "Ada"}], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 2, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0, "service_tier": "standard"}}


def ledger(tmp_path):
    return direct.DirectLedger(tmp_path / "direct.jsonl", KEY, Ledger(tmp_path / "openrouter.jsonl", KEY))


def test_message_projection_keeps_content_order_and_every_frozen_control():
    payload = direct.request_for(MODEL, original())
    direct.validate_request(MODEL, payload)
    assert payload["system"] == original()[0]["content"]
    assert [m["content"][0]["text"] for m in payload["messages"]] == [m["content"] for m in original()[1:]]
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["service_tier"] == "standard_only"
    assert payload["max_tokens"] == 512 and payload["temperature"] == 0
    for mutation in ("cache_control", "service_tier", "temperature", "max_tokens", "thinking"):
        bad = deepcopy(payload)
        bad[mutation] = "changed"
        with pytest.raises(PilotError):
            direct.validate_request(MODEL, bad)
    bad = deepcopy(payload)
    bad["messages"][0]["content"][0]["cache_control"] = {"type": "ephemeral"}
    with pytest.raises(PilotError):
        direct.validate_request(MODEL, bad)


def test_documented_context_capacity_reserve_and_tariff_cost():
    assert direct.upper_cost(MODEL) == Decimal(".60768")
    assert direct.upper_cost("anthropic/claude-haiku-4.5") == Decimal(".20256")
    parsed = direct.parse_response(MODEL, response())
    assert parsed["cost_usd"] == "0.000330"
    assert parsed["cost_basis"] == "tariff_reconstructed_not_provider_billed"
    r = response()
    r["usage"].pop("service_tier")
    r["usage"].pop("cache_read_input_tokens")
    parsed = direct.parse_response(MODEL, r)
    assert parsed["service_tier_reported"] is None
    assert "cache_read_input_tokens" not in parsed["usage"]


@pytest.mark.parametrize("field,value", [("model", "alias"), ("content", [{"type": "thinking", "thinking": "x"}]),
    ("stop_reason", "unknown"), ("usage", []), ("content", ["malformed"])])
def test_invalid_attribution_and_response_shapes_stop(field, value):
    r = response()
    r[field] = value
    with pytest.raises(PilotError):
        direct.parse_response(MODEL, r)


@pytest.mark.parametrize("field,value", [("input_tokens", True), ("output_tokens", -1), ("output_tokens", 513),
    ("input_tokens", 200001), ("cache_read_input_tokens", 5), ("cache_creation_input_tokens", 1),
    ("service_tier", "priority")])
def test_invalid_usage_or_changed_uncached_condition_stops(field, value):
    r = response()
    r["usage"][field] = value
    with pytest.raises(PilotError):
        direct.parse_response(MODEL, r)


def test_resume_uses_exact_cached_result_without_redispatch(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(direct, "send", lambda key, payload: (calls.append(payload) or response(), {"request_id": "req_test", "http_status": 200}))
    store = ledger(tmp_path)
    payload = direct.request_for(MODEL, original())
    first = store.call(KEY, "id", "stage", MODEL, payload, {"task": "t"})
    second = store.call(KEY, "id", "stage", MODEL, payload, {"task": "t"})
    assert len(calls) == 1
    assert first == {k: v for k, v in second.items() if k not in {"at", "key_id"}}
    assert store.summary()["tariff_reconstructed_cost_usd"] == "0.000330"
    assert store.summary()["unresolved_calls"] == []
    with pytest.raises(PilotError, match="resume"):
        store.call(KEY, "id", "stage", MODEL, payload, {"task": "changed"})
    assert KEY not in store.path.read_text()


def test_invalid_success_persists_response_and_blocks_next_request(tmp_path, monkeypatch):
    bad = response()
    bad["model"] = "wrong"
    calls = []
    monkeypatch.setattr(direct, "send", lambda *args: (calls.append(1) or bad, {"request_id": "req_test", "http_status": 200}))
    store = ledger(tmp_path)
    payload = direct.request_for(MODEL, original())
    with pytest.raises(PilotError, match="attribution"):
        store.call(KEY, "id", "stage", MODEL, payload, {})
    events = [json.loads(line) for line in store.path.read_text().splitlines()]
    assert [e["event"] for e in events] == ["reserve", "response"]
    assert events[1]["request_id"] == "req_test"
    with pytest.raises(PilotError, match="unresolved"):
        store.call(KEY, "different", "stage", MODEL, payload, {})
    assert len(calls) == 1


def test_transport_failure_is_not_retried(tmp_path, monkeypatch):
    calls = []
    def failure(*args):
        calls.append(1)
        raise PilotError("timeout")
    monkeypatch.setattr(direct, "send", failure)
    store = ledger(tmp_path)
    payload = direct.request_for(MODEL, original())
    for call_id in ("one", "two"):
        with pytest.raises(PilotError):
            store.call(KEY, call_id, "stage", MODEL, payload, {})
    assert calls == [1]
    assert store.summary()["unresolved_calls"] == ["one"]


def test_local_and_shared_campaign_caps_include_unresolved_exposure(tmp_path, monkeypatch):
    monkeypatch.setattr(direct, "send", lambda *args: pytest.fail("must not dispatch"))
    store = ledger(tmp_path)
    with store.openrouter.locked() as handle:
        store.openrouter.append(handle, {"event": "result", "id": "or1", "cost_usd": "49"})
        store.openrouter.append(handle, {"event": "reserve", "id": "or2", "upper_cost_usd": "0.5"})
    assert store.campaign_exposure() == Decimal("49.5")
    with pytest.raises(PilotError, match="combined"):
        store.call(KEY, "id", "stage", MODEL, direct.request_for(MODEL, original()), {})
    with store.locked() as handle:
        store.append(handle, {"event": "result", "id": "old", "cost_usd": "2.5"})
    with pytest.raises(PilotError, match="USD3"):
        store.call(KEY, "id", "stage", MODEL, direct.request_for(MODEL, original()), {})


def test_key_loader_never_evaluates_dotenv(tmp_path):
    path = tmp_path / "dotenv"
    path.write_text("UNRELATED=ignore\nexport ANTHROPIC_API_KEY='TEST_KEY'\n")
    assert direct.load_key(path) == "TEST_KEY"
    path.write_text("ANTHROPIC_API_KEY=a\nANTHROPIC_API_KEY=b\n")
    with pytest.raises(PilotError):
        direct.load_key(path)


def test_subset_is_first32_frozen_question_ids_without_outcome_access():
    frontier = json.loads((direct.ROOT / "bench/repro/openrouter-frontier-2026-09-04/manifest.json").read_text())
    schedule = direct.schedule_for(frontier)
    ids = list(dict.fromkeys(r["task_id"] for r in frontier["schedule"] if r["phase"] == "holdout"))[:32]
    assert len(schedule) == 128
    assert set(r["task_id"] for r in schedule) == set(ids)
    assert all(r["arm"] == "full" and r["model"] in direct.MODELS for r in schedule)


def test_run_rejects_missing_resume_ledger_before_any_duplicate_dispatch(tmp_path, monkeypatch):
    frontier = json.loads((direct.ROOT / "bench/repro/openrouter-frontier-2026-09-04/manifest.json").read_text())
    natural, extended = tmp_path / "natural.json", tmp_path / "extended.json"
    natural.write_text("[]")
    extended.write_text("[]")
    frontier["fixtures"] = {"natural": direct.file_hash(natural), "extended": direct.file_hash(extended)}
    directory = tmp_path / "frontier"
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps(frontier))
    ids = {r["task_id"] for r in direct.schedule_for(frontier)}
    tasks = {c: {i: {"task_id": i, "prompt": "Who?", "expected": "Ada", "meta": {"paragraphs": [
        {"title": "P", "sentences": ["Ada is here."]}]}} for i in ids} for c in ("natural", "extended")}
    monkeypatch.setattr(direct, "load_tasks", lambda *args: tasks)
    calls = []
    def fake_send(key, payload):
        calls.append(payload)
        r = response()
        r["model"] = payload["model"]
        return r, {"request_id": "req_test", "http_status": 200}
    monkeypatch.setattr(direct, "send", fake_send)
    env_file = tmp_path / "dotenv"
    env_file.write_text(f"ANTHROPIC_API_KEY={KEY}\nOPENROUTER_API_KEY={KEY}\n")
    args = Namespace(frontier=directory, natural=natural, extended=extended, output=tmp_path / "direct",
        env_file=env_file, ledger=tmp_path / "original.jsonl", openrouter_ledger=tmp_path / "or.jsonl", max_calls=1)
    direct.prepare(args)
    direct.run(args)
    direct.run(args)
    assert len(calls) == 1
    args.ledger = tmp_path / "missing.jsonl"
    with pytest.raises(PilotError, match="no matching completed"):
        direct.run(args)
    assert len(calls) == 1
