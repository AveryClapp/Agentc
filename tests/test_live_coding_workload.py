"""Fresh trajectories, original-ledger calls, and fail-closed native inputs."""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench import live_coding_workload as live


def messages():
    return [
        {"role": "system", "content": "System policy"},
        {"role": "user", "content": "Original issue description"},
        {"role": "assistant", "content": "actual previous action"},
        {"role": "user", "content": "actual tool output"},
    ]


class Journal:
    def __init__(self):
        self.events = []

    def append(self, value):
        self.events.append(deepcopy(value))


class Ledger:
    def __init__(self, finish="stop"):
        self.calls = []
        self.finish = finish

    def call(self, key, call_id, stage, cap, payload, metadata):
        self.calls.append(deepcopy((call_id, stage, cap, payload, metadata)))
        return {
            "answer": "new response for this arm",
            "finish_reason": self.finish,
            "cost_usd": "0.001",
            "usage": {"prompt_tokens": 25, "completion_tokens": 7},
        }


def model(monkeypatch, *, arm="source", finish="stop"):
    monkeypatch.setattr(live, "verify_endpoint", lambda key: {})
    ledger, journal = Ledger(finish), Journal()
    return live.BudgetModel(
        ledger, "unused", "stage", "task", arm, Path("unused"), journal
    )


def test_source_preserves_actual_history_and_charges_original_ledger(monkeypatch):
    m = model(monkeypatch)
    history = messages()
    original = deepcopy(history)
    assert m.query(history) == {"content": "new response for this arm"}
    assert history == original
    call = m.ledger.calls[0]
    assert call[2] == Decimal("0.50")
    assert call[3]["messages"] == original
    assert call[3]["provider"]["only"] == ["openai"]
    assert call[4]["dispatch_contract"]["endpoint_model"] == live.ENDPOINT
    assert [e["event"] for e in m.transcript.events] == ["request", "response"]
    assert m.n_calls == 1


def test_compression_never_replaces_history_or_supplies_archived_answer(monkeypatch):
    m = model(monkeypatch, arm="compress")
    monkeypatch.setattr(
        live,
        "compress",
        lambda ms, *_: (
            deepcopy(ms[:2]),
            {"applied": True, "retained_indices": [0, 1]},
        ),
    )
    history = messages()
    original = deepcopy(history)
    response = m.query(history)
    assert history == original
    assert m.ledger.calls[0][3]["messages"] == original[:2]
    assert m.transcript.events[0]["original_messages"] == original
    assert response["content"] == "new response for this arm"


def test_truncated_response_is_charged_but_not_returned_to_agent(monkeypatch):
    m = model(monkeypatch, finish="length")
    with pytest.raises(live.PilotError, match="truncated"):
        m.query(messages())
    assert m.n_calls == 1
    assert m.cost == 0.001
    assert len(m.ledger.calls) == 1
    assert m.transcript.events[-1]["event"] == "response"


def test_paid_failure_is_never_retried(monkeypatch):
    m = model(monkeypatch)
    calls = []

    def failure(*args):
        calls.append(args)
        raise live.PilotError("retained failed reservation")

    m.ledger.call = failure
    with pytest.raises(live.PilotError):
        m.query(messages())
    assert len(calls) == 1
    assert m.n_calls == 0
    assert [e["event"] for e in m.transcript.events] == ["request"]


@pytest.mark.parametrize(
    "indices", [[0, 2, 3], [1, 2, 3], [0, 1, 1], [0, 1, 99], [1, 0], [0, True]]
)
def test_instruction_deletion_and_invalid_receipts_never_reach_dispatch(
    monkeypatch, indices
):
    monkeypatch.setattr(
        live,
        "load_attention",
        lambda: SimpleNamespace(compute_attention_scores=lambda *_: ([0.0] * 4, [])),
    )
    monkeypatch.setattr(
        live,
        "command",
        lambda *_args, **_kwargs: live.canonical({"retained_indices": indices}),
    )
    with pytest.raises(live.PilotError):
        live.compress(messages(), Path("unused"), "task")


def test_task_selection_uses_only_repo_and_id():
    rows = [
        {
            "repo": repo,
            "instance_id": f"{repo.replace('/', '__')}-{i}",
            "success": i % 2,
        }
        for repo in ["pytest-dev/pytest", "psf/requests"]
        for i in range(8)
    ]
    first = live.selected_ids(rows, [])
    for row in rows:
        row["success"] = 1 - row["success"]
        row["problem_statement"] = "changed body"
    assert live.selected_ids(list(reversed(rows)), []) == first
    assert all(i not in first for i in live.selected_ids(rows, first))


def test_existing_run_directory_cannot_trigger_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "load_key", lambda *_: pytest.fail("credentials touched"))
    with pytest.raises(live.PilotError, match="no implicit retry"):
        live.run({}, tmp_path, tmp_path)


def test_manifest_hash_failure_precedes_environment_access(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text("{}")
    with pytest.raises(live.PilotError, match="manifest hash"):
        live.load_manifest(p, "0" * 64, tmp_path)
