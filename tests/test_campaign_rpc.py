"""Capability ownership, concurrent probes, partial frames and fatal accounting."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from bench.campaign_chat import ChatContract
from bench.campaign_rpc import ChildTransport, MAX_PACKET, TransportBridge, receive
from bench.campaign_transport import CampaignStopped


@pytest.fixture
def bridge(tmp_path):
    calls = []
    transport = SimpleNamespace(
        contracts={
            "fixture": ChatContract("fixture", "fixture", "Fixture", "fixture", 128, 16)
        },
        key_id="a" * 64,
        _key="never-forward-this-provider-key",
        http=SimpleNamespace(base_url="http://127.0.0.1:1/api/v1"),
        session_id="owned",
        ledger=tmp_path / "ledger",
        call=lambda **kw: calls.append(kw) or {"id": kw["call_id"]},
        call_accounting=lambda call_id: {
            "known_cost_usd": "0.1",
            "uncertain_budget_usd": "0.2",
            "complete": False,
        },
        call_binding=lambda call_id: {"call_id": call_id, "status": "completed"},
        assert_complete=lambda: {"pending": 0},
        budget_snapshot=lambda: {"remaining_upper_usd": "0.8", "dispatch_authorized": False},
    )
    value = TransportBridge(
        transport,
        cell={"cell_id": "cell", "actor": "agent"},
        deadline=time.monotonic() + 10,
    )
    try:
        yield value, ChildTransport(value.descriptor()), calls
    finally:
        value.close()


def call(child, identity="first", **metadata):
    return child.call(
        call_id=identity,
        request={"model": "fixture"},
        metadata={"cell_id": "cell", "actor": "agent", **metadata},
    )


def test_child_only_has_parent_capabilities_and_exact_liabilities(bridge):
    parent, child, calls = bridge
    assert "never-forward-this-provider-key" not in repr(parent.descriptor())
    assert not hasattr(child, "_key")
    assert call(child) == {"id": "first"}
    assert calls[0]["cancelled"] is parent.cancelled
    assert child.call_accounting("first") == {
        "known_cost_usd": "0.1",
        "uncertain_budget_usd": "0.2",
        "complete": False,
    }
    assert child.assert_complete() == {"pending": 0}


@pytest.mark.parametrize("change", [{"actor": "judge"}, {"cell_id": "foreign"}])
def test_foreign_actor_or_cell_never_dispatches(bridge, change):
    parent, child, calls = bridge
    with pytest.raises(CampaignStopped):
        call(child, **change)
    assert calls == [] and parent.failed.is_set()


def test_call_replay_and_foreign_liability_lookup_are_rejected(bridge):
    parent, child, calls = bridge
    call(child)
    with pytest.raises(CampaignStopped):
        call(child)
    with pytest.raises(CampaignStopped):
        child.call_accounting("another-cell-call")
    assert len(calls) == 1


def test_reference_dispatch_does_not_wait_for_blocked_counterfactual(bridge):
    parent, child, calls = bridge
    entered, release = threading.Event(), threading.Event()

    def dispatch(**kw):
        if kw["call_id"] == "probe":
            entered.set()
            assert release.wait(3)
        return {"id": kw["call_id"]}

    parent.transport.call = dispatch
    with ThreadPoolExecutor(max_workers=2) as pool:
        probe = pool.submit(call, child, "probe")
        try:
            assert entered.wait(2)
            assert pool.submit(call, child, "reference").result(timeout=2) == {
                "id": "reference"
            }
            assert not probe.done()
        finally:
            release.set()
        assert probe.result(timeout=2) == {"id": "probe"}


def test_bridge_close_interrupts_a_half_written_packet_and_reaps_handlers(bridge):
    parent, child, calls = bridge
    with socket.socket(socket.AF_UNIX) as sock:
        sock.connect(parent.path)
        sock.sendall(b"\x00\x00")
        deadline = time.monotonic() + 2
        while not parent._handlers and time.monotonic() < deadline:
            time.sleep(0.005)
        parent.close()
        assert not parent._handlers and not Path(parent.path).exists()
    assert not calls


@pytest.mark.parametrize("header", [0, MAX_PACKET + 1])
def test_frame_bound_is_checked_before_reading_a_body(header):
    left, right = socket.socketpair()
    try:
        left.sendall(struct.pack("!I", header))
        with pytest.raises(ValueError, match="bound"):
            receive(right, time.monotonic() + 1)
    finally:
        left.close()
        right.close()


def test_invalid_capability_cannot_dispatch(bridge):
    parent, child, calls = bridge
    descriptor = parent.descriptor() | {"token": "wrong"}
    with pytest.raises(CampaignStopped):
        call(ChildTransport(descriptor))
    assert not calls


def test_child_reads_all_actor_budget_capacity_without_dispatch_capability(bridge):
    parent, child, calls = bridge
    assert child.budget_snapshot() == {"remaining_upper_usd": "0.8", "dispatch_authorized": False}
    assert calls == []
    parent.failed.set()
    with pytest.raises(CampaignStopped):
        child.budget_snapshot()
    assert calls == []


def test_child_call_binding_is_limited_to_its_own_claims(bridge):
    parent, child, calls = bridge
    with pytest.raises(CampaignStopped):
        child.call_binding("foreign")
    parent.failed.clear()
    call(child, "owned")
    assert child.call_binding("owned") == {"call_id": "owned", "status": "completed"}
    assert len(calls) == 1
