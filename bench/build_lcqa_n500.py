"""Build long_context_qa_n500.json (mirrors run_lcqa_warmup_n300's builder at N=500).

Source hotpot_distractor has 500 tasks; each gets +10 distractor paragraphs and a
shuffle, producing 13-18 KB prompts above the 8 KB CC activation gate.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
SRC = _REPO / "bench" / "fixtures" / "hotpot_distractor.json"
OUT = _REPO / "bench" / "fixtures" / "long_context_qa_n500.json"
N_TASKS = 500
EXTRAS = 10


def main() -> None:
    src = json.loads(SRC.read_text())
    pool, seen = [], set()
    for task in src:
        for para in (task.get("meta") or {}).get("paragraphs", []):
            if para.get("supporting"):
                continue
            title = para.get("title", "")
            if title and title not in seen:
                seen.add(title)
                pool.append(para)
    rng = random.Random(42)
    out = []
    for task in src[:N_TASKS]:
        meta = dict(task.get("meta") or {})
        paragraphs = list(meta.get("paragraphs", []))
        existing = {p.get("title", "") for p in paragraphs}
        added, attempts = [], 0
        while len(added) < EXTRAS and attempts < 200:
            cand = rng.choice(pool)
            t = cand.get("title", "")
            if t and t not in existing:
                added.append({"title": t, "sentences": cand.get("sentences", []), "supporting": False})
                existing.add(t)
            attempts += 1
        new_paras = paragraphs + added
        rng.shuffle(new_paras)
        new_task = dict(task)
        new_task["meta"] = {**meta, "paragraphs": new_paras}
        out.append(new_task)
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    sizes = [sum(len(p["title"]) + sum(len(s) for s in p.get("sentences", [])) for p in t["meta"]["paragraphs"]) for t in out]
    over = sum(1 for s in sizes if s > 8192)
    print(f"built {len(out)} tasks, {over}/{len(out)} above 8 KB gate -> {OUT}")


if __name__ == "__main__":
    main()
