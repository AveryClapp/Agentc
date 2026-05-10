"""LLMLingua comparison baseline (EXP-009).

Runs LLMLingua as a preprocessing step on long_context_qa fixture.
Compares against Agentc's ContextCompress on the same tasks.

Install: pip install llmlingua
If not installed, skips LLMLingua and reports "N/A — install llmlingua".

Outputs:
  bench/paper_results/llmlingua_comparison.csv:
    task_id, method, input_tokens_before, input_tokens_after,
    reduction_pct, answer, passed, compression_ms
"""
from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import agentc
from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import load_tasks, llm_client

AGENT_KEY = "llmlingua_baseline"
OUTPUT_PATH = Path("bench/paper_results/llmlingua_comparison.csv")

_LLMLINGUA_AVAILABLE = False
try:
    from llmlingua import PromptCompressor  # type: ignore[import]
    _LLMLINGUA_AVAILABLE = True
except ImportError:
    pass


@dataclass
class ComparisonRow:
    task_id: str
    method: str  # "llmlingua" | "agentc_cc" | "baseline" | "llmlingua_na"
    input_tokens_before: int
    input_tokens_after: int
    compression_ms: float
    answer: str
    passed: bool


def _count_tokens(text: str) -> int:
    """Rough token count (words / 0.75)."""
    return int(len(text.split()) / 0.75)


def _run_llmlingua(task: SyntheticTask, compressor: Any) -> ComparisonRow:
    client = llm_client()
    model = os.environ.get("BENCH_BASELINE_MODEL", "gpt-4o-mini-2024-07-18")
    paragraphs = (task.meta or {}).get("paragraphs") or []
    doc = "\n\n".join(
        f"{p['title']}\n{' '.join(p.get('sentences', []))}"
        for p in paragraphs
    )
    tokens_before = _count_tokens(doc)

    t0 = time.perf_counter()
    compressed = compressor.compress_prompt(
        [doc], rate=0.5, force_tokens=[task.prompt]
    )["compressed_prompt"]
    compression_ms = (time.perf_counter() - t0) * 1000.0
    tokens_after = _count_tokens(compressed)

    if client is None:
        return ComparisonRow(
            task.task_id, "llmlingua", tokens_before, tokens_after,
            compression_ms, "[stub]", False,
        )

    msgs = [
        {"role": "system", "content": "Answer using only the context. One sentence."},
        {"role": "user", "content": compressed},
        {"role": "user", "content": f"Question: {task.prompt}"},
    ]
    resp = client.chat.completions.create(model=model, messages=msgs, temperature=0)
    answer = resp.choices[0].message.content or ""
    passed = str(task.expected).lower() in answer.lower()
    return ComparisonRow(
        task.task_id, "llmlingua", tokens_before, tokens_after,
        compression_ms, answer, passed,
    )


def run(n_tasks: int = 50) -> list[ComparisonRow]:
    tasks = load_tasks("long_context_qa", [])[:n_tasks]
    rows: list[ComparisonRow] = []
    if _LLMLINGUA_AVAILABLE:
        compressor = PromptCompressor(  # type: ignore[name-defined]
            model_name="microsoft/llmlingua-2-xlm-roberta-large-meetingbank"
        )
        for t in tasks:
            rows.append(_run_llmlingua(t, compressor))
    else:
        print("LLMLingua not installed. Install with: pip install llmlingua")
        print("Reporting N/A rows for LLMLingua.")
        for t in tasks:
            rows.append(ComparisonRow(t.task_id, "llmlingua_na", 0, 0, 0.0, "N/A", False))
    return rows


def save(rows: list[ComparisonRow]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "task_id", "method", "tokens_before", "tokens_after",
            "reduction_pct", "compression_ms", "answer", "passed",
        ])
        for r in rows:
            pct = (1 - r.input_tokens_after / max(r.input_tokens_before, 1)) * 100
            w.writerow([
                r.task_id, r.method, r.input_tokens_before, r.input_tokens_after,
                f"{pct:.1f}", f"{r.compression_ms:.1f}", r.answer[:80], r.passed,
            ])
    print(f"Wrote {len(rows)} rows to {OUTPUT_PATH}")


if __name__ == "__main__":
    n = int(os.environ.get("BENCH_MAX_TASKS", "50"))
    rows = run(n_tasks=n)
    save(rows)
