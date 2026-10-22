"""CPU/RSS units, kernel peaks and owned process termination."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from bench import process_resources as resources
from bench.swe_agent_process import _stop
from bench.campaign_transport import CampaignStopped


@pytest.mark.parametrize("platform,factor", [("darwin", 1), ("linux", 1024)])
def test_rss_units_are_normalized_without_summing_process_peaks(
    monkeypatch, platform, factor
):
    monkeypatch.setattr(resources.sys, "platform", platform)
    raw = SimpleNamespace(
        ru_utime=1.5,
        ru_stime=0.5,
        ru_maxrss=12,
        ru_minflt=1,
        ru_majflt=2,
        ru_nvcsw=3,
        ru_nivcsw=4,
    )
    assert resources.usage_record(raw)["peak_rss_bytes"] == 12 * factor
    raw.ru_utime = float("nan")
    with pytest.raises(ValueError):
        resources.usage_record(raw)


def test_container_peak_is_a_kernel_counter_not_sampled_usage():
    container = SimpleNamespace(
        id="owned",
        stats=lambda **kw: {
            "cpu_stats": {"cpu_usage": {"total_usage": 2_000_000_000}},
            "memory_stats": {"usage": 3},
        },
        exec_run=lambda command: SimpleNamespace(exit_code=0, output=b"4096\n"),
    )
    value = resources.container_usage(container)
    assert value["cpu_seconds"] == 2 and value["peak_memory_bytes"] == 4096
    assert value["sampled_memory_bytes"] == 3 and value["peak_memory_available"] is True
    container.exec_run = lambda command: SimpleNamespace(exit_code=3, output=b"")
    value = resources.container_usage(container)
    assert (
        value["peak_memory_bytes"] is None and value["peak_memory_available"] is False
    )


def test_owned_process_group_is_terminated_and_reaped():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    usage = _stop(process)
    assert process.returncode < 0 and usage is not None
    with pytest.raises(ChildProcessError):
        import os

        os.wait4(process.pid, os.WNOHANG)


@pytest.mark.parametrize("leader_finished", [False, True])
def test_permission_error_requires_terminal_leader_and_absent_group(
    monkeypatch, leader_finished
):
    from bench import swe_agent_process as worker

    process = SimpleNamespace(pid=123, returncode=None)

    def signal_group(pid, sig):
        if sig == worker.signal.SIGKILL:
            raise PermissionError("signal denied")
        # A surviving group makes even a terminal leader insufficient proof.

    monkeypatch.setattr(worker.os, "killpg", signal_group)
    monkeypatch.setattr(
        worker.os, "wait4", lambda *args: (123 if leader_finished else 0, 15, object())
    )
    monkeypatch.setattr(worker.time, "sleep", lambda *args: None)
    expected = CampaignStopped if leader_finished else PermissionError
    with pytest.raises(expected):
        worker._stop(process)
