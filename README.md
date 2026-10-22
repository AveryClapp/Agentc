# Agentc

**Evidence-guided selection of model-and-rewrite plans for LLM agent calls.**

Multi-step LLM agents send long sequences of provider requests. A request often carries more context, a more expensive model, or a larger output limit than its step needs. Agentc sits between the agent and the provider SDK, intercepts each request, and can replace it with a cheaper complete alternative: a target model plus the ordered request rewrites that will actually execute. It exposes an alternative only when it has evidence for that exact plan. Otherwise the original request goes through unchanged. The application keeps choosing its next step and running its own tools; Agentc changes no application code for supported adapters.

```
  Agent code            LangGraph · CrewAI · AutoGen · plain SDK calls
         │              chooses the next step, runs tools
         ▼
      Agentc            intercepts each provider request,
         │              sends an admitted plan or the original
         ▼
      LLM APIs          OpenAI · Anthropic · LiteLLM-routed providers
```

---

## Status

**Implemented.** The runtime builds candidate plans per call site by crossing compatible rewrite sets with versioned model targets. It admits a candidate using paired observations of that exact plan, response divergence against the original, evidence freshness, and an exposure budget. Cold candidates gather evidence through bounded, leased counterfactual calls while the caller still receives the original response. The profiler, semantic memoization layer, and `agentc` CLI (`record`, `traces`, `analyze`, `report`, `cache`, `optimize`) are implemented and tested.

**Engineering checks (no provider spend).** The [joint-campaign engineering check](bench/repro/README.md#joint-campaign-engineering-check-no-api-key) runs ten evaluation arms in isolated processes with zero network calls and produces hashed raw records, paired summaries, and a completion manifest. A native preflight exercises warm exact-plan selection. Both use synthetic costs and outcomes: they verify the harness and the selector's mechanics, not savings or task quality.

**Not yet established.** Whether complete-plan selection lowers all-calls spending at acceptable task quality, compared with strong controls on independent workloads. The frozen evaluation protocol that would answer this is in [bench/repro/mlsys-2027-evaluation-protocol.md](bench/repro/mlsys-2027-evaluation-protocol.md). No confirmatory result has been reported.

### Historical rule experiments

These experiments predate the current selector. They characterize individual rewrites on their earlier rule planner, often on fixtures built so a rule activates, with warmup excluded from measurement. They motivate measuring complete plans; they are not evidence of the selector's end-to-end benefit.

| Workload / configuration | Tasks | Efficiency observation | Accuracy change | Evidence boundary |
|---|---|---|---|---|
| Long-context QA / `ContextCompress` only | 300 | 36.3% fewer input tokens | +1.7 pp | Length-expanded fixture |
| Routing fixture / `ModelDowngrade` only | 127 | 11.23 mUSD less recorded cost; input tokens about unchanged | -3.9 pp | Historical routing guard |
| Iterative refinement / `StateDrop` only | 50 | 10.8% fewer input tokens | 0.0 pp | Explicit state annotations |
| Natural HotpotQA / `ContextCompress` only | 300 | 0.19% fewer input tokens; 1 activation | -2.0 pp | Sparse activation |
| Autogen bridge / `ContextCompress` only | 300 | 26.0% of captured input-token sum | -1.3 pp | Span completeness unverified |
| Autogen bridge / all rules on | 300 | 14.0% of captured input-token sum | -1.0 pp | Span completeness unverified |

mUSD is a thousandth of a dollar. Accuracy changes are observed pass-rate differences, not non-inferiority results. The paper's appendix maps each row to its CSV in [bench/paper_results/](bench/paper_results/), indexed by `DATA_MANIFEST.txt`.

---

## How a Request Is Handled

```
Provider request (intercepted via SDK patch or framework adapter)
       │
       ▼
  Call-site version   →  content-free identity: template shape, provider, tools, config
       │
       ▼
  Candidate plans     →  compatible rewrite sets × model targets, bounded depth
       │
       ▼
  Admission           →  exact-plan profile: paired evidence, divergence, freshness,
       │                 exposure budget, expected shadow and fallback cost
       ▼
  Dispatch            →  admitted plan, or the original request
       │
       ▼
  Observation         →  usage, latency, sampled reference comparison, guard state
```

Cold call sites pass through. After `AGENTC_OPTIMIZE_HOT_THRESHOLD` observations (default 3), the planner enumerates candidates. An under-observed candidate may run off-path while the original response stays user-visible (`AGENTC_OPTIMIZE_EXPLORATION`, default on; up to 20 real, potentially billed attempts per call-site version per 24 hours). A plan becomes eligible once it has 20 paired observations, its latest pair is under 24 hours old, and it passes the divergence and exposure checks. On about 2% of optimized calls (`AGENTC_OPTIMIZE_SHADOW`, default 0.02) the original request also runs, to detect drift. Both paths can incur provider cost; set `AGENTC_OPTIMIZE_EXPLORATION=0` and `AGENTC_OPTIMIZE_SHADOW=0` when no extra calls are acceptable. [CONFIG.md](CONFIG.md) lists every setting.

The runtime fails open by default. The optimizer's FFI boundary traps panics (`ffi::optimize_plan`), and every patch and adapter degrades to sending the call unmodified, so a bug in Agentc does not break the agent. `AGENTC_FAIL_OPEN=false` re-raises Agentc-internal errors for debugging.

### Rewrite vocabulary

Candidate plans combine a model target with these rewrites:

| Rule | Changes | What it does |
|---|---|---|
| `ContextCompress` | Input tokens | Drops low-relevance messages from large prompts (8 KB gate, IDF-weighted proxy) |
| `StateDrop` | Input tokens | Prunes state-tagged messages outside the current read window (needs `agentc.state_write()` annotations) |
| `PromptDedup` | Input tokens | Removes near-duplicate message segments |
| `StructuredTruncation` | Input tokens | Projects out unreferenced JSON tool-output fields |
| `ModelDowngrade` | Model price | Routes to a cheaper model target |
| `OutputBudget` | Output tokens | Caps the output limit at the call site's observed high quantile |
| `DeadOutputTruncation` | Output tokens | Caps output that feeds a branch nothing reads |
| `CacheHit` | Whole call | Replays a compatible cached response through the memoization cache |
| `ParallelBranch` | Structure | Detects independent sibling calls and emits an audited parallel certificate |

The historical experiments above cover `ContextCompress`, `ModelDowngrade`, and `StateDrop`. The other rules are implemented and tested, and the paper makes no efficacy claim for them. `ParallelBranch` does not run calls concurrently by itself; concurrency comes from the user-side `parallel_map`.

---

## Components

### Profiler
Instruments a Python agent and records every LLM call (tokens, latency, model, cost, prompt and response, embedding) as structured traces in SQLite. Written in Rust behind PyO3, with `wrapt`-based patches for the OpenAI, Anthropic, and LiteLLM SDKs. Spec: [specs/profiler.md](specs/profiler.md).

### Semantic memoization
Opt-in caching of LLM responses. Exact-prompt hash lookup on the hot path, with LSH over 256-dim model2vec embeddings as a secondary tier for similar prompts. Spec: [specs/memoization.md](specs/memoization.md).

### Optimizer
Builds, admits, and dispatches complete plans as described above, and records every decision in an audit database. Spec: [specs/optimizer.md](specs/optimizer.md).

Storage is three SQLite databases under `~/.agentc` (`AGENTC_STORAGE_PATH`): `traces.db` (spans and cache entries), `cost_model.db` (per-call-site and per-plan evidence), and `optimizer_audit.db` (every plan decision with the input that produced it).

```
crates/                      Rust workspace
├── agentc-core              span schema, SQLite storage, embedding I/O
├── agentc-embed             model2vec embeddings + LSH
├── agentc-memo              memoization cache: canonical keys, eviction, FFI
├── agentc-profiler          PyO3 module: Python bindings
├── agentc-analyzer          cost breakdown + waste detectors over traces
├── agentc-optimizer         plan identities, candidate construction, admission,
│                            exploration, rewrite rules, provider accounting
└── agentc-cli               `agentc` binary

python/agentc/               Python SDK: SDK patches, framework adapters,
                             request canonicalization, interception, billing

bench/                       Evaluation harness
├── agents/                  reference agents for the historical rule experiments
├── repro/                   evaluation protocol, campaign specs, preflight records
├── paper_results/           committed result CSVs + DATA_MANIFEST.txt
├── paper_figures/           figure generators
└── *_campaign_*, *_worker   joint-campaign harness: coding, tau, guard, calibration

specs/                       Technical specifications
paper-intelligence/          Literature, venue, and evidence notes for the paper
tests/                       Python tests
```

---

## Quick Start

Build the Rust workspace and install the Python SDK:

```bash
# agentc-profiler is a PyO3 extension module; it links through maturin, not cargo.
cargo build --release --workspace --exclude agentc-profiler
maturin develop --release            # builds the PyO3 extension into the active venv
pip install -e ".[test]"             # full test/bench deps; ".[dev,openai]" for a minimal SDK env
```

Run the Python SDK and benchmark checks, matching CI:

```bash
pytest tests/ bench/ -v --tb=short
```

Profile an agent script:

```bash
agentc record -- python my_agent.py
agentc traces                         # list recent runs
agentc analyze <trace_id>             # cost breakdown + waste detection
agentc report --last 20               # aggregate across runs
```

Inspect the optimizer:

```bash
agentc optimize report                # rule firing rates, savings, accuracy
agentc optimize inspect <call_site>   # cost model + ablation status per call site
agentc optimize disable --rule ModelDowngrade --call-site 'app.*' --hours 24
agentc optimize bench --agent path/to/agent.py
```

Rerun the historical rule experiments (warmup-corrected drivers; `run_paper_ablation.sh` is an older cold-start harness and does not reproduce them):

```bash
python -m bench.build_hotpot_fixture       # fixtures from public datasets
python -m bench.build_gaia_fixture         # HF_TOKEN required
python -m bench.build_long_context_fixture

python bench/run_lcqa_warmup_n300.py       # ContextCompress, n=300
python bench/run_gaia_warmup.py            # ModelDowngrade, n=127
python bench/run_refiner_warmup.py         # StateDrop, n=50
```

The reference agents stub LLM calls when no API key is set, so the harness runs without spending money. For real cost and accuracy numbers, copy [`.env.example`](.env.example) to `.env` and fill in the keys you need; the file lists what each key unlocks.

The joint-campaign harness, the frozen SWE-bench and tau cohorts, guard studies, and the paper build are documented step by step in [bench/repro/README.md](bench/repro/README.md).

---

## Scope

Agentc works at the provider-request boundary. It needs no control over model weights, KV cache, batching, or accelerator scheduling, which is what lets it sit in front of hosted APIs. The same boundary means it cannot use those internal signals to prove two responses equivalent. Its admission screen uses response divergence from the original request as an operational proxy; task quality still has to be validated separately on benchmarks. It does not search alternative tool calls or control flow: a complete plan covers one call, not an agent's whole trajectory.

---

## Documentation

- [specs/optimizer.md](specs/optimizer.md): plan identities, admission, exploration, rule definitions
- [specs/profiler.md](specs/profiler.md): instrumentation, span schema, waste detectors
- [specs/memoization.md](specs/memoization.md): canonical keys, LSH, cache lifecycle
- [CONFIG.md](CONFIG.md): every environment variable the runtime and harness read
- [bench/repro/README.md](bench/repro/README.md): reproduction and campaign instructions
- [specs/future-work.md](specs/future-work.md): items intentionally out of scope

---

## Related Work

Several systems already optimize model allocations, prompts, and workflow configurations together, including LLMSelector, Cognify, and Murakkab. AgentOpt intercepts client-side calls to track, cache, and route them without changing agent code. Serving-layer systems such as Parrot, SGLang, Agentix (Autellix), and Halo schedule and share work inside the inference stack. Model routing (FrugalGPT, RouteLLM), prompt compression (LLMLingua, LLMLingua-2), semantic caching (GPTCache, vCache), and parallel tool execution (LLMCompiler) each have dedicated systems.

Agentc studies a narrower unit: a complete alternative for one intercepted provider call, admitted only on evidence for that alternative, with bounded learning cost and the original request as the fallback.

---

## Name

**Agentc** is short for "agent compiler", the project's original framing.
