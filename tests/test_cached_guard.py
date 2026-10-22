"""Cached responses use exact plan feedback and no fresh provider usage."""

from types import SimpleNamespace
from unittest.mock import patch
import importlib
import pickle
import subprocess
import sys

import pytest

from agentc._optimizer import Plan
from agentc._patches._optimizer_glue import (
    build_outcome_anthropic,
    build_outcome_openai,
    cache_response_attributes,
    maybe_shadow_record,
    maybe_shadow_record_async,
)


def response(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=300,
            input_tokens=100,
            output_tokens=300,
            cost=0.05,
            is_byok=False,
        ),
    )


@pytest.mark.parametrize("builder", [build_outcome_openai, build_outcome_anthropic])
@pytest.mark.parametrize("fallback", [False, True])
def test_cached_outcome_separates_historical_usage_from_current_spend(
    builder, fallback
):
    plan = Plan(kind="cached", dispatch_fallback=fallback)
    outcome = builder(
        response("cached answer"),
        elapsed_s=0.01,
        model="gpt-4o",
        call_site_id="cache",
        plan=plan,
    )
    assert outcome["input_tokens"] == (100 if fallback else 0)
    assert outcome["output_tokens"] == (300 if fallback else 0)
    assert (outcome["cost_usd"] > 0) == fallback
    assert outcome["latency_ms"] == 10
    assert outcome["output_is_short"] is False


def test_cached_shadow_compares_once_and_forwards_its_opaque_token(monkeypatch):
    monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "1")
    plan = Plan(kind="cached", observation_token="opaque-cache-token")
    calls = []

    def original():
        calls.append("reference")
        return response("different answer")

    with patch("agentc._optimizer.record_divergence") as record:
        maybe_shadow_record(plan, "cache", response("cached answer"), original)
    assert calls == ["reference"]
    record.assert_called_once()
    assert record.call_args.args[0] == "opaque-cache-token"
    assert record.call_args.args[1] > 0


def test_cache_span_records_primary_zero_usage_without_mutating_history():
    attrs = {
        "gen_ai.usage.input_tokens": 100,
        "gen_ai.usage.output_tokens": 300,
        "gen_ai.response.model": "snapshot",
    }
    cached = cache_response_attributes(
        Plan(kind="cached", value={"source": {"kind": "lsh"}}), attrs
    )
    assert attrs["gen_ai.usage.input_tokens"] == 100
    assert cached["gen_ai.usage.input_tokens"] == 0
    assert cached["gen_ai.usage.output_tokens"] == 0
    assert cached["agentc.memoization.recorded.input_tokens"] == 100
    assert cached["agentc.memoization.source"] == "lsh"
    assert (
        cache_response_attributes(Plan(kind="cached", dispatch_fallback=True), attrs)
        == attrs
    )


@pytest.mark.asyncio
async def test_async_cache_shadow_respects_decode_fallback(monkeypatch):
    monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "1")
    calls = []

    async def original():
        calls.append("reference")
        return response("same")

    with patch("agentc._optimizer.record_divergence") as record:
        await maybe_shadow_record_async(
            Plan(kind="cached", observation_token="token"),
            "cache",
            response("same"),
            original,
        )
        await maybe_shadow_record_async(
            Plan(kind="cached", observation_token="token", dispatch_fallback=True),
            "cache",
            response("same"),
            original,
        )
    assert calls == ["reference"]
    record.assert_called_once_with("token", 0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor", ["openai", "anthropic", "litellm"])
@pytest.mark.parametrize("async_path", [False, True])
async def test_all_adapters_return_decoded_cache_response_and_sample_it(
    vendor, async_path, monkeypatch
):
    module = importlib.import_module("agentc._patches._" + vendor)
    wrappers = {
        "openai": (
            "_wrap_create",
            "_wrap_create_async",
            "_plan_openai_call",
            "_observe_openai_outcome",
        ),
        "anthropic": (
            "_wrap_create",
            "_wrap_create_async",
            "_plan_anthropic_call",
            "_observe_anthropic_outcome",
        ),
        "litellm": ("_wrap_completion", "_wrap_acompletion", "_plan_call", "_observe"),
    }
    sync_name, async_name, planner, observer_name = wrappers[vendor]
    from agentc._response_cache import VERSION

    protocol = {
        "openai": "openai.chat.completions.v1",
        "anthropic": "anthropic.messages.v1",
        "litellm": "litellm.completion.v1",
    }[vendor]
    plan = Plan(
        kind="cached",
        value={
            "output_content_id": "fixture",
            "source": {"kind": "exact"},
            "sdk_response_cache_version": VERSION,
            "provider_protocol": protocol,
            "provider_namespace": vendor,
        },
    )
    monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "1")
    monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "lexical")
    monkeypatch.setattr(module, "_is_initialized", lambda: True)
    monkeypatch.setattr(module, planner, lambda *args: (plan, "cache.fixture"))
    monkeypatch.setattr(module, "_write_root_span", lambda *args, **kwargs: None)
    observations = []

    def observe(*args, **kwargs):
        selected = args[0] if args else kwargs["plan"]
        decoded = args[1] if args else kwargs["response"]
        observations.append(decoded)
        selected.observation_token = "opaque-cache-token"

    monkeypatch.setattr(module, observer_name, observe)
    calls = []

    def original(*args, **kwargs):
        calls.append(kwargs)
        return response("reference answer")

    async def original_async(*args, **kwargs):
        return original(*args, **kwargs)

    request = {
        "model": "fixture-model",
        "messages": [{"role": "user", "content": "task"}],
        "max_tokens": 1024,
    }
    with (
        patch(
            "agentc._native.output_content_load",
            return_value=pickle.dumps(
                {
                    "version": VERSION,
                    "provider_protocol": protocol,
                    "provider_namespace": vendor,
                    "response": response("cached answer"),
                }
            ),
        ),
        patch("agentc._optimizer.record_divergence") as record,
        patch.object(module, "_emit_span") as emit,
    ):
        wrapper = getattr(module, async_name if async_path else sync_name)
        result = (
            await wrapper(original_async, None, (), request)
            if async_path
            else wrapper(original, None, (), request)
        )
    assert result.choices[0].message.content == "cached answer"
    assert observations == [result]
    assert calls == [request]
    record.assert_called_once()
    assert record.call_args.args[0] == "opaque-cache-token"
    assert record.call_args.args[1] > 0
    attrs = emit.call_args.kwargs["attrs"]
    assert attrs["gen_ai.usage.input_tokens"] == 0
    assert attrs["gen_ai.usage.output_tokens"] == 0
    assert attrs["agentc.memoization.hit"] is True


def test_native_payload_loader_reads_merged_store_without_python_sqlite_overlap(
    tmp_path, monkeypatch
):
    import agentc
    from agentc import _native

    agentc.shutdown()
    _native.optimize_reset()
    monkeypatch.setenv("AGENTC_OPTIMIZE", "1")
    monkeypatch.setenv("AGENTC_MODEL_CATALOG_OFFLINE", "1")
    monkeypatch.setenv("AGENTC_STORAGE_PATH", str(tmp_path))
    with patch("agentc._lifecycle._apply_patches"):
        agentc.init(storage_path=str(tmp_path), capture_embeddings=False)
        # Merge after closing the writer, as a CLI or subsequent process does.
        agentc.shutdown()
        _native.merge_all_pending()
        agentc.init(storage_path=str(tmp_path), capture_embeddings=False)
    try:
        # Populate only the canonical file from an independent SQLite process.
        # The capture connection's output table has no such payload.
        script = "import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=rw',uri=True); c.execute('INSERT INTO output_content(content_id,content_text,created_at) VALUES(?,?,?)',('canonical-only',b'merged-payload',1)); c.commit(); c.close()"
        inserted = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path / "traces.db")],
            capture_output=True,
            text=True,
        )
        assert inserted.returncode == 0, inserted.stderr
        assert _native.output_content_load("canonical-only") == b"merged-payload"
        assert _native.output_content_load("missing") is None
    finally:
        agentc.shutdown()
        _native.optimize_reset()
