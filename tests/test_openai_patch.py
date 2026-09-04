"""Tests for OpenAI SDK patches (bd-1kh).

Run: maturin develop && pytest tests/test_openai_patch.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agentc
from agentc._context import SpanContext, set_current_span
from agentc._lifecycle import _initialized, _shutdown_in_progress
from agentc._patches._openai import (
    _extract_input_messages,
    _extract_request_attrs,
    _extract_response_attrs,
    _extract_output_messages,
    _inject_stream_options,
    _wrap_create,
    _wrap_create_async,
    patch as patch_openai,
    unpatch as unpatch_openai,
)


# --- Mock OpenAI response objects ---


class MockUsage:
    def __init__(self, prompt_tokens: int = 100, completion_tokens: int = 50) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class MockMessage:
    def __init__(self, role: str = "assistant", content: str = "Hello!") -> None:
        self.role = role
        self.content = content

    def model_dump(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class MockChoice:
    def __init__(self, message: MockMessage | None = None, finish_reason: str = "stop") -> None:
        self.message = message or MockMessage()
        self.finish_reason = finish_reason


class MockChatCompletion:
    def __init__(
        self,
        *,
        model: str = "gpt-4o",
        usage: MockUsage | None = None,
        choices: list[MockChoice] | None = None,
        id: str = "chatcmpl-123",
    ) -> None:
        self.model = model
        self.usage = usage or MockUsage()
        self.choices = choices or [MockChoice()]
        self.id = id


class MockDelta:
    def __init__(self, content: str | None = None) -> None:
        self.content = content


class MockStreamChoice:
    def __init__(self, delta: MockDelta | None = None) -> None:
        self.delta = delta or MockDelta()


class MockStreamChunk:
    def __init__(
        self,
        choices: list[MockStreamChoice] | None = None,
        usage: MockUsage | None = None,
    ) -> None:
        self.choices = choices or [MockStreamChoice()]
        self.usage = usage


# --- Fixtures ---


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    _initialized.clear()
    _shutdown_in_progress.clear()
    set_current_span(None)
    yield
    if agentc.is_initialized():
        agentc.shutdown()
    _initialized.clear()
    _shutdown_in_progress.clear()
    set_current_span(None)
    import agentc._span as span_mod
    span_mod._logged_not_initialized = False


@pytest.fixture()
def tmp_storage(tmp_path: Path) -> Path:
    return tmp_path / "agentc"


@pytest.fixture()
def initialized(tmp_storage: Path) -> Path:
    with patch("agentc._lifecycle._apply_patches"):
        agentc.init(storage_path=str(tmp_storage))
    return tmp_storage


# --- Tests ---


class TestExtractHelpers:
    def test_extract_request_attrs(self) -> None:
        kwargs = {
            "model": "gpt-4o",
            "temperature": 0.7,
            "top_p": 0.9,
            "max_tokens": 1024,
        }
        attrs = _extract_request_attrs(kwargs)
        assert attrs["gen_ai.operation.name"] == "chat"
        assert attrs["gen_ai.provider.name"] == "openai"
        assert attrs["gen_ai.request.model"] == "gpt-4o"
        assert attrs["gen_ai.request.temperature"] == 0.7

    def test_extract_request_max_completion_tokens(self) -> None:
        kwargs = {"model": "gpt-4o", "max_completion_tokens": 2048}
        attrs = _extract_request_attrs(kwargs)
        assert attrs["gen_ai.request.max_tokens"] == 2048

    def test_extract_response_attrs(self) -> None:
        resp = MockChatCompletion(
            model="gpt-4o",
            usage=MockUsage(prompt_tokens=200, completion_tokens=100),
            id="chatcmpl-abc",
        )
        attrs = _extract_response_attrs(resp)
        assert attrs["gen_ai.response.model"] == "gpt-4o"
        assert attrs["gen_ai.response.id"] == "chatcmpl-abc"
        assert attrs["gen_ai.response.finish_reasons"] == "stop"
        assert attrs["gen_ai.usage.input_tokens"] == 200
        assert attrs["gen_ai.usage.output_tokens"] == 100

    def test_extract_input_messages(self) -> None:
        kwargs = {"messages": [{"role": "user", "content": "hi"}]}
        result = _extract_input_messages(kwargs)
        assert result is not None
        parsed = json.loads(result)
        assert parsed[0]["role"] == "user"

    def test_extract_output_messages(self) -> None:
        resp = MockChatCompletion()
        result = _extract_output_messages(resp)
        assert result is not None
        parsed = json.loads(result)
        assert parsed[0]["role"] == "assistant"


class TestStreamOptionsInjection:
    def test_inject_when_absent(self) -> None:
        kwargs: dict[str, Any] = {"stream": True}
        injected = _inject_stream_options(kwargs)
        assert injected
        assert kwargs["stream_options"] == {"include_usage": True}

    def test_merge_when_present_without_include_usage(self) -> None:
        kwargs: dict[str, Any] = {"stream": True, "stream_options": {"other": True}}
        injected = _inject_stream_options(kwargs)
        assert injected
        assert kwargs["stream_options"]["include_usage"] is True
        assert kwargs["stream_options"]["other"] is True

    def test_noop_when_include_usage_present(self) -> None:
        kwargs: dict[str, Any] = {"stream": True, "stream_options": {"include_usage": True}}
        injected = _inject_stream_options(kwargs)
        assert not injected

    def test_noop_when_not_streaming(self) -> None:
        kwargs: dict[str, Any] = {"stream": False}
        injected = _inject_stream_options(kwargs)
        assert not injected


class TestSyncCreateWrapper:
    def test_live_cold_plan_runs_reference_visible_counterfactual_off_path(
        self,
        tmp_storage: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from contextlib import closing
        import sqlite3
        import threading

        monkeypatch.setenv("AGENTC_ENABLED_RULES", "OutputBudget")
        monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "1")
        monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "0")
        with patch("agentc._lifecycle._apply_patches"):
            agentc.init(storage_path=str(tmp_storage))

        candidate_entered = threading.Event()
        release_candidate = threading.Event()
        calls: list[int] = []

        def provider(**kwargs: Any) -> MockChatCompletion:
            cap = int(kwargs.get("max_tokens", 0) or 0)
            calls.append(cap)
            if cap == 64:
                candidate_entered.set()
                assert release_candidate.wait(timeout=2)
                return MockChatCompletion(
                    model="gpt-4o",
                    choices=[MockChoice(MockMessage(content="discarded candidate"))],
                )
            return MockChatCompletion(
                model="gpt-4o",
                choices=[MockChoice(MockMessage(content="reference visible"))],
            )

        request = {
            "model": "gpt-4o",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "same shaped prompt"}],
        }
        responses = []
        with patch("agentc._patches._openai._write_root_span"):
            for _ in range(4):
                responses.append(_wrap_create(provider, None, (), request))

        assert all(
            response.choices[0].message.content == "reference visible"
            for response in responses
        )
        assert candidate_entered.wait(timeout=2)
        assert calls.count(1024) == 4
        assert calls.count(64) == 1

        release_candidate.set()
        from agentc._patches._optimizer_glue import drain_exploration

        drain_exploration(2_000)
        agentc.shutdown()

        database = tmp_storage / "cost_model.db"
        with closing(sqlite3.connect(database)) as connection:
            completed = connection.execute(
                "SELECT status, execution_plan_id FROM execution_plan_exploration"
            ).fetchone()
            assert completed is not None
            paired = connection.execute(
                "SELECT paired_observations FROM execution_plan_profile "
                "WHERE execution_plan_id = ?",
                (completed[1],),
            ).fetchone()

        assert completed[0] == "completed"
        assert paired == (1,)

    def test_excluded_scope_preserves_request_and_span(self, initialized: Path) -> None:
        from agentc import optimization_scope

        written: list[dict[str, Any]] = []
        mock_response = MockChatCompletion()
        request = {
            "model": "user-simulator-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
        wrapped = MagicMock(return_value=mock_response)

        with optimization_scope("tau2.user_simulator", optimize=False), patch(
            "agentc._patches._openai._write_root_span",
            side_effect=lambda span: written.append(span),
        ), patch("agentc._optimizer.plan_call") as planner:
            result = _wrap_create(wrapped, None, (), request)

        assert result is mock_response
        planner.assert_not_called()
        wrapped.assert_called_once_with(**request)
        attrs = json.loads(written[0]["attributes"])
        assert attrs["agentc.optimization.scope"] == "tau2.user_simulator"
        assert attrs["agentc.optimization.eligible"] is False
        assert attrs["agentc.optimization.decision_reason"] == "scope_excluded"

    def test_observes_primary_before_shadow_reference(self, initialized: Path) -> None:
        from agentc._optimizer import Plan

        events: list[str] = []
        plan = Plan(
            kind="rewritten",
            rule="OutputBudget",
            call={"model": "gpt-4o", "messages": []},
        )
        response = MockChatCompletion()
        observe = MagicMock(side_effect=lambda **_: events.append("observe"))

        with patch("agentc._patches._openai._write_root_span"), patch(
            "agentc._patches._openai._plan_openai_call",
            return_value=(plan, "site"),
        ), patch(
            "agentc._patches._optimizer_glue.dispatch_sync",
            return_value=response,
        ), patch(
            "agentc._patches._openai._observe_openai_outcome",
            new=observe,
        ), patch(
            "agentc._patches._optimizer_glue.maybe_explore_record",
            side_effect=lambda *_: events.append("explore"),
        ), patch(
            "agentc._patches._optimizer_glue.maybe_shadow_record",
            side_effect=lambda *_: events.append("shadow"),
        ), patch(
            "agentc._patches._openai._now_us",
            side_effect=[1_000_000, 1_100_000, 2_100_000],
        ):
            result = _wrap_create(
                MagicMock(return_value=response),
                None,
                (),
                {"model": "gpt-4o", "messages": []},
            )

        assert result is response
        assert events == ["observe", "explore", "shadow"]
        assert observe.call_args.kwargs["elapsed_s"] == pytest.approx(0.1)

    def test_captures_span(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []
        mock_response = MockChatCompletion()

        with patch("agentc._patches._openai._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=mock_response)
            result = _wrap_create(
                wrapped, None, (),
                {"model": "gpt-4o", "max_tokens": 1024, "messages": [{"role": "user", "content": "hello"}]},
            )

        assert result is mock_response
        assert len(written) == 1
        span = written[0]
        assert span["kind"] == "chat"
        assert span["name"] == "openai.chat.completions.create"
        assert span["status"] == "OK"
        attrs = json.loads(span["attributes"])
        assert attrs["gen_ai.provider.name"] == "openai"

    def test_captures_all_gen_ai_attrs(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []
        mock_response = MockChatCompletion(
            model="gpt-4o",
            usage=MockUsage(prompt_tokens=200, completion_tokens=100),
            id="chatcmpl-xyz",
        )

        with patch("agentc._patches._openai._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=mock_response)
            _wrap_create(
                wrapped, None, (),
                {"model": "gpt-4o", "max_tokens": 1024, "temperature": 0.5, "messages": []},
            )

        attrs = json.loads(written[0]["attributes"])
        assert attrs["gen_ai.request.model"] == "gpt-4o"
        assert attrs["gen_ai.response.model"] == "gpt-4o"
        assert attrs["gen_ai.usage.input_tokens"] == 200
        assert attrs["gen_ai.usage.output_tokens"] == 100
        assert attrs["gen_ai.response.id"] == "chatcmpl-xyz"

    def test_error_captures_error_span(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._patches._openai._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(side_effect=RuntimeError("API error"))
            with pytest.raises(RuntimeError, match="API error"):
                _wrap_create(wrapped, None, (), {"model": "gpt-4o", "messages": []})

        assert len(written) == 1
        assert written[0]["status"] == "ERROR"
        attrs = json.loads(written[0]["attributes"])
        assert attrs["error.type"] == "RuntimeError"

    def test_noop_without_init(self) -> None:
        mock_response = MockChatCompletion()
        wrapped = MagicMock(return_value=mock_response)
        result = _wrap_create(wrapped, None, (), {"model": "test", "messages": []})
        assert result is mock_response

    def test_fail_open_on_planning_error_returns_user_response(self, initialized: Path) -> None:
        # End-to-end fail-open (bd-jvk, P7-5): if the optimizer planner blows
        # up (here plan_call, the immediate Python caller of
        # _native.optimize_plan, raises), the user's chat.completions.create
        # must still return its real response — the failure never surfaces.
        # This is the Python-side counterpart to fail_open.rs's Rust check.
        mock_response = MockChatCompletion()
        wrapped = MagicMock(return_value=mock_response)

        with patch("agentc._patches._openai._write_root_span", side_effect=lambda d: None), patch(
            "agentc._optimizer.plan_call", side_effect=RuntimeError("planner exploded")
        ):
            result = _wrap_create(
                wrapped,
                None,
                (),
                {"model": "gpt-4o", "max_tokens": 1024, "messages": [{"role": "user", "content": "hi"}]},
            )

        assert result is mock_response
        wrapped.assert_called_once()

    def test_input_output_captured(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._patches._openai._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=MockChatCompletion())
            _wrap_create(
                wrapped, None, (),
                {"model": "test", "messages": [{"role": "user", "content": "hello"}]},
            )

        span = written[0]
        assert "input_messages" in span
        assert "output_messages" in span


class TestAsyncCreateWrapper:
    @pytest.mark.asyncio
    async def test_native_openai_runs_async_shadow_guard(
        self, initialized: Path
    ) -> None:
        from agentc._optimizer import Plan

        plan = Plan(
            kind="rewritten",
            rule="OutputBudget",
            call={"model": "gpt-4o", "messages": []},
        )
        response = MockChatCompletion()
        events: list[str] = []
        shadow = AsyncMock(side_effect=lambda *_: events.append("shadow"))
        observe = MagicMock(side_effect=lambda **_: events.append("observe"))
        explore = MagicMock(side_effect=lambda *_: events.append("explore"))

        with patch("agentc._patches._openai._write_root_span"), patch(
            "agentc._patches._openai._plan_openai_call",
            return_value=(plan, "site"),
        ), patch(
            "agentc._executor.dispatch",
            new=AsyncMock(return_value=response),
        ), patch(
            "agentc._patches._optimizer_glue.maybe_shadow_record_async",
            new=shadow,
        ), patch(
            "agentc._patches._optimizer_glue.maybe_explore_record_async",
            new=explore,
        ), patch(
            "agentc._patches._openai._observe_openai_outcome",
            new=observe,
        ):
            result = await _wrap_create_async(
                AsyncMock(return_value=response),
                None,
                (),
                {"model": "gpt-4o", "messages": []},
            )

        assert result is response
        shadow.assert_awaited_once()
        explore.assert_called_once()
        assert shadow.await_args.args[:3] == (plan, "site", response)
        assert explore.call_args.args[:2] == (plan, response)
        assert events == ["observe", "explore", "shadow"]


class TestStreamingWrapper:
    def test_ttft_captured(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        chunks = [
            MockStreamChunk(choices=[MockStreamChoice(MockDelta(content=None))]),
            MockStreamChunk(choices=[MockStreamChoice(MockDelta(content="Hello"))]),
            MockStreamChunk(choices=[MockStreamChoice(MockDelta(content=" world"))]),
        ]

        with patch("agentc._patches._openai._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=iter(chunks))
            result = _wrap_create(
                wrapped, None, (),
                {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            )
            # Consume the stream
            for _ in result:
                pass

        assert len(written) == 1
        attrs = json.loads(written[0]["attributes"])
        assert "agentc.ttft_ms" in attrs

    def test_usage_from_final_chunk(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        chunks = [
            MockStreamChunk(choices=[MockStreamChoice(MockDelta(content="Hi"))]),
            MockStreamChunk(choices=[], usage=MockUsage(prompt_tokens=50, completion_tokens=25)),
        ]

        with patch("agentc._patches._openai._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=iter(chunks))
            result = _wrap_create(
                wrapped, None, (),
                {"model": "gpt-4o", "stream": True, "messages": []},
            )
            for _ in result:
                pass

        attrs = json.loads(written[0]["attributes"])
        assert attrs["gen_ai.usage.input_tokens"] == 50
        assert attrs["gen_ai.usage.output_tokens"] == 25


class TestWithTraceContext:
    def test_span_inherits_trace(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        parent_ctx = SpanContext(span_id="parent123456789a", trace_id="trace12345678901234567890123456ab", name="my-agent")
        set_current_span(parent_ctx)

        # Non-root spans now route through ``_enqueue_span`` (bd-4hy);
        # mock both endpoints so the test stays correct regardless of
        # which path the patch chooses.
        with patch("agentc._patches._openai._write_root_span", side_effect=lambda d: written.append(d)), \
             patch("agentc._patches._openai._enqueue_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=MockChatCompletion())
            _wrap_create(wrapped, None, (), {"model": "test", "messages": []})

        set_current_span(None)

        span = written[0]
        assert span["trace_id"] == "trace12345678901234567890123456ab"
        assert span["parent_span_id"] == "parent123456789a"


class TestPatchUnpatch:
    def test_patch_applies(self) -> None:
        pytest.importorskip("openai")
        import agentc._patches._openai as mod

        mod._patched = False
        patch_openai()
        try:
            import openai.resources.chat.completions as cc
            assert hasattr(cc.Completions.create, "__wrapped__")
        finally:
            unpatch_openai()
            mod._patched = False

    def test_skip_if_not_installed(self) -> None:
        import agentc._patches._openai as mod

        mod._patched = False
        with patch.dict("sys.modules", {"openai": None}):
            with patch("builtins.__import__", side_effect=ImportError("no openai")):
                patch_openai()
        assert not mod._patched
