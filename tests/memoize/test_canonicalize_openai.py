"""Table-driven tests for the OpenAI adapter."""

from __future__ import annotations

import hashlib

import pytest

from agentc._canonicalize import canonicalize_prompt, parameters_hash, prompt_hash
from agentc._canonicalize._core import deterministic_dumps, sha256_hex


def _hash(obj) -> str:
    return sha256_hex(deterministic_dumps(obj))


OPENAI_CASES = [
    (
        "plain_messages_list",
        [{"role": "user", "content": "hi"}],
        b'{"messages":[{"content":"hi","role":"user"}],"provider":"openai","response_schema_hash":null,"tools":[]}',
    ),
    (
        "role_case_normalized",
        {"messages": [{"role": "System", "content": "be kind"}, {"role": "USER", "content": " yo "}]},
        b'{"messages":[{"content":"be kind","role":"system"},{"content":"yo","role":"user"}],"provider":"openai","response_schema_hash":null,"tools":[]}',
    ),
    (
        "text_parts_collapse_to_string",
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "one "},
                        {"type": "text", "text": "two"},
                    ],
                }
            ]
        },
        b'{"messages":[{"content":"one two","role":"user"}],"provider":"openai","response_schema_hash":null,"tools":[]}',
    ),
]


@pytest.mark.parametrize("case_id,raw,expected", OPENAI_CASES, ids=[c[0] for c in OPENAI_CASES])
def test_openai_canonical_forms(case_id: str, raw, expected: bytes) -> None:
    assert canonicalize_prompt(raw, "openai") == expected


def test_openai_tools_sorted_and_hashed() -> None:
    raw = {
        "messages": [{"role": "user", "content": "x"}],
        "tools": [
            {"type": "function", "function": {"name": "beta", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "alpha", "parameters": {"type": "object"}}},
        ],
    }
    schema_hash = _hash({"type": "object"})
    expected = (
        b'{"messages":[{"content":"x","role":"user"}],'
        b'"provider":"openai","response_schema_hash":null,'
        b'"tools":[{"name":"alpha","schema_hash":"' + schema_hash.encode() + b'"},'
        b'{"name":"beta","schema_hash":"' + schema_hash.encode() + b'"}]}'
    )
    assert canonicalize_prompt(raw, "openai") == expected


def test_openai_image_url_hashes_to_url() -> None:
    url = "https://example.com/cat.png"
    raw = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ]
    }
    out = canonicalize_prompt(raw, "openai")
    expected_sha = hashlib.sha256(url.encode()).hexdigest()
    assert expected_sha.encode() in out
    assert b'"image"' in out


def test_openai_tool_call_hashes_arguments() -> None:
    raw = {
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"q": "hi"}'},
                    }
                ],
            }
        ]
    }
    out = canonicalize_prompt(raw, "openai")
    args_sha = hashlib.sha256(b'{"q": "hi"}').hexdigest()
    assert args_sha.encode() in out
    assert b'"tool_use"' in out


def test_openai_tool_role_preserves_tool_call_id() -> None:
    raw = {
        "messages": [
            {"role": "tool", "tool_call_id": "call_42", "content": "result"},
        ]
    }
    out = canonicalize_prompt(raw, "openai")
    assert b'"tool_call_id":"call_42"' in out


def test_hash_is_stable_across_key_reorder() -> None:
    a = {"messages": [{"role": "user", "content": "hi"}], "tools": []}
    b = {"tools": [], "messages": [{"role": "user", "content": "hi"}]}
    assert prompt_hash(a, "openai") == prompt_hash(b, "openai")


def test_parameters_hash_drops_incidental_keys() -> None:
    a = {"temperature": 0.5, "stream": True, "user": "u1"}
    b = {"temperature": 0.5}
    assert parameters_hash(a) == parameters_hash(b)
