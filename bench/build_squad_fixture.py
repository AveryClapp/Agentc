"""Build a SQuAD reading-comprehension fixture for the StateDrop generalization
probe (different domain from the HotpotQA-derived corpora).

Output: bench/fixtures/squad_qa.json
  [{task_id, prompt(question), expected(answer), meta:{context}}, ...]
"""
from __future__ import annotations

import json
from pathlib import Path

from datasets import load_dataset

OUT = Path(__file__).resolve().parent / "fixtures" / "squad_qa.json"
N = 200


def main() -> None:
    ds = load_dataset("squad", split="validation")
    rows = []
    seen_q = set()
    for ex in ds:
        ans = ex["answers"]["text"]
        if not ans or not ans[0].strip():
            continue
        q = ex["question"].strip()
        if q in seen_q:
            continue
        seen_q.add(q)
        rows.append({
            "task_id": f"squad-{ex['id']}",
            "prompt": q,
            "expected": ans[0].strip(),
            "meta": {"context": ex["context"].strip()},
        })
        if len(rows) >= N:
            break
    OUT.write_text(json.dumps(rows, indent=2) + "\n")
    print(f"wrote {len(rows)} SQuAD tasks -> {OUT}")


if __name__ == "__main__":
    main()
