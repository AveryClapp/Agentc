"""Python<->Rust canonicalizer byte-equality.

If either side drifts, cache-key hashes diverge and memoization silently
stops hitting. Keep this fixture corpus growing whenever a new provider
quirk is added.
"""

from __future__ import annotations

import json

import pytest

from agentc._canonicalize import canonicalize_parameters, canonicalize_prompt

try:
    from agentc._native import (
        canonicalize_parameters_bytes as rust_canonicalize_parameters,
        canonicalize_prompt_bytes as rust_canonicalize_prompt,
    )
    _HAS_RUST_MIRROR = True
except ImportError:  # pragma: no cover
    _HAS_RUST_MIRROR = False


pytestmark = pytest.mark.skipif(
    not _HAS_RUST_MIRROR, reason="Rust canonicalize_*_bytes FFI not built yet"
)


FIXTURES_PROMPTS = [
    (
        "openai_plain",
        "openai",
        {"messages": [{"role": "user", "content": "hi"}]},
    ),
    (
        "openai_multi_role",
        "openai",
        {
            "messages": [
                {"role": "SYSTEM", "content": "  be brief "},
                {"role": "User", "content": [{"type": "text", "text": "how "}, {"type": "text", "text": "are you?"}]},
            ],
            "tools": [
                {"type": "function", "function": {"name": "b", "parameters": {"type": "object"}}},
                {"type": "function", "function": {"name": "a", "parameters": {"type": "object"}}},
            ],
        },
    ),
    (
        "anthropic_with_system",
        "anthropic",
        {
            "system": "be helpful",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
    ),
    (
        "cohere_full",
        "cohere",
        {
            "preamble": "be helpful",
            "chat_history": [
                {"role": "USER", "message": "a"},
                {"role": "CHATBOT", "message": "b"},
            ],
            "message": "c",
        },
    ),
    (
        "raw_string",
        "raw",
        "  hello  ",
    ),
    (
        "raw_list",
        "raw",
        [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}],
    ),
]


@pytest.mark.parametrize(
    "case_id,provider,raw",
    FIXTURES_PROMPTS,
    ids=[c[0] for c in FIXTURES_PROMPTS],
)
def test_prompt_parity(case_id: str, provider: str, raw) -> None:
    py_bytes = canonicalize_prompt(raw, provider)
    rust_bytes = rust_canonicalize_prompt(json.dumps(raw).encode("utf-8"), provider)
    assert py_bytes == rust_bytes, (
        f"mismatch for {case_id}:\npython={py_bytes!r}\nrust={rust_bytes!r}"
    )


FIXTURES_PARAMS = [
    ("empty", {}),
    ("simple", {"temperature": 0.5, "max_tokens": 100}),
    ("rounding", {"temperature": 0.12345678, "top_p": 0.9999999}),
    ("drop_incidentals", {"stream": True, "user": "u1", "agentc_tag": "x", "temperature": 0.1}),
    ("stop_list_sort", {"stop": ["z", "a", "m"]}),
    ("logit_bias", {"logit_bias": {"42": -100, "7": 10}}),
    ("tool_choice_fn", {"tool_choice": {"type": "function", "function": {"name": "lookup"}}}),
    ("response_format_schema", {"response_format": {"type": "json_schema", "schema": {"type": "object"}}}),
]


@pytest.mark.parametrize(
    "case_id,raw",
    FIXTURES_PARAMS,
    ids=[c[0] for c in FIXTURES_PARAMS],
)
def test_parameters_parity(case_id: str, raw) -> None:
    py_bytes = canonicalize_parameters(raw)
    rust_bytes = rust_canonicalize_parameters(json.dumps(raw).encode("utf-8"))
    assert py_bytes == rust_bytes, (
        f"mismatch for {case_id}:\npython={py_bytes!r}\nrust={rust_bytes!r}"
    )
