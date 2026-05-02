"""Configuration management for Agentc.

Config precedence: init() kwargs > env vars > ~/.agentc/config.toml > defaults.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("agentc")

_KNOWN_TOML_KEYS = {"capture_content", "capture_embeddings", "fail_open", "storage_path"}

_ENV_MAP = {
    "AGENTC_CAPTURE_CONTENT": "capture_content",
    "AGENTC_CAPTURE_EMBEDDINGS": "capture_embeddings",
    "AGENTC_FAIL_OPEN": "fail_open",
    "AGENTC_STORAGE_PATH": "storage_path",
}


@dataclass(frozen=True)
class Config:
    """Resolved profiler configuration."""

    capture_content: bool
    capture_embeddings: bool
    fail_open: bool
    storage_path: Path


def _parse_bool(value: str) -> bool:
    return value.lower() in ("true", "1", "yes")


def _resolve_storage_path(raw: str) -> Path:
    """Resolve storage path, expanding ~ and falling back to temp dir."""
    if raw.startswith("~"):
        try:
            return Path.home() / raw[2:]  # strip ~/
        except RuntimeError:
            import tempfile

            fallback = Path(tempfile.gettempdir()) / "agentc"
            logger.warning("HOME not set, using temp directory: %s", fallback)
            return fallback
    return Path(raw)


def _read_config_toml(storage_path: Path) -> dict[str, Any]:
    """Read config.toml if it exists. Returns empty dict on missing/error."""
    config_path = storage_path / "config.toml"
    if not config_path.exists():
        return {}
    try:
        import tomllib

        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        # Warn on unknown keys
        unknown = set(data.keys()) - _KNOWN_TOML_KEYS
        if unknown:
            logger.warning("Unknown keys in config.toml: %s. Ignoring.", ", ".join(sorted(unknown)))
        return {k: v for k, v in data.items() if k in _KNOWN_TOML_KEYS}
    except Exception:
        logger.debug("Failed to read config.toml", exc_info=True)
        return {}


def _read_env_vars() -> dict[str, Any]:
    """Read AGENTC_* environment variables."""
    result: dict[str, Any] = {}
    for env_key, config_key in _ENV_MAP.items():
        value = os.environ.get(env_key)
        if value is not None:
            if config_key in ("capture_content", "capture_embeddings", "fail_open"):
                result[config_key] = _parse_bool(value)
            else:
                result[config_key] = value
    return result


def resolve_config(
    *,
    capture_content: bool | None = None,
    capture_embeddings: bool | None = None,
    fail_open: bool | None = None,
    storage_path: str | None = None,
) -> Config:
    """Resolve config with precedence: kwargs > env > toml > defaults.

    ``None`` for any arg means "not explicitly passed" — env / toml / defaults
    apply for that field. Pass a concrete value only to force it past those layers.
    """
    defaults: dict[str, Any] = {
        "capture_content": True,
        "capture_embeddings": None,
        "fail_open": True,
        "storage_path": "~/.agentc",
    }

    kwargs: dict[str, Any] = {}
    if capture_content is not None:
        kwargs["capture_content"] = capture_content
    if capture_embeddings is not None:
        kwargs["capture_embeddings"] = capture_embeddings
    if fail_open is not None:
        kwargs["fail_open"] = fail_open
    if storage_path is not None:
        kwargs["storage_path"] = storage_path

    # Read toml (needs resolved storage path to find config.toml)
    resolved_path = _resolve_storage_path(kwargs.get("storage_path", defaults["storage_path"]))
    toml_config = _read_config_toml(resolved_path)

    # Read env
    env_config = _read_env_vars()

    # Merge: kwargs > env > toml > defaults
    merged: dict[str, Any] = {}
    for key in ("capture_content", "capture_embeddings", "fail_open", "storage_path"):
        if key in kwargs:
            merged[key] = kwargs[key]
        elif key in env_config:
            merged[key] = env_config[key]
        elif key in toml_config:
            merged[key] = toml_config[key]
        else:
            merged[key] = defaults[key]

    # Resolve capture_embeddings default: follows capture_content
    if merged["capture_embeddings"] is None:
        merged["capture_embeddings"] = merged["capture_content"]

    n_kwargs = len(kwargs)
    n_env = len(env_config)
    found_toml = bool(toml_config)
    logger.debug("Config loaded: kwargs=%d, env=%d, config.toml=%s", n_kwargs, n_env, found_toml)

    return Config(
        capture_content=bool(merged["capture_content"]),
        capture_embeddings=bool(merged["capture_embeddings"]),
        fail_open=bool(merged["fail_open"]),
        storage_path=_resolve_storage_path(str(merged["storage_path"])),
    )
