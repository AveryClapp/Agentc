"""Unit tests for the degradation logging policy (bd-ybd / P13-7).

Pure-Python: no native extension required.
"""

from __future__ import annotations

import logging

from agentc._degradation import (
    degradation_counts,
    log_degraded,
    reset_degradation_counts,
)


def test_log_degraded_counts_by_event():
    reset_degradation_counts()
    log_degraded("attention_failed", "site=x", exc_info=False)
    log_degraded("attention_failed", "site=y", exc_info=False)
    log_degraded("trace_record_failed", "site=z", exc_info=False)
    assert degradation_counts() == {"attention_failed": 2, "trace_record_failed": 1}


def test_log_degraded_emits_warning_with_event_name(caplog):
    reset_degradation_counts()
    with caplog.at_level(logging.WARNING, logger="agentc"):
        log_degraded("rewrite_dispatch_failed", "plan reverted", exc_info=False)
    assert any(
        rec.levelno == logging.WARNING and "event=rewrite_dispatch_failed" in rec.getMessage()
        for rec in caplog.records
    )


def test_log_degraded_never_raises():
    reset_degradation_counts()
    # Odd inputs must not blow up the caller — this runs on the fail-open path.
    log_degraded("weird", None, exc_info=False)  # type: ignore[arg-type]
    assert degradation_counts().get("weird") == 1


def test_reset_clears_counts():
    log_degraded("e", "m", exc_info=False)
    reset_degradation_counts()
    assert degradation_counts() == {}
