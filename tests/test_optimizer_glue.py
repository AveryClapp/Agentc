"""Unit tests for ``agentc._patches._optimizer_glue``.

Covers:
- ``build_call_dict_openai``: StateDrop / ContextCompress provenance contract.
- ``_text_divergence``: lexical, normalized, and embedding divergence modes.
"""

from __future__ import annotations

import pytest

from agentc._patches._optimizer_glue import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    LITELLM_COMPLETION_PROTOCOL,
    OPENAI_CHAT_COMPLETIONS_PROTOCOL,
    UnsafeModelRouteError,
    _text_divergence,
    apply_call_mutations_anthropic,
    apply_call_mutations_openai,
    build_call_dict_anthropic,
    build_call_dict_openai,
    dispatch_sync,
    maybe_shadow_record,
    resolve_executed_model_id,
)
from agentc._provenance import (
    UserInput,
    clear,
    consume_state_reads,
    record_state_read,
    state_read,
    state_write,
    tag,
)


@pytest.fixture(autouse=True)
def _reset_provenance():
    clear()
    yield
    clear()


def _build(messages: list[dict]) -> dict:
    return build_call_dict_openai(
        {"model": "gpt-4o-mini", "messages": messages},
        call_site_id="test:site:1",
        trace_id_hex="00" * 16,
        span_id_hex="00" * 8,
    )


def _build_anthropic(kwargs: dict) -> dict:
    return build_call_dict_anthropic(
        kwargs,
        call_site_id="test:site:1",
        trace_id_hex="00" * 16,
        span_id_hex="00" * 8,
    )


def _route_contract(
    *,
    protocol: str,
    namespace: str,
    requested: str,
    target: str,
    output_parameter: str,
) -> dict[str, str]:
    return {
        "catalog_version": "test-catalog-v1",
        "price_table_version": "test-prices-v1",
        "provider_protocol": protocol,
        "provider_namespace": namespace,
        "requested_model_id": requested,
        "resolved_requested_model_id": requested,
        "target_model_id": target,
        "target_model_version": target,
        "target_revision_kind": "immutable_snapshot",
        "output_token_parameter": output_parameter,
    }


def test_message_deps_mirrors_input_deps_for_untagged():
    msgs = [
        {"role": "system", "content": "sys-prompt-content-xxxxx"},
        {"role": "user", "content": "user-prompt-content-xxxxx"},
    ]
    call = _build(msgs)
    assert call["input_deps"] == [{"kind": "literal"}, {"kind": "literal"}]
    assert call["parameters"]["extra"]["message_deps"] == [
        {"kind": "literal"},
        {"kind": "literal"},
    ]


def test_state_tag_appears_in_message_deps():
    notes = "research-notes-content-xxxxx"
    state_write("notes", notes)
    msgs = [{"role": "user", "content": notes}]
    call = _build(msgs)
    assert call["parameters"]["extra"]["message_deps"] == [
        {"kind": "state", "key": "notes"}
    ]


def test_user_input_tag_appears_in_message_deps():
    prompt = "user-prompt-content-xxxxx"
    tag(prompt, UserInput(span_id="a" * 16))
    msgs = [{"role": "user", "content": prompt}]
    call = _build(msgs)
    assert call["parameters"]["extra"]["message_deps"] == [
        {"kind": "user_input", "span_id": "a" * 16}
    ]


def test_window_state_reads_is_consumed_and_cleared():
    record_state_read("notes")
    record_state_read("plan")
    msgs = [{"role": "user", "content": "anything-content-xxxxx"}]
    call = _build(msgs)
    assert call["parameters"]["extra"]["window_state_reads"] == ["notes", "plan"]
    # The next build should see an empty window — consume cleared it.
    call2 = _build(msgs)
    assert call2["parameters"]["extra"]["window_state_reads"] == []


def test_attention_scores_populated_for_hotpot_shape():
    """ContextCompress reads attention_scores + follow_on_tokens. Verify
    a HotpotQA-shaped call (system + paragraphs + question) produces
    them via the single-turn fallback in ``_attention``."""
    msgs = [
        {"role": "system", "content": "Answer the question briefly."},
        {
            "role": "user",
            "content": "Scott Derrickson is an American director and screenwriter.",
        },
        {
            "role": "user",
            "content": "Henry IV established the Plantagenet dynasty in medieval England.",
        },
        {
            "role": "user",
            "content": "Were Scott Derrickson and Ed Wood of the same nationality?",
        },
    ]
    call = _build(msgs)
    extra = call["parameters"]["extra"]
    assert "attention_scores" in extra
    assert len(extra["attention_scores"]) == len(msgs)
    assert "follow_on_tokens" in extra
    # Question's own tokens are in the follow_on set.
    assert "scott" in extra["follow_on_tokens"]
    assert "derrickson" in extra["follow_on_tokens"]
    # Distractor (Henry IV) has near-zero overlap with the question.
    assert extra["attention_scores"][2] <= 0.05
    # The question itself overlaps perfectly with itself.
    assert extra["attention_scores"][3] == pytest.approx(1.0)


def test_attention_scores_omitted_when_no_signal():
    """When the call has no user message and no trace history, the proxy
    returns ``([], [])`` and the glue must not include the keys at all
    (the rule will refuse to fire on length-mismatched input)."""
    msgs = [
        {"role": "system", "content": "be brief"},
        {"role": "assistant", "content": "ok"},
    ]
    call = _build(msgs)
    extra = call["parameters"]["extra"]
    assert "attention_scores" not in extra
    assert "follow_on_tokens" not in extra


def test_state_drop_payload_shape_matches_rule_contract():
    """Round-trip a refiner-shaped call. State("notes") is in messages
    but only "critique" is in the read window — the Rust rule should be
    able to identify "notes" as drop-eligible."""
    notes = "research-notes-content-xxxxx"
    critique = "critique-content-xxxxx"
    final = "final-prompt-content-xxxxx"
    state_write("notes", notes)
    state_write("critique", critique)
    # Simulate the agent reading critique just before this LLM call.
    state_read("critique", critique)

    msgs = [
        {"role": "system", "content": "sys-content-xxxxx"},
        {"role": "user", "content": notes},
        {"role": "user", "content": critique},
        {"role": "user", "content": final},
    ]
    call = _build(msgs)
    extra = call["parameters"]["extra"]
    assert extra["message_deps"] == [
        {"kind": "literal"},
        {"kind": "state", "key": "notes"},
        {"kind": "state", "key": "critique"},
        {"kind": "literal"},
    ]
    assert extra["window_state_reads"] == ["critique"]
    # consume_state_reads must have cleared the window.
    assert consume_state_reads() == []


# ---------------------------------------------------------------------------
# Native structured-message preservation (bd-voua)
# ---------------------------------------------------------------------------


def test_openai_multimodal_parameter_rewrite_preserves_native_blocks() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect this screenshot."},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        }
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "click",
                "parameters": {"type": "object"},
            },
        }
    ]
    kwargs = {
        "model": "gpt-5.4-2026-03-05",
        "messages": messages,
        "tools": tools,
    }
    mutated = build_call_dict_openai(
        kwargs,
        call_site_id="osworld:agent:1",
        trace_id_hex="00" * 16,
        span_id_hex="00" * 8,
    )
    extra = mutated["parameters"]["extra"]
    assert extra["agentc_native_messages_opaque"] is True
    assert mutated["messages"][0]["content"] == "Inspect this screenshot. [image_url]"
    assert "AAAA" not in repr(mutated)

    # Even a malformed/version-skewed plan that carries a structural mutation
    # cannot make the Python adapter rebuild an opaque vendor message.
    mutated["messages"] = []
    mutated["parameters"]["max_output_tokens"] = 64
    new_kwargs = apply_call_mutations_openai(kwargs, mutated)

    assert new_kwargs["messages"] is messages
    assert new_kwargs["messages"] == kwargs["messages"]
    assert new_kwargs["tools"] is tools
    assert new_kwargs["max_completion_tokens"] == 64
    assert "max_tokens" not in new_kwargs


def test_anthropic_osworld_shape_survives_parameter_and_model_rewrite() -> None:
    system = [
        {
            "type": "text",
            "text": "Operate the computer safely.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "AAAA",
                    },
                },
                {"type": "text", "text": "What should I click?"},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "computer",
                    "input": {"action": "screenshot"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01",
                    "content": [
                        {"type": "text", "text": "Success"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "BBBB",
                            },
                        },
                    ],
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
    ]
    tools = [
        {
            "name": "computer",
            "type": "computer_20250124",
            "display_width_px": 1280,
            "display_height_px": 720,
            "display_number": 1,
        }
    ]
    betas = ["computer-use-2025-01-24", "prompt-caching-2024-07-31"]
    extra_body = {"thinking": {"type": "enabled", "budget_tokens": 2048}}
    kwargs = {
        "model": "claude-sonnet-4-5-20250929",
        "system": system,
        "messages": messages,
        "max_tokens": 1024,
        "tools": tools,
        "betas": betas,
        "extra_body": extra_body,
    }
    mutated = _build_anthropic(kwargs)
    assert mutated["parameters"]["extra"]["agentc_native_messages_opaque"] is True

    mutated["model"] = "claude-haiku-4-5-20251001"
    mutated["parameters"]["extra"]["agentc_routed_target"] = _route_contract(
        protocol=ANTHROPIC_MESSAGES_PROTOCOL,
        namespace="anthropic",
        requested="claude-sonnet-4-5-20250929",
        target="claude-haiku-4-5-20251001",
        output_parameter="max_tokens",
    )
    mutated["messages"] = []
    mutated["parameters"]["max_output_tokens"] = 128
    new_kwargs = apply_call_mutations_anthropic(kwargs, mutated)

    assert new_kwargs["system"] is system
    assert new_kwargs["messages"] is messages
    assert new_kwargs["tools"] is tools
    assert new_kwargs["betas"] is betas
    assert new_kwargs["extra_body"] is extra_body
    assert new_kwargs["system"] == kwargs["system"]
    assert new_kwargs["messages"] == kwargs["messages"]
    assert new_kwargs["model"] == "claude-haiku-4-5-20251001"
    assert new_kwargs["max_tokens"] == 128


def test_plain_text_messages_remain_structurally_rewritable() -> None:
    kwargs = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "drop me"},
            {"role": "user", "content": "keep me"},
        ],
    }
    mutated = build_call_dict_openai(
        kwargs,
        call_site_id="plain:agent:1",
        trace_id_hex="00" * 16,
        span_id_hex="00" * 8,
    )
    assert "agentc_native_messages_opaque" not in mutated["parameters"]["extra"]
    mutated["messages"] = [mutated["messages"][0], mutated["messages"][2]]

    new_kwargs = apply_call_mutations_openai(kwargs, mutated)

    assert new_kwargs["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "keep me"},
    ]
    assert new_kwargs["messages"] is not kwargs["messages"]


@pytest.mark.parametrize(
    ("model", "original_cap", "expected_field"),
    [
        ("gpt-4o-mini", {"max_tokens": 256}, "max_tokens"),
        (
            "gpt-5.4-2026-03-05",
            {"max_completion_tokens": 256},
            "max_completion_tokens",
        ),
        ("gpt-5.4-2026-03-05", {}, "max_completion_tokens"),
        ("gpt-4o-mini", {}, "max_tokens"),
    ],
)
def test_openai_output_budget_uses_one_compatible_cap_field(
    model: str,
    original_cap: dict[str, int],
    expected_field: str,
) -> None:
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": "answer briefly"}],
        **original_cap,
    }
    mutated = build_call_dict_openai(
        kwargs,
        call_site_id="caps:agent:1",
        trace_id_hex="00" * 16,
        span_id_hex="00" * 8,
    )
    mutated["parameters"]["max_output_tokens"] = 64

    new_kwargs = apply_call_mutations_openai(kwargs, mutated)

    other_field = (
        "max_tokens"
        if expected_field == "max_completion_tokens"
        else "max_completion_tokens"
    )
    assert new_kwargs[expected_field] == 64
    assert other_field not in new_kwargs


def test_adapter_route_context_captures_provider_and_request_requirements() -> None:
    kwargs = {
        "model": "gpt-5.4-2026-03-05",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect"},
                    {"type": "image_url", "image_url": {"url": "data:x"}},
                ],
            }
        ],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 128,
    }
    call = build_call_dict_openai(
        kwargs,
        call_site_id="site",
        trace_id_hex="00" * 16,
        span_id_hex="00" * 8,
    )
    context = call["parameters"]["extra"]["agentc_route_context"]
    assert context["provider_protocol"] == OPENAI_CHAT_COMPLETIONS_PROTOCOL
    assert context["provider_namespace"] == "openai"
    assert context["image_input"] is True
    assert context["tool_calling"] is True
    assert context["structured_outputs"] is True
    assert context["input_tokens_upper_bound"] == 2**32 - 1


def test_route_context_detects_typed_output_config() -> None:
    class OutputConfig:
        def model_dump(self) -> dict:
            return {"format": {"type": "json_schema"}}

    call = build_call_dict_openai(
        {
            "model": "gpt-5.4-2026-03-05",
            "messages": [{"role": "user", "content": "hello"}],
            "output_config": OutputConfig(),
        },
        call_site_id="site",
        trace_id_hex="00" * 16,
        span_id_hex="00" * 8,
    )
    context = call["parameters"]["extra"]["agentc_route_context"]
    assert context["structured_outputs"] is True


def test_unencodable_route_input_forces_catalog_abstention_bound() -> None:
    class Unencodable:
        def __str__(self) -> str:
            raise RuntimeError("cannot project")

    call = build_call_dict_openai(
        {
            "model": "gpt-5.4-2026-03-05",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [Unencodable()],
        },
        call_site_id="site",
        trace_id_hex="00" * 16,
        span_id_hex="00" * 8,
    )
    context = call["parameters"]["extra"]["agentc_route_context"]
    assert context["input_tokens_upper_bound"] == 2**32 - 1


def test_catalog_route_uses_target_output_token_convention() -> None:
    requested = "gpt-5.4-2026-03-05"
    target = "gpt-5.4-mini-2026-03-17"
    kwargs = {
        "model": requested,
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 200,
    }
    mutated = {
        "model": target,
        "messages": kwargs["messages"],
        "parameters": {
            "max_output_tokens": 64,
            "extra": {
                "agentc_routed_target": _route_contract(
                    protocol=OPENAI_CHAT_COMPLETIONS_PROTOCOL,
                    namespace="openai",
                    requested=requested,
                    target=target,
                    output_parameter="max_completion_tokens",
                )
            },
        },
    }

    dispatched = apply_call_mutations_openai(kwargs, mutated)

    assert dispatched["model"] == target
    assert dispatched["max_completion_tokens"] == 64
    assert "max_tokens" not in dispatched


def test_executed_model_prefers_routed_target_when_response_omits_model() -> None:
    from types import SimpleNamespace

    from agentc._optimizer import Plan

    plan = Plan(kind="rewritten", executed_model_id="gpt-5.4-mini-2026-03-17")
    assert (
        resolve_executed_model_id(plan, SimpleNamespace(), "gpt-5.4-2026-03-05")
        == "gpt-5.4-mini-2026-03-17"
    )


def test_litellm_cross_provider_route_is_rejected() -> None:
    requested = "openai/gpt-5.4"
    target = "anthropic/claude-haiku-4-5-20251001"
    kwargs = {"model": requested, "messages": []}
    mutated = {
        "model": target,
        "messages": [],
        "parameters": {
            "extra": {
                "agentc_routed_target": _route_contract(
                    protocol=LITELLM_COMPLETION_PROTOCOL,
                    namespace="openai",
                    requested=requested,
                    target=target,
                    output_parameter="max_completion_tokens",
                )
            }
        },
    }

    with pytest.raises(UnsafeModelRouteError, match="credential namespace"):
        apply_call_mutations_openai(
            kwargs,
            mutated,
            provider_protocol=LITELLM_COMPLETION_PROTOCOL,
        )


def test_routed_failure_replays_exact_original_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentc._optimizer import Plan

    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "lookup"}}]
    original_kwargs = {
        "model": "openai/gpt-5.4",
        "messages": messages,
        "tools": tools,
    }
    # Missing route metadata makes the mutation fail before any provider call.
    plan = Plan(
        kind="rewritten",
        rule="ModelDowngrade",
        call={"model": "anthropic/claude-haiku-4-5", "messages": []},
    )
    original_calls = 0
    mutated_calls = 0

    def run_original() -> str:
        nonlocal original_calls
        original_calls += 1
        assert original_kwargs["messages"] is messages
        assert original_kwargs["tools"] is tools
        return "reference"

    def run_mutated(mutated_call: dict) -> str:
        nonlocal mutated_calls
        mutated_calls += 1
        apply_call_mutations_openai(
            original_kwargs,
            mutated_call,
            provider_protocol=LITELLM_COMPLETION_PROTOCOL,
        )
        return "unreachable"

    response = dispatch_sync(
        plan,
        run_original=run_original,
        run_mutated=run_mutated,
    )
    monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "1.0")
    maybe_shadow_record(plan, "site", response, run_original)

    assert response == "reference"
    assert original_calls == 1
    assert mutated_calls == 1
    assert plan.dispatch_fallback is True
    assert plan.dispatch_fallback_reason == "mutated_dispatch_failed"


# ---------------------------------------------------------------------------
# _text_divergence — embedding mode
# ---------------------------------------------------------------------------


class TestEmbeddingDivergenceMode:
    """Tests for AGENTC_SHADOW_DIVERGENCE_MODE=embedding."""

    def test_identical_strings_score_near_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "embedding")
        score = _text_divergence("The answer is Paris.", "The answer is Paris.")
        assert score < 0.05, (
            f"identical strings gave divergence {score:.4f}; expected < 0.05"
        )

    def test_unrelated_strings_score_high(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "embedding")
        score = _text_divergence(
            "The capital of France is Paris.",
            "Photosynthesis converts sunlight into chemical energy in plants.",
        )
        assert score > 0.2, (
            f"unrelated strings gave low divergence {score:.4f}; expected > 0.2"
        )

    def test_paraphrase_lower_than_raw_lexical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The motivating case: 'The answer is Paris' vs 'Paris is the answer'
        should score LOWER divergence in embedding mode than in lexical mode,
        because the embedding captures semantic equivalence."""
        a = "The answer is Paris."
        b = "Paris is the answer."

        monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "lexical")
        lexical_score = _text_divergence(a, b)

        monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "embedding")
        embedding_score = _text_divergence(a, b)

        assert embedding_score < lexical_score, (
            f"embedding ({embedding_score:.4f}) should be < lexical ({lexical_score:.4f}) "
            "for a semantically equivalent paraphrase"
        )

    def test_fallback_when_native_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When _native.embed_text_bytes raises, the mode falls back to
        'normalized' (containment) rather than crashing."""
        import unittest.mock

        monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "embedding")
        # Patch embed_text_bytes to raise so we exercise the fallback path.
        with unittest.mock.patch(
            "agentc._native.embed_text_bytes",
            side_effect=RuntimeError("embedder offline"),
        ):
            # Must not raise; should return a valid float via normalized fallback.
            score = _text_divergence("hello world", "hello world")
            assert isinstance(score, float)
            assert 0.0 <= score <= 1.0


class TestDispatchSyncCachedFallback:
    """Regression (bd-8ln): a cached plan whose payload cannot be decoded must
    fall back to the real call, never return None to the app while booking a
    cache hit."""

    @staticmethod
    def _plan(kind: str, value: object = None):
        from types import SimpleNamespace

        return SimpleNamespace(kind=kind, value=value, call=None, rule="R")

    def test_cached_decode_none_falls_back_to_original(self) -> None:
        out = dispatch_sync(
            self._plan("cached", value={"output_content_id": "x"}),
            run_original=lambda: "REAL_COMPLETION",
            run_mutated=lambda c: "MUT",
            decode_cached=lambda v: None,  # decoder returns None instead of raising
        )
        assert out == "REAL_COMPLETION"

    def test_cached_decode_raise_falls_back_to_original(self) -> None:
        def _boom(_v):
            raise RuntimeError("decode failed")

        out = dispatch_sync(
            self._plan("cached", value={"output_content_id": "x"}),
            run_original=lambda: "REAL",
            run_mutated=lambda c: "MUT",
            decode_cached=_boom,
        )
        assert out == "REAL"

    def test_cached_valid_value_is_returned(self) -> None:
        out = dispatch_sync(
            self._plan("cached", value={"output_content_id": "x"}),
            run_original=lambda: "REAL",
            run_mutated=lambda c: "MUT",
            decode_cached=lambda v: "DECODED",
        )
        assert out == "DECODED"

    def test_cached_falsy_but_valid_value_is_returned(self) -> None:
        # Only None triggers fallback — a legitimately cached 0/""/False stays.
        out = dispatch_sync(
            self._plan("cached", value={"output_content_id": "x"}),
            run_original=lambda: "REAL",
            run_mutated=lambda c: "MUT",
            decode_cached=lambda v: 0,
        )
        assert out == 0


class TestShadowGuardPythonEntry:
    """Regression (bd-rj7): the Python ENTRY of the accuracy-guard loop —
    maybe_shadow_record -> record_divergence — was exercised by zero tests
    (only the middle Rust link, Budget, was covered). The Rust EXIT of the loop
    (record_sample -> planner disable gate) is covered by the Rust test
    planner::budget_disabled_rule_is_gated_off. Together they close the loop."""

    @staticmethod
    def _resp(text: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
        )

    @staticmethod
    def _plan():
        from types import SimpleNamespace

        return SimpleNamespace(kind="rewritten", rule="ContextCompress", call={})

    def test_divergent_shadow_forwards_positive_divergence(self, monkeypatch):
        import agentc._optimizer

        recorded: list = []
        monkeypatch.setattr(
            agentc._optimizer,
            "record_divergence",
            lambda site, rule, div: recorded.append((site, rule, div)),
        )
        monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "1.0")  # always sample

        optimized = self._resp("Paris is the capital of France")
        original = self._resp("Photosynthesis converts sunlight in plants")
        maybe_shadow_record(
            self._plan(), "site", optimized, run_original=lambda: original
        )

        assert len(recorded) == 1
        site, rule, div = recorded[0]
        assert (site, rule) == ("site", "ContextCompress")
        assert div > 0.0, "divergent shadow outputs must forward a positive divergence"

    def test_identical_shadow_forwards_zero_divergence(self, monkeypatch):
        import agentc._optimizer

        recorded: list = []
        monkeypatch.setattr(
            agentc._optimizer,
            "record_divergence",
            lambda site, rule, div: recorded.append((site, rule, div)),
        )
        monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "1.0")

        resp = self._resp("identical output text")
        maybe_shadow_record(self._plan(), "site", resp, run_original=lambda: resp)
        assert recorded == [("site", "ContextCompress", 0.0)]
