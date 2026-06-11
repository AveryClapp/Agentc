"""Microbenchmark: per-sample CPU overhead of the shadow-divergence accuracy guard.

The guard's steady-state cost has two parts:
  1. Shadow inference  -- the 2% sampling rate adds ~2% amortized full-cost calls
     (the dominant, tunable cost; measured analytically, not here).
  2. Per-sample bookkeeping -- for each *sampled* call the guard computes the
     output divergence metric and folds it into the Rust accuracy budget.

This benchmark measures (2): the wall-clock CPU cost of the bookkeeping path
  _text_divergence(a, b)        (Python: normalized containment metric)
  record_divergence(site, rule, d)  (PyO3 -> Rust budget fold)
on representative agent outputs, to show it is negligible against an LLM call
(O(microseconds) vs O(seconds)). Run: python -m bench.guard_overhead_bench
"""

import os
import statistics
import time

os.environ.setdefault("AGENTC_SHADOW_DIVERGENCE_MODE", "normalized")

from agentc._patches._optimizer_glue import _text_divergence
from agentc._optimizer import record_divergence

# Representative outputs: a ~60-token answer and a benign elaboration of it
# (the agreement case the normalized metric is built for).
BASE = (
    "The Kansas Song, also titled We're From Kansas, was adopted as the official "
    "state song. It was written in the early twentieth century and is performed at "
    "state ceremonies and university events across the region to this day."
)
ELAB = BASE + " The melody is widely recognized throughout the midwestern United States."


def _bench(fn, iters: int) -> dict:
    # warm up
    for _ in range(2000):
        fn()
    samples = []
    for _ in range(iters):
        t0 = time.perf_counter_ns()
        fn()
        samples.append(time.perf_counter_ns() - t0)
    samples.sort()
    return {
        "mean_us": statistics.mean(samples) / 1e3,
        "p50_us": samples[len(samples) // 2] / 1e3,
        "p99_us": samples[int(len(samples) * 0.99)] / 1e3,
    }


def main() -> None:
    iters = 50_000
    div = _bench(lambda: _text_divergence(BASE, ELAB), iters)
    # fold a precomputed divergence into the budget (same key each time)
    fold = _bench(lambda: record_divergence("bench_site", "ContextCompress", 0.0), iters)
    total_mean = div["mean_us"] + fold["mean_us"]

    print(f"divergence metric (_text_divergence, normalized): "
          f"mean={div['mean_us']:.2f}us p50={div['p50_us']:.2f}us p99={div['p99_us']:.2f}us")
    print(f"budget fold (record_divergence, PyO3->Rust):       "
          f"mean={fold['mean_us']:.2f}us p50={fold['p50_us']:.2f}us p99={fold['p99_us']:.2f}us")
    print(f"TOTAL per sampled call: mean={total_mean:.2f}us "
          f"({iters:,} iters each; mode=normalized)")
    print(f"  vs a typical LLM call (~1-5 s): the bookkeeping is ~{5e6/total_mean:,.0f}x cheaper")


if __name__ == "__main__":
    main()
