"""The replay audit must distinguish intact histories from plausible corruption."""

from copy import deepcopy

import pytest

from bench import compression_history_audit as audit


def request(indices=None, arm="compress"):
    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "old action"},
        {"role": "user", "content": "old result"},
        {"role": "assistant", "content": "current action"},
        {"role": "user", "content": "é"},
    ]
    indices = list(range(6)) if indices is None else indices
    return {
        "original_messages": messages,
        "payload": {"messages": [messages[i] for i in indices]},
        "metadata": {
            "arm": arm,
            "original_messages_sha256": audit.live.digest(messages),
            "rewrite": {"retained_indices": indices, "applied": len(indices) < 6},
        },
    }


def test_audit_detects_orphan_results_and_lost_current_action():
    result = audit.validate_request(request([0, 1, 3, 5]))
    assert result["partial_exchanges"] == 2
    assert result["latest_action_missing"]
    assert result["current_exchange_missing_messages"] == 1


def test_complete_old_exchange_can_be_removed():
    result = audit.validate_request(request([0, 1, 4, 5]))
    assert result == {
        "partial_exchanges": 0,
        "latest_action_missing": False,
        "current_exchange_missing_messages": 0,
        "retained_content_bytes": len("policytaskcurrent actioné".encode()),
    }


@pytest.mark.parametrize("indices", [[0, 1, 1], [0, 1, 99], [0, True], [1, 0], [0, 2]])
def test_invalid_index_provenance_rejected(indices):
    with pytest.raises(ValueError, match="indices"):
        audit.retention(request()["original_messages"], indices)


@pytest.mark.parametrize("mutation", ["digest", "payload", "status", "source"])
def test_recorded_dispatch_corruption_rejected(mutation):
    row = deepcopy(request([0, 1, 4, 5]))
    if mutation == "digest":
        row["metadata"]["original_messages_sha256"] = "0" * 64
    elif mutation == "payload":
        row["payload"]["messages"] = row["payload"]["messages"][:-1]
    elif mutation == "status":
        row["metadata"]["rewrite"]["applied"] = False
    else:
        row["metadata"]["arm"] = "source"
    with pytest.raises(ValueError):
        audit.validate_request(row)


def test_bad_manifest_rejected_before_native_execution(tmp_path, monkeypatch):
    (tmp_path / "manifest.json").write_text("{}")
    monkeypatch.setattr(
        audit.live, "compress", lambda *_: pytest.fail("native executed")
    )
    with pytest.raises(ValueError, match="manifest"):
        audit.audit(tmp_path, "0" * 64, tmp_path / "candidate")
