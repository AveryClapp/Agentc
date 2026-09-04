# Reproduction: accuracy guard and cross-model selectivity

Committed drivers for the shadow-divergence accuracy-guard results. Each writes
`gsweep_*.csv` summary files into `bench/paper_results/` (plus `*.per_task.csv`
pass/fail vectors). See the top-level `Reproducibility` section / `tab:repro` for
the rule-validation tables; this directory covers the guard and cross-model results.

The prospective main-track campaign is governed by the frozen
[MLSys 2027 staged evaluation protocol](mlsys-2027-evaluation-protocol.md). Its
engineering, calibration, pilot, and confirmatory stages must not be mixed with
the historical guard results described below.

## Prerequisites

- Python env: `.venv` with the project installed (`PYTHONPATH=python`).
- `OPENAI_API_KEY` for the gpt-4o-mini runs.
- For cross-model runs, an OpenAI-compatible endpoint + key (see below). No code
  changes are needed: routing is via `BENCH_OPENAI_BASE_URL` + `BENCH_BASELINE_MODEL`.

## Script -> paper artifact map

| Script | Reproduces | Notes |
|---|---|---|
| `guard_frontier.sh` | `tab:guard`, `fig:metric-tradeoff` | gpt-4o-mini, n=200, budget frontier tau in {0.10,0.20,0.30,0.50} |
| `crossmodel_selectivity.sh` | `tab:xmodel` (one row) | n=100; run once per model family (below) |
| `../guard_overhead_bench.py` | fresh complete-plan feedback overhead | `python -m bench.guard_overhead_bench`; structured Stage E0 output, CPU/SQLite only, no API |
| `../paper_figures/fig9_metric_tradeoff.py` | renders `fig:metric-tradeoff` | reads the `gsweep_tradeoff_*` CSVs |

## Reproducing `tab:guard` + `fig:metric-tradeoff`

```bash
OPENAI_API_KEY=... bash bench/repro/guard_frontier.sh
python bench/paper_figures/fig9_metric_tradeoff.py
```

## Reproducing `tab:xmodel` (4 families)

Each family is one invocation of `crossmodel_selectivity.sh`. The operating budget
`TAU` is a per-model hyperparameter (the verbose Qwen3 regime calibrates tighter):

```bash
# gpt-4o-mini (OpenAI), tau=0.20  -- default endpoint
MODEL=gpt-4o-mini-2024-07-18 BASE=https://api.openai.com/v1 TAU=0.20 PREFIX=gpt \
  OPENAI_API_KEY=... bash bench/repro/crossmodel_selectivity.sh

# Llama-3.3-70B (Meta) via Together, tau=0.20
MODEL=meta-llama/Llama-3.3-70B-Instruct-Turbo BASE=https://api.together.xyz/v1 \
  TAU=0.20 PREFIX=xmodel TOGETHER_API_KEY=... bash bench/repro/crossmodel_selectivity.sh

# Claude Haiku 4.5 (Anthropic) via the OpenAI-compat endpoint, tau=0.20
MODEL=claude-haiku-4-5 BASE=https://api.anthropic.com/v1/ \
  TAU=0.20 PREFIX=claude ANTHROPIC_API_KEY=... bash bench/repro/crossmodel_selectivity.sh

# Qwen3-235B (Alibaba) via Together, tau=0.10 (its operating point)
MODEL=Qwen/Qwen3-235B-A22B-Instruct-2507-tput BASE=https://api.together.xyz/v1 \
  TAU=0.10 PREFIX=qwen3 TOGETHER_API_KEY=... bash bench/repro/crossmodel_selectivity.sh
```

## Notes

- `AGENTC_OPTIMIZE_SHADOW=1` (full sampling) is set so the predecessor guard's
  auto-disable triggers within a single run; the runtime default is 0.02. The
  disable timing, damage, fire retention, latency, and net cost are all
  sampling-rate-dependent, so these cells support only metric selectivity under
  dense feedback. They do not validate the 2% operating point. Shadow calls are
  absent from the reported token and cost totals, making guarded savings gross
  main-path quantities rather than net deployment savings.
- Temperature-1 stochasticity gives ~+/-2-3 pp accuracy noise at these n; the
  decision (rule kept vs disabled) is robust, recovery completeness is not.
- Qwen3-235B served via Together throughput tier can occasionally stall a request;
  re-run the affected single cell if a run hangs with no progress.

## Offline optimizer activation preflight

`bench.activation_preflight` screens real benchmark request shapes before any
paid run. It executes the normal SDK patch and native optimizer while replacing
OpenAI dispatch with a deterministic local response. Provider credentials and
compatible-endpoint settings are masked in every worker. The JSON retains only
prompt sizes, content digests, call-site IDs, plans, and rule counts.

This is explicitly **not paper evidence**: model output, quality, token usage,
latency, and cost are synthetic; the optimizer overhead ceiling is also raised
to remove debug-build timing noise. Use it to decide which workload/rule pairs
merit a live paired evaluation, not to claim savings or semantic safety.

```bash
# Requires the native extension, OpenAI SDK, and the desired local fixtures.
maturin develop -m crates/agentc-profiler/Cargo.toml
python -m pip install -e '.[openai]'

PREFLIGHT_STORAGE="$(mktemp -d /tmp/agentc-activation-preflight.XXXXXX)"
python -m bench.activation_preflight \
  --tasks 8 \
  --storage-root "$PREFLIGHT_STORAGE" \
  --output bench/repro/activation-preflight-2026-09-03.json
```

The committed [activation-preflight-2026-09-03.json](activation-preflight-2026-09-03.json)
records the source commit and SHA-256 of every local fixture. The exact run used
generated, gitignored fixtures; `bd-bjs` tracks the still-missing clean-clone
bootstrap path, including the RAG fixture generator.

The initial screen produced the following post-warm-up rule counts. An asterisk
marks output-dependent behavior that the local response can trigger but cannot
validate.

| Workload | Class | ContextCompress gates (size / dead fraction) | Rules |
|---|---|---:|---|
| HotpotQA | natural request | 3/8 / 0/8 | OutputBudget 5* |
| Wikipedia QA | natural request | 12/16 / 0/16 | OutputBudget 10* |
| SWE-bench planner | task-prompt proxy | 0/8 / 8/8 | OutputBudget 5* |
| RAG summarizer | engineered reference | 0/24 / 24/24 | ParallelBranch 14; OutputBudget 7* |
| Long-context QA | purpose-built control | 8/8 / 8/8 | ContextCompress 5; OutputBudget 5* |
| Multi-rule QA | purpose-built control | 24/24 / 24/24 | ContextCompress 11; StateDrop 7; OutputBudget 18* |

The RAG row is diagnostic rather than clean evidence: chunk-summary and combine
calls currently share one optimizer call-site profile. `bd-8uxj` tracks that
benchmark-validity defect.

### Live Hotpot follow-up smoke

The [live n=8 result](live-hotpot-smoke-2026-09-03.json) checked the natural
Hotpot row with pinned `gpt-4o-mini-2024-07-18` calls. It reproduced the offline
plan sequence (three cold pass-throughs, then five OutputBudget rewrites), with
the same 5/8 quality outcome and exactly the same token counts and $0.0023913
cost in both arms. Every answer was already below the proposed 64-token cap, so
the rule activated without realizing savings. The file is labeled engineering
smoke and records the unrandomized order, debug build, disabled shadow sampling,
and other reasons it is not paper evidence.

### OSWorld request-shape preflight

The [OSWorld request preflight](osworld-request-preflight-2026-09-03.json)
replays the native request structure used by the frozen OSWorld V2
`AnthropicAgent` without contacting Anthropic or launching a desktop. It verifies
that Agentc intercepts `client.beta.messages.create`, blocks structural rewrites
on opaque multimodal/tool histories, permits `OutputBudget`, and returns the
original system, message, tool, beta-header, and thinking objects by identity.
It also records the output-quantile overshoot discovered during the screen as
`bd-pbus`. This is Stage E0 integration evidence only, not a benchmark result.

The follow-up [output-quantile preflight](output-quantile-preflight-2026-09-03.json)
replays the triggering constant-output case after the estimator correction.
The persisted p95/p99 now equal the three observed 80-token completions and the
resulting cap is 96 rather than 143. The file also states the conservative
all-history-maximum contract, legacy-profile implications, and the bounded-
window work deferred to `bd-bwgu`.

### Complete-plan guard preflights

Three zero-network Stage E0 checks exercise the complete-plan controller rather
than the retained per-rule compatibility path:

```bash
python -m bench.guard_persistence_preflight \
  --output /tmp/complete-plan-guard-persistence.json
python -m bench.guard_input_validation_preflight \
  --output /tmp/complete-plan-guard-input-validation.json
python -m bench.guard_overhead_bench \
  --output /tmp/complete-plan-guard-overhead.json
```

The persistence check warms a canonical `ContextCompress+OutputBudget` plan,
persists two positive-exposure samples (`E=0.8`), restarts, crosses the plan
budget on the third (`E=1.2`), and verifies the exact-plan disable across a
second restart. The input check rejects invalid divergence values without any
paired or guard state and verifies that invalid configured thresholds resolve
to the composed plan's declared `0.01` minimum. Both require zero legacy rule
rows. The overhead check issues a fresh observation token per sample and times
token validation, exact-plan profile/guard updates, and synchronous SQLite
durability separately from the divergence metric.

These checks are deliberately marked `paper_evidence=false`. In particular,
the overhead result is a single-machine local diagnostic that excludes the
shadow provider call, request dispatch, contention, and billed tokens. The
historical `18 us/sample` claim came from replaying one synthetic token through
the legacy idempotence path and is superseded; do not cite it.

Committed outputs from the corrected harnesses are
[persistence](complete-plan-guard-persistence-preflight-2026-09-03.json),
[input validation](complete-plan-guard-input-validation-preflight-2026-09-03.json),
and [overhead](complete-plan-guard-overhead-preflight-2026-09-03.json). On the
recorded development-machine run, 2,000 fresh feedback samples had a 306.2 us
mean and 412.3 us p99, plus a separately measured 5.0 us divergence metric.
Those values characterize this E0 run only.
