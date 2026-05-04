"""Lifecycle management: init(), shutdown(), atexit/signal handlers.

This module owns the global initialization state and coordinates startup/teardown.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import threading
from types import FrameType
from typing import Any

from agentc._config import Config, resolve_config

logger = logging.getLogger("agentc")

# Global state
_initialized = threading.Event()
_shutdown_in_progress = threading.Event()
_init_lock = threading.Lock()
_config: Config | None = None
_prev_sigterm: Any = None
_prev_sigint: Any = None


def is_initialized() -> bool:
    """Check if agentc has been initialized."""
    return _initialized.is_set()


def get_config() -> Config | None:
    """Get the current config, or None if not initialized."""
    return _config


def init(
    *,
    capture_content: bool | None = None,
    capture_embeddings: bool | None = None,
    fail_open: bool | None = None,
    storage_path: str | None = None,
) -> None:
    """Initialize the Agentc profiler runtime.

    Idempotent: second calls are a no-op. First-caller-wins for config.
    After shutdown(), init() can be called again to re-initialize.

    Unset args (``None``) defer to env vars / config.toml / defaults — pass
    a value here only to force it past those layers.

    Raises on failure (not suppressed by fail_open — init is an explicit user call).
    """
    global _config

    if _initialized.is_set():
        logger.debug("agentc.init() called again — no-op (already initialized)")
        return

    with _init_lock:
        # Double-check under lock to prevent concurrent init()
        if _initialized.is_set():
            return

        config = resolve_config(
            capture_content=capture_content,
            capture_embeddings=capture_embeddings,
            fail_open=fail_open,
            storage_path=storage_path,
        )

        # Create directories
        config.storage_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        active_dir = config.storage_path / "active"
        active_dir.mkdir(mode=0o700, exist_ok=True)

        # Create per-process DB
        pid = os.getpid()
        db_path = active_dir / f"pid-{pid}.db"
        from agentc._native import create_db

        create_db(
            str(db_path),
            False,  # per-process DB, no traces VIEW
            config.capture_content,
            config.capture_embeddings,
        )

        # Store config
        _config = config

        # Start background writer
        from agentc._writer import start as start_writer

        start_writer()

        # Apply SDK patches
        _apply_patches()

        # Register shutdown handlers (signal handlers require main thread)
        _register_shutdown_handlers()

        # Mark as initialized
        _initialized.set()
        _shutdown_in_progress.clear()

    logger.info(
        "agentc initialized (capture_content=%s, capture_embeddings=%s, storage_path=%s, fail_open=%s)",
        config.capture_content,
        config.capture_embeddings,
        config.storage_path,
        config.fail_open,
    )


def shutdown(timeout_ms: int = 5000) -> None:
    """Flush pending spans and shut down the background writer.

    Reentrant guard: if called while already shutting down, returns immediately.
    Raises on merge failure (not suppressed by fail_open).

    Args:
        timeout_ms: Max time to wait for queue drain before proceeding with merge.
    """
    global _config

    if not _initialized.is_set():
        return

    # Reentrant guard
    if _shutdown_in_progress.is_set():
        return
    _shutdown_in_progress.set()

    logger.info("agentc shutdown started (timeout_ms=%d)", timeout_ms)

    try:
        # Drain the span queue (writer logs its own counters on stop).
        _flush_queue(timeout_ms)
        # Force-flush the cost model so the final partial batch (anything
        # below COST_MODEL_FLUSH_EVERY since the last periodic flush) lands
        # in cost_model.db before the process exits.
        try:
            from agentc import _native

            _native.optimize_flush()
        except BaseException:
            logger.debug("optimize_flush failed (suppressed)", exc_info=True)
        # Merge happens on the writer thread (bd-2os.2) and on CLI reads —
        # nothing to do here.
        _remove_patches()
        logger.info("agentc shutdown complete")
    finally:
        _initialized.clear()
        _config = None


def _apply_patches() -> None:
    """Apply SDK monkey-patches via wrapt + framework provenance adapters.

    Framework adapters (langgraph / crewai / autogen) tag inter-node
    payloads with ``LlmOutput`` so ``ParallelBranch`` and ``StateDrop``
    can see provenance for messages that didn't originate in user code.
    Each adapter no-ops if its framework isn't importable, so this is
    safe to call unconditionally."""
    from agentc._patches._anthropic import patch as patch_anthropic
    from agentc._patches._openai import patch as patch_openai
    from agentc._provenance_frameworks import install_all

    patch_anthropic()
    patch_openai()
    try:
        installed = install_all()
        active = [name for name, ok in installed.items() if ok]
        if active:
            logger.debug("provenance adapters installed: %s", ", ".join(active))
    except BaseException:
        logger.debug("provenance adapter install failed (suppressed)", exc_info=True)


def _remove_patches() -> None:
    """Remove SDK monkey-patches and framework provenance adapters."""
    from agentc._patches._anthropic import unpatch as unpatch_anthropic
    from agentc._patches._openai import unpatch as unpatch_openai
    from agentc._provenance_frameworks import uninstall_all

    unpatch_anthropic()
    unpatch_openai()
    try:
        uninstall_all()
    except BaseException:
        logger.debug("provenance adapter uninstall failed (suppressed)", exc_info=True)


def _flush_queue(timeout_ms: int) -> None:
    """Flush the span queue by stopping the background writer."""
    from agentc._writer import stop as stop_writer

    stop_writer(timeout_ms=timeout_ms)


def _register_shutdown_handlers() -> None:
    """Register atexit and signal handlers for clean shutdown."""
    global _prev_sigterm, _prev_sigint

    # atexit handler — best effort, never propagate
    atexit.register(_atexit_handler)
    logger.debug("atexit handler registered")

    # Signal handlers — chain previous handlers (main thread only)
    import threading as _th

    if _th.current_thread() is _th.main_thread():
        _prev_sigterm = signal.getsignal(signal.SIGTERM)
        _prev_sigint = signal.getsignal(signal.SIGINT)

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)
        logger.debug("Signal handlers registered (SIGTERM, SIGINT)")
    else:
        logger.debug("Not on main thread — skipping signal handler registration")


def _atexit_handler() -> None:
    """atexit callback — flush and merge, never propagate exceptions."""
    try:
        shutdown(timeout_ms=3000)
    except BaseException:
        logger.debug("atexit shutdown error (suppressed)", exc_info=True)


def _signal_handler(signum: int, frame: FrameType | None) -> Any:
    """Signal handler for SIGTERM/SIGINT — trigger shutdown, chain previous handler."""
    try:
        shutdown(timeout_ms=3000)
    except BaseException:
        logger.debug("Signal handler shutdown error (suppressed)", exc_info=True)

    # Chain to previous handler
    prev = _prev_sigterm if signum == signal.SIGTERM else _prev_sigint
    if prev is not None and prev not in (signal.SIG_DFL, signal.SIG_IGN):
        if callable(prev):
            prev(signum, frame)
    elif prev == signal.SIG_DFL:
        # Re-raise with default handler
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
