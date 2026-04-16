"""Table-driven tests for the Cohere adapter."""

from __future__ import annotations

import pytest

from agentc._canonicalize import canonicalize_prompt


COHERE_CASES = [
    (
        "string_message_only",
        "hello",
        b'{"messages":[{"content":"hello","role":"user"}],"provider":"cohere","response_schema_hash":null,"tools":[]}',
    ),
    (
        "preamble_plus_history_plus_message",
        {
            "preamble": "be helpful",
            "chat_history": [
                {"role": "USER", "message": "first"},
                {"role": "CHATBOT", "message": "reply"},
            ],
            "message": "second",
        },
        b'{"messages":['
        b'{"content":"be helpful","role":"system"},'
        b'{"content":"first","role":"user"},'
        b'{"content":"reply","role":"assistant"},'
        b'{"content":"second","role":"user"}'
        b'],"provider":"cohere","response_schema_hash":null,"tools":[]}',
    ),
    (
        "empty_message_field_dropped",
        {
            "chat_history": [{"role": "USER", "message": "q"}],
            "message": "",
        },
        b'{"messages":[{"content":"q","role":"user"}],"provider":"cohere","response_schema_hash":null,"tools":[]}',
    ),
]


@pytest.mark.parametrize("case_id,raw,expected", COHERE_CASES, ids=[c[0] for c in COHERE_CASES])
def test_cohere_canonical_forms(case_id: str, raw, expected: bytes) -> None:
    assert canonicalize_prompt(raw, "cohere") == expected
