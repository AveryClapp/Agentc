---
title: Strategy and Venues
status: active
last-updated: 2026-09-04
owner: paper-intelligence
---

# Strategy and Venues

This is the paper strategy document: venue ladder, framing options, outline choices, title/figure ideas, and writer-facing section guidance.

Supersedes:

- `venue-positioning-matrix.md`
- `paper-angle-matrix.md`
- `title-and-abstract-idea-bank.md`
- `figure-idea-bank.md`
- `manual-writing-brief.md`
- `decision-log.md`
- `outline-options.md`
- `positioning-taxonomy.md`
- `section-briefs/*`

## Current Strategic Read

The default paper angle should be a narrowed `ANG-001`: **a guarded, application-side rewrite control plane over opaque LLM APIs**. Use `ANG-003` as the practical motivation and `ANG-004` as the evaluation-strengthening thread. Routing is one action in the joint policy, not the novelty by itself. Do not make `ANG-005` the main claim until every rewrite is clearly labeled as validated, promising, or future work.

Short version:

- Best current story: AgentC profiles call sites online and jointly chooses model targets and semantic rewrites under explicit abstention and damage limits.
- Best current evidence: `ContextCompress` and `ModelDowngrade`.
- Most useful caveat: `StateDrop` is promising but not yet a sound compiler-style pass.
- Biggest experiment gap: no held-out joint routing-plus-rewrite result against route-only, rewrite-only, ordering, and static controls.
- Biggest engineering blocker: synchronous audit persistence creates 14.6–46.1ms C=32 p99 complete-call latency and weak throughput scaling in the current Stage E0 diagnostic.

## Venue Ladder

| ID | Venue/family | Recommendation | Best framing | Current readiness | Must improve |
|---|---|---|---|---|---|
| `VEN-001` | MLSys | strongest long-run topical fit | guarded joint routing-and-rewrite control plane | promising but not comfortable yet | positive held-out joint-policy result, non-blocking audit path, post-fix scaling, baselines |
| `VEN-002` | Systems venues broadly | mixed | systems/runtime infrastructure | ATC/EuroSys plausible; OSDI/SOSP/NSDI weak right now | deeper systems evidence and operational realism |
| `VEN-003` | NLP/LLM venues | possible | LM cost-quality optimizer and evaluation methodology | COLM strongest | LM-facing breadth, stochastic quality, baseline comparisons |
| `VEN-004` | Broad AI/ML venues | weak-main, possible workshop | agent optimization method | not a main default | stronger algorithmic novelty and broad benchmarks |
| `VEN-005` | Agent/compound-AI workshops | strong workshop fit | agent systems/infrastructure | good feedback route | updated CFP watchlist |
| `VEN-006` | Efficient LLM / routing / serving workshops | strong workshop fit | cost-saving runtime layer | good if scoped | distinguish from provider serving and routing-only papers |
| `VEN-007` | Software engineering / AI tooling | possible | agentic engineering tool/runtime | not primary | developer/task breadth |
| `VEN-008` | HCI/tooling | not recommended now | developer-facing interface | weak | user study or interactive tooling contribution |
| `VEN-009` | ATC 2026 operational systems track | most actionable near-term main venue | practical runtime/optimizer with operational lessons | possible but rushed | overheads, failure modes, deployment/trace story, extended abstract |
| `VEN-010` | COLM | best LM-native fit | LM runtime cost-quality optimizer | promising next cycle | broader LM-facing evaluation and cost-quality frontier |
| `VEN-011` | ICML 2026 Agents in the Wild workshop | strong workshop fit, deadline passed | agent systems/infrastructure | missed for 2026 | use as style/feedback reference |

## Post-June 2026 Plan

| Window | Venue/lane | Current fit | Why it matters | Evidence needed |
|---|---|---|---|---|
| Late July/August 2026 | AAAI 2027 watch | possible | Broad AI target if framed as generally useful agent infrastructure. | stronger behavior-preservation story and broader evaluation |
| August 2026 | CIDR 2027 | possible for short/vision/demo | Good if reframed as a query-optimizer/control-plane architecture. | concise architecture claim and system insight |
| September 2026 | EuroSys 2027 | possible but hard | Best fall systems lane if evidence matures. | overhead, scaling, multi-framework evidence, rule interactions |
| September 2026 | NSDI 2027 | high risk | Only plausible with networked/distributed/frontiers angle. | realistic orchestration and distributed-systems relevance |
| Late September 2026 | ICLR 2027 watch | possible | Needs reusable optimizer/runtime methodology, not just engineering. | conceptual model, broad benchmarks, surprising findings |
| Late October 2026 | MLSys 2027 watch | strongest | Best match for agentic AI systems, ML runtimes, efficient inference. | polished artifact, end-to-end evidence, overhead, baselines |
| Rolling/monthly | ARR/VLDB/SIGMOD | conditional | Fallback or demo lanes. | NLP framing for ARR; data-management framing for DB venues |

## Paper Angles

| ID | Angle | Central claim | Current status | Best audience | Red-flag objection |
|---|---|---|---|---|---|
| `ANG-001` | Guarded optimizer for compound AI systems | AgentC profiles opaque application call sites online and jointly selects model targets and semantic rewrites under one persistent risk contract. | promising default | MLSys, ATC, agent systems | Is the joint policy measurably better than routing and rewriting separately? |
| `ANG-002` | JIT-style optimizer for LLM workloads | Hot call sites can be profiled and rewritten under safety checks. | promising but risky | MLSys/PL-ish systems | Is JIT language too strong? |
| `ANG-003` | Practical cost optimizer for AI agents | AgentC saves cost on suitable agent call sites with low application-code burden. | strong practical story | ATC/workshops | Why not just routing/compression/caching glued together? |
| `ANG-004` | Evaluation methodology for LLM-runtime optimizers | Optimizer evaluation must report cost, quality, activation boundaries, and stochastic uncertainty. | promising support thread | COLM/MLSys workshops | Do current results prove behavior preservation? |
| `ANG-005` | Rule library for agent-call optimization | A small set of rewrites covers common inefficiencies. | needs evidence | agent frameworks/tooling | CacheHit/ParallelBranch are not validated yet. |

## Recommended Outline

Use a systems-first outline for MLSys/ATC/EuroSys-style submissions:

1. Introduction and motivation.
2. AgentC system overview.
3. Runtime profiling and planner.
4. Rewrite rules and safety checks.
5. Evaluation methodology.
6. Results.
7. Related work.
8. Limitations and future work.

Backup outlines:

- Results-first for workshops or applied venues.
- Compiler/JIT analogy only if the paper stays careful about "inspired by" rather than "sound compiler optimization."
- Evaluation-methodology paper only if close related systems make the system novelty too narrow.

## Writer Brief

### Strongest current claims

- AgentC is a transparent runtime layer over LLM calls.
- The best current frame is runtime optimization for compound AI systems.
- `ContextCompress` and `ModelDowngrade` have the strongest current headline savings.
- `StateDrop` has real but caveated input-token savings.
- Real HotpotQA boundary behavior supports the activation-gate story.

### Do not say yet

- Do not claim all five rewrite rules are equally benchmarked.
- Do not claim partial matrices are complete.
- Do not claim oracle-level compression is achieved by the automated rule.
- Do not claim "first runtime optimizer" without narrowing against Agentix/Autellix, Halo, Murakkab, AIOS, Cognify, DSPy, LMQL, SGLang, LLMCompiler, LLM-Tool Compiler, vCache, and rule-specific baselines.
- Do not claim behavior-preserving semantics without defining metric, tolerance, and uncertainty treatment.
- Do not treat `StateDrop` as sound program slicing unless AgentC defines its dependency/read-window model.

## Section Briefs

### Contribution framing

Lead with AgentC as a guarded runtime optimizer for compound AI systems. The contribution is not routing, compression, caching, or parallelism alone; it is application-side, call-conditional joint selection across model targets and semantic rewrites, with interaction handling, abstention, and persistent damage control despite opaque provider internals.

Evidence hooks: `CLM-001`, `CLM-007`, `RES-001`, `RES-002`, `GAP-010`, `LIT-024`, `LIT-025`, `LIT-040`, `LIT-043`, `LIT-044`.

### System and rules

Explain AgentC as:

- Python interception captures LLM calls and routes planning through the Rust optimizer.
- The planner waits for hot call sites before proposing rewrites.
- Rules have explicit preconditions and projected savings.
- If no proposal passes checks, the call is passed through unchanged.

Rules to describe:

- `ContextCompress`: removes low-salience context from large prompts.
- `ModelDowngrade`: routes simple structured call sites to cheaper models.
- `StateDrop`: removes stale state-tagged context when current reads do not require it.
- `CacheHit` and `ParallelBranch`: future/conditional unless new evidence lands.

### Methodology

Use rule-specific workloads to isolate behavior, then make the held-out joint-policy campaign the central test: fixed model, route only, rewrite only, both sequential orders, current greedy, learned/joint AgentC, and best static policy. Report complete-call overhead separately; the current Stage E0 scaling diagnostic is a negative result until audit persistence is moved off the request path and rerun.

### Results

Use `RES-001` and `RES-002` as the strongest current rule-level results. Use `RES-004` as promising but partial, `RES-005` as activation-boundary evidence, and `RES-006` only as diagnostic headroom until trace-query support exists. Treat the `RES-013` size/concurrency artifact as an honest negative systems diagnostic, not a latency win.

### Related work

Organize around:

- compound AI systems and agent frameworks;
- runtime/workflow optimizers;
- rewrite families;
- compiler/state analogies;
- serving systems as orthogonal;
- stochastic evaluation methodology.

Closest systems threats: Agentix/Autellix, Halo, Murakkab, AIOS, Cognify, DSPy, LMQL, SGLang, LLMCompiler, LLM-Tool Compiler, vCache.

### Limitations

Keep limitations honest but contained:

- workloads are targeted;
- some matrices are partial;
- quality preservation needs stronger paired/repeated analysis;
- StateDrop needs a dependency model;
- provider pricing/cache behavior affects billed-cost interpretation;
- CacheHit/ParallelBranch are not current headline results.
- the synchronous audit writer does not yet meet the complete-call scaling target.

## Title Ideas

| ID | Title idea | Status | Risk |
|---|---|---|---|
| `TTL-001` | AgentC: A Runtime Optimizer for Compound AI Systems | promising | "compound AI" needs literature support. |
| `TTL-002` | Profiling and Rewriting LLM Agent Calls for Lower Cost | promising | May sound too narrow. |
| `TTL-003` | Toward JIT Optimization for LLM Agent Workloads | needs care | JIT analogy may overclaim. |
| `TTL-004` | Transparent Cost Optimization for Multi-Step LLM Applications | promising | "transparent" needs definition. |
| `TTL-005` | Runtime Rewrite Rules for Cost-Aware LLM Agents | candidate | May overemphasize rule count. |

## Figure And Table Ideas

| ID | Status | Idea | Purpose | Risk |
|---|---|---|---|---|
| `FIG-001` | promising | System architecture | Show AgentC between frameworks and LLM APIs. | May be too high-level. |
| `FIG-002` | promising | Two-gate rule pipeline | Explain hot-call/propose/safety/pass-through behavior. | Needs concise design. |
| `FIG-003` | promising | Headline savings bar chart | Compare ContextCompress, ModelDowngrade, StateDrop. | StateDrop caveat must be visible. |
| `FIG-004` | promising | StateDrop noise vs signal | Explain cost vs input-token divergence. | Needs exact source data. |

## Decision Log

| ID | Date | Decision | Consequence |
|---|---|---|---|
| `DEC-001` | 2026-05-08 | Use `paper-intelligence`, not `paper-writeup`. | Directory supports evidence organization, not final prose drafting. |
| `DEC-002` | 2026-05-08 | Keep manuscript scaffolding out of scope for now. | No `paper/agentc/` directory. |
| `DEC-003` | 2026-05-08 | Move root paper artifacts into `references/source/`. | Artifacts have checksums/inventory entries. |
| `DEC-004` | 2026-05-08 | Use stable IDs across ledgers. | Claims link to evidence and gaps. |
| `DEC-005` | 2026-05-08 | Adapt Pizza's process structure, not its domain content. | AgentC-specific claims and evidence. |
| `DEC-006` | 2026-05-09 | Move Paper Intelligence out of `specs/` into top-level `paper-intelligence/`. | `specs/` remains reserved for technical specs. |

## Next Strategy Decisions

1. Treat MLSys as the primary long-run lane only if the audit-path fix and held-out joint-policy result both land.
2. Decide whether `CacheHit` and `ParallelBranch` stay future work.
3. Decide whether StateDrop gets stronger evidence or stays a caveated supporting result.
4. Keep the system contribution foregrounded: joint online choice under an explicit risk contract, supported by practical cost savings and rigorous evaluation.
