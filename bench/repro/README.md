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
| `../optimizer_e2e_overhead.py` | complete native planning-call overhead, paired with the internal pre-audit clock | `python -m bench.optimizer_e2e_overhead`; release extension, SQLite only, no API |
| `../optimizer_e2e_scaling.py` | complete-call size/concurrency scaling, paired by span with audit rows | `python -m bench.optimizer_e2e_scaling`; release extension, threaded SQLite contention, no API |
| `../../crates/agentc-optimizer/examples/exploration_preflight.rs` | bounded exploration persistence and accounting | `cargo run -p agentc-optimizer --example exploration_preflight --quiet`; structured Stage E0 output, SQLite only, no API |
| `../../crates/agentc-optimizer/examples/joint_planner_preflight.rs` | live exact-profile joint selection and planning overhead | `cargo run --release -p agentc-optimizer --example joint_planner_preflight --quiet`; structured Stage E0 output, no API |
| `../live_exploration_preflight.py` | production-adapter reference-visible exploration and restart admission | `python bench/live_exploration_preflight.py --output /tmp/live-exploration.json`; deterministic fake provider, no network |
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

### Bounded exploration preflight

The zero-network Rust preflight exercises the initial-calibration controller:

```bash
cargo run -p agentc-optimizer --example exploration_preflight --quiet
```

The committed [output](bounded-exploration-preflight-2026-09-03.json) records a
seeded two-plan scenario. Every decision returns the reference result while at
most one candidate holds a counterfactual lease. The first candidate exhausts
its exact-plan divergence-exposure and labeled task-damage budgets and is not
retried; the other candidate consumes the remaining four-call site budget. The
call cap, $0.04 counterfactual cost, divergence exposure, and separately labeled
task damage survive a database close and restart.

This is Stage E0 mechanism evidence only. It issues no provider calls and tests
the controller independently of provider dispatch, so it cannot support a
quality, cost-savings, or safety claim.

### Live profiled joint-planner preflight

The zero-network release preflight exercises the production planning function:

```bash
cargo run --release -p agentc-optimizer --example joint_planner_preflight --quiet
```

It warms a reference call site, enumerates the reference, routing-only,
rewrite-only, and joint candidates, persists exact reference and joint profiles,
reloads them after a SQLite restart, and repeatedly runs the same function used
by the PyO3 interceptor. The committed
[output](joint-planner-preflight-2026-09-04.json) contains three 20,000-call
replications. All 60,000 decisions selected the one evidenced
`OutputBudget + gpt-4o-mini` plan with the expected identity; no unrelated or
reference identity was returned. Median p50 was 127.25 us and the largest p99
was 303.833 us on the recorded arm64 development machine.

The timing includes JSON decode/encode, rewrite proposal and bounded
enumeration, model cross-product, canonical identity hashing, exact-profile and
guard lookup, and constrained selection. It excludes providers, task quality,
counterfactual calls, and concurrent SQLite writes. Accordingly it is Stage E0
mechanism evidence, not support for savings, quality, or main-track efficacy.

### Live reference-visible exploration preflight

The deterministic Python preflight exercises the real OpenAI adapter, native
joint planner, background counterfactual worker, durable lease controller, and
restart warmup without contacting a provider:

```bash
python bench/live_exploration_preflight.py \
  --output /tmp/live-exploration-preflight.json
```

It performs three reference warmups followed by 20 reference-visible
calibration calls. Each calibration call returns the original 1,024-token-cap
request response and runs one 121-token-cap candidate off-path. After shutdown
and restart, the 20 exact paired observations admit that candidate on the next
user-visible call. The committed
[output](live-exploration-preflight-2026-09-04.json) records 23 reference calls,
20 background candidates, one post-restart admitted candidate, and 44 total
fake-provider invocations.

This is Stage E0 mechanism evidence with `network_calls=0` and
`paper_evidence=false`. Its sub-millisecond local timings are not provider
latency results and must not appear as efficacy evidence.

### Complete optimizer-call overhead preflight

The historical `plan_audit.overhead_us` clock ends before the synchronous audit
write and before the native call returns to Python. The paired benchmark wraps
the complete `_native.optimize_plan` call and then aligns each sample with its
exact audit row:

```bash
maturin develop --release --manifest-path crates/agentc-profiler/Cargo.toml
python -m bench.optimizer_e2e_overhead \
  --build-profile release \
  --output /tmp/optimizer-e2e-overhead.json \
  --raw-output /tmp/optimizer-e2e-overhead.csv
```

The committed [summary](optimizer-e2e-overhead-2026-09-04.json) and
[30,000 paired samples](optimizer-e2e-overhead-2026-09-04.csv) contain five
2,000-call replications of three steady-state paths. Complete-call p50/p99 was
99.958/164.750 us for guarded reference selection, 115.000/307.209 us for an
admitted joint rewrite, and 74.959/219.750 us for the legacy greedy rewrite.
The corresponding internal pre-audit medians were 62, 71, and 42 us. The paired
residual includes the Python/Rust boundary, native-state lookup, audit
serialization and commit, clock quantization, and return conversion; it must
not be interpreted as an audit-only timer.

This is a release-mode, zero-network, single-machine Stage E0 diagnostic. It
validates full-call timing mechanics and shows sub-millisecond p99 on the
recorded host, but remains `paper_evidence=false` under the frozen protocol.

### Complete optimizer-call size/concurrency preflight

The scaling companion crosses exact 4/8/16/32/64 KiB serialized calls with
1/8/32 Python callers and guarded-reference/admitted-joint-rewrite paths:

```bash
maturin develop --release --manifest-path crates/agentc-profiler/Cargo.toml
python -m bench.optimizer_e2e_scaling \
  --build-profile release \
  --output /tmp/optimizer-e2e-scaling.json \
  --raw-output /tmp/optimizer-e2e-scaling.csv.gz
```

The committed [summary](optimizer-e2e-scaling-2026-09-04.json) and
[153,600 paired samples](optimizer-e2e-scaling-2026-09-04.csv.gz) contain five
randomized 1,024-call replications per cell. Replication-unique span IDs pair concurrent
outer timings to exact audit rows. Sequential p50 is 0.108--0.253 ms across
paths and sizes. At 32 callers, p50 is 1.744--2.853 ms, pooled p99 is
14.620--46.122 ms, and throughput improves only 1.35--1.98x over one caller.
The internal pre-audit p99 remains at or below 0.721 ms at 32 callers, while
the combined boundary/state/audit residual accounts for 94--97% of mean time.
Two of 76,800 admitted calls (0.0026%) safely fall back because planning exceeds
the configured 5 ms deadline.

This is a release-mode, zero-network, single-machine Stage E0 diagnostic with
fixed-structure synthetic inputs. It identifies synchronous audit persistence
as a scaling target but remains `paper_evidence=false`; it is not task-quality,
provider-latency, or savings evidence.

### Joint policy campaign harness

`bench.joint_campaign` is the prospective runner for the ten frozen policy
arms. It schedules each task/arm/repetition with protocol-derived seeds and arm
order, keeps a separate persistent Agentc store per arm and repetition,
validates every worker result before appending it, and seals five canonical
artifacts:

- `raw-records.jsonl`: intention-to-treat task outcomes and raw model calls;
- `campaign.json`: the exact frozen input contract;
- `run-context.json`: the Git revision and digest of any local source changes,
  locked before the first cell and required to match on resume;
- `analysis.json`: per-arm quality, cost, tokens, tail latency, abstention,
  exploration cost, task damage, interaction contrasts, negative regimes, and
  hierarchical paired intervals;
- `manifest.json`: source, task, protocol, schedule, state, and artifact
  digests plus frozen expected spend, actual billed-spend basis, the 125% stop
  threshold, stop reason, and completeness.

Expected spend is frozen both for the campaign and for each workload/model
cell. Before every provider-backed task, the worker receives that cell's limit
and spend-to-date. Crossing 125% seals a partial `manifest.json`, preserves the
intention-to-treat records already observed, and refuses further execution or
resume under the old budget.

Held-out Stage P/T configurations are rejected unless they contain a Stage-C
calibration lock. The runner never derives policy settings from held-out
outcomes, and it rejects any paper-evidence run from a dirty source tree. It
also refuses incomplete arm sets, duplicate task IDs, changed source, changed
protocol or task-list digests, unsafe resume ledgers, non-finite metrics,
network use in a network-forbidden cell, and home-directory paths in worker
records.

The committed `joint-campaign-e0.json` drives a two-family, two-repetition
no-network admission campaign over the exact frozen tau2 and SWE-agent source
revisions. Supply only machine-local paths through environment variables:

```bash
export AGENTC_TAU2_ROOT=/path/to/tau2-v1.0.1
export AGENTC_TAU2_PYTHON="$AGENTC_TAU2_ROOT/.venv/bin/python"
export AGENTC_SWEAGENT_ROOT=/path/to/swe-agent-v1.1.0
export AGENTC_SWEAGENT_PYTHON="$AGENTC_SWEAGENT_ROOT/.venv/bin/python"
export AGENTC_SWEBENCH_PARQUET=/path/to/test-00000-of-00001.parquet

python -m bench.joint_campaign bench/repro/joint-campaign-e0.json \
  --output /tmp/agentc-joint-campaign-e0
```

The E0 worker invokes real upstream LiteLLM call sites with deterministic mock
responses and a socket guard. It does not run or score complete tasks, measure
provider latency, or estimate instrumentation overhead. Its static/sequential
arms only validate orchestration. Therefore every resulting metric remains
`paper_evidence=false`; only a worker that passes the stricter Stage C/P/T
conformance fields can create efficacy evidence.
