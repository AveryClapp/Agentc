"""Checks for evidence boundaries and faithful structured-tool handling."""

import json
from copy import deepcopy

import pytest

from bench.tau_service_preflight import check_tool_links, deny_network, private_write


@pytest.fixture
def messages():
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"x": 1}'},
                },
                {
                    "id": "b",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"x": 2}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "b", "content": "second"},
        {"role": "tool", "tool_call_id": "a", "content": "first"},
    ]


def test_structured_parallel_results_are_valid_and_unchanged(messages):
    before = deepcopy(messages)
    assert check_tool_links(messages) == 2
    assert messages == before
    assert json.loads(messages[0]["tool_calls"][0]["function"]["arguments"]) == {"x": 1}


@pytest.mark.parametrize(
    "mutation", ["orphan", "duplicate", "missing", "interposed", "flattened"]
)
def test_incomplete_or_corrupted_tool_links_fail(messages, mutation):
    if mutation == "orphan":
        messages[1]["tool_call_id"] = "unknown"
    elif mutation == "duplicate":
        messages[0]["tool_calls"][1]["id"] = "a"
    elif mutation == "missing":
        messages.pop()
    elif mutation == "interposed":
        messages.insert(1, {"role": "assistant", "content": "early"})
    else:
        messages[0].pop("tool_calls")
    with pytest.raises(ValueError):
        check_tool_links(messages)


def test_private_write_is_exclusive_and_private(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "trace.json"
    private_write(path, b"fixture")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        private_write(path, b"replacement")
    assert path.read_bytes() == b"fixture"


def test_private_write_rejects_git_and_permissive_directory(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="outside Git"):
        private_write(tmp_path / "trace.json", b"fixture")
    (tmp_path / ".git").rmdir()
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="0700"):
        private_write(tmp_path / "trace.json", b"fixture")


@pytest.mark.parametrize(
    "event", ["socket.connect", "socket.getaddrinfo", "socket.sendto"]
)
def test_network_guard_blocks_connect_and_resolution(event):
    with pytest.raises(RuntimeError, match="disabled"):
        deny_network(event, ())
