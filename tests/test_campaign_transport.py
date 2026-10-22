"""Paid-dispatch invariants exercised only on temp journals and loopback HTTP."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bench import campaign_accounting as accounting
from bench import campaign_chat as chat
from bench.campaign_chat import ChatContract, PARAMETERS, prepare_request
from bench.campaign_http import CampaignHTTP
from bench.campaign_transport import CampaignStopped, CampaignTransport

KEY = "literal-fixture-key"
KEY_ID = hashlib.sha256(KEY.encode()).hexdigest()
CONTRACT = ChatContract(
    "fixture/model", "fixture", "Fixture", "fixture/model-v1", 128, 16
)


def request():
    return {
        "model": CONTRACT.model,
        "messages": [{"role": "user", "content": "fixture input"}],
        "max_tokens": 8,
        "temperature": 0.7,
    }


def metadata(actor="agent", kind="primary"):
    return {
        "actor": actor,
        "kind": kind,
        "task_id": "task",
        "arm": "fixture",
        "turn_id": "turn",
    }


def event(kind, **fields):
    return {"event": kind, "key_id": KEY_ID, "at": "fixture", **fields}


def write(path, rows):
    data = b"".join(accounting.canonical(row) + b"\n" for row in rows)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def response(generation="generation"):
    return {
        "id": generation,
        "model": CONTRACT.model,
        "provider": CONTRACT.provider_name,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "answer"},
            }
        ],
        "usage": {
            "cost": "0.0001",
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "is_byok": False,
        },
        "openrouter_metadata": {
            "requested": CONTRACT.model,
            "strategy": "direct",
            "attempt": 1,
            "is_byok": False,
            "endpoints": {
                "available": [
                    {
                        "provider": CONTRACT.provider_name,
                        "model": CONTRACT.endpoint_model,
                        "selected": True,
                    }
                ]
            },
            "pipeline": [],
        },
    }


@pytest.fixture
def provider():
    class State:
        account = {
            "usage": "7",
            "byok_usage": "0",
            "limit_remaining": "43",
            "limit_reset": None,
        }
        endpoint = {
            "tag": CONTRACT.provider_tag,
            "provider_name": CONTRACT.provider_name,
            "name": "Fixture | " + CONTRACT.endpoint_model,
            "context_length": CONTRACT.context_tokens,
            "pricing": {"prompt": "0.000001", "completion": "0.000002", "request": "0"},
            "supported_parameters": [*PARAMETERS, "structured_outputs"],
        }
        posts = []
        headers = []
        mutate = staticmethod(lambda body: body)
        status = 200
        cache = "MISS"
        raw = None
        background_started = threading.Event()
        release_background = threading.Event()
        block_all = False
        stall_read = False
        gets = []

    state = State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, status=200):
            raw = json.dumps(value).encode() if state.raw is None else state.raw
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Content-Type", "application/json")
            self.send_header("X-OpenRouter-Cache-Status", state.cache)
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            state.gets.append(self.path)
            if self.path == "/api/v1/key":
                self.reply({"data": state.account})
            else:
                self.reply({"data": {"endpoints": [state.endpoint]}})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.posts.append(body)
            generation_index = len(state.posts)
            # Do not retain an Authorization value even in fixture diagnostics.
            state.headers.append(
                {
                    name: self.headers.get(name)
                    for name in ("X-OpenRouter-Cache", "X-OpenRouter-Metadata")
                }
            )
            if state.block_all or body["messages"][-1]["content"] == "background":
                state.background_started.set()
                state.release_background.wait(10)
            answer = state.mutate(response("generation-" + str(generation_index)))
            if state.stall_read:
                raw = json.dumps(answer).encode()
                self.send_response(state.status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    for byte in raw:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.reply(answer, state.status)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.http = CampaignHTTP.local_test(
        f"http://127.0.0.1:{server.server_port}/api/v1", deadline_seconds=5
    )
    try:
        yield state
    finally:
        state.release_background.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def transport(tmp_path, provider, *, rows=None, cap="1", **kwargs):
    path = tmp_path / "original.jsonl"
    sha = write(path, rows if rows is not None else [event("origin", usage_usd="7")])
    return CampaignTransport(
        ledger=path,
        expected_sha256=sha,
        key=KEY,
        stage="fresh-stage",
        stage_cap_usd=cap,
        contracts=[CONTRACT],
        http=provider.http,
        **kwargs,
    )


def rows(client):
    return accounting.read_snapshot(client.ledger, expected_key_id=KEY_ID).events


def tool_history():
    value = request()
    value["messages"] = [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{malformed"},
                },
                {
                    "id": "b",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"x":2}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "b", "content": "result b"},
        {"role": "tool", "tool_call_id": "a", "content": "result a"},
    ]
    value["tools"] = [
        {
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object"}},
        }
    ]
    value.update(tool_choice="auto", parallel_tool_calls=True, seed=3, stop=["END"])
    return value


def test_structured_all_actor_calls_preserve_real_history_and_parameters(
    tmp_path, provider
):
    client = transport(tmp_path, provider)
    value = tool_history()
    original = deepcopy(value)
    for index, actor in enumerate(("agent", "user", "judge")):
        reply = client.call(
            call_id=f"call-{index}", request=value, metadata=metadata(actor)
        )
        assert reply["choices"][0]["message"]["content"] == "answer"
    assert value == original
    assert len(provider.posts) == 3
    for actual in provider.posts:
        for name, expected in value.items():
            assert actual[name] == expected
        assert actual["provider"]["allow_fallbacks"] is False
        assert actual["transforms"] == []
    assert all(
        headers == {"X-OpenRouter-Cache": "false", "X-OpenRouter-Metadata": "enabled"}
        for headers in provider.headers
    )
    history = rows(client)
    assert [e["metadata"]["actor"] for e in history if e["event"] == "reserve"] == [
        "agent",
        "user",
        "judge",
    ]
    for e in history:
        if e["event"] == "response":
            assert json.loads(base64.b64decode(e["raw_base64"])) == e["response"]
    summary = accounting.read_snapshot(client.ledger).summary()
    assert summary["known_cost_usd"] == "0.0003"
    assert summary["pending_upper_bound_usd"] == "0"
    assert not summary["dispatch_authorized"]


@pytest.mark.parametrize(
    "finish,message",
    [
        ("length", {"role": "assistant", "content": "partial"}),
        ("stop", {"role": "assistant", "content": None, "refusal": "declined"}),
        ("content_filter", {"role": "assistant", "content": None}),
        (
            "tool_calls",
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "fresh",
                        "type": "function",
                        "function": {"name": "bad_tool", "arguments": "{invalid"},
                    }
                ],
            },
        ),
    ],
)
def test_billable_quality_failures_reach_the_application_unchanged(
    tmp_path, provider, finish, message
):
    client = transport(tmp_path, provider)
    provider.mutate = lambda body: {
        **body,
        "choices": [{"finish_reason": finish, "message": message}],
    }
    reply = client.call(call_id="call", request=request(), metadata=metadata())
    assert reply["choices"][0]["message"] == message
    result = next(e for e in rows(client) if e["event"] == "result")
    assert result["finish_reason"] == finish
    assert result["message"] == message
    assert result["paper_evidence"] is False


def test_foreground_completes_while_probe_and_its_reservation_are_in_flight(
    tmp_path, provider
):
    client = transport(tmp_path, provider)
    background = request()
    background["messages"][-1]["content"] = "background"
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(
            client.call,
            call_id="probe",
            request=background,
            metadata=metadata(kind="exploration"),
        )
        assert provider.background_started.wait(4)
        snapshot = accounting.read_snapshot(client.ledger)
        assert [r["id"] for r in snapshot.state["pending"]] == ["probe"]
        foreground = pool.submit(
            client.call, call_id="foreground", request=request(), metadata=metadata()
        )
        reply = foreground.result(timeout=4)
        assert reply["choices"][0]["message"]["content"] == "answer"
        assert not future.done()
        assert len(provider.posts) == 2
        assert [
            r["id"] for r in accounting.read_snapshot(client.ledger).state["pending"]
        ] == ["probe"]
        provider.release_background.set()
        future.result(timeout=4)
    assert accounting.read_snapshot(client.ledger).state["pending_usd"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "error",
        "wrong_model",
        "byok",
        "cost",
        "missing_cost",
        "token_bool",
        "route",
        "retry",
        "pipeline",
        "no_metadata",
    ],
)
def test_provider_contract_failures_retain_allowance_and_stop_further_dispatch(
    tmp_path, provider, mutation
):
    client = transport(tmp_path, provider)

    def mutate(body):
        if mutation == "error":
            body["error"] = {"code": 503, "message": "private error"}
        elif mutation == "wrong_model":
            body["model"] = "other/model"
        elif mutation == "byok":
            body["usage"]["is_byok"] = True
        elif mutation == "cost":
            body["usage"]["cost"] = "2"
        elif mutation == "missing_cost":
            del body["usage"]["cost"]
        elif mutation == "token_bool":
            body["usage"]["prompt_tokens"] = True
        elif mutation == "route":
            body["openrouter_metadata"]["endpoints"]["available"][0]["provider"] = (
                "Other"
            )
        elif mutation == "retry":
            body["openrouter_metadata"]["attempt"] = 2
        elif mutation == "pipeline":
            body["openrouter_metadata"]["pipeline"] = [{"type": "response_healing"}]
        else:
            del body["openrouter_metadata"]
        return body

    provider.mutate = mutate
    with pytest.raises(CampaignStopped) as error:
        client.call(call_id="call", request=request(), metadata=metadata())
    assert "private" not in str(error.value) and KEY not in str(error.value)
    state = accounting.read_snapshot(client.ledger).state
    assert state["failed_calls"] == {"call"}
    assert state["pending_usd"] == 0
    bound = CONTRACT.bound(prepare_request(CONTRACT, request()))
    assert state["known_usd"] + state["holds_usd"] == max(
        bound, accounting.money("2") if mutation == "cost" else bound
    )
    before = len(provider.gets)
    with pytest.raises(CampaignStopped):
        client.call(call_id="later", request=request(), metadata=metadata())
    assert len(provider.posts) == 1
    assert len(provider.gets) == before


@pytest.mark.parametrize(
    "status,cache,raw",
    [
        (429, "MISS", None),
        (302, "MISS", None),
        (200, "HIT", None),
        (200, "UNKNOWN", None),
        (200, "MISS", b"{broken"),
    ],
)
def test_http_errors_cache_hits_and_invalid_json_are_not_retried(
    tmp_path, provider, status, cache, raw
):
    client = transport(tmp_path, provider)
    provider.status, provider.cache, provider.raw = status, cache, raw
    # The fixture uses the raw override for POST only in this test.
    old_raw = provider.raw
    provider.raw = None
    provider.mutate = lambda body: setattr(provider, "raw", old_raw) or body
    with pytest.raises(CampaignStopped):
        client.call(call_id="call", request=request(), metadata=metadata())
    history = rows(client)
    assert len(provider.posts) == 1
    assert [e["event"] for e in history][-2:] == ["response", "attempt_failure"]
    receipt = history[-2]
    assert receipt["http_status"] == status
    assert base64.b64decode(receipt["raw_base64"]) == (
        old_raw or json.dumps(response("generation-1")).encode()
    )


def test_duplicate_generation_is_charged_and_held_without_replay(tmp_path, provider):
    client = transport(tmp_path, provider)
    provider.mutate = lambda body: {**body, "id": "repeated-generation"}
    client.call(call_id="first", request=request(), metadata=metadata())
    with pytest.raises(CampaignStopped):
        client.call(call_id="second", request=request(), metadata=metadata())
    assert len(provider.posts) == 2
    assert accounting.read_snapshot(client.ledger).state["failed_calls"] == {"second"}


def test_reused_call_does_not_return_a_cached_result(tmp_path, provider):
    client = transport(tmp_path, provider)
    client.call(call_id="first", request=request(), metadata=metadata())
    with pytest.raises(CampaignStopped):
        client.call(call_id="first", request=request(), metadata=metadata())
    assert len(provider.posts) == 1


@pytest.mark.parametrize(
    "mode",
    [
        "stage",
        "external_spend",
        "key_balance",
        "usage_reset",
        "byok",
        "price",
        "price_tier",
        "unknown_price",
        "capability",
    ],
)
def test_live_funding_and_endpoint_failures_precede_paid_reservation(
    tmp_path, provider, mode
):
    client = transport(tmp_path, provider, cap="0.0005" if mode == "stage" else "1")
    if mode == "external_spend":
        provider.account["usage"] = "56.9999"
    elif mode == "key_balance":
        provider.account["limit_remaining"] = "0.0001"
    elif mode == "usage_reset":
        provider.account["usage"] = "6"
    elif mode == "byok":
        provider.account["byok_usage"] = "0.1"
    elif mode == "price":
        provider.endpoint["pricing"]["prompt"] = "0.1"
    elif mode == "price_tier":
        provider.endpoint["pricing"]["overrides"] = [
            {"min_prompt_tokens": 20, "completion": "0.1"}
        ]
    elif mode == "unknown_price":
        provider.endpoint["pricing"]["reasoning"] = "0.1"
    elif mode == "capability":
        provider.endpoint["supported_parameters"] = []
    with pytest.raises(CampaignStopped):
        client.call(call_id="call", request=request(), metadata=metadata())
    assert provider.posts == []
    assert not any(e["event"] == "reserve" for e in rows(client))


def test_prior_abandoned_allowance_counts_against_both_account_and_campaign(
    tmp_path, provider
):
    claim = event(
        "reserve",
        id="old",
        stage="old",
        attempt_id="old",
        fingerprint="old",
        upper_cost_usd="49.9995",
    )
    terminal = event(
        "attempt_abandoned",
        id="old",
        stage="old",
        attempt_id="old",
        reserve_sha256=accounting.digest(claim),
        budget_hold_usd=claim["upper_cost_usd"],
        reported_cost_usd=None,
    )
    client = transport(
        tmp_path, provider, rows=[event("origin", usage_usd="7"), claim, terminal]
    )
    with pytest.raises(CampaignStopped):
        client.call(call_id="call", request=request(), metadata=metadata())
    assert provider.posts == []
    assert accounting.read_snapshot(client.ledger).state[
        "holds_usd"
    ] == accounting.money("49.9995")


def test_inflight_bound_is_reserved_before_parallel_dispatch(tmp_path, provider):
    bound = CONTRACT.bound(prepare_request(CONTRACT, request()))
    client = transport(
        tmp_path, provider, cap=str(bound * 2 - accounting.money("0.0000001"))
    )
    background = request()
    background["messages"][-1]["content"] = "background"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.call,
            call_id="probe",
            request=background,
            metadata=metadata(kind="shadow"),
        )
        assert provider.background_started.wait(4)
        with pytest.raises(CampaignStopped):
            client.call(call_id="next", request=request(), metadata=metadata())
        assert len(provider.posts) == 1
        provider.release_background.set()
        future.result(timeout=4)
    assert accounting.read_snapshot(client.ledger).state[
        "known_usd"
    ] == accounting.money("0.0001")


@pytest.mark.parametrize("slow_read", [False, True])
def test_whole_operation_deadline_retains_bound_even_if_socket_makes_progress(
    tmp_path, provider, slow_read
):
    provider.http.deadline_seconds = 1.5
    provider.block_all = not slow_read
    provider.stall_read = slow_read
    client = transport(tmp_path, provider)
    started = time.monotonic()
    with pytest.raises(CampaignStopped):
        client.call(call_id="timeout", request=request(), metadata=metadata())
    assert time.monotonic() - started < 5
    state = accounting.read_snapshot(client.ledger).state
    assert state["pending_usd"] == 0
    assert state["known_usd"] + state["holds_usd"] == CONTRACT.bound(
        prepare_request(CONTRACT, request())
    )
    assert len(provider.posts) == 1


def test_a_second_dispatcher_cannot_claim_an_existing_process_reservation(
    tmp_path, provider
):
    client = transport(tmp_path, provider)
    background = request()
    background["messages"][-1]["content"] = "background"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.call, call_id="probe", request=background, metadata=metadata()
        )
        assert provider.background_started.wait(4)
        snapshot = accounting.read_snapshot(client.ledger)
        with pytest.raises(CampaignStopped):
            CampaignTransport(
                ledger=client.ledger,
                expected_sha256=snapshot.sha256,
                key=KEY,
                stage="second-stage",
                stage_cap_usd="1",
                contracts=[CONTRACT],
                http=provider.http,
            )
        assert len(provider.posts) == 1
        provider.release_background.set()
        future.result(timeout=4)


@pytest.mark.parametrize(
    "mode", ["missing", "changed_prefix", "wrong_key", "same_stage"]
)
def test_enrollment_never_creates_resets_or_resumes_a_journal(tmp_path, provider, mode):
    path = tmp_path / "journal"
    sha = write(path, [event("origin", usage_usd="7")])
    if mode == "missing":
        path.unlink()
    elif mode == "changed_prefix":
        sha = "0" * 64
    elif mode == "same_stage":
        client = CampaignTransport(
            ledger=path,
            expected_sha256=sha,
            key=KEY,
            stage="stage",
            stage_cap_usd="1",
            contracts=[CONTRACT],
            http=provider.http,
        )
        sha = accounting.read_snapshot(client.ledger).sha256
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(CampaignStopped):
        CampaignTransport(
            ledger=path,
            expected_sha256=sha,
            key="different" if mode == "wrong_key" else KEY,
            stage="stage",
            stage_cap_usd="1",
            contracts=[CONTRACT],
            http=provider.http,
        )
    assert (path.read_bytes() if path.exists() else None) == before
    assert provider.posts == []


@pytest.mark.parametrize(
    "mutation",
    [
        "orphan",
        "missing_result",
        "duplicate_tool",
        "cache_control",
        "plugins",
        "stream",
        "nan",
        "cap",
        "tools",
        "bad_choice",
    ],
)
def test_unenrolled_or_corrupted_requests_never_reach_provider(
    tmp_path, provider, mutation
):
    client = transport(tmp_path, provider)
    value = tool_history()
    if mutation == "orphan":
        value["messages"][-1]["tool_call_id"] = "missing"
    elif mutation == "missing_result":
        value["messages"].pop()
    elif mutation == "duplicate_tool":
        value["messages"][2]["tool_calls"][1]["id"] = "a"
    elif mutation == "cache_control":
        value["messages"][0]["cache_control"] = {"type": "ephemeral"}
    elif mutation == "plugins":
        value["plugins"] = [{"id": "web"}]
    elif mutation == "stream":
        value["stream"] = True
    elif mutation == "nan":
        value["temperature"] = float("nan")
    elif mutation == "cap":
        value["max_tokens"] = 17
    elif mutation == "tools":
        value["tools"] = [{"type": "web_search"}]
    else:
        value["tool_choice"] = {"type": "function", "function": {"name": "unknown"}}
    with pytest.raises(CampaignStopped):
        client.call(call_id="call", request=value, metadata=metadata())
    assert provider.posts == [] and provider.gets == []


def test_sdk_style_exception_fallback_cannot_retry_fatal_dispatch(tmp_path, provider):
    client = transport(tmp_path, provider, cap="0.0001")
    fallback = []
    with pytest.raises(CampaignStopped):
        try:
            client.call(
                call_id="candidate",
                request=request(),
                metadata=metadata(kind="exploration"),
            )
        except Exception:
            fallback.append("unreserved retry")
    assert fallback == []


def test_real_process_loss_retains_the_probe_allowance(tmp_path, provider):
    client = transport(tmp_path, provider)
    background = request()
    background["messages"][-1]["content"] = "background"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.call,
            call_id="probe",
            request=background,
            metadata=metadata(kind="exploration"),
        )
        assert provider.background_started.wait(4)
        with client._mutex:
            operation = client._active["probe"][1]
            operation.process.terminate()
            operation.process.join(timeout=1)
        with pytest.raises(CampaignStopped):
            future.result(timeout=4)
    state = accounting.read_snapshot(client.ledger).state
    assert state["failed_calls"] == {"probe"}
    assert state["holds_usd"] == CONTRACT.bound(prepare_request(CONTRACT, request()))
    assert provider.posts and len(provider.posts) == 1


def test_parent_cancellation_retains_full_bound_before_propagating(
    tmp_path, provider, monkeypatch
):
    client = transport(tmp_path, provider)
    original_start = provider.http.start

    def start(key, requests):
        operation = original_start(key, requests)
        if requests[0][1] is not None:

            def cancel():
                raise KeyboardInterrupt

            operation.receive = cancel
        return operation

    monkeypatch.setattr(provider.http, "start", start)
    with pytest.raises(KeyboardInterrupt):
        client.call(call_id="cancel", request=request(), metadata=metadata())
    state = accounting.read_snapshot(client.ledger).state
    assert state["failed_calls"] == {"cancel"}
    assert state["known_usd"] + state["holds_usd"] == CONTRACT.bound(
        prepare_request(CONTRACT, request())
    )
    # No assumption about whether the provider received the dispatched request.
    assert len(provider.posts) <= 1


def test_existing_parent_alarm_is_not_taken_over(tmp_path, provider):
    client = transport(tmp_path, provider)
    original_handler = signal.getsignal(signal.SIGALRM)
    original_timer = signal.getitimer(signal.ITIMER_REAL)
    assert original_timer == (0.0, 0.0)
    fired = []

    def handler(*args):
        fired.append(True)

    try:
        signal.signal(signal.SIGALRM, handler)
        signal.setitimer(signal.ITIMER_REAL, 30)
        client.call(call_id="call", request=request(), metadata=metadata())
        assert signal.getsignal(signal.SIGALRM) is handler
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert 20 < remaining < 30 and interval == 0
        assert fired == []
    finally:
        signal.setitimer(signal.ITIMER_REAL, *original_timer)
        signal.signal(signal.SIGALRM, original_handler)


def test_lagging_account_balance_includes_successful_but_unconfirmed_charge(
    tmp_path, provider
):
    client = transport(tmp_path, provider)
    client.call(call_id="first", request=request(), metadata=metadata())
    bound = CONTRACT.bound(prepare_request(CONTRACT, request()))
    provider.account["limit_remaining"] = str(bound + accounting.money("0.00005"))
    with pytest.raises(CampaignStopped):
        client.call(call_id="second", request=request(), metadata=metadata())
    assert len(provider.posts) == 1


def historical_charge():
    return [
        event("origin", usage_usd="7"),
        event("reserve", id="old", stage="old", attempt_id="old",
              fingerprint="old", upper_cost_usd="0.1", account_usage_before_usd="15"),
        event("result", id="old", stage="old", attempt_id="old",
              fingerprint="old", cost_usd="0.01"),
    ]


@pytest.mark.parametrize("next_usage", ["15", "16"])
def test_external_usage_cannot_clear_completed_campaign_charge(
    tmp_path, provider, next_usage
):
    bound = CONTRACT.bound(prepare_request(CONTRACT, request()))
    balance = accounting.money("0.01") + bound + accounting.money("0.00005")
    provider.account.update(usage="15", limit_remaining=str(balance))
    client = transport(tmp_path, provider, rows=historical_charge())
    client.call(call_id="first", request=request(), metadata=metadata())
    provider.account["usage"] = next_usage
    assert accounting.money(client.budget_snapshot()["remaining_upper_usd"]) == (
        balance - accounting.money("0.0101")
    )
    before = client.ledger.read_bytes()
    with pytest.raises(CampaignStopped):
        client.call(call_id="second", request=request(), metadata=metadata(actor="judge"))
    assert client.ledger.read_bytes() == before
    snapshot = accounting.read_snapshot(client.ledger)
    assert snapshot.state["known_usd"] == accounting.money("0.0101")
    assert snapshot.state["pending_usd"] == 0
    assert client.budget_snapshot()["remaining_upper_usd"] == "0"
    assert client.budget_snapshot()["blocked"]
    assert len(provider.posts) == 1
    assert rows(client)[3]["policy"] == chat.POLICY


def test_external_usage_keeps_pending_shadow_and_completed_judge_charged(
    tmp_path, provider
):
    bound = CONTRACT.bound(prepare_request(CONTRACT, request()))
    provider.account.update(
        usage="15",
        limit_remaining=str(accounting.money("0.01") + 2 * bound + accounting.money("0.00005")),
    )
    client = transport(tmp_path, provider, rows=historical_charge())
    background = request()
    background["messages"][-1]["content"] = "background"
    with ThreadPoolExecutor(max_workers=1) as pool:
        shadow = pool.submit(client.call, call_id="shadow", request=background,
                             metadata=metadata(kind="exploration"))
        try:
            assert provider.background_started.wait(4)
            client.call(call_id="judge", request=request(), metadata=metadata(actor="judge"))
            provider.account["usage"] = "16"
            state = accounting.read_snapshot(client.ledger).state
            assert state["known_usd"] == accounting.money("0.0101")
            assert state["pending_usd"] == bound
            before = client.ledger.read_bytes()
            with pytest.raises(CampaignStopped):
                client.call(call_id="third", request=request(), metadata=metadata())
            assert client.ledger.read_bytes() == before
            assert len(provider.posts) == 2 and not shadow.done()
        finally:
            provider.release_background.set()
        shadow.result(timeout=4)
    state = accounting.read_snapshot(client.ledger).state
    assert state["known_usd"] == accounting.money("0.0102")
    assert state["pending_usd"] == 0


@pytest.mark.parametrize("remaining", [None, "0.02"])
def test_external_usage_with_adequate_funding_preserves_original_debits(
    tmp_path, provider, remaining
):
    provider.account.update(usage="15", limit_remaining=remaining)
    client = transport(tmp_path, provider, rows=historical_charge())
    client.call(call_id="first", request=request(), metadata=metadata())
    provider.account["usage"] = "16"
    client.call(call_id="second", request=request(), metadata=metadata(actor="judge"))
    snapshot = accounting.read_snapshot(client.ledger)
    assert snapshot.state["known_usd"] == accounting.money("0.0102")
    assert snapshot.state["pending_usd"] == 0
    projection = client.budget_snapshot()
    assert projection["remaining_upper_usd"] == ("0.9998" if remaining is None else "0.0098")
    assert not projection["live_account_reconciled"] and not projection["dispatch_authorized"]
    assert len(provider.posts) == 2


def test_legacy_funding_history_remains_auditable_but_new_stage_is_strict(
    tmp_path, provider, monkeypatch
):
    # Produce an original v1 fixture with its former admission rule, then restore
    # current code before auditing or enrolling any new work. No history rewrite.
    bound = CONTRACT.bound(prepare_request(CONTRACT, request()))
    provider.account.update(usage="15", limit_remaining=str(bound))
    with monkeypatch.context() as old:
        old.setattr("bench.campaign_transport.POLICY", chat.LEGACY_POLICY)
        old.setattr("bench.campaign_transport.check_funding",
                    lambda *args: chat._check_funding(*args, legacy=True))
        client = transport(tmp_path, provider, rows=historical_charge())
        client.call(call_id="old-first", request=request(), metadata=metadata())
        client.call(call_id="old-second", request=request(), metadata=metadata())
    before = client.ledger.read_bytes()
    snapshot = accounting.read_snapshot(client.ledger)
    assert snapshot.state["known_usd"] == accounting.money("0.0102")
    assert client.ledger.read_bytes() == before
    current = CampaignTransport(
        ledger=client.ledger, expected_sha256=snapshot.sha256, key=KEY,
        stage="new-stage", stage_cap_usd="1", contracts=[CONTRACT], http=provider.http,
    )
    enrolled = current.ledger.read_bytes()
    assert rows(current)[-1]["policy"] == chat.POLICY
    with pytest.raises(CampaignStopped):
        current.call(call_id="new", request=request(), metadata=metadata())
    assert current.ledger.read_bytes() == enrolled
    assert current.budget_snapshot()["remaining_upper_usd"] == "0"
    assert len(provider.posts) == 2


def test_reservation_fsync_failure_prevents_network_dispatch(
    tmp_path, provider, monkeypatch
):
    client = transport(tmp_path, provider)

    def failed_sync(descriptor):
        raise OSError("fixture fsync failure")

    monkeypatch.setattr("bench.campaign_transport.os.fsync", failed_sync)
    with pytest.raises(CampaignStopped):
        client.call(call_id="call", request=request(), metadata=metadata())
    assert provider.posts == []
    state = accounting.read_snapshot(client.ledger).state
    assert state["pending_usd"] + state["known_usd"] + state[
        "holds_usd"
    ] == CONTRACT.bound(prepare_request(CONTRACT, request()))


def test_torn_post_dispatch_journal_stays_unresolved_and_is_never_recreated(
    tmp_path, provider, monkeypatch
):
    client = transport(tmp_path, provider)
    append = client._append

    def tear(stream, events, record):
        if record["event"] == "response":
            stream.seek(0, 2)
            stream.write(b'{"event":"response"')
            stream.flush()
            raise OSError("fixture torn write")
        append(stream, events, record)

    monkeypatch.setattr(client, "_append", tear)
    with pytest.raises(CampaignStopped):
        client.call(call_id="call", request=request(), metadata=metadata())
    before = client.ledger.read_bytes()
    with pytest.raises(accounting.AccountingError):
        accounting.read_snapshot(client.ledger)
    with pytest.raises(CampaignStopped):
        client.call(call_id="later", request=request(), metadata=metadata())
    assert client.ledger.read_bytes() == before
    assert len(provider.posts) == 1


def test_replaced_enrolled_inode_prevents_dispatch(tmp_path, provider):
    client = transport(tmp_path, provider)
    contents = client.ledger.read_bytes()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(contents)
    replacement.replace(client.ledger)
    with pytest.raises(CampaignStopped):
        client.call(call_id="call", request=request(), metadata=metadata())
    assert provider.posts == []
    assert client.ledger.read_bytes() == contents


@pytest.mark.parametrize(
    "mutation",
    [
        "raw",
        "response",
        "stage",
        "reserve_hash",
        "cost",
        "finish",
        "session",
        "prefix",
        "request",
        "failure_hold",
    ],
)
def test_offline_audit_rejects_unbound_fresh_lifecycle_receipts(
    tmp_path, provider, mutation
):
    client = transport(tmp_path, provider)
    if mutation == "failure_hold":
        provider.status = 429
        with pytest.raises(CampaignStopped):
            client.call(call_id="call", request=request(), metadata=metadata())
    else:
        client.call(call_id="call", request=request(), metadata=metadata())
    history = rows(client)
    record = history[-1]
    receipt = history[-2]
    if mutation == "raw":
        receipt["raw_base64"] = base64.b64encode(b"{}").decode()
    elif mutation == "response":
        receipt["response"]["usage"]["cost"] = "0"
    elif mutation == "stage":
        record["stage"] = "other"
    elif mutation == "reserve_hash":
        record["reserve_sha256"] = "0" * 64
    elif mutation == "cost":
        record["cost_usd"] = "0"
    elif mutation == "finish":
        record["finish_reason"] = "length"
    elif mutation == "session":
        record["session_id"] = "other"
    elif mutation == "prefix":
        history[1]["prefix_sha256"] = "0" * 64
    elif mutation == "request":
        history[2]["request"]["messages"][0]["content"] = "different"
    else:
        record["budget_hold_usd"] = "0"
    write(client.ledger, history)
    with pytest.raises(accounting.AccountingError):
        accounting.read_snapshot(client.ledger)


@pytest.mark.parametrize(
    "field,value",
    [
        ("endpoints", None),
        ("endpoints", {"available": None}),
        ("attempts", []),
        ("attempt", True),
    ],
)
def test_malformed_router_metadata_is_durably_failed_without_losing_charge(
    tmp_path, provider, field, value
):
    client = transport(tmp_path, provider)

    def mutate(body):
        body["openrouter_metadata"][field] = value
        return body

    provider.mutate = mutate
    with pytest.raises(CampaignStopped):
        client.call(call_id="call", request=request(), metadata=metadata())
    state = accounting.read_snapshot(client.ledger).state
    assert state["failed_calls"] == {"call"}
    assert state["known_usd"] == accounting.money("0.0001")


def test_completion_gate_catches_swallowed_probe_failure(tmp_path, provider):
    client = transport(tmp_path, provider)
    provider.status = 503
    try:
        client.call(
            call_id="probe", request=request(), metadata=metadata(kind="exploration")
        )
    except BaseException:
        pass
    with pytest.raises(CampaignStopped):
        client.assert_complete()
    assert accounting.read_snapshot(client.ledger).state["failed_calls"] == {"probe"}


def test_completion_gate_rejects_pending_work_then_accepts_real_completion(
    tmp_path, provider
):
    client = transport(tmp_path, provider)
    background = request()
    background["messages"][-1]["content"] = "background"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.call,
            call_id="probe",
            request=background,
            metadata=metadata(kind="exploration"),
        )
        assert provider.background_started.wait(4)
        with pytest.raises(CampaignStopped):
            client.assert_complete()
        provider.release_background.set()
        future.result(timeout=4)
    assert client.assert_complete()["unresolved_calls"] == []


def test_explicit_nonstreaming_flag_and_judge_schema_are_preserved(tmp_path, provider):
    client = transport(tmp_path, provider)
    value = request()
    value["stream"] = False
    value["response_format"] = {
        "type": "json_schema",
        "json_schema": {
            "name": "judge",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "assertions": {"type": "array", "items": {"type": "boolean"}}
                },
                "required": ["assertions"],
                "additionalProperties": False,
            },
        },
    }
    client.call(call_id="judge", request=value, metadata=metadata("judge"))
    assert provider.posts[0]["stream"] is False
    assert provider.posts[0]["response_format"] == value["response_format"]
    result = rows(client)[-1]
    assert result["latency_ms"] > 0
    assert result["pre_dispatch_ms"] > 0
    assert client.assert_complete()["known_cost_usd"] == "0.0001"


def test_truncated_completed_call_cannot_reset_a_live_dispatcher(tmp_path, provider):
    client = transport(tmp_path, provider)
    enrollment = client.ledger.read_bytes()
    client.call(call_id="first", request=request(), metadata=metadata())
    client.ledger.write_bytes(enrollment)
    with pytest.raises(CampaignStopped):
        client.call(call_id="first", request=request(), metadata=metadata())
    assert len(provider.posts) == 1
    assert client.ledger.read_bytes() == enrollment


def test_path_replacement_during_reservation_sync_prevents_paid_dispatch(
    tmp_path, provider, monkeypatch
):
    client = transport(tmp_path, provider)
    contents = client.ledger.read_bytes()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(contents)
    original_sync = os.fsync

    def replace_on_sync(descriptor):
        original_sync(descriptor)
        replacement.replace(client.ledger)

    monkeypatch.setattr("bench.campaign_transport.os.fsync", replace_on_sync)
    with pytest.raises(CampaignStopped):
        client.call(call_id="call", request=request(), metadata=metadata())
    assert provider.posts == []
    assert client.ledger.read_bytes() == contents


def test_real_sdk_dispatch_does_not_fallback_after_fatal_budget_stop(
    tmp_path, provider
):
    from agentc._optimizer import Plan
    from agentc._patches._optimizer_glue import dispatch_sync

    client = transport(tmp_path, provider, cap="0.0001")
    original_calls = []

    def original():
        original_calls.append(True)
        return client.call(
            call_id="fallback", request=request(), metadata=metadata(kind="fallback")
        )

    with pytest.raises(CampaignStopped):
        dispatch_sync(
            Plan(kind="rewritten", call=request()),
            run_original=original,
            run_mutated=lambda value: client.call(
                call_id="candidate", request=value, metadata=metadata()
            ),
        )
    assert original_calls == []
    assert provider.posts == []


def test_process_cleanup_failure_is_fatal_after_a_durable_billed_result(
    tmp_path, provider, monkeypatch
):
    client = transport(tmp_path, provider)
    original_start = provider.http.start

    def start(key, requests):
        operation = original_start(key, requests)
        close = operation.close
        if requests[0][1] is not None:

            def fail_close():
                close()
                raise OSError("fixture process cleanup failure")

            operation.close = fail_close
        return operation

    monkeypatch.setattr(provider.http, "start", start)
    with pytest.raises(CampaignStopped):
        client.call(call_id="call", request=request(), metadata=metadata())
    state = accounting.read_snapshot(client.ledger).state
    assert state["known_usd"] == accounting.money("0.0001")
    assert state["pending_usd"] == state["holds_usd"] == 0
    assert len(provider.posts) == 1
    with pytest.raises(CampaignStopped):
        client.assert_complete()


def _partial_ipc_fixture(connection, key, operations):
    try:
        os.write(connection.fileno(), struct.pack("!i", 1000) + b"partial")
        connection.recv_bytes()
    finally:
        connection.close()


def test_total_deadline_also_bounds_a_child_stalled_inside_an_ipc_frame(monkeypatch):
    from bench import campaign_http

    monkeypatch.setattr(campaign_http, "_http_worker", _partial_ipc_fixture)
    http = CampaignHTTP.local_test("http://127.0.0.1:1/api/v1", deadline_seconds=1.5)
    operation = http.start(KEY, [("/key", None)])
    started = time.monotonic()
    try:
        with pytest.raises(accounting.AccountingError, match="total deadline"):
            operation.receive()
        assert time.monotonic() - started < 3
    finally:
        operation.close()


def test_audit_module_cli_sanitizes_fresh_validator_failures(tmp_path, provider):
    client = transport(tmp_path, provider)
    client.call(call_id="call", request=request(), metadata=metadata())
    history = rows(client)
    history[-1]["cost_usd"] = "0"
    write(client.ledger, history)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "bench.campaign_accounting",
            "--ledger",
            str(client.ledger),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 1
    assert "Journal audit failed:" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert completed.stdout == ""
    assert KEY not in completed.stderr


@pytest.mark.parametrize("historical", [False, True])
def test_account_usage_regression_above_origin_cannot_restore_headroom(
    tmp_path, provider, historical
):
    original = event("origin", usage_usd="7")
    if historical:
        claim = event(
            "reserve",
            id="old",
            stage="old",
            attempt_id="old",
            fingerprint="old",
            upper_cost_usd="0.1",
            account_usage_before_usd="8",
        )
        paid = event(
            "result",
            id="old",
            stage="old",
            attempt_id="old",
            fingerprint="old",
            cost_usd="0.01",
        )
        client = transport(tmp_path, provider, rows=[original, claim, paid])
    else:
        client = transport(tmp_path, provider)
        provider.account["usage"] = "8"
        client.call(call_id="first", request=request(), metadata=metadata())
    count = len(provider.posts)
    provider.account["usage"] = "7.5"
    with pytest.raises(CampaignStopped):
        client.call(call_id="next", request=request(), metadata=metadata())
    assert len(provider.posts) == count


def test_graph_budget_snapshot_is_read_only_and_bound_to_original_session(tmp_path, provider):
    client = transport(tmp_path, provider)
    before = client.ledger.read_bytes()
    first = client.budget_snapshot()
    assert client.ledger.read_bytes() == before
    assert provider.gets == provider.posts == []
    assert first["remaining_upper_usd"] == "1"
    assert not first["blocked"] and not first["dispatch_authorized"]
    assert not first["live_account_reconciled"]
    assert first["snapshot_sha256"] == accounting.digest({k: v for k, v in first.items() if k != "snapshot_sha256"})
    assert first["binding"]["session_sha256"] == accounting.digest(rows(client)[-1])
    assert KEY not in json.dumps(first) and "fixture input" not in json.dumps(first)
    client.call(call_id="judge", request=request(), metadata=metadata(actor="judge"))
    after = client.ledger.read_bytes()
    gets = len(provider.gets)
    second = client.budget_snapshot()
    assert second["binding"] == first["binding"]
    assert second["journal_bytes"] > first["journal_bytes"]
    assert second["remaining_upper_usd"] == "0.9999"
    assert second["known_cost_usd"] == second["stage_committed_usd"] == "0.0001"
    assert client.ledger.read_bytes() == after and len(provider.gets) == gets


def test_graph_budget_snapshot_charges_pending_other_actor_and_releases_only_unused_bound(tmp_path, provider):
    client = transport(tmp_path, provider)
    value = request()
    value["messages"][-1]["content"] = "background"
    bound = CONTRACT.bound(prepare_request(CONTRACT, value))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.call, call_id="simulator", request=value, metadata=metadata(actor="user", kind="shadow"))
        try:
            assert provider.background_started.wait(4)
            snapshot = client.budget_snapshot()
            assert not snapshot["blocked"]
            assert accounting.money(snapshot["pending_upper_bound_usd"]) == bound
            assert accounting.money(snapshot["remaining_upper_usd"]) == accounting.money("1") - bound
            owner = client._active.pop("simulator")
            try:
                lost = client.budget_snapshot()
                assert lost["blocked"] and lost["remaining_upper_usd"] == "0"
                assert lost["pending_upper_bound_usd"] == snapshot["pending_upper_bound_usd"]
            finally:
                client._active["simulator"] = owner
        finally:
            provider.release_background.set()
        future.result(timeout=4)
    settled = client.budget_snapshot()
    assert settled["pending_upper_bound_usd"] == "0"
    assert settled["remaining_upper_usd"] == "0.9999"


def test_graph_budget_snapshot_keeps_failed_work_charged_and_blocks_forecasts(tmp_path, provider):
    client = transport(tmp_path, provider)
    provider.status = 500
    with pytest.raises(CampaignStopped):
        client.call(call_id="failed", request=request(), metadata=metadata())
    before = client.ledger.read_bytes()
    snapshot = client.budget_snapshot()
    bound = CONTRACT.bound(prepare_request(CONTRACT, request()))
    assert snapshot["blocked"] and snapshot["remaining_upper_usd"] == "0"
    assert accounting.money(snapshot["known_cost_usd"]) + accounting.money(snapshot["retained_uncertainty_usd"]) == bound
    assert client.ledger.read_bytes() == before


def test_graph_budget_snapshot_uses_original_global_holds_and_stored_usage_floor(tmp_path, provider):
    claim = event("reserve", id="historical", stage="old", attempt_id="old", fingerprint="old", upper_cost_usd="49.9995", account_usage_before_usd="7")
    history = [event("origin", usage_usd="7"), claim,
               event("attempt_abandoned", id="historical", stage="old", attempt_id="old", reserve_sha256=accounting.digest(claim), budget_hold_usd=claim["upper_cost_usd"], reported_cost_usd=None)]
    client = transport(tmp_path, provider, rows=history)
    snapshot = client.budget_snapshot()
    assert snapshot["remaining_upper_usd"] == "0.0005"
    assert snapshot["retained_uncertainty_usd"] == "49.9995"
    assert snapshot["stage_committed_usd"] == "0"


@pytest.mark.parametrize("field,value", [("cap", accounting.money("2")), ("stage", "replacement"), ("session_id", "replacement"), ("contracts", {}), ("baseline", accounting.money("8"))])
def test_graph_budget_snapshot_rejects_mutable_session_or_policy_changes(tmp_path, provider, field, value):
    client = transport(tmp_path, provider)
    before = client.ledger.read_bytes()
    setattr(client, field, value)
    with pytest.raises(CampaignStopped):
        client.budget_snapshot()
    assert client.ledger.read_bytes() == before and provider.posts == []


def test_graph_budget_snapshot_keeps_historical_account_usage_and_key_liabilities(tmp_path, provider):
    claim = event("reserve", id="old", stage="old", attempt_id="old", fingerprint="old", upper_cost_usd="0.1", account_usage_before_usd="15")
    result = event("result", id="old", stage="old", attempt_id="old", fingerprint="old", cost_usd="0.01")
    client = transport(tmp_path, provider, cap="49", rows=[event("origin", usage_usd="7"), claim, result])
    first = client.budget_snapshot()
    assert first["conservative_committed_usd"] == "8"
    assert first["remaining_upper_usd"] == "42"
    provider.account.update(usage="15", limit_remaining="0.02")
    client.call(call_id="fresh", request=request(), metadata=metadata())
    second = client.budget_snapshot()
    assert second["remaining_upper_usd"] == "0.0099"
    assert second["stage_committed_usd"] == "0.0001"


def test_owned_call_binding_uses_original_hashes_and_survives_other_actor_spend(tmp_path, provider):
    client = transport(tmp_path, provider)
    client.call(call_id="owned", request=request(), metadata=metadata())
    before = client.ledger.read_bytes()
    binding = client.call_binding("owned")
    history = rows(client)
    claim = next(e for e in history if e["event"] == "reserve")
    assert binding["reservation_sha256"] == accounting.digest(claim)
    assert binding["request_sha256"] == accounting.digest(claim["request"])
    assert binding["metadata_sha256"] == accounting.digest(claim["metadata"])
    assert binding["response_sha256"] == accounting.digest(next(e for e in history if e["event"] == "response"))
    assert binding["result_sha256"] == accounting.digest(next(e for e in history if e["event"] == "result"))
    assert binding["status"] == "completed" and binding["failure_sha256"] is None
    assert binding["binding_sha256"] == accounting.digest({k: v for k, v in binding.items() if k != "binding_sha256"})
    assert client.ledger.read_bytes() == before
    assert KEY not in json.dumps(binding) and "fixture input" not in json.dumps(binding)
    client.call(call_id="other", request=request(), metadata=metadata(actor="judge"))
    assert client.call_binding("owned") == binding
    with pytest.raises(CampaignStopped):
        client.call_binding("unclaimed")


def test_owned_call_binding_tracks_pending_and_failed_receipts_without_fake_completion(tmp_path, provider):
    client = transport(tmp_path, provider)
    value = request()
    value["messages"][-1]["content"] = "background"
    provider.status = 500
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.call, call_id="owned", request=value, metadata=metadata())
        try:
            assert provider.background_started.wait(4)
            pending = client.call_binding("owned")
            assert pending["status"] == "pending"
            assert all(pending[n] is None for n in ("response_sha256", "result_sha256", "failure_sha256"))
        finally:
            provider.release_background.set()
        with pytest.raises(CampaignStopped):
            future.result(timeout=4)
    failed = client.call_binding("owned")
    assert failed["status"] == "failed" and failed["result_sha256"] is None
    assert failed["response_sha256"] is not None and failed["failure_sha256"] is not None
    assert failed["reservation_sha256"] == pending["reservation_sha256"]


def test_real_child_bridge_budget_includes_spend_from_other_actors(tmp_path, provider):
    from bench.campaign_rpc import ChildTransport, TransportBridge

    client = transport(tmp_path, provider)
    bridge = TransportBridge(client, cell={"cell_id": "owned", "actor": "agent"}, deadline=time.monotonic() + 10)
    child = ChildTransport(bridge.descriptor())
    try:
        first = child.budget_snapshot()
        assert first == client.budget_snapshot()
        child.call(call_id="agent", request=request(), metadata={**metadata(), "cell_id": "owned"})
        binding = child.call_binding("agent")
        client.call(call_id="judge", request=request(), metadata=metadata(actor="judge"))
        snapshot = child.budget_snapshot()
        assert snapshot["known_cost_usd"] == "0.0002"
        assert snapshot["remaining_upper_usd"] == "0.9998"
        assert snapshot["binding"] == first["binding"]
        assert child.call_binding("agent") == binding == client.call_binding("agent")
        assert len(provider.posts) == 2
        with pytest.raises(CampaignStopped):
            child.call_binding("judge")
    finally:
        bridge.close()


@pytest.mark.parametrize("change", ["rollback", "replacement"])
def test_external_budget_and_call_bindings_reject_changed_original_journal(tmp_path, provider, change):
    client = transport(tmp_path, provider)
    prefix = client.ledger.read_bytes()
    client.call(call_id="owned", request=request(), metadata=metadata())
    if change == "rollback":
        client.ledger.write_bytes(prefix)
    else:
        replacement = tmp_path / "replacement.jsonl"
        replacement.write_bytes(client.ledger.read_bytes())
        replacement.replace(client.ledger)
    count = len(provider.posts)
    with pytest.raises(CampaignStopped):
        client.budget_snapshot()
    with pytest.raises(CampaignStopped):
        client.call_binding("owned")
    assert len(provider.posts) == count
