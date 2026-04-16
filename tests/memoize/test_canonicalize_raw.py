"""Table-driven tests for the raw fallback adapter."""

from __future__ import annotations

import pytest

from agentc._canonicalize import canonicalize_prompt


RAW_CASES = [
    (
        "plain_string",
        "hello",
        b'{"messages":[{"content":"hello","role":"user"}],"provider":"raw","response_schema_hash":null,"tools":[]}',
    ),
    (
        "strips_outer_whitespace",
        "   hi\n",
        b'{"messages":[{"content":"hi","role":"user"}],"provider":"raw","response_schema_hash":null,"tools":[]}',
    ),
    (
        "list_of_message_dicts",
        [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
        b'{"messages":[{"content":"be brief","role":"system"},{"content":"hi","role":"user"}],"provider":"raw","response_schema_hash":null,"tools":[]}',
    ),
    (
        "unknown_provider_falls_back_to_raw",
        "hi",
        b'{"messages":[{"content":"hi","role":"user"}],"provider":"raw","response_schema_hash":null,"tools":[]}',
    ),
]


@pytest.mark.parametrize(
    "case_id,raw,expected",
    [(c[0], c[1], c[2]) for c in RAW_CASES[:-1]],
    ids=[c[0] for c in RAW_CASES[:-1]],
)
def test_raw_canonical_forms(case_id: str, raw, expected: bytes) -> None:
    assert canonicalize_prompt(raw, "raw") == expected


def test_unknown_provider_falls_back_to_raw() -> None:
    assert canonicalize_prompt("hi", "weirdprovider") == RAW_CASES[-1][2]
