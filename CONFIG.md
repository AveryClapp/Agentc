# Configuration

Every environment variable the Agentc runtime and benchmark harness read, in one
place. This is the source of truth; the README and specs link here rather than
re-declaring knobs.

Agentc uses one shared bootstrap file: `config.toml` in the selected data
directory (`~/.agentc/config.toml` by default). `agentc.init(storage_path=...)`
wins over `AGENTC_STORAGE_PATH` when choosing that file. Otherwise the
environment path wins over the default. A `storage_path` value inside the file
may relocate runtime databases, but the optimizer still reads the exact
bootstrap file that selected the relocation; config lookup is not recursive.

The Python profiler owns the root `capture_*`, `fail_open`, and `storage_path`
keys. The Rust optimizer owns the strict `[optimizer]` subtree. Field
precedence for both is **explicit API argument (where available) > environment
> TOML > default**. Unknown root keys warn and are ignored. Unknown keys or
invalid values inside `[optimizer]` disable optimization and exploration for
the process; profiling and the original LLM call continue.

Memoization, divergence metrics, provider hints, and `bench/` controls are
still read at their point of use. This document is the single place all knobs
are described.

## Shared `config.toml`

All keys are optional:

```toml
capture_content = true
capture_embeddings = true
fail_open = true
storage_path = "~/.agentc"

[optimizer]
enabled = true
hot_threshold = 3
cost_model_window = 50
plan_profile_window = 50
divergence_window = 50
max_overhead_ms = 5
shadow_rate = 0.02
compose = true

[optimizer.selection]
objective = "cost"                     # "cost" or "latency"
min_plan_evidence = 20
plan_profile_freshness_hours = 24
max_rewrite_depth = 3
divergence_exposure_budget = 1.0
# global_divergence_threshold = 0.03    # optional fraction in [0, 1]

[optimizer.exploration]
enabled = true
calls_per_site_24h = 20
max_concurrent_counterfactuals = 1

[optimizer.evaluation]
task_damage_budget = 5.0
# non_inferiority_margin = -0.03       # workload-specific, in [-1, 0]
```

Rule-specific TOML tables are not implemented. Use `AGENTC_ENABLED_RULES` for
the current ablation whitelist; typed per-rule configuration remains tracked
separately.

## Runtime knobs (`AGENTC_*`)

These change shipped runtime behavior. The profiler and planner fields shown in
the schema above can also be set in the shared TOML file.

| Variable | Default | Purpose |
|---|---|---|
| `AGENTC_STORAGE_PATH` | `~/.agentc` | Data dir for `traces.db`, `cost_model.db`, `optimizer_audit.db`. |
| `AGENTC_CONFIG_PATH` | `<selected-storage>/config.toml` | Advanced/native override for the exact shared bootstrap file. `agentc.init()` manages this automatically. |
| `AGENTC_CAPTURE_CONTENT` | `true` | Capture prompt/response text in spans. |
| `AGENTC_CAPTURE_EMBEDDINGS` | follows `capture_content` | Capture embeddings in spans. |
| `AGENTC_FAIL_OPEN` | `true` | Fail-open boundary. `false` re-raises Agentc-internal errors (debugging opt-in). |
| `AGENTC_MEMOIZE` | `true` | `0/false/no/off` turns every `@memoize` into a pass-through. |
| `AGENTC_MEMOIZE_SIMILARITY` | `0.92` | LSH similarity threshold for a cache hit. |
| `AGENTC_MEMOIZE_TTL` | `3600` | Cache TTL, seconds. |
| `AGENTC_OPTIMIZE` | `true` | Master optimizer switch. `0/false` → always `PassThrough`. |
| `AGENTC_OPTIMIZE_HOT_THRESHOLD` | `3` | Observations before a call site is "hot" enough to optimize. |
| `AGENTC_OPTIMIZE_COST_MODEL_WINDOW` | `50` | Rolling per-call-site cost-model sample window. |
| `AGENTC_OPTIMIZE_DIVERGENCE_WINDOW` | `50` | Exact newest shadow samples retained per call-site/rule divergence estimate. |
| `AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW` | `50` | Independent exact execution-outcome and paired-divergence windows per complete plan. |
| `AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE` | `20` | Paired complete-plan observations required before user-visible admission. |
| `AGENTC_OPTIMIZE_OBJECTIVE` | `cost` | Complete-plan objective: `cost` or `latency`. |
| `AGENTC_OPTIMIZE_PLAN_PROFILE_FRESHNESS_HOURS` | `24` | Maximum age of the newest paired observation. |
| `AGENTC_OPTIMIZE_MAX_REWRITE_DEPTH` | `3` | Maximum semantic rewrites composed into one candidate (valid range 1–3). |
| `AGENTC_OPTIMIZE_MAX_OVERHEAD_MS` | `5` | Plan-overhead kill-switch budget in every build profile. |
| `AGENTC_OPTIMIZE_EXPLORATION` | `true` | Initial reference-visible calibration. Each leased candidate is a second, **real and potentially billed** provider call; `0/false/no/off` disables it. The persisted default cap is 20 attempts per call-site version per 24h, with one active lease per site and four workers process-wide. |
| `AGENTC_OPTIMIZE_EXPLORATION_CALLS_PER_SITE_24H` | `20` | Positive rolling counterfactual-call cap per call-site version. |
| `AGENTC_OPTIMIZE_MAX_CONCURRENT_COUNTERFACTUALS` | `1` | Positive live counterfactual cap per call-site version. |
| `AGENTC_OPTIMIZE_DIVERGENCE_EXPOSURE_BUDGET` | `1.0` | Finite non-negative cumulative complete-plan exposure ceiling. |
| `AGENTC_OPTIMIZE_TASK_DAMAGE_BUDGET` | `5.0` | Evaluation-only labeled task-damage ceiling; it does not affect unlabeled production admission. |
| `AGENTC_OPTIMIZE_NON_INFERIORITY_MARGIN` | unset | Evaluation workload quality margin in `[-1, 0]`; no universal production default. |
| `AGENTC_OPTIMIZE_SHADOW` | `0.02` | Bernoulli shadow-sampling rate. A sampled call issues a second, **real and billed** un-rewritten call inline to measure divergence. |
| `AGENTC_COMPOSE` | `true` | V2 composition (`1`) vs V1 first-match (`0`). |
| `AGENTC_SHADOW_DIVERGENCE_MODE` | `lexical` | Guard divergence metric: `lexical`, `normalized`, or `embedding`. `embedding` falls back to `normalized` if the embedder is unavailable. |
| `AGENTC_SHADOW_DIVERGENCE_BUDGET` | per-rule budget, else `0.05` | Overrides the divergence threshold above which a rule is disabled for a call site. |
| `AGENTC_PROVIDER` | `openai` | Canonicalization provider hint. |
| `AGENTC_ENABLED_RULES` | unset = all | Comma-separated rule whitelist (ablation use). |
| `AGENTC_BIN` | auto-discovered | Path to a prebuilt `agentc` binary for the micro-benchmark. |

`AGENTC_SHADOW_DIVERGENCE_MODE`, `AGENTC_SHADOW_DIVERGENCE_BUDGET`, and
`AGENTC_OPTIMIZE_DIVERGENCE_WINDOW` define the accuracy-guard measurement
surface.

## Secrets and provider selection

API keys live in `.env.example` (copy to `.env`), not here:
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `TOGETHER_API_KEY`, `GROQ_API_KEY`,
`HF_TOKEN` (or `HUGGINGFACE_API_KEY`). `BENCH_OPENAI_BASE_URL` /
`OPENAI_MAX_RETRIES` / `ANTHROPIC_MAX_RETRIES` tune the clients.

## Benchmark & reproduction knobs

Experiment-driver parameters, not shipped config. Read inline by `bench/`.

| Family | Variables | Purpose |
|---|---|---|
| Common | `BENCH_BASELINE_MODEL`, `BENCH_CHEAP_MODEL`, `BENCH_MAX_TASKS`, `BENCH_TASK_OFFSET`, `BENCH_FIXTURE_OVERRIDE`, `BENCH_STUB_MODE`, `BENCH_CONCURRENCY`, `BENCH_LLM_RETRIES` | Model ids, task window/count, fixture override, stubbed (no-spend) mode, concurrency, retries. |
| Guard sweep | `GSWEEP_N`, `GSWEEP_THRESHOLDS` | Task count and divergence thresholds for `run_guard_sweep.py`. |
| Guard eval | `GE_AGENT` (required), `GE_FIXTURE`, `GE_W`, `GE_N`, `GE_TAG`, `GE_CONFIGS` | `run_guard_eval.py` agent/fixture/warmup/measured/tag/configs. |
| Autogen | `AB_W`, `AB_N`, `AB_CONFIGS` | Autogen warmup/measured/configs. |
| Research-planner | `RP_W`, `RP_N`, `RP_CONFIGS` | Research-planner warmup/measured/configs. |
| Analyst-QA | `AQ_W`, `AQ_N`, `AQ_CONFIGS` | Analyst-QA warmup/measured/configs. |
| Cascade baseline | `CASCADE_THRESHOLD` | Confidence threshold for the frugal-cascade escalation baseline. |

`GE_W=0` in some sweep drivers means **no warmup** — those cells are behavioral
(rule-retention) only, not savings-comparable. See the reproduction docs.
