"""Cached text content is explicitly enrolled and preserved as structured bytes."""

from copy import deepcopy
from dataclasses import replace

import pytest

from bench.campaign_accounting import AccountingError
from bench.campaign_chat import ChatContract, prepare_request

CONTRACT = ChatContract(
    "fixture/model", "fixture", "Fixture", "fixture/model-v1", 128, 16
)


def request():
    return {
        "model": CONTRACT.model,
        "max_tokens": 8,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "exact\r\ntext",
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": "uncached tail"},
                ],
            },
        ],
    }


def test_default_contract_bytes_and_rejection_are_unchanged():
    assert "cached_text_blocks" not in CONTRACT.record()
    assert ChatContract(**CONTRACT.record()).record() == CONTRACT.record()
    with pytest.raises(AccountingError):
        prepare_request(CONTRACT, request())


def test_explicit_contract_preserves_blocks_order_text_and_cache_markers():
    cached = replace(CONTRACT, cached_text_blocks=True)
    assert cached.record()["cached_text_blocks"] is True
    assert ChatContract(**cached.record()) == cached
    value = request()
    before = deepcopy(value)
    wire = prepare_request(cached, value)
    assert value == before and wire["messages"] == before["messages"]
    assert cached.bound(wire) == CONTRACT.bound(wire)


@pytest.mark.parametrize(
    "mutation", ["image", "unknown", "ttl", "persistent", "empty", "null", "top_level"]
)
def test_enrollment_does_not_accept_unfrozen_content_features(mutation):
    value = request()
    block = value["messages"][0]["content"][0]
    if mutation == "image":
        block["type"] = "image_url"
    elif mutation == "unknown":
        block["unfrozen"] = True
    elif mutation == "ttl":
        block["cache_control"]["ttl"] = "1h"
    elif mutation == "persistent":
        block["cache_control"]["type"] = "persistent"
    elif mutation == "empty":
        value["messages"][0]["content"] = []
    elif mutation == "null":
        block["text"] = None
    else:
        value["messages"][0]["cache_control"] = {"type": "ephemeral"}
    with pytest.raises(AccountingError):
        prepare_request(replace(CONTRACT, cached_text_blocks=True), value)


@pytest.mark.parametrize("enabled", [1, None, "true"])
def test_contract_enrollment_requires_a_boolean(enabled):
    with pytest.raises(AccountingError):
        replace(CONTRACT, cached_text_blocks=enabled)
