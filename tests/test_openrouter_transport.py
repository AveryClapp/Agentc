"""Deadline tests use only in-process callbacks, never network or provider APIs."""
import asyncio
import signal
import threading
import time
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from bench import openrouter_transport as transport


POSIX_TIMERS = all(hasattr(signal, name) for name in (
    "SIGALRM", "ITIMER_REAL", "getitimer", "setitimer", "pthread_sigmask",
    "SIG_BLOCK", "SIG_SETMASK", "sigpending", "sigwait",
))


@pytest.fixture
def signal_state():
    if not POSIX_TIMERS:
        pytest.skip("requires POSIX signal timers")
    transport.require_deadline_support()
    handler = signal.getsignal(signal.SIGALRM)
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        yield handler, mask
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        signal.setitimer(signal.ITIMER_REAL, 0)
        if signal.SIGALRM in signal.sigpending():
            signal.sigwait({signal.SIGALRM})
        signal.signal(signal.SIGALRM, handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def assert_clean(handler, mask):
    assert signal.getsignal(signal.SIGALRM) is handler
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == mask
    assert not transport._deadline_active


def test_preflight_does_not_change_signal_state(signal_state):
    transport.require_deadline_support()
    assert_clean(*signal_state)


def test_success_restores_foreign_handler_and_unrelated_mask(signal_state):
    def foreign_handler(*_):
        pytest.fail("foreign handler should not run")
    signal.signal(signal.SIGALRM, foreign_handler)
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    with transport.total_deadline(0.2) as value:
        assert value is None
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
    assert_clean(foreign_handler, mask)


@pytest.mark.parametrize("phase", ["open", "read"])
def test_slow_open_or_read_is_bounded(phase, signal_state):
    def callback():
        time.sleep(0.2)
    started = time.monotonic()
    with pytest.raises(transport.DeadlineExpired):
        with transport.total_deadline(0.03):
            if phase == "open":
                callback()
            if phase == "read":
                callback()
    assert time.monotonic() - started < 0.18
    assert_clean(*signal_state)


def test_heartbeat_progress_does_not_reset_whole_operation_deadline(signal_state):
    heartbeats = []
    def read():
        for _ in range(100):
            time.sleep(0.003)
            heartbeats.append(b" ")
        return b"".join(heartbeats)
    started = time.monotonic()
    with pytest.raises(transport.DeadlineExpired):
        with transport.total_deadline(0.04):
            read()
    assert 1 <= len(heartbeats) < 100
    assert time.monotonic() - started < 0.2
    assert_clean(*signal_state)


def test_open_time_and_read_time_share_one_allowance(signal_state):
    read_started = False
    with pytest.raises(transport.DeadlineExpired):
        with transport.total_deadline(0.06):
            time.sleep(0.035)  # fake open
            read_started = True
            time.sleep(0.035)  # fake read; individually below the total allowance
    assert read_started
    assert_clean(*signal_state)


@pytest.mark.parametrize("error", [ValueError("failed"), KeyboardInterrupt(), asyncio.CancelledError()])
def test_exception_and_cancellation_restore_signal_state(error, signal_state):
    with pytest.raises(type(error)) as caught:
        with transport.total_deadline(0.2):
            raise error
    assert caught.value is error
    assert_clean(*signal_state)


def test_urllib_wrapped_deadline_remains_distinguishable(signal_state):
    with pytest.raises(transport.DeadlineExpired) as caught:
        with transport.total_deadline(0.02):
            try:
                time.sleep(0.1)
            except OSError as exc:
                raise URLError(exc) from exc
    assert isinstance(caught.value.__cause__, URLError)
    assert_clean(*signal_state)


def test_swallowed_deadline_cannot_return_success(signal_state):
    with pytest.raises(transport.DeadlineExpired):
        with transport.total_deadline(0.02):
            try:
                time.sleep(0.1)
            except transport.DeadlineExpired:
                pass
    assert_clean(*signal_state)


def test_monotonic_backstop_rejects_late_normal_return(signal_state, monkeypatch):
    times = iter((0.0, 0.001, 0.5))
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(times))
    with pytest.raises(transport.DeadlineExpired):
        with transport.total_deadline(0.2):
            pass
    assert_clean(*signal_state)


def test_nested_deadline_rejected_without_disarming_outer(signal_state):
    with transport.total_deadline(0.2):
        handler = signal.getsignal(signal.SIGALRM)
        before = signal.getitimer(signal.ITIMER_REAL)
        with pytest.raises(RuntimeError, match="nested"):
            with transport.total_deadline(0.01):
                pytest.fail("nested body ran")
        assert signal.getsignal(signal.SIGALRM) is handler
        assert 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= before[0]
    assert_clean(*signal_state)


def test_existing_foreign_repeating_timer_is_not_replaced(signal_state):
    def foreign_handler(*_):
        pytest.fail("foreign timer fired during short test")
    signal.signal(signal.SIGALRM, foreign_handler)
    signal.setitimer(signal.ITIMER_REAL, 1.0, 0.5)
    before = signal.getitimer(signal.ITIMER_REAL)
    with pytest.raises(RuntimeError, match="existing"):
        transport.require_deadline_support()
    with pytest.raises(RuntimeError, match="existing"):
        with transport.total_deadline(0.01):
            pytest.fail("body ran with foreign timer active")
    after = signal.getitimer(signal.ITIMER_REAL)
    assert signal.getsignal(signal.SIGALRM) is foreign_handler
    assert 0 < after[0] <= before[0]
    assert after[1] == before[1] == 0.5


def test_blocked_sigalrm_fails_before_body(signal_state):
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    with pytest.raises(RuntimeError, match="unblocked"):
        with transport.total_deadline(0.01):
            pytest.fail("body ran with blocked alarm")
    assert_clean(signal_state[0], mask)


def test_pending_foreign_signal_is_not_consumed(signal_state):
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    signal.raise_signal(signal.SIGALRM)
    with pytest.raises(RuntimeError):
        transport.require_deadline_support()
    with pytest.raises(RuntimeError):
        with transport.total_deadline(0.01):
            pytest.fail("body ran with pending foreign alarm")
    assert signal.SIGALRM in signal.sigpending()
    assert signal.getsignal(signal.SIGALRM) is signal_state[0]
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_queued_owned_alarm_is_drained_before_restoring_handler(signal_state):
    foreign_calls = []
    def foreign_handler(*_):
        foreign_calls.append(True)
    signal.signal(signal.SIGALRM, foreign_handler)
    with transport.total_deadline(0.2):
        # Simulate expiry queued at the cleanup boundary, not a real wait.
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        signal.raise_signal(signal.SIGALRM)
        assert signal.SIGALRM in signal.sigpending()
    assert not foreign_calls
    assert signal.SIGALRM not in signal.sigpending()
    assert_clean(foreign_handler, signal_state[1])


def test_timer_acquired_after_preflight_is_left_untouched(signal_state, monkeypatch):
    getitimer = signal.getitimer
    checks = 0
    def acquire_foreign_timer(which):
        nonlocal checks
        checks += 1
        if checks == 2:
            signal.setitimer(which, 1.0, 0.5)
        return getitimer(which)
    with monkeypatch.context() as patch:
        patch.setattr(signal, "getitimer", acquire_foreign_timer)
        with pytest.raises(RuntimeError, match="became occupied"):
            with transport.total_deadline(0.2):
                pytest.fail("body ran after foreign timer acquisition")
    delay, interval = signal.getitimer(signal.ITIMER_REAL)
    assert 0 < delay <= 1.0 and interval == 0.5
    assert signal.getsignal(signal.SIGALRM) is signal_state[0]
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == signal_state[1]
    assert not transport._deadline_active


def test_unrestorable_native_handler_is_rejected(signal_state, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(signal, "getsignal", lambda _: None)
        with pytest.raises(RuntimeError, match="non-Python"):
            with transport.total_deadline(0.1):
                pytest.fail("body ran with unrestorable foreign handler")
    assert_clean(*signal_state)


def test_nonmain_thread_fails_before_body(signal_state):
    errors = []
    def worker():
        try:
            transport.require_deadline_support()
        except RuntimeError as exc:
            errors.append(str(exc))
        try:
            with transport.total_deadline(0.01):
                errors.append("body ran")
        except RuntimeError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=0.5)
    assert not thread.is_alive()
    assert len(errors) == 2 and all("main thread" in error for error in errors)
    assert_clean(*signal_state)


def test_unsupported_platform_fails_before_body(monkeypatch):
    monkeypatch.setattr(transport, "signal", SimpleNamespace())
    with pytest.raises(RuntimeError, match="POSIX"):
        transport.require_deadline_support()
    with pytest.raises(RuntimeError, match="POSIX"):
        with transport.total_deadline(1):
            pytest.fail("unsupported body ran")


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), 10**1000, True, "1", None])
def test_invalid_duration_never_enters_body(seconds):
    with pytest.raises(ValueError, match="finite and positive"):
        with transport.total_deadline(seconds):
            pytest.fail("invalid duration entered body")


@pytest.mark.parametrize("error", [OSError("timer setup failed"), KeyboardInterrupt(), asyncio.CancelledError()])
def test_timer_setup_failure_restores_foreign_handler(error, signal_state, monkeypatch):
    setitimer = signal.setitimer
    def fail_arm(which, seconds, interval=0):
        if seconds:
            raise error
        return setitimer(which, seconds, interval)
    with monkeypatch.context() as patch:
        patch.setattr(signal, "setitimer", fail_arm)
        with pytest.raises(type(error)) as caught:
            with transport.total_deadline(0.2):
                pytest.fail("body ran after setup failure")
        assert caught.value is error
    assert_clean(*signal_state)
