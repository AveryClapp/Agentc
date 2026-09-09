"""Verify real-history replay cannot turn future outputs into current evidence."""

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from bench import realistic_workloads as w


def history():
    return {
        "instance_id": "repo__project-42",
        "info": {"submission": "unused"},
        "messages": [
            {"role": "system", "content": "System instructions."},
            {"role": "user", "content": "Repair the actual issue."},
            {"role": "assistant", "content": "Inspect a file."},
            {"role": "user", "content": "Actual tool output."},
            {"role": "assistant", "content": "Edit and test."},
            {"role": "user", "content": "Terminal patch."},
        ],
    }


def inventories():
    return {
        run: [
            {
                "Key": f"bash-only/{name}/trajs/repo__task-{i}/repo__task-{i}.traj.json",
                "Size": "123",
                "ETag": '"' + "a" * 32 + '"',
            }
            for i in range(20)
        ]
        for run, name in w.RUNS.items()
    }


def test_selection_cannot_use_size_order_or_etag():
    original = inventories()
    changed = deepcopy(original)
    for rows in changed.values():
        rows.reverse()
        for i, row in enumerate(rows):
            row.update(Size=str(i * 90000), ETag="different")
    assert w.select(original)["ids"] == w.select(changed)["ids"]


def test_selection_never_shrinks_or_accepts_duplicate_ids():
    rows = inventories()
    rows["gpt"] = rows["gpt"][:2]
    with pytest.raises(w.ContractError, match="no shrinking"):
        w.select(rows)
    rows = inventories()
    rows["gpt"].append(rows["gpt"][0])
    with pytest.raises(w.ContractError, match="duplicate"):
        w.select(rows)


def test_request_prefixes_preserve_history_and_exclude_current_and_future_answers():
    doc = history()
    original = deepcopy(doc)
    calls = list(w.prefixes(doc, doc["instance_id"]))
    assert calls == [doc["messages"][:2], doc["messages"][:4]]
    doc["messages"][4]["content"] = "Counterfactual future answer"
    doc["info"] = object()  # No scoring or metadata access is needed.
    assert list(w.prefixes(doc, doc["instance_id"])) == calls
    calls[0][0]["content"] = "mutated candidate"
    assert original["messages"][:4] == doc["messages"][:4]


def test_terminal_limit_is_not_a_call():
    doc = history()
    doc["messages"].append({"role": "user", "content": "cost limit"})
    assert len(list(w.prefixes(doc, doc["instance_id"]))) == 2


@pytest.mark.parametrize("change", ["tools", "wrong_id", "wrong_sequence"])
def test_unsupported_schema_fails_without_flattening(change):
    doc = history()
    if change == "tools":
        doc["messages"][2]["tool_calls"] = [{"id": "actual-tool-id"}]
    elif change == "wrong_id":
        doc["instance_id"] = "another-task"
    else:
        doc["messages"][3]["role"] = "system"
    with pytest.raises(w.ContractError):
        list(w.prefixes(doc, "repo__project-42"))


def test_native_input_preserves_text_and_does_not_invent_state():
    messages = history()["messages"][:4]
    original = deepcopy(messages)
    call = w.make_call(messages, "gpt", "repo__project-42", 1, w.attention_module())
    assert call["messages"] == original
    assert call["input_deps"] == [{"kind": "literal"}] * 4
    assert call["parameters"]["extra"]["window_state_reads"] == []
    assert call["parameters"]["extra"]["dead_attention_epsilon"] == 0.10
    assert messages == original


def test_task_tag_is_explicit_and_preserves_content():
    messages = history()["messages"][:4]
    tagged = w.make_call(
        messages, "gpt", "task", 1, w.attention_module(), "task_tagged"
    )
    assert tagged["messages"] == messages
    assert tagged["input_deps"][1]["kind"] == "user_input"
    assert all(
        d["kind"] == "literal" for i, d in enumerate(tagged["input_deps"]) if i != 1
    )


def test_archived_content_blocks_are_preserved_but_never_flattened_for_native():
    doc = history()
    doc["messages"][1]["content"] = [
        {"type": "text", "text": "Task", "cache_control": {"type": "ephemeral"}}
    ]
    messages = next(w.prefixes(doc, doc["instance_id"]))
    assert messages == doc["messages"][:2]
    assert w.content_bytes(messages[1]["content"]) == 4
    with pytest.raises(w.ContractError, match="content blocks"):
        w.make_call(messages, "sonnet", "task", 0, w.attention_module())


@pytest.mark.parametrize("retained", [[0, 0], [-1], [0, 99], [1, 0], [True]])
def test_materializer_rejects_invalid_deletion_receipts(monkeypatch, retained):
    call = w.make_call(
        history()["messages"][:2], "gpt", "task", 0, w.attention_module()
    )

    def fake(*args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"retained_indices": retained}).encode()
        )

    monkeypatch.setattr(w.subprocess, "run", fake)
    with pytest.raises(w.ContractError, match="indices"):
        w.materialize(Path("unused"), call, "ContextCompress")


def test_private_cache_and_no_overwrite(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(w.ContractError, match="outside Git"):
        w.private_directory(tmp_path / "corpus")
    target = tmp_path / "receipt.json"
    w.save(target, {"a": 1})
    with pytest.raises(FileExistsError):
        w.save(target, {"a": 2})
    assert json.loads(target.read_bytes()) == {"a": 1}


def test_strict_json():
    for raw in [b'{"a":1,"a":2}', b'{"a":NaN}']:
        with pytest.raises(w.ContractError):
            w.decode(raw)
