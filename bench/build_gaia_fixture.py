"""One-shot generator / extender for ``bench/fixtures/gaia_router.json``.

Pulls the GAIA validation split from HuggingFace and writes (or extends)
the fixture the bench harness uses.  Tasks that require file attachments
(``file_name != ""``) are skipped — the bench agent is text-only.

HuggingFace setup (one-time, requires accepting the GAIA licence):

    huggingface-cli login
    huggingface-cli download gaia-benchmark/GAIA \\
        --repo-type dataset

Usage:

    python -m bench.build_gaia_fixture              # extend to default 200
    python -m bench.build_gaia_fixture --total 200
    python -m bench.build_gaia_fixture --total 200 --overwrite
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


_FIXTURE_PATH = Path("bench/fixtures/gaia_router.json")

# Prefer Level-1 tasks (shorter reasoning chains); include Level-2 if needed
# to reach the target.  Level-3 tasks require many tool calls and the bench
# agent doesn't use tools, so they almost always fail — exclude them.
_LEVEL_PREFERENCE = ["1", "2", "3"]


def _load_dataset() -> list[dict[str, Any]]:
    """Load GAIA validation rows via the HuggingFace datasets library.

    Falls back to the parquet download path used by build_hotpot_fixture
    if the high-level ``datasets`` API is unavailable.
    """
    try:
        from datasets import load_dataset  # type: ignore[import]

        ds = load_dataset("gaia-benchmark/GAIA", "2023_all", split="validation",
                          trust_remote_code=True)
        return list(ds)
    except Exception:
        pass

    # Parquet fallback — works if the user ran huggingface-cli download.
    from huggingface_hub import hf_hub_download
    import pyarrow.parquet as pq

    path = hf_hub_download(
        repo_id="gaia-benchmark/GAIA",
        filename="2023/validation/metadata.jsonl",
        repo_type="dataset",
    )
    rows: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _to_fixture(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw GAIA row to our fixture schema."""
    level = str(row.get("Level", row.get("level", "")))
    # HF datasets may expose these under different key cases.
    question = row.get("Question") or row.get("question") or ""
    answer = row.get("Final answer") or row.get("final_answer") or ""
    task_id = row.get("task_id") or row.get("id") or ""
    annotator = row.get("Annotator Metadata") or row.get("annotator_metadata") or {}

    return {
        "task_id": f"gaia-{task_id}",
        "prompt": question,
        "expected": answer,
        "meta": {
            "level": level,
            "annotator_metadata": annotator,
        },
    }


def _is_text_only(row: dict[str, Any]) -> bool:
    fname = row.get("file_name") or row.get("filename") or ""
    return fname == "" or fname is None


def build(total: int, overwrite: bool) -> list[dict[str, Any]]:
    existing: list[dict[str, Any]] = []
    if _FIXTURE_PATH.is_file() and not overwrite:
        existing = json.loads(_FIXTURE_PATH.read_text())

    existing_ids = {t["task_id"] for t in existing}
    print(f"Existing tasks: {len(existing)}, target total: {total}")

    needed = total - len(existing)
    if needed <= 0:
        print(f"Already at {len(existing)} tasks — nothing to add.")
        return existing

    rows = _load_dataset()
    print(f"Loaded {len(rows)} raw rows from GAIA validation split")

    # Sort: Level 1 first, then Level 2.
    def sort_key(r: dict[str, Any]) -> int:
        lvl = str(r.get("Level") or r.get("level") or "3")
        try:
            return _LEVEL_PREFERENCE.index(lvl)
        except ValueError:
            return len(_LEVEL_PREFERENCE)

    rows.sort(key=sort_key)

    added: list[dict[str, Any]] = []
    skipped_file, skipped_dup, skipped_empty = 0, 0, 0
    for row in rows:
        if len(added) >= needed:
            break
        if not _is_text_only(row):
            skipped_file += 1
            continue
        task = _to_fixture(row)
        if task["task_id"] in existing_ids:
            skipped_dup += 1
            continue
        if not task["prompt"] or not task["expected"]:
            skipped_empty += 1
            continue
        lvl = str(row.get("Level") or row.get("level") or "?")
        if lvl not in _LEVEL_PREFERENCE:
            continue
        added.append(task)
        existing_ids.add(task["task_id"])

    print(
        f"Added {len(added)} tasks  "
        f"(skipped: {skipped_dup} dups, {skipped_file} file-attachments, "
        f"{skipped_empty} empty)"
    )
    result = existing + added
    print(f"Total tasks in fixture: {len(result)}")
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m bench.build_gaia_fixture",
        description="Extend (or rebuild) bench/fixtures/gaia_router.json from GAIA.",
    )
    p.add_argument(
        "--total",
        type=int,
        default=127,
        help="Target total number of tasks in the fixture (default: 200)",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Rebuild from scratch instead of extending the existing fixture",
    )
    p.add_argument(
        "--out",
        default=str(_FIXTURE_PATH),
        help=f"Output path (default: {_FIXTURE_PATH})",
    )
    args = p.parse_args(argv)

    tasks = build(total=args.total, overwrite=args.overwrite)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(tasks, indent=2) + "\n")
    print(f"Wrote {len(tasks)} tasks to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
