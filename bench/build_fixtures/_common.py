"""Helpers shared by every fixture builder."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def require_datasets():
    """Return the ``datasets`` module, or raise a clear installation
    hint. Kept as a function so module import is not coupled to the
    dep (the fixture-building path is separate from the run path)."""
    try:
        import datasets  # type: ignore[import-not-found]
    except ImportError as e:
        raise SystemExit(
            "This builder requires the 'datasets' library.\n"
            "Install it with:\n"
            "    uv pip install datasets\n"
        ) from e
    return datasets


def write_fixture(agent_key: str, rows: Iterable[dict[str, Any]]) -> Path:
    """Write ``rows`` to ``bench/fixtures/<agent_key>.json``.

    Returns the output path. Creates the directory on first call."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURES_DIR / f"{agent_key}.json"
    data = list(rows)
    with out.open("w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"wrote {len(data)} rows → {out}")
    return out
