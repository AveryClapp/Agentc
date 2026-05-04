"""Generate ``bench/fixtures/long_context_qa.json``.

Extends ``hotpot_distractor.json`` by injecting extra distractor
paragraphs into each task. The original HotpotQA-distractor row has
10 paragraphs (~6-9 KB total prompt) — sometimes below the
ContextCompress 8 KB activation gate. We inject 10 more distractors
sampled from *other* tasks' pools, yielding 20 paragraphs and
~13-18 KB prompts. The supporting paragraphs remain in place; only
the noise floor grows.

Usage:
    python -m bench.build_long_context_fixture                    # default 100 tasks
    python -m bench.build_long_context_fixture --total 100        # explicit
    python -m bench.build_long_context_fixture --extras 12        # add 12 distractors per task
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any


_INPUT = Path("bench/fixtures/hotpot_distractor.json")
_OUTPUT = Path("bench/fixtures/long_context_qa.json")


def _ctx_bytes(paragraphs: list[dict[str, Any]]) -> int:
    return sum(
        len(p["title"]) + sum(len(s) for s in p.get("sentences", []))
        for p in paragraphs
    )


def build(total: int, extras: int, seed: int) -> list[dict[str, Any]]:
    src = json.loads(_INPUT.read_text())

    pool: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for task in src:
        for para in (task.get("meta") or {}).get("paragraphs", []):
            if para.get("supporting"):
                continue
            title = para.get("title", "")
            if title in seen_titles:
                continue
            seen_titles.add(title)
            pool.append(para)

    rng = random.Random(seed)
    out: list[dict[str, Any]] = []
    for task in src[:total]:
        meta = dict(task.get("meta") or {})
        paragraphs: list[dict[str, Any]] = list(meta.get("paragraphs", []))
        existing = {p.get("title", "") for p in paragraphs}

        added: list[dict[str, Any]] = []
        attempts = 0
        while len(added) < extras and attempts < 200:
            cand = rng.choice(pool)
            t = cand.get("title", "")
            if t and t not in existing:
                # Always add as a non-supporting paragraph; even if the
                # source task had it as supporting, in this task it's
                # context noise.
                added.append({
                    "title": t,
                    "sentences": cand.get("sentences", []),
                    "supporting": False,
                })
                existing.add(t)
            attempts += 1

        new_paras = paragraphs + added
        rng.shuffle(new_paras)

        new_task = dict(task)
        new_task["meta"] = {**meta, "paragraphs": new_paras}
        out.append(new_task)

    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m bench.build_long_context_fixture",
        description="Build long-context QA fixture from hotpot_distractor.",
    )
    p.add_argument("--total", type=int, default=100, help="Tasks to include (default: 100)")
    p.add_argument("--extras", type=int, default=10, help="Extra distractors per task (default: 10)")
    p.add_argument("--seed", type=int, default=42, help="RNG seed for distractor sampling")
    p.add_argument("--out", default=str(_OUTPUT), help=f"Output path (default: {_OUTPUT})")
    args = p.parse_args(argv)

    if not _INPUT.is_file():
        print(f"error: {_INPUT} missing — run `python -m bench.build_hotpot_fixture` first")
        return 1

    tasks = build(total=args.total, extras=args.extras, seed=args.seed)

    sizes = [_ctx_bytes(t["meta"]["paragraphs"]) for t in tasks]
    over_8k = sum(1 for s in sizes if s > 8192)
    print(
        f"Wrote {len(tasks)} tasks  |  "
        f"context bytes: min={min(sizes)} median={sorted(sizes)[len(sizes)//2]} max={max(sizes)}  |  "
        f"{over_8k}/{len(sizes)} above 8 KB"
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(tasks, indent=2) + "\n")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
