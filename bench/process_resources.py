"""POSIX process and Linux container counters with explicit measurement scopes."""

from __future__ import annotations

import math
import resource
import sys


def usage_record(usage) -> dict:
    """Normalize wait4/getrusage counters without pretending RSS is additive."""
    factor = 1 if sys.platform == "darwin" else 1024
    values = {
        "user_cpu_seconds": float(usage.ru_utime),
        "system_cpu_seconds": float(usage.ru_stime),
        "peak_rss_bytes": int(usage.ru_maxrss * factor),
        "minor_page_faults": int(usage.ru_minflt),
        "major_page_faults": int(usage.ru_majflt),
        "voluntary_context_switches": int(usage.ru_nvcsw),
        "involuntary_context_switches": int(usage.ru_nivcsw),
    }
    if not all(math.isfinite(v) and v >= 0 for v in values.values()):
        raise ValueError("invalid OS resource counters")
    return values


def process_usage() -> dict:
    return {
        "self": usage_record(resource.getrusage(resource.RUSAGE_SELF)),
        "reaped_children": usage_record(resource.getrusage(resource.RUSAGE_CHILDREN)),
        "scope": "process lifetime, all threads; reaped descendants separate",
        "rss_semantics": "per-process lifetime maximum; never sum RSS counters",
    }


def container_usage(container) -> dict:
    """Read cumulative CPU and kernel peak memory before the owned container dies."""
    stats = container.stats(stream=False)
    cpu = stats["cpu_stats"]["cpu_usage"]["total_usage"]
    if type(cpu) is not int or cpu < 0:
        raise ValueError("invalid container CPU counter")
    # Docker's sampled memory.usage cannot recover the lifetime maximum.
    # Prefer the kernel's peak counter on either supported cgroup hierarchy.
    result = container.exec_run(
        [
            "/bin/sh",
            "-c",
            "if [ -r /sys/fs/cgroup/memory.peak ]; then "
            "cat /sys/fs/cgroup/memory.peak; "
            "elif [ -r /sys/fs/cgroup/memory/memory.max_usage_in_bytes ]; then "
            "cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes; else exit 3; fi",
        ]
    )
    peak = None
    if result.exit_code == 0:
        raw = result.output.strip()
        if not raw.isdigit():
            raise ValueError("invalid container peak memory counter")
        peak = int(raw)
    return {
        "container_id": container.id,
        "cpu_seconds": cpu / 1_000_000_000,
        "peak_memory_bytes": peak,
        "peak_memory_available": peak is not None,
        "peak_memory_unavailable_reason": None
        if peak is not None
        else "kernel_peak_counter_unavailable",
        "scope": "owned task container, bootstrap and tools through pre-removal snapshot",
        "sampled_memory_bytes": stats.get("memory_stats", {}).get("usage"),
        "cpu_snapshot_precedes_peak_counter_read": True,
    }
