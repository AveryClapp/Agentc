"""@agentc.memoize decorator and cache invalidation helpers.

Fail-open posture: every lookup and enqueue path is wrapped in a bare
``except BaseException`` and degrades to a cache miss or no-op. Nothing
raised by the caching layer ever reaches the wrapped function's caller.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import pickle
from typing import Any, Callable

from agentc._canonicalize import canonicalize_prompt, parameters_hash, prompt_hash
from agentc._writer import CacheInsertMsg, enqueue_cache_insert

logger = logging.getLogger("agentc")

DEFAULT_TTL_S = 3600
DEFAULT_SIMILARITY = 0.92
LSH_DISABLED = 1.0


def _env_override_enabled() -> bool:
    """AGENTC_MEMOIZE=0 turns every decorator into a passthrough."""
    raw = os.environ.get("AGENTC_MEMOIZE")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_override_similarity(default: float) -> float:
    raw = os.environ.get("AGENTC_MEMOIZE_SIMILARITY")
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_override_ttl(default: int) -> int:
    raw = os.environ.get("AGENTC_MEMOIZE_TTL")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _default_call_site_id(fn: Callable[..., Any]) -> str:
    """Return ``module.qualname:line`` for the wrapped function.

    Used when the caller does not pass ``call_site_id``. Derived once at
    decoration time so runtime cost is zero.
    """
    module = getattr(fn, "__module__", "unknown")
    qualname = getattr(fn, "__qualname__", getattr(fn, "__name__", "unknown"))
    try:
        line = inspect.getsourcelines(fn)[1]
    except (OSError, TypeError):
        line = 0
    return f"{module}.{qualname}:{line}"


def _build_canonical_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Shape positional + keyword args into a dict the canonicalizer accepts.

    The raw adapter coerces arbitrary Python to a single-message "user"
    envelope — that gives us a stable canonical form without requiring the
    caller's function signature to follow an LLM-provider shape.
    """
    return {"args": list(args), "kwargs": dict(kwargs)}


def _prompt_text_for_embedding(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """Extract plain text to embed for LSH, or ``None`` to skip embedding.

    We only embed if there is exactly one string-ish argument. Anything
    else (structured args, multiple strings, binary payloads) falls back
    to exact-hash-only caching — LSH over pickle-dumped bytes is noise.
    """
    candidates: list[str] = []
    for a in args:
        if isinstance(a, str):
            candidates.append(a)
    for v in kwargs.values():
        if isinstance(v, str):
            candidates.append(v)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _compute_cache_key(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    model: str,
) -> tuple[bytes, bytes] | None:
    """Canonicalize + hash. Returns ``(prompt_hash, parameters_hash)`` or ``None``.

    ``None`` means "do not attempt caching" — e.g. the canonicalizer raised
    on something unpicklable. The wrapped function still runs.
    """
    try:
        raw_prompt = _build_canonical_args(args, kwargs)
        p_hash = prompt_hash(raw_prompt, "raw")
        params = {"model": model} if model else {}
        param_hash = parameters_hash(params)
        return p_hash, param_hash
    except BaseException:
        logger.debug("memoize: canonicalization failed", exc_info=True)
        return None


def _deserialize_hit(hit: dict[str, Any]) -> Any | None:
    """Fetch and decode the cached output, or ``None`` on any failure.

    The native layer returns ``output_content_id`` (a content-addressed
    reference into the shared ``output_content`` table). We currently load
    the row directly and unpickle its payload. Any decode failure is
    treated as a miss.
    """
    try:
        content_id = hit.get("output_content_id")
        if not content_id:
            return None
        from agentc._native import output_content_load  # type: ignore[attr-defined]

        raw = output_content_load(content_id)
        if raw is None:
            return None
        return pickle.loads(raw)
    except BaseException:
        logger.debug("memoize: deserialize failed", exc_info=True)
        return None


def _serialize_output(value: Any) -> bytes | None:
    try:
        return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    except BaseException:
        logger.debug("memoize: pickle failed", exc_info=True)
        return None


def _embedding_bytes(prompt_text: str | None) -> bytes | None:
    if prompt_text is None:
        return None
    try:
        from agentc._native import embed_text_bytes

        return embed_text_bytes(prompt_text)
    except BaseException:
        logger.debug("memoize: embedding failed", exc_info=True)
        return None


def _gate_enabled(
    enabled: bool | Callable[..., bool],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> bool:
    if callable(enabled):
        try:
            return bool(enabled(*args, **kwargs))
        except BaseException:
            logger.debug("memoize: enabled() predicate raised", exc_info=True)
            return False
    return bool(enabled)


def memoize(
    *,
    ttl: int = DEFAULT_TTL_S,
    similarity: float = DEFAULT_SIMILARITY,
    models: list[str] | None = None,
    call_site_id: str | None = None,
    enabled: bool | Callable[..., bool] = True,
    model: str = "",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a function to serve repeat calls from the memoization cache.

    See ``specs/memoization.md`` for the full interface contract. All keyword
    arguments follow the spec's defaults; environment variables take
    precedence per ``AGENTC_MEMOIZE*`` so operators can disable caching
    without editing code.

    The ``model`` argument is an Agentc-specific opt-in: it pins the cache
    key to a specific model name so the same function called against
    different backends does not collide. When the wrapped function is a
    plain Python callable (no model parameter), leave it empty.
    """
    effective_ttl = _env_override_ttl(ttl)
    effective_similarity = _env_override_similarity(similarity)
    globally_enabled = _env_override_enabled()

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        site_id = call_site_id or _default_call_site_id(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not globally_enabled or not _gate_enabled(enabled, args, kwargs):
                return fn(*args, **kwargs)

            if models is not None and model and model not in models:
                return fn(*args, **kwargs)

            key = _compute_cache_key(fn, args, kwargs, model)
            if key is None:
                return fn(*args, **kwargs)
            p_hash, param_hash = key

            prompt_text = _prompt_text_for_embedding(args, kwargs)
            emb = (
                _embedding_bytes(prompt_text)
                if effective_similarity < LSH_DISABLED
                else None
            )

            try:
                from agentc._native import cache_lookup

                hit = cache_lookup(
                    p_hash,
                    model,
                    param_hash,
                    site_id,
                    emb,
                    effective_similarity,
                )
            except BaseException:
                logger.debug("memoize: cache_lookup failed", exc_info=True)
                hit = None

            if hit is not None:
                cached = _deserialize_hit(hit)
                if cached is not None:
                    return cached

            result = fn(*args, **kwargs)

            output_bytes = _serialize_output(result)
            if output_bytes is not None:
                try:
                    enqueue_cache_insert(
                        CacheInsertMsg(
                            prompt_hash=p_hash,
                            model=model,
                            parameters_hash=param_hash,
                            call_site_id=site_id,
                            output_bytes=output_bytes,
                            input_tokens=0,
                            output_tokens=0,
                            recorded_cost_usd=0.0,
                            ttl_seconds=effective_ttl,
                            embedding=emb,
                        )
                    )
                except BaseException:
                    logger.debug("memoize: enqueue_cache_insert failed", exc_info=True)

            return result

        return wrapper

    return decorator


def cache_invalidate(pattern: str) -> int:
    """Delete cache entries whose ``call_site_id`` matches ``pattern``.

    Returns rows deleted. Any internal failure is swallowed and returns 0
    — invalidation is advisory; a stale entry expires at TTL anyway.
    """
    try:
        from agentc._native import cache_invalidate as _native_invalidate

        return int(_native_invalidate(pattern))
    except BaseException:
        logger.debug("memoize: cache_invalidate failed", exc_info=True)
        return 0


def cache_invalidate_all() -> int:
    """Delete every cache entry. Convenience wrapper over ``cache_invalidate('*')``."""
    return cache_invalidate("*")
