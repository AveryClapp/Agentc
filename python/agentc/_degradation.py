"""Degradation logging policy (bd-ybd / P13-7).

Invariant enforced across ``python/agentc/``:

    Any ``except`` that results in Agentc NOT doing something it *intended*
    to do — a rule that should have fired, a rewrite that should have been
    dispatched, a recommendation that should have been injected — logs at
    **WARNING** with a stable ``event`` name via :func:`log_degraded`.

    ``log.debug`` is reserved for genuinely uninteresting cases: "Agentc
    correctly decided not to optimize" (a cold call, a rule that does not
    apply, an opt-out). Those are NOT degradations and must not use this.

Why: before this policy the dangerous silent-disable paths logged at DEBUG,
so "Agentc broke" was indistinguishable from "Agentc chose not to act" — and
a benchmark run would report honest-looking zero savings with no signal that
anything failed. Routing every degradation through one WARNING helper with a
stable event name makes silent degradation impossible to miss.

The in-process counter (:func:`degradation_counts`) lets tests assert that a
given failure was recorded. Surfacing it in ``agentc optimize report`` (a
separate process) needs DB persistence and is tracked as follow-up work.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter

logger = logging.getLogger("agentc")

_lock = threading.Lock()
_counts: Counter[str] = Counter()


def log_degraded(event: str, message: str, *, exc_info: bool = True) -> None:
    """Record that Agentc wanted to act but could not.

    Logs at WARNING with a stable ``event=`` tag and increments the
    in-process counter for ``event``. ``exc_info`` defaults to True so the
    triggering exception is captured — call inside an ``except`` block.

    This helper never raises: a logging/counter failure must not itself break
    the fail-open path it is reporting on.
    """
    try:
        with _lock:
            _counts[event] += 1
        logger.warning("agentc degraded: event=%s %s", event, message, exc_info=exc_info)
    except BaseException:  # pragma: no cover - logging must never break callers
        pass


def degradation_counts() -> dict[str, int]:
    """Snapshot of per-event degradation counts recorded this process."""
    with _lock:
        return dict(_counts)


def reset_degradation_counts() -> None:
    """Clear the counter (test helper)."""
    with _lock:
        _counts.clear()
