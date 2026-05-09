# Agentc Feedback

The compiler analogy is compelling and the problem (multi-agent token waste) is real. What Agentc proposes is essentially formalized harness engineering — the same optimizations people already do by hand (context compression, model routing, parallel dispatch), but systematized as rewrite rules over a DAG instead of ad-hoc decisions. The difference between "harness engineering" and "Agentc" is the difference between hand-optimizing assembly and having a compiler. The following covers what is strong, where the hard problems are, and ideas worth exploring.

---

## What's Strong

**The problem statement is real.** Multi-agent systems are expensive and nobody is systematically optimizing token spend across an execution plan. The "compiler-shaped hole" framing is clean — there's clearly room for an optimization layer that doesn't exist yet.

**The "layer beneath agents" thesis is the key insight.** The idea that agents are already too abstracted, and that a faster execution engine underneath them makes agents on top inherently cheaper — that is the right framing. The goal is not to replace Claude Code, but to build the runtime it should be sitting on.

**The optimizer rules are intuitive and practical.** ContextCompress, ModelDowngrade, ParallelBranch — people already do these by hand in every agent harness. The contribution isn't inventing new optimizations; it's formalizing them as named, composable rewrite rules over a graph so they can be studied, measured, and applied systematically. That formalization is what makes this publishable rather than "just another harness."

**Rust + Python split makes sense.** Right tool for each job. Rust for the runtime — concurrent execution, scheduling, caching layers. Python for the LLM API ecosystem and benchmarking.

---

## Hard Problems to Think Through

### 1. The DAG Planning Paradox

For a task like "fix bug X in repo Y," you can't build the execution DAG without first understanding the bug — and understanding the bug requires the LLM calls that the DAG is supposed to optimize.

Real workflow:
1. Read error message → figure out which files to look at (requires LLM reasoning)
2. Read those files → realize the bug is in a different module (surprise — DAG changes)
3. Understand root cause → now you can plan the fix
4. Implement fix → tests fail for unrelated reason → need to debug that too

You can't pre-plan steps 2-4 at step 1. The DAG is discovered during execution, not known upfront. A traditional compiler sees the complete source code before optimizing. Agentc would be compiling a program that rewrites itself as it runs.

**The question:** When does the optimizer actually run? If it runs on a speculative DAG that keeps getting invalidated, does the optimization overhead eat the savings?

### 2. Reactive Execution vs. Static Plans

Real agentic work is fundamentally reactive:
- Tests fail unexpectedly
- Architecture turns out different than assumed
- Tool calls fail, need completely different approach
- LLM hallucinates an API that doesn't exist, needs to backtrack

A static DAG optimized before execution can't handle this without re-planning, which costs tokens. At some point the optimizer replanning constantly is just... an agent.

### 3. The Observability Problem

To build the cost model, you need detailed data from real executions. But there's a gap between what you need and what's available:

| Data Needed | Available? |
|-------------|-----------|
| Full prompt for every LLM call | Only if you control the harness |
| Token-level attribution (what the model actually used) | No — not exposed by any provider |
| Alternative execution paths (what could have happened) | No — only the path taken |
| Context compaction decisions (what was kept vs. dropped) | Only if you control the harness |

This actually strengthens the case for building your own harness from scratch — you get full observability. But it means the cost model has to be trained on your own execution data, not on traces from Claude Code or Cursor.

### 4. The Abstraction Level Question

How concrete are the DAG nodes? Concrete nodes ("call GPT-4 with this 2000-token prompt") give precise cost estimates but break when anything changes. Abstract nodes ("do code analysis") are stable but the cost model can't predict much.

Traditional compilers work because `ADD` and `LOAD` have precise semantics. LLM calls have fuzzy, context-dependent semantics. "Summarize this code" might need 100 or 10,000 tokens depending on the code.

### 5. Working Beneath vs. Against RL-Tuned Models

The thesis — build the execution engine beneath agents — is the right framing. But it's worth noting: Anthropic and OpenAI are RL-training models to manage context, select tools, and route themselves efficiently. The model has learned some of the optimizations that Agentc wants to do from outside.

This isn't necessarily a problem. If Agentc operates at the inference/runtime level (KV cache optimization, context window management, parallel dispatch), it's genuinely complementary to the model's learned behavior. But if it operates at the decision level (which model to use, what context to include), it might fight the model's own optimization.

**Key question:** Which of the optimizer rules operate at the runtime level (complementary to RL) vs. the decision level (competing with RL)?

---

## Where I Think the Strongest Version Lives

Rather than trying to compile an entire open-ended task upfront, the strongest approach might be **runtime optimization on a live execution**:

- **Context window optimization**: When any LLM call is about to fire with 100K tokens, decide what to compress/drop based on what's actually needed
- **KV cache-aware scheduling**: If two calls share a prefix, schedule them to reuse the cache
- **Model routing at the call level**: For each individual call, pick the cheapest model that meets the quality bar
- **Parallel dispatch**: When independent calls are queued, fire them concurrently

This is the "layer beneath agents" thesis executed literally — a runtime that any agent framework sits on top of, optimizing every call without needing to understand the overall task.

---

## Questions Worth Pushing On

1. **What's the evaluation plan?** SWE-bench/GAIA are good targets, but how do you measure "Agentc saved X tokens while maintaining Y accuracy"? What's the baseline — same agent without the runtime? Different harness entirely?

2. **Can you show it on a fixed-structure pipeline first?** Before tackling open-ended tasks, prove the optimizer on a pipeline where the DAG *is* known upfront (e.g., "analyze 50 files for security vulns" — embarrassingly parallel, clear structure). That's where the compiler analogy fully holds.

3. **What about the cost of the optimizer itself?** If building + optimizing the DAG requires LLM calls, those tokens count against savings. What's the break-even point?

4. **Building on pi-mono vs. from scratch?** Using an existing open-source harness like [pi](https://github.com/badlogic/pi-mono) as a base gives you a working agent to optimize immediately. Starting from scratch gives full control over the runtime. Tradeoff worth discussing.

---

## Ideas Worth Exploring

### 1. Speculative Execution & Trajectory Prediction
Branch prediction for LLM calls. Profile real agent traces to predict the next tool call, then speculatively fire it on a cheap/fast model before the agent explicitly asks. If prediction hits, you skip a round-trip. If it misses, you eat the small cost. Agent tool-call sequences are way more predictable than general text — most follow a handful of patterns (search → read → edit, think → act → observe).
**Paper:** Measure hit rates and latency savings on real workloads; compare to CPU branch prediction literature.
**Feasibility:** Very doable — log traces, build a Markov/n-gram predictor in Rust, wire into proxy.

### 2. Context Mesh with Semantic Memoization
Content-addressed knowledge sharing across agents and across time. Hash inference inputs using LSH over embeddings so semantically equivalent prompts (not just exact matches) return cached results. Add negative caching — if agent A explored a dead-end, agent B skips it. Basically a CDN for inference: dedup at the semantic level. The cache becomes shared memory that gets smarter as more agents run.
**Paper:** Novel caching layer; measure token savings and cache hit rates vs. naive exact-match caching.
**Feasibility:** Straightforward — embedding + LSH in Rust, Redis/SQLite backing, proxy intercepts.

### 3. Waste Pattern Immune System
Runtime anomaly detection that kills pathological agent behaviors: retry storms, context bloat spirals, infinite loops, redundant re-reads. Track token flow, API call patterns, context utilization. When a pattern matches a known waste signature or exceeds a budget, intervene — kill the call, truncate context, force cheaper model. Garbage collector + circuit breaker for agentic compute.
**Paper:** Taxonomy of agent waste patterns with detection heuristics and measured savings.
**Feasibility:** Medium — detection is easy (counters + pattern matching in Rust), intervention policy is the research question.

### 4. Inference-Aware Scheduling
Treat inference calls as schedulable work units with known cost profiles. Model attention cost as O(n·m), account for KV cache reuse across sequential calls, batch requests sharing prefixes. Add provider arbitrage and BBR-style congestion control — probe available capacity, back off on 429s, maximize throughput per dollar. A query optimizer for LLM inference.
**Paper:** Formalize inference scheduling as resource allocation; benchmark against naive sequential execution.
**Feasibility:** High — Rust proxy with provider profiling, prefix-aware batching, adaptive routing.

### 5. Compilation by Demonstration (PGO for Agents)
Record agent executions on recurring tasks, replay to identify the critical path, compile an optimized plan that skips exploratory steps. Profile-guided optimization for agents. First run is exploratory and expensive; subsequent runs use the learned plan. The "compiler" learns which LLM calls were load-bearing vs. wasted.
**Paper:** "PGO for agents" is immediately legible to systems people. Measure speedup on repeated tasks.
**Feasibility:** Moderate — trace logging easy, plan extraction needs heuristics, replay engine is real work.

### 6. Write-Ahead Logging for Agent State
ARIES-style crash recovery. Log every state transition to a WAL so if an agent dies mid-task, it resumes from checkpoint instead of restarting from scratch. A 30-minute session that dies at minute 28 shouldn't cost 28 minutes of tokens to replay. Also enables debugging (replay past executions), auditing, and differential analysis.
**Paper:** Apply classic DB recovery theory to agentic AI; measure recovery time and cost savings.
**Feasibility:** Very doable in Rust — structured log, checkpoint/restore. Interesting part is defining "agent state" at the right granularity.

### How These Combine

| Start Here | Then Layer |
|-----------|-----------|
| **Waste Immune System** (#3) — empirical foundation, profile real waste | **Inference Scheduler** (#4) — optimize the calls that survive |
| **Semantic Memoization** (#2) — fastest to measurable results | **WAL** (#6) — crash recovery reuses the cache |
| **PGO** (#5) — headline result for SWE-bench | **Speculation** (#1) — hide latency on top of the optimized plan |

---

## References & Related Work

**Prior work:**
- **[An LLM Compiler for Parallel Function Calling](https://arxiv.org/abs/2312.04511)** — Kim et al. (2024). Closest prior work: treats multi-tool agent workflows as a compiler problem, constructs DAGs, executes in topological order. Up to 3.7x latency speedup, 6.7x cost savings, ~9% accuracy improvement vs. ReAct.
- **[Compound AI Systems Optimization: A Survey](https://aclanthology.org/2025.emnlp-main.1463/)** — EMNLP 2025. Formalizes optimization of multi-module LLM pipelines.
- **[MasRouter: Learning to Route LLMs for Multi-Agent Systems](https://aclanthology.org/2025.acl-long.757.pdf)** — ACL 2025. Learned routing to assign cheaper models to simpler sub-tasks.

**Harness engineering discourse:**
- **[Harness Engineering](https://martinfowler.com/articles/exploring-gen-ai/harness-engineering.html)** — Birgitta Böckeler / Martin Fowler site (Feb 2026). Analyzes harness engineering as a discipline.
- **[Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)** — Anthropic Engineering (Nov 2025). Context window management, error recovery, checkpointing.
- **[Building AI Coding Agents for the Terminal](https://arxiv.org/html/2603.05344v1)** — Bui (March 2026). Technical report on terminal-native agent design: context engineering, model routing, safety architecture.
- **[2025 LLM Year in Review](https://karpathy.bearblog.dev/year-in-review-2025/)** — Andrej Karpathy (Dec 2025). Documents RLVR as dominant training paradigm, calls Claude Code "first convincing LLM agent."

**Key gap:** No single paper unifies harness engineering, compiler-style DAG optimization, and runtime inference optimization into one framework. The space is converging but still fragmented.

---

## Differentiation from Prior Work

**Kim et al. — "An LLM Compiler for Parallel Function Calling" (2024).** Closest prior work. They parallelize independent tool calls within a single agent turn — up to 3.7x latency speedup, 6.7x cost savings vs. ReAct. But they optimize call scheduling — ordering and parallelism. Agentc operates one level deeper: model selection, context compression, semantic caching, inference scheduling. Kim optimizes the plan; Agentc optimizes the execution engine the plan runs on.

**Model routing work (e.g., MasRouter-style).** Learned routing that picks which model handles which sub-task. Overlaps with Agentc's ModelDowngrade rule, but routing alone is a standalone decision — not part of a unified runtime that also handles caching, scheduling, and context management.

**Compound AI optimization literature.** Formalizes multi-module LLM pipeline optimization but tends to focus on differentiable/learned approaches. Agentc's rewrite-rule approach (closer to traditional compiler passes) is a less-explored angle in this space.

---

## Summary

The problem is real. The "layer beneath agents" thesis is the strongest framing — don't try to replace or compete with agent harnesses, build the runtime they all sit on. The hard problems (DAG planning paradox, reactive execution) push the design toward runtime optimization rather than static compilation.

**Concrete next steps:**
1. **Profile first.** Instrument real multi-agent SWE-bench runs. Characterize where tokens go — this data alone is publishable.
2. **Build semantic memoization.** Fastest path to measurable savings. Two weeks to prototype, another two to benchmark.
3. **Add waste detection.** Pattern-match the obvious anti-patterns (retry storms, redundant context). Quantify savings.
4. **Evaluate on SWE-bench.** Same tasks, same agent, with and without the Agentc runtime. Measure cost reduction vs. accuracy retention.
5. **Write the paper.** Frame as: "Kim et al. optimized tool call scheduling; we optimize the full inference runtime beneath it." Differentiation is clean.

The research paper writes itself: formalize the IR, profile real multi-agent executions, show which optimizations give the biggest wins, evaluate on SWE-bench. Even partial results are publishable.

---

## Value Analysis

**As a research paper — strong.** The "token profiling + runtime optimization" angle is timely and under-explored. Venues like MLSys, NeurIPS systems track, or ICML workshops are actively looking for this kind of work. Even a workshop paper with partial results (profiling data + one optimization) would be a solid first publication. Kim et al. optimized tool call scheduling; model routing work optimizes model selection; compound AI surveys formalize pipeline optimization — Agentc unifies all three beneath a single runtime, which is a clean differentiator reviewers can immediately understand. Worst case, the profiling dataset and waste taxonomy stand on their own as an empirical contribution.

**As a learning experience — extremely high.** Building this means working at every level of the agentic AI stack: raw LLM inference characteristics (attention costs, KV caching), harness engineering (context management, model routing, tool orchestration), and systems engineering (Rust runtime, concurrent execution, caching layers). Most people either use agents or build models — very few understand the runtime layer between them. Building Agentc means understanding harness engineering deeply enough to formalize it, which is a different skill than just doing it ad-hoc.

**As open source — the profiler ships first.** A standalone tool that instruments any agent pipeline and answers "where did my tokens go?" has immediate utility. Tools like LangSmith and Helicone offer token-level tracing, but none do cost-based optimization or waste pattern detection at the runtime level. The optimization runtime is harder to ship as OSS (requires trust, integration effort), but the profiler could get traction on its own and serve as the adoption funnel.

**As a commercial direction — possible but premature.** If the runtime demonstrably cuts multi-agent costs by 30-50%, there's a real market among teams running expensive agent pipelines (CI/CD agents, code review bots, customer support agents). The business model would be usage-based — take a percentage of the savings. But this only makes sense after the research validates that the optimizations work at scale. Build the paper first, commercialize later if the numbers are there.

