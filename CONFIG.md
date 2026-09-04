# Configuration

Every environment variable the Agentc runtime and benchmark harness read, in one
place. This is the source of truth; the README and specs link here rather than
re-declaring knobs.

Agentc has three config layers, by design:

1. **Profiler config** (`python/agentc/_config.py`) — the only typed surface.
   Precedence is **kwargs > environment > `~/.agentc/config.toml` > defaults**.
   Unknown TOML keys warn. Covers the four `capture_*` / `fail_open` /
   `storage_path` knobs.
2. **Optimizer config** (`crates/agentc-optimizer/src/config.rs`) — a parallel
   Rust struct with its own `[optimizer]` TOML section and env overrides.
3. **Ad-hoc reads** — memoization, guard, provider, and every `bench/` knob are
   read inline at their point of use.

Unifying these into one registry is future work; this document is the single
place they are all *described*.

## Runtime knobs (`AGENTC_*`)

These change shipped runtime behavior. Set them in the environment or (for the
profiler four) in `~/.agentc/config.toml`.

| Variable | Default | Purpose |
|---|---|---|
| `AGENTC_STORAGE_PATH` | `~/.agentc` | Data dir for `traces.db`, `cost_model.db`, `optimizer_audit.db`. |
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
| `AGENTC_OPTIMIZE_MAX_OVERHEAD_MS` | `5` (release) / `50` (debug) | Plan-overhead kill-switch budget. |
| `AGENTC_OPTIMIZE_EXPLORATION` | `true` | Initial reference-visible calibration. Each leased candidate is a second, **real and potentially billed** provider call; `0/false/no/off` disables it. The persisted default cap is 20 attempts per call-site version per 24h, with one active lease per site and four workers process-wide. |
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
