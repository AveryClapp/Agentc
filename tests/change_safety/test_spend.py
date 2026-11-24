"""Spend ledger: reservations, cap and failure accounting."""

from __future__ import annotations

import json

import pytest

from bench.change_safety.spend import SpendCapReached, SpendLedger


class _Response:
    def __init__(self, cost):
        self._hidden_params = {"response_cost": cost}


def request(model="paid/m", max_tokens=100):
    return {"model": model, "messages": [{"role": "user", "content": "x" * 300}], "max_tokens": max_tokens}


def test_reservation_blocks_before_dispatch(tmp_path):
    ledger = SpendLedger(tmp_path / "s.jsonl", cap_usd=0.01, prices={"paid/m": (1e-6, 1e-5)})
    sent = []
    ledger.call(role="agent", request=request(), dispatch=lambda r: sent.append(r) or _Response(0.002))
    assert ledger.committed_usd == pytest.approx(0.002)
    with pytest.raises(SpendCapReached):
        ledger.call(role="agent", request=request(max_tokens=100_000), dispatch=lambda r: sent.append(r))
    assert len(sent) == 1


def test_failed_call_keeps_reservation(tmp_path):
    ledger = SpendLedger(tmp_path / "s.jsonl", cap_usd=1.0, prices={"paid/m": (1e-6, 1e-6)})

    def boom(r):
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        ledger.call(role="fork", request=request(), dispatch=boom)
    entry = json.loads((tmp_path / "s.jsonl").read_text())
    assert entry["error"].startswith("RuntimeError") and entry["cost_usd"] == entry["reserved_usd"] > 0


def test_unknown_price_and_missing_cap_refused(tmp_path):
    ledger = SpendLedger(tmp_path / "s.jsonl", cap_usd=1.0)
    with pytest.raises(SpendCapReached):
        ledger.call(role="agent", request=request(model="nobody/unknown-model"), dispatch=lambda r: None)
    with pytest.raises(SpendCapReached):
        ledger.call(role="agent", request={"model": "mock/a", "messages": []}, dispatch=lambda r: None)


def test_mock_models_are_free(tmp_path):
    ledger = SpendLedger(tmp_path / "s.jsonl", cap_usd=0.0)
    ledger.call(role="agent", request=request(model="mock/a"), dispatch=lambda r: _Response(5.0))
    assert ledger.committed_usd == 0.0
