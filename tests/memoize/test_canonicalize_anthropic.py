"""Table-driven tests for the Anthropic adapter."""

from __future__ import annotations

import hashlib

import pytest

from agentc._canonicalize import canonicalize_prompt, prompt_hash


ANTHROPIC_CASES = [
    (
        "system_lifted_to_first_message",
        {"system": "be helpful", "messages": [{"role": "user", "content": "hi"}]},
        b'{"messages":[{"content":"be helpful","role":"system"},{"content":"hi","role":"user"}],"provider":"anthropic","response_schema_hash":null,"tools":[]}',
    ),
    (
        "no_system_passthrough",
        {"messages": [{"role": "user", "content": "hi"}]},
        b'{"messages":[{"content":"hi","role":"user"}],"provider":"anthropic","response_schema_hash":null,"tools":[]}',
    ),
    (
        "content_blocks_collapse",
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}],
                }
            ]
        },
        b'{"messages":[{"content":"hello world","role":"user"}],"provider":"anthropic","response_schema_hash":null,"tools":[]}',
    ),
]


@pytest.mark.parametrize("case_id,raw,expected", ANTHROPIC_CASES, ids=[c[0] for c in ANTHROPIC_CASES])
def test_anthropic_canonical_forms(case_id: str, raw, expected: bytes) -> None:
    assert canonicalize_prompt(raw, "anthropic") == expected


def test_anthropic_image_block_hashes_source_data() -> None:
    raw = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "abc123",
                        },
                    },
                ],
            }
        ]
    }
    out = canonicalize_prompt(raw, "anthropic")
    expected = hashlib.sha256(b"abc123").hexdigest()
    assert expected.encode() in out


def test_anthropic_tool_uses_input_schema() -> None:
    raw = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
    }
    out = canonicalize_prompt(raw, "anthropic")
    assert b'"name":"lookup"' in out
    assert b'"schema_hash":"' in out


def test_hash_matches_across_system_position() -> None:
    a = {"system": "x", "messages": [{"role": "user", "content": "hi"}]}
    b = {"messages": [{"role": "user", "content": "hi"}], "system": "x"}
    assert prompt_hash(a, "anthropic") == prompt_hash(b, "anthropic")
