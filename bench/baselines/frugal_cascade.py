"""FrugalGPT-style score-threshold cascade baseline (EXP-CASCADE).

Implements a two-tier cascade over the gaia_router workload (n=127 fixture):
  1. Send each task to the CHEAP model (gpt-4o-mini-2024-07-18) with an
     instruction to self-report a confidence score 0-100.
  2. If reported confidence < CASCADE_THRESHOLD (default 70), escalate to
     the FRONTIER model (gpt-4o) and use its answer instead (paying both calls).

Both tiers use the SAME model pair as Agentc's ModelDowngrade rule on the
gaia_router workload (see bench/run_gaia_warmup.py: BENCH_BASELINE_MODEL=gpt-4o,
agent internal model=gpt-4o-mini-2024-07-18), ensuring a fair cost comparison.

RouteLLM is cited but not reproduced: its router checkpoints require
preference-data calibration that would confound the comparison.

Outputs (written by run()):
  bench/paper_results/frugal_cascade_gaia.csv        -- aggregate summary
  bench/paper_results/frugal_cascade_gaia.per_task.csv -- paired analysis input

The per_task CSV is compatible with bench/paired_analysis.py:
  columns: agent_module, config, task_id, baseline_passed, optimized_passed
  where baseline = cheap-only result, optimized = cascade result.

Stub mode: when no OPENAI_API_KEY is set, llm_client() returns None and the
bench/_runtime.py stub path fires -- $0 spend, deterministic output.
Set BENCH_STUB_MODE=1 to force stub mode even when a .env key is present
(useful when .env auto-loads but you want to verify without spending).

Real-run cost estimate at n=127 (threshold=70):
  gpt-4o-mini: ~500 tokens/call (prompt ~300 + completion ~200), 2 calls/task
    = 127 * 2 * 500 = 127,000 tokens
    at $0.15/1M input + $0.60/1M output => ~$0.035 cheap tier
  gpt-4o escalations: assume ~40% of 127 tasks escalate = 51 tasks
    = 51 * ~800 tokens/call
    at $2.50/1M input + $10.00/1M output => ~$0.06 frontier tier
  Total estimate: ~$0.10-$0.15 for n=127 at threshold=70
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bench.agents._fixtures import GAIA_ROUTER
from bench.agents._runtime import default_check, load_tasks, llm_client

AGENT_KEY = "gaia_router"
AGENT_MODULE = "bench.baselines.frugal_cascade"
CONFIG_NAME = "frugal_cascade"

CHEAP_MODEL = "gpt-4o-mini-2024-07-18"
FRONTIER_MODEL = "gpt-4o"

OUTPUT_DIR = Path("bench/paper_results")
SUMMARY_PATH = OUTPUT_DIR / "frugal_cascade_gaia.csv"
PER_TASK_PATH = OUTPUT_DIR / "frugal_cascade_gaia.per_task.csv"

_ANSWER_SYSTEM = (
    "Answer the user's question concisely. Output only the answer, no prose around it. "
    "On the final line, write exactly: CONFIDENCE: <0-100>"
)


@dataclass
class CascadeResult:
    task_id: str
    answer: str
    passed: bool
    cheap_passed: bool
    tier: str          # "cheap" | "frontier"
    confidence: int
    cheap_input_tokens: int
    cheap_output_tokens: int
    frontier_input_tokens: Optional[int]
    frontier_output_tokens: Optional[int]
    total_cost_usd: float


def _parse_confidence(text: str) -> tuple[str, int]:
    """Extract confidence score from model output.

    Returns (clean_answer, confidence_int). If no CONFIDENCE line is found,
    defaults to 50 (triggers escalation at default threshold).
    """
    match = re.search(r"CONFIDENCE:\s*(\d+)", text, re.IGNORECASE)
    if match:
        score = min(100, max(0, int(match.group(1))))
        clean = text[: match.start()].strip()
        return clean, score
    return text.strip(), 50


def _token_count(text: str) -> int:
    """Rough token estimate: words / 0.75."""
    return max(1, int(len(text.split()) / 0.75))


def _model_cost_usd(
    model: str, input_tokens: int, output_tokens: int
) -> float:
    """Per-call cost estimate using published prices (as of 2025).

    gpt-4o-mini: $0.15/1M input, $0.60/1M output
    gpt-4o:      $2.50/1M input, $10.00/1M output
    """
    if "mini" in model:
        return input_tokens * 0.15e-6 + output_tokens * 0.60e-6
    return input_tokens * 2.50e-6 + output_tokens * 10.00e-6


def _chat(
    client,
    prompt: str,
    system: str,
    model: str,
) -> tuple[str, int, int]:
    """Call the model; return (text, input_tokens, output_tokens).

    When client is None OR BENCH_STUB_MODE=1 is set, returns a deterministic
    stub string with a CONFIDENCE line baked in so the parser fires correctly.
    Set BENCH_STUB_MODE=1 to force stub mode even when a .env key is present.
    """
    if client is None or os.environ.get("BENCH_STUB_MODE") == "1":
        stub_text = f"[stub:{model}] {prompt}\nCONFIDENCE: 85"
        return stub_text, _token_count(system + prompt), _token_count(stub_text)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    resp = client.chat.completions.create(model=model, messages=messages, temperature=0)
    text = resp.choices[0].message.content or ""
    usage = resp.usage
    in_tok = usage.prompt_tokens if usage else _token_count(system + prompt)
    out_tok = usage.completion_tokens if usage else _token_count(text)
    return text, in_tok, out_tok


def run_one(
    task,
    client,
    threshold: int,
) -> CascadeResult:
    """Run the cascade for a single task."""
    cheap_raw, cheap_in, cheap_out = _chat(
        client, task.prompt, _ANSWER_SYSTEM, CHEAP_MODEL
    )
    cheap_answer, confidence = _parse_confidence(cheap_raw)
    cheap_passed = default_check(cheap_answer, task.expected)
    cheap_cost = _model_cost_usd(CHEAP_MODEL, cheap_in, cheap_out)

    if confidence >= threshold:
        return CascadeResult(
            task_id=task.task_id,
            answer=cheap_answer,
            passed=cheap_passed,
            cheap_passed=cheap_passed,
            tier="cheap",
            confidence=confidence,
            cheap_input_tokens=cheap_in,
            cheap_output_tokens=cheap_out,
            frontier_input_tokens=None,
            frontier_output_tokens=None,
            total_cost_usd=cheap_cost,
        )

    # Escalate to frontier (we pay for BOTH calls)
    frontier_raw, frontier_in, frontier_out = _chat(
        client, task.prompt, _ANSWER_SYSTEM, FRONTIER_MODEL
    )
    frontier_answer, _ = _parse_confidence(frontier_raw)
    frontier_passed = default_check(frontier_answer, task.expected)
    frontier_cost = _model_cost_usd(FRONTIER_MODEL, frontier_in, frontier_out)

    return CascadeResult(
        task_id=task.task_id,
        answer=frontier_answer,
        passed=frontier_passed,
        cheap_passed=cheap_passed,
        tier="frontier",
        confidence=confidence,
        cheap_input_tokens=cheap_in,
        cheap_output_tokens=cheap_out,
        frontier_input_tokens=frontier_in,
        frontier_output_tokens=frontier_out,
        total_cost_usd=cheap_cost + frontier_cost,
    )


def run(threshold: Optional[int] = None, n_tasks: Optional[int] = None) -> list[CascadeResult]:
    """Run the cascade over the gaia_router fixture.

    Args:
        threshold: escalation threshold (0-100). Defaults to CASCADE_THRESHOLD
                   env var or 70.
        n_tasks:   cap task count. Defaults to BENCH_MAX_TASKS env var or all.
    """
    if threshold is None:
        threshold = int(os.environ.get("CASCADE_THRESHOLD", "70"))

    tasks = load_tasks(AGENT_KEY, GAIA_ROUTER)
    cap = n_tasks or os.environ.get("BENCH_MAX_TASKS")
    if cap:
        tasks = tasks[: int(cap)]

    client = llm_client()
    stub = client is None or os.environ.get("BENCH_STUB_MODE") == "1"
    if stub:
        print(f"Stub mode (threshold={threshold}) -- $0 spend")

    results: list[CascadeResult] = []
    for task in tasks:
        r = run_one(task, client, threshold)
        marker = "PASS" if r.passed else "FAIL"
        print(f"{marker}  [{r.tier:8s}  conf={r.confidence:3d}]  {r.task_id}")
        results.append(r)

    return results


def save(results: list[CascadeResult], threshold: int) -> None:
    """Write summary CSV and per_task CSV to bench/paper_results/."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    n = len(results)
    n_pass = sum(1 for r in results if r.passed)
    n_cheap_pass = sum(1 for r in results if r.cheap_passed)
    n_escalated = sum(1 for r in results if r.tier == "frontier")
    total_cost = sum(r.total_cost_usd for r in results)
    cheap_only_cost = sum(
        _model_cost_usd(CHEAP_MODEL, r.cheap_input_tokens, r.cheap_output_tokens)
        for r in results
    )

    acc = 100.0 * n_pass / n if n else 0.0
    cheap_acc = 100.0 * n_cheap_pass / n if n else 0.0
    esc_rate = 100.0 * n_escalated / n if n else 0.0
    overhead = 100.0 * (total_cost - cheap_only_cost) / cheap_only_cost if cheap_only_cost else 0.0

    # Aggregate summary CSV
    with SUMMARY_PATH.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "threshold", "n", "n_pass", "acc_pct",
            "n_cheap_pass", "cheap_acc_pct",
            "n_escalated", "escalation_rate_pct",
            "total_cost_usd", "cheap_only_cost_usd", "cost_overhead_pct",
        ])
        w.writerow([
            threshold, n, n_pass, f"{acc:.2f}",
            n_cheap_pass, f"{cheap_acc:.2f}",
            n_escalated, f"{esc_rate:.1f}",
            f"{total_cost:.6f}", f"{cheap_only_cost:.6f}", f"{overhead:.1f}",
        ])

    print(f"Wrote summary to {SUMMARY_PATH}")
    print(
        f"  n={n}  acc={acc:.1f}%  escalated={n_escalated} ({esc_rate:.1f}%)  "
        f"cost=${total_cost:.6f}"
    )

    # Per-task CSV (paired_analysis.py format)
    # baseline = cheap-only answer; optimized = cascade answer
    with PER_TASK_PATH.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["agent_module", "config", "task_id", "baseline_passed", "optimized_passed"])
        for r in results:
            w.writerow([
                AGENT_MODULE,
                CONFIG_NAME,
                r.task_id,
                int(r.cheap_passed),
                int(r.passed),
            ])

    print(f"Wrote per_task to {PER_TASK_PATH}")


if __name__ == "__main__":
    threshold = int(os.environ.get("CASCADE_THRESHOLD", "70"))
    results = run(threshold=threshold)
    save(results, threshold)
