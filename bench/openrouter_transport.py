"""Whole-operation deadlines for the POSIX, main-thread benchmark CLI.

Enclose both urllib ``open`` and the complete response ``read`` in one scope.
Socket timeouts alone reset on progress; this timer does not. This is not a
general threaded/async timeout facility or a way to stop arbitrary native code
that defers Python signal handling. The body must not reassign SIGALRM or its
timer, block that signal, or suppress cancellation and keep doing work.
"""
from __future__ import annotations

import math
import os
import signal
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType


class DeadlineExpired(TimeoutError):
    """The enclosing operation exhausted its total elapsed-time allowance."""


_deadline_active = False


def require_deadline_support() -> None:
    """Fail before a paid reservation if this process cannot own a deadline.

    Raises RuntimeError without taking over another handler, timer, or blocked
    signal. ``total_deadline`` repeats this check before entering its body.
    """
    if os.name != "posix" or any(not hasattr(signal, name) for name in (
        "SIGALRM", "ITIMER_REAL", "SIG_BLOCK", "SIG_SETMASK", "getitimer",
        "setitimer", "pthread_sigmask", "sigpending", "sigwait",
    )):
        raise RuntimeError("total deadline requires POSIX signal timer support")
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("total deadline requires the main thread")
    if _deadline_active:
        raise RuntimeError("nested total deadlines are not supported")
    if any(signal.getitimer(signal.ITIMER_REAL)):
        raise RuntimeError("total deadline cannot replace an existing SIGALRM timer")
    if signal.getsignal(signal.SIGALRM) is None:
        raise RuntimeError("total deadline cannot restore a non-Python SIGALRM handler")
    # SIG_BLOCK with an empty set only queries the current thread's mask.
    if signal.SIGALRM in signal.pthread_sigmask(signal.SIG_BLOCK, set()):
        raise RuntimeError("total deadline requires unblocked SIGALRM")
    if signal.SIGALRM in signal.sigpending():
        raise RuntimeError("total deadline cannot consume a pending foreign SIGALRM")


@contextmanager
def total_deadline(seconds: float) -> Iterator[None]:
    """Bound one synchronous operation, restoring signal ownership on exit.

    ``seconds`` must be a finite positive int/float (not bool). Unsupported or
    occupied timer state raises RuntimeError before the body executes. The
    prior timer must be inactive: a foreign active timer is never suspended or
    restarted. Deadline expiry is not evidence of provider cancellation or zero
    cost; the caller must retain the dispatched request's financial allowance.
    """
    global _deadline_active
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("total deadline seconds must be finite and positive")
    try:
        seconds = float(seconds)
    except OverflowError:
        raise ValueError("total deadline seconds must be finite and positive") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("total deadline seconds must be finite and positive")
    require_deadline_support()
    previous_handler = signal.getsignal(signal.SIGALRM)
    if previous_handler is None:
        raise RuntimeError("total deadline cannot restore a non-Python SIGALRM handler")
    started = time.monotonic()
    expired = False
    acquired = False

    def expire(_signum: int, _frame: FrameType | None) -> None:
        nonlocal expired
        expired = True
        raise DeadlineExpired("provider operation exceeded its total deadline")

    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        # Recheck while blocked before taking ownership of any signal state.
        if any(signal.getitimer(signal.ITIMER_REAL)) or signal.SIGALRM in signal.sigpending():
            raise RuntimeError("SIGALRM became occupied before deadline entry")
        acquired = True
        _deadline_active = True
        signal.signal(signal.SIGALRM, expire)
        remaining = seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise DeadlineExpired("provider operation exceeded its total deadline")
        signal.setitimer(signal.ITIMER_REAL, remaining)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        try:
            yield
        except Exception as exc:
            # urllib can wrap an OSError (including our TimeoutError) in URLError.
            if expired and not isinstance(exc, DeadlineExpired):
                raise DeadlineExpired("provider operation exceeded its total deadline") from exc
            raise
        else:
            # Also reject a late return if a callback swallowed the signal error.
            if expired or time.monotonic() - started >= seconds:
                raise DeadlineExpired("provider operation exceeded its total deadline")
    finally:
        try:
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        finally:
            try:
                if acquired:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    # Do not deliver our queued alarm to the restored foreign handler.
                    if signal.SIGALRM in signal.sigpending():
                        signal.sigwait({signal.SIGALRM})
            finally:
                try:
                    if acquired:
                        signal.signal(signal.SIGALRM, previous_handler)
                finally:
                    _deadline_active = False
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
