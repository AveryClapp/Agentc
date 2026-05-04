"""One-shot generator for ``bench/fixtures/hotpot_distractor.json``.

Reads HotpotQA-distractor (validation split) from the HuggingFace cache
and writes a filtered, normalized fixture the bench harness can consume.
Each task carries 10 paragraphs (2 supporting + 8 distractor, labeled),
the gold answer, and the original metadata for downstream analysis.

Usage:
    python -m bench.build_hotpot_fixture [--limit 150] [--min-bytes 6000]

The HF parquet must already be downloaded — typically by:
    huggingface-cli download hotpot_qa distractor/validation-*.parquet
or by running this script once with network access.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_parquet() -> Any:
    """Locate the validation parquet in the HF cache (download on miss)."""
    from huggingface_hub import hf_hub_download
    import pyarrow.parquet as pq

    path = hf_hub_download(
        repo_id="hotpot_qa",
        filename="distractor/validation-00000-of-00001.parquet",
        repo_type="dataset",
    )
    return pq.read_table(path)


def _ctx_bytes(paragraphs: list[dict[str, Any]]) -> int:
    return sum(
        len(p["title"]) + sum(len(s) for s in p["sentences"])
        for p in paragraphs
    )


def build(limit: int, min_bytes: int) -> list[dict[str, Any]]:
    table = _load_parquet()
    out: list[dict[str, Any]] = []
    for row in table.to_pylist():
        ctx = row["context"]
        sup_titles = set(row["supporting_facts"]["title"])
        paragraphs = [
            {
                "title": title,
                "sentences": sentences,
                "supporting": title in sup_titles,
            }
            for title, sentences in zip(ctx["title"], ctx["sentences"])
        ]
        # HotpotQA distractor is 10 paragraphs by construction; skip any
        # malformed row rather than guess.
        if len(paragraphs) != 10:
            continue
        if sum(1 for p in paragraphs if p["supporting"]) < 2:
            continue
        if _ctx_bytes(paragraphs) < min_bytes:
            continue
        out.append(
            {
                "task_id": f"hotpot_{row['id']}",
                "prompt": row["question"],
                "expected": row["answer"],
                "meta": {
                    "paragraphs": paragraphs,
                    "gold_answer": row["answer"],
                    "type": row["type"],
                    "level": row["level"],
                },
            }
        )
        if len(out) >= limit:
            break
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--min-bytes", type=int, default=6000)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "fixtures" / "hotpot_distractor.json",
    )
    args = parser.parse_args()

    tasks = build(args.limit, args.min_bytes)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(tasks, indent=2))
    print(f"wrote {len(tasks)} tasks to {args.out}")


if __name__ == "__main__":
    main()
