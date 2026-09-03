---
title: MLSys Field Relevance and Main-Track Readiness
status: active
last-updated: 2026-09-03
owner: paper-intelligence
---

# MLSys Field Relevance and Main-Track Readiness

## Scope and method

This is a point-in-time assessment as of **2026-09-03**. It compares the current Agentc design, manuscript, presubmit state, venue roadmap, literature ledger, publishability assessment, and canonical data manifest against primary sources only: official proceedings and conference pages, original papers, and authors' official project repositories. Statements about what another system does are source-backed; statements about novelty, reviewer reaction, or the experiments Agentc should run are explicitly judgments.

The short answer is not “the field has moved on.” The field has moved *into* this problem, quickly and at high systems depth. That makes Agentc more topical and its current broad framing less defensible at the same time.

## Executive verdict

**Agentc remains relevant, but its current umbrella claim has been surpassed.** Agent runtime optimization is now unmistakably a main-track MLSys topic: the MLSys 2027 call explicitly names autonomous and agentic AI systems, inference and serving, testing and monitoring, programming models, languages, compilers, runtimes, benchmarks, and tooling. Research papers are selected for novelty, quality, interest, and impact. The paper therefore does not have a scope problem. It has a differentiation and evidence problem. [MLSys 2027 call for papers](https://mlsys.org/Conferences/2027/CallForResearchPapers)

The broad story—“a JIT/compiler/runtime observes multi-step agent work and optimizes it”—is no longer novel enough. Parrot already exposes cross-request semantic dataflow; SGLang supplies a language/runtime for structured LM programs; Agentix intercepts calls and schedules programs; ApproxMLIR jointly optimizes approximations in compound AI systems; Murakkab combines a profile-guided optimizer with an adaptive workflow runtime; Agent JIT Compilation literally uses the JIT label for agent planning and scheduling; and AgentOpt now performs framework-agnostic client-side interception, tracking, caching, and runtime routing. [Parrot](https://www.usenix.org/conference/osdi24/presentation/lin-chaofan), [SGLang](https://proceedings.neurips.cc/paper_files/paper/2024/hash/724be4472168f31ba1c9ac630f15dec8-Abstract-Conference.html), [Agentix](https://www.usenix.org/conference/nsdi26/presentation/luo), [ApproxMLIR](https://proceedings.mlsys.org/paper_files/paper/2026/hash/bbd3e0e9913824bbc46e7e87b11461ae-Abstract-Conference.html), [Murakkab](https://www.usenix.org/conference/osdi26/presentation/chaudhry), [Agent JIT Compilation](https://arxiv.org/abs/2605.21470), [AgentOpt](https://github.com/AgentOptimizer/agentopt)

Individual mechanisms are also mature prior art. Model routing, prompt compression, semantic caching, KV/context reuse, and parallel tool execution each have strong dedicated systems with substantially broader evaluations than Agentc currently offers. Static semantic-cache thresholds are specifically below the 2026 state of the art: vCache learns per-entry thresholds online under user-defined error bounds. ParallelBranch cannot carry a systems claim while it only emits a certificate/audit plan and the synchronous executor degrades that plan; LLMCompiler, AI Metropolis, AsyncFC, and Agent JIT all execute real concurrency. [RouteLLM](https://openreview.net/forum?id=8sSqNntaMr), [LLMLingua-2](https://aclanthology.org/2024.findings-acl.57/), [vCache](https://proceedings.iclr.cc/paper_files/paper/2026/hash/9559cd2116de7a8f5672eac3fcd232cc-Abstract-Conference.html), [LLMCompiler](https://proceedings.mlr.press/v235/kim24y.html), [AI Metropolis](https://proceedings.mlsys.org/paper_files/paper/2025/hash/4f31327e046913c7238d5b671f5d820e-Abstract-Conference.html), [AsyncFC](https://arxiv.org/abs/2605.15077)

**A narrow contribution may still be open:** a client-side control plane that operates over closed-provider calls, recovers useful structure from existing applications, applies *heterogeneous semantic rewrites* rather than only routing or serving decisions, abstains when its evidence is insufficient, and controls damage with online counterfactual sampling. I did not find a primary-source system demonstrating that exact combination. This is an absence-of-evidence judgment, not a firstness proof. AgentOpt is the most dangerous neighbor on transparent deployment; ApproxMLIR is the most dangerous neighbor on accuracy-aware multi-transformation optimization; Murakkab is the strongest broad workflow-runtime neighbor; and Agent JIT Compilation makes the unqualified “agent JIT” phrase unavailable.

The paper should therefore pivot from:

> a nine-rule JIT optimizer for multi-step LLM agents

to something closer to:

> a guarded, application-side rewrite control plane for opaque LLM-agent calls, designed for deployments that cannot replace the agent framework, declare a workflow graph, access the serving engine, or obtain task labels online.

That is a useful and timely systems point **only if the paper demonstrates the deployment constraint and the guard in realistic conditions**. Today it does not. The present evidence is strong enough for a carefully scoped artifact/workshop or perhaps an ML/LLM venue that values the methodology, but it is below the current MLSys main-track bar. The existing venue roadmap's weak-reject assessment remains directionally right, and the 2026 literature makes the novelty risk higher than that document could account for.

**Recommendation: conditional go for MLSys 2027, with an early kill gate.** Run a frozen, blind pilot on unengineered public agents immediately. Continue the main-track push only if at least two natural workloads show meaningful end-to-end benefit, with task-quality non-inferiority and a credible guard at its claimed production sampling rate. Otherwise stop expanding the compiler metaphor, submit a narrower safety/methodology paper elsewhere, and treat the runtime as an artifact rather than claiming a general optimizer.

## What the repository currently proves

The repository contains a real runtime, not a paper-only proposal. Its differentiating implementation ingredients are SDK interception, a typed trace/IR, observe-before-act hotness, per-call-site empirical profiles, deterministic rewrite preconditions, a composition planner, fail-open behavior, and a sampled shadow-output circuit breaker. Those are a credible substrate.

The evidence, however, is much narrower than the abstract and contribution list imply:

| Current evidence | What it establishes | What it does not establish |
|---|---|---|
| ContextCompress on `long_context_qa`, (n=300): 33.9% cost and 34.0% input-token savings, 261/300 fires | A deterministic, message-level pruning rule can save materially on a deliberately long, separable context fixture | Natural prevalence. On real HotpotQA near the 8 KB boundary the manuscript reports only 0–1 fires in 300 tasks and about 0.19% savings; natural prose triggers no rewrite |
| ModelDowngrade on `gaia_router`, (n=127): 11.4% cost savings; paired flips 7 versus 2, (p=0.1797) | A configured cheaper route can reduce cost after warmup without a detected significant quality loss on this fixture | A competitive routing result, a general quality guarantee, or a selection-valid learned policy |
| StateDrop on `iterative_refiner`, (n=50): 6.1% cost and 10.8% input-token savings within run | Explicit read/write metadata can enable useful state liveness pruning | “No application changes.” StateDrop requires state read/write/dependency annotations and uses a coarse task scorer |
| ModelDowngrade + ContextCompress, (n=20): 95.2% of a projected additive ideal | The two mechanisms can coexist in one small mechanistic experiment | Measured end-to-end composition at power, a globally optimal planner, or quality preservation |
| ContextCompress + StateDrop, (n=30): 100.5% of additive token ideal, but StateDrop contributes only 0.06% alone | Same-driver rules need not interfere when they affect disjoint message subsets | A meaningful multi-rule gain or a general interference model |
| Deliberately harmful StateDrop guard sweep | The implementation can observe output divergence, disable a rule, and sharply limit a constructed failure under dense shadowing | An “accuracy guarantee,” guard false-positive/false-negative rates, restart durability, semantic equivalence, or the claimed result at 2% sampling |
| A handful of authors' agents and one unseen third-party integration | The interception path can operate outside the micro-fixtures and sometimes abstain correctly | Broad production prevalence, independent production deployment, diverse framework compatibility, or statistically powered generalization |

There are also submission-blocking synchronization issues. The current `DATA_MANIFEST.txt`, generated 2026-05-17, lists `autogen_bridge` at (n=200) with 38.5% token savings, while `main.tex` reports (n=300) and two operating points at 23.5% and 14.0%. The manifest marks StateDrop precondition validation “IN PROGRESS,” while the manuscript reports 0/320 versus 116/320 behavior. This may simply mean the manuscript has newer runs, but the canonical data ledger and paper currently disagree. No reviewer should be asked to infer which is authoritative. At inspection time the presubmit file left every gate open; this assessment has since verified the target CFP, but the manuscript is still not in the MLSys format and the visible author block still violates double-blind submission requirements.

### Reconciliation with the July hostile-review verdict

The 2026-07-14 venue roadmap records a **weak reject for MLSys main even after assuming all 144 correctness fixes were complete**. The current manuscript is stronger than an early alpha: it now foregrounds the dual-regime LLMLingua-2 comparison, gives the guard a cross-model/selectivity story, reports larger `autogen_bridge` runs, includes several realistic agents plus an unseen integration, and states important limitations. Those are real improvements. They do not answer the hostile reviewer's structural objection:

- “Real agent” is not the same as a blind sample of independent production traffic. The paper's strongest naturally occurring savings still come from agents/workloads whose structure is unusually favorable, while the unengineered HotpotQA and natural-prose controls mostly abstain.
- The guard results are stronger as a *mechanism demonstration*, but the abstract combines a result obtained with dense shadowing and an operating-cost claim for 2% sampling. No current experiment establishes the same damage containment at 2%.
- The paper now documents the composition caveats, but documentation does not turn (n=20) projected additivity or a 0.06% second-rule contribution into end-to-end multi-rule evidence.
- The newer field scan worsens, rather than improves, the novelty side of the July verdict: AgentOpt now directly occupies transparent client-side interception/routing/caching, while 2026 archival papers raise the bar for workflow runtimes, safety, and agent evaluation.

Accordingly, the July verdict is **not stale**. The stronger manuscript makes the workshop/COLM case more compelling and makes the honest core easier to see, but MLSys main remains a weak reject until the same missing experiment identified in July—blind, unengineered traffic at useful scale, over repeated trials—comes back positive. The concrete October gate below is the decision procedure for revisiting that verdict.

## Closest-work matrix

“Overlap” below is deliberately strict: it identifies the exact portion of Agentc that a reviewer could say is already known. “Remaining difference” is the narrow separation Agentc could defend, not an assertion of novelty.

The evidence classes must not be conflated. The first table contains peer-reviewed archival work plus two accepted ICML 2026 papers whose primary public evidence is currently an author paper/repository. The second table contains public projects and preprints; these are not archival priority evidence, but they will be visible to 2027 reviewers and materially constrain broad novelty claims. **AgentOpt is the central practical novelty threat, but it is in the second class: a public technical report/project, not a peer-reviewed archival paper located in this scan.**

### Peer-reviewed archival and accepted work

| Work | Date and venue/status | Exact overlap with Agentc | Remaining difference and consequence |
|---|---|---|---|
| [Parrot: Efficient Serving of LLM-based Applications with Semantic Variable](https://www.usenix.org/conference/osdi24/presentation/lin-chaofan) | July 2024, OSDI 2024 | Gives prompt inputs/outputs a semantic-variable abstraction, connects requests into data pipelines, performs dataflow analysis, and reports up to order-of-magnitude improvement | Parrot requires applications to use its abstraction and service. Agentc can distinguish inferred/adapter-derived provenance over existing provider SDK calls—but cannot imply that semantic variables, cross-call dataflow, or compiler analysis are new |
| [LLMCompiler](https://proceedings.mlr.press/v235/kim24y.html) | July 2024, ICML 2024 | Compiler-inspired planning, dependency-aware parallel function execution, closed- and open-model support; up to 3.7× latency and 6.7× cost gains | Parallel tool execution is established. Agentc needs a real asynchronous executor, sound dependency/effect conditions, and an end-to-end latency study before retaining ParallelBranch as a contribution |
| [SGLang](https://proceedings.neurips.cc/paper_files/paper/2024/hash/724be4472168f31ba1c9ac630f15dec8-Abstract-Conference.html) | December 2024, NeurIPS 2024 main track | A frontend language and runtime for multi-call LM programs, with parallelism primitives, KV reuse, structured decoding, and up to 6.4× throughput | SGLang assumes a language/runtime and serving control. Agentc is above a black-box API boundary and rewrites message/state semantics; it should be presented as complementary, not a superior general runtime |
| [FrugalGPT](https://openreview.net/forum?id=cSimKw5p6R) and [RouteLLM](https://openreview.net/forum?id=8sSqNntaMr) | December 2024, TMLR; April 2025, ICLR 2025 | Cost-aware cascades and preference-trained strong/weak model routing establish per-query model choice as a mature mechanism | ModelDowngrade is an integration rule, not a novelty claim. It requires a competitive routing baseline and a held-out, selection-valid evaluation if kept in headline results |
| [AI Metropolis](https://proceedings.mlsys.org/paper_files/paper/2025/hash/4f31327e046913c7238d5b671f5d820e-Abstract-Conference.html) | 2025, MLSys 2025 | Dynamically tracks real dependencies and performs out-of-order multi-agent execution, reporting 1.3–4.15× speedups as scale grows | Its simulation domain is narrower, but its concurrency mechanism and scaling evidence are much deeper. Agentc's current bookkeeping-only parallel plan is not comparable |
| [ApproxMLIR](https://proceedings.mlsys.org/paper_files/paper/2026/hash/bbd3e0e9913824bbc46e7e87b11461ae-Abstract-Conference.html) | 2026, MLSys 2026 | A unified IR for approximations across LLM and non-ML components plus an accuracy-aware optimizer; evaluated on three compound AI systems | It requires an MLIR-based stack, declared transformations, and quality-oriented tuning. Agentc's possible separation is live, label-free, client-side control over opaque APIs. “Accuracy-aware compiler for compound AI” is already occupied |
| [Using Span Queries to Optimize Cache and Attention Locality](https://proceedings.mlsys.org/paper_files/paper/2026/hash/f5d77f1e501e0496377d8b68c8e81a48-Abstract-Conference.html) | 2026, MLSys 2026 | Expression trees encode inference calls and commutativity constraints; the optimizer changes KV and attention locality and reports 10–20× TTFT reductions | This is a deeper explicit IR/optimizer with real engine execution. Agentc must quantify the value and accuracy of recovering structure from existing apps rather than receiving it explicitly |
| [AgenticCache](https://proceedings.mlsys.org/paper_files/paper/2026/hash/c66a9db149261435664284a20b6f1d42-Abstract-Conference.html) | 2026, MLSys 2026 | Reuses plan transitions while a background LLM validates/refines entries; four embodied multi-agent benchmarks × three models; reports +22% success, −65% latency, and −50% tokens on average | It is domain-specific, but it establishes the empirical bar for guarded agent caching. Agentc's CacheHit fire-rate statistic and static policy are not independently publishable |
| [ContextPilot](https://proceedings.mlsys.org/paper_files/paper/2026/hash/b0131b6ee02a00b03fc3320176fec8f5-Abstract-Conference.html) | 2026, MLSys 2026 | Finds overlapping context, aligns/de-duplicates it for KV reuse, uses annotations to protect quality, integrates with inference engines, and reports up to 3× lower prefill latency | It optimizes representation/reuse, not semantic message deletion. Agentc can be orthogonal, but should measure interaction with provider prefix caching and not call all context optimization its territory |
| [vCache](https://proceedings.iclr.cc/paper_files/paper/2026/hash/9559cd2116de7a8f5672eac3fcd232cc-Abstract-Conference.html) | 2026, ICLR 2026 | Learns a threshold for each cached prompt online under a user-defined error bound; reports up to 12.5× higher hit rate and 26× lower error than static/fine-tuned baselines | A fixed similarity threshold is below the current correctness bar. Either integrate vCache-style calibration, benchmark against it, or remove semantic caching from novelty claims |
| [Agentix](https://www.usenix.org/conference/nsdi26/presentation/luo) | May 2026, NSDI 2026 | Intercepts LLM calls, enriches scheduling with program context, and schedules single-threaded/distributed agent programs; reports 4–15× throughput over serving baselines | It owns transparent call interception for program-aware serving/scheduling. Agentc remains distinct only at the application-side semantic rewrite layer over closed services |
| [Agent JIT Compilation for Latency-Optimizing Web Agent Planning and Scheduling](https://arxiv.org/abs/2605.21470) | May 2026 preprint; accepted ICML 2026 | Uses the exact JIT-compilation framing; generates and validates executable plans, searches parallel schedules from learned latency distributions, and uses tool pre/postcondition invariants; reports 10.4× and 2.4× speedups on web applications | It compiles task descriptions into code rather than rewriting observed API calls. Nonetheless, Agentc should not use “Agent JIT” as an unqualified firstness or category-defining claim |
| [ThunderAgent](https://github.com/ThunderAgent-org/ThunderAgent) | July 2026, ICML 2026 Spotlight, per the authors' repository | Treats an agent program as a scheduling/resource unit spanning KV cache, system state, and tools; integrates with vLLM/SGLang and reports 1.5–3.6× serving throughput on SWE-Agent, OpenHands, and ToolOrchestra | It requires serving infrastructure and a program identifier. Agentc can be complementary above remote APIs, but the comparison highlights Agentc's missing scale and representative-agent evidence |
| [Murakkab](https://www.usenix.org/conference/osdi26/presentation/chaudhry) | July 2026, OSDI 2026 | Declarative workflow abstraction, profile-guided optimizer, adaptive runtime, cross-layer orchestration/model/hardware choices, and explicit SLOs; up to 2.8× lower GPU use, 3.7× lower energy, and 4.3× lower cost | Murakkab owns the broad “profile-guided adaptive agent-workflow runtime” story. Agentc's defensible constraint is no declared graph, no server/hardware control, and no app migration |
| [When Machine Learning Isn't Sure](https://proceedings.mlsys.org/paper_files/paper/2026/hash/96aca14d6c4dcd3adf54bc2c5ad7f138-Abstract-Conference.html) | 2026, MLSys 2026 | Measures runtime uncertainty, rejects unreliable ML decisions, and selects safe fallbacks across three systems case studies | MLSys has accepted measured runtime rejection/fallback as a systems contribution. Agentc's guard is on-topic, but needs calibrated uncertainty, false-positive/negative analysis, and outcome-aware fallback evaluation |

### Non-archival projects and preprints visible to 2027 reviewers

| Work | Date and status | Exact overlap with Agentc | Remaining difference and consequence |
|---|---|---|---|
| [AgentOpt technical report](https://arxiv.org/abs/2604.06296) and [official repository](https://github.com/AgentOptimizer/agentopt) | April 2026, public technical report/project; no archival venue located | Framework-agnostic client-side optimization; intercepts `httpx` or uses an HTTPS proxy for subprocess agents; records calls, exact-caches requests, searches model assignments, and routes per call at runtime | **Central novelty threat.** It defeats “first client-side,” “framework-agnostic,” “transparent interception,” and basic tracking/routing/cache firstness in practical terms. Agentc must win on heterogeneous *semantic* rewrites, trace-derived structure, composition, and online damage control. It is a must-run baseline or a must-explain incompatibility |
| [FlowCompile](https://arxiv.org/abs/2605.13647) and [SkVM](https://arxiv.org/abs/2604.03088) | May and April 2026 preprints | FlowCompile explores model, reasoning-budget, and workflow-structure configurations and builds a reusable frontier; SkVM compiles skills across eight models and three harnesses, including concurrency extraction and adaptive recompilation | Both make “compiler for workflows” crowded. Agentc should emphasize deployment-time rewrite governance, not general compilation |
| [Halo](https://arxiv.org/abs/2509.02121) and [Scepsy](https://arxiv.org/abs/2604.15186) | September 2025 and April 2026 preprints | Structured/aggregate workflow models drive joint serving decisions, cache reuse, batching, allocation, and placement at realistic scale | These are self-hosted serving systems, not semantic request rewriting, but they raise expectations for cost models, scale, and whole-system evaluation |
| [AsyncFC](https://arxiv.org/abs/2605.15077) | May 2026 preprint | Overlaps decoding with tool execution and parallelizes independent functions without model fine-tuning or function changes | Another direct warning that concurrency must be executed and measured. Agentc's possible contribution is inferred cross-call safety, not concurrency itself |
| [Atomix](https://arxiv.org/abs/2602.14849), [Cordon](https://arxiv.org/abs/2606.17573), and [REVISE](https://arxiv.org/abs/2609.00643) | February, June, and September 2026 preprints | Transactional effects, task-level commit/rollback/audit, and dependency-guided invalidation/revalidation show an emerging runtime-safety standard for stateful agents | These systems address external-state correctness, while Agentc compares model outputs. Agentc must call its mechanism a sampled output-divergence circuit breaker, not comprehensive runtime safety or an accuracy guarantee |
| [Opportunity Is Not Realizability](https://arxiv.org/abs/2608.08265) | August 2026 preprint | Separates oracle routing opportunity, signal-conditioned opportunity, and held-out deployable routing; supplies selection-valid confidence intervals | It raises the statistical bar: on its tasks the strongest prompt router recovers only 7.5–14.4% of the oracle gap. Agentc should not infer deployable savings from oracle/model-pool opportunity or tune/report on the same tasks |
| [Cost-Aware Optimization for Agentic Query Execution](https://arxiv.org/abs/2606.03152) | June 2026 preprint | Treats quality/cost-sensitive LLM operator plans as a runtime query-optimization problem and evaluates a learned optimizer across four databases | It makes a heuristic downgrade rule plus a small mechanistic composition study look preliminary, while remaining domain-specific and evaluator-driven |

## What is commoditized and what may still be novel

| Status in 2026 | Capability | Implication for the paper |
|---|---|---|
| Commoditized substrate | SDK/HTTP interception, token/cost/latency tracing, exact replay caching, per-call policy hooks | Describe as engineering and deployment mechanism. AgentOpt is broader at the wire boundary than Agentc's current OpenAI/Anthropic Python SDK patching |
| Established research area | Strong/weak model routing, cascades, per-query model choice | ModelDowngrade is a pass inside the system, not a paper contribution by itself. Use RouteLLM/FrugalGPT/AgentOpt baselines |
| Established research area | Prompt/context compression | The publishable question is whether structure-aware abstention provides a better deployment frontier than LLMLingua-2 and cheap extractive heuristics on *natural* agent traces |
| Established research area | Exact and semantic response caching | Exact caching is utility functionality. Semantic caching needs calibrated correctness comparable to vCache, not a global cosine threshold |
| Established research area | Tool-call DAG construction and parallel execution | Remove ParallelBranch from contribution counts until it actually changes end-to-end execution; compare to LLMCompiler/AsyncFC when it does |
| Established research area | Semantic variables, workflow graphs, compiler/IR framing | The IR is useful internal architecture. Novelty can only come from structure recovered at a low-adoption boundary or a new safety/composition contract |
| Established systems pattern | Profile-guided optimization, hotness gates, adaptive runtime decisions, SLO controllers | Observe-before-act is good design but not sufficient novelty after Murakkab, Agentix, and many serving runtimes |
| Potentially differentiating, unproven | One client-side controller composing routing, prompt/state deletion, output caps, and cache decisions over opaque provider calls | Show that one controller is better than installing independent point solutions: more savings, fewer unsafe interactions, lower integration cost, or a stronger Pareto frontier |
| Potentially differentiating, unproven | Inferring trace provenance/dependencies from unmodified applications | Measure precision/recall against manually labeled graphs and report missing/ambiguous cases. StateDrop's current annotations mean this is not yet universal |
| Potentially differentiating, unproven | Rule-specific abstention plus online counterfactual shadowing across heterogeneous rewrites | Frame as damage control, not accuracy certification. Evaluate natural and injected regressions at the real sampling rate, including detection delay and benign disables |
| Potentially differentiating, unproven | Explicit interference-aware composition of semantic rewrites | The current planner is a fixed compatibility/order policy. To matter, it must beat best-single and naïve sequential policies end to end on workloads where multiple rules contribute materially |
| Potentially differentiating | Application-layer optimization orthogonal to provider prefix caching and self-hosted serving optimizers | Demonstrate additive benefit on top of a modern serving/runtime stack or at least isolate interaction with native prefix caching |

The strongest intellectual center is therefore not the list of rules. It is a **rewrite contract**: what evidence a runtime must have before changing an opaque call; how it predicts cost, quality risk, and interference; when it abstains; what counterfactual it samples; and how it limits cumulative damage. If formalized and validated, that is deeper than “nine optimizations.” If it remains a set of thresholds plus a five-strike disable rule, reviewers will see an optimization library with careful benchmarking rather than a new systems abstraction.

## Paper-claim red team

| Current or likely claim | Reviewer attack | Defensible replacement now | Evidence needed to restore a stronger claim |
|---|---|---|---|
| “First JIT optimizer/runtime for LLM agents” | Parrot, Agentix, Murakkab, Agent JIT Compilation, ApproxMLIR, FlowCompile, and SkVM all occupy parts of that space | “An application-side runtime for guarded semantic rewriting of opaque LLM-agent calls” | A documented search plus an explicit capability matrix showing the unique constraint combination; avoid “first” unless every axis is verified |
| “One `agentc.init()` call; no application changes” | StateDrop requires message dependencies and state-read/state-write metadata; some evaluated agents contain instrumentation | “OpenAI/Anthropic call interception is one-line; rules that require provenance need optional annotations” | A zero-touch study over several unmodified frameworks, including failure/abstention rates, or automatic provenance inference with measured accuracy |
| “Framework-agnostic” | Current patching is provider-SDK-specific; AgentOpt intercepts `httpx` and subprocess HTTPS traffic | “Framework-independent for applications that transit the supported OpenAI/Anthropic Python SDKs” | HTTP-layer interception or a tested compatibility matrix across LangChain, LangGraph, AutoGen/AG2, CrewAI, OpenAI Agents SDK, and subprocess agents |
| “Nine rewrite rules” as a contribution | Six lack isolated end-to-end validation; rule count is not systems depth | “A runtime substrate evaluated through three active rewrite families; other passes are implementation case studies” | Natural activation, isolated causal effect, quality, and overhead for each claimed rule. Remove or demote rules that do not change execution |
| “Up to 34% savings without accuracy loss” | The maximum is on a fixture intentionally built to cross the gate and expose separable distractors; natural HotpotQA mostly abstains | “33.9% on the activating fixture; near-zero on the natural boundary workload; savings depend on structural prevalence” | Blind unengineered workloads with aggregate savings distribution, confidence intervals, and prevalence/abstention breakdown |
| “Three real production agents” | They appear to be local/evaluation agents; `debug_agent` lacks task accuracy; “production” implies independent deployment evidence | “Three realistic agent programs plus an unseen third-party integration” | External deployment, anonymized production traces, operator testimony, or remove “production” everywhere |
| “Across three model providers” | Together-hosted open weights are not a model provider/category equivalent to a distinct closed-model protocol; pricing was missing in the frozen table | Name the exact models, hosts, API protocols, and pricing source | Re-run with pinned identifiers and complete prices on at least two closed-provider protocols plus one self-hosted/open stack |
| “Automatic label-free accuracy guard” | Output divergence is not task accuracy. Lexically different correct answers and lexically similar wrong answers defeat it | “A label-free sampled output-divergence circuit breaker” | Calibrated semantic risk, human/task-label audit, false-negative/false-positive curves, and non-inferiority at a declared damage budget |
| “Bounded 2% shadow-sampling cost prevents 97% of damage” | The showcased guard experiments use dense or effectively full shadowing; at 2%, five consecutive violations can require hundreds of eligible calls and each shadow is a synchronous billed request | State the measured sampling rate for every experiment and separately project—not claim—2% deployment overhead | Run 0/1/2/5/10/100% sampling with real stochastic timing, sequential detection analysis, cumulative loss before disable, retained savings, p95/p99 latency, and dollars |
| “Fail-open safety” | Fail-open protects availability, not semantic correctness or external tool effects | “Exceptions preserve baseline request execution” | Pair with effect-aware or task-aware safeguards; do not conflate operational fallback with safe agent behavior |
| “95.2% of additive ideal” | (n=20), projected cost arithmetic, and no powered quality result; this is mechanistic | “A small orthogonality check reaches 95.2% of the projected additive ideal” | End-to-end measured composition on natural workloads, multiple seeds, CIs, best-single and naïve-sequential controls |
| “Composition planner” | The implementation uses fixed driver compatibility and ordering; it is not a search/learned global optimizer | “A deterministic compatibility and ordering policy” | Cost-quality-risk objective, uncertainty, counterfactual alternatives, and evidence it chooses better plans than strong policies |
| “ParallelBranch reduces latency” | The manuscript itself says the synchronous executor degrades the plan and concurrency comes from a separate helper | Remove the performance claim and demote the rule | Implement actual dispatch; test dependencies, side effects, contention, throughput, and tail latency against LLMCompiler/AsyncFC |
| “Generalizes to any agent with the target structural properties” | The property prevalence and inferred-property correctness are not measured on independent traces | “Applies when explicit, auditable preconditions are present; otherwise abstains” | A representative trace corpus, blinded precondition prevalence, inference precision/recall, and held-out task/model/framework studies |
| “Sub-millisecond overhead” | Local planner overhead can be sub-ms while shadow calls and remote tail latency dominate deployment cost | “Local rewrite bookkeeping is sub-ms on warm paths” | Whole-path overhead including persistence, embeddings, cold start, contention, and sampled counterfactual calls at p50/p95/p99 |

Before any external submission, synchronize the manuscript, data manifest, figures, and claim ledger into a single authoritative snapshot. The present `autogen_bridge` sample-size/result mismatch and the stale “IN PROGRESS” StateDrop manifest entry are enough to undermine reviewer confidence even if the underlying new data are valid.

## What MLSys main-track reviewers currently expect

### Verified venue requirements

MLSys 2027 is a plausible venue and the timeline is unforgiving. The official deadline is **2026-10-30 at 20:00 UTC**. Research submissions are double blind, require good-faith anonymization, use the 2025 MLSys style, and are limited to ten pages excluding references. Appendices are uploaded separately and reviewers are not required to read them. The current author-identifying `acmart` manuscript is therefore not submission-ready. [MLSys 2027 call for papers](https://mlsys.org/Conferences/2027/CallForResearchPapers), [MLSys 2027 dates](https://mlsys.org/Conferences/2027/Dates)

Artifact evaluation is voluntary and does not determine paper acceptance, but the official process assesses availability, functionality, and reproducibility of code, data, workflows, and results. It asks authors to state requirements, validation steps, and expected outcomes. The artifact should be built during experimentation, not reconstructed after acceptance. [MLSys 2026 artifact evaluation call](https://mlsys.org/Conferences/2026/CallForAEs)

### Inferred empirical bar from accepted work

The following are not formal acceptance rules. They are the standard suggested by recent accepted systems:

- **Representative breadth.** AgenticCache evaluates four embodied multi-agent benchmarks across three models; OSWorld-Human evaluates 16 computer-use agents and human-annotated trajectories; ApproxMLIR uses three different compound systems. A three-fixture, mostly single-model study is not competitive on external validity. [AgenticCache](https://proceedings.mlsys.org/paper_files/paper/2026/hash/c66a9db149261435664284a20b6f1d42-Abstract-Conference.html), [OSWorld-Human](https://proceedings.mlsys.org/paper_files/paper/2026/hash/5edb57c05c81d04beb716ef1d542fe9e-Abstract-Conference.html), [ApproxMLIR](https://proceedings.mlsys.org/paper_files/paper/2026/hash/bbd3e0e9913824bbc46e7e87b11461ae-Abstract-Conference.html)
- **End-to-end systems metrics.** Recent papers report throughput, tail latency, resource or energy use, dollars/tokens, task quality, and scaling—not only local transformation overhead or projected token counts. Agentix, Murakkab, Span Queries, and ThunderAgent exemplify this standard. [Agentix](https://www.usenix.org/conference/nsdi26/presentation/luo), [Murakkab](https://www.usenix.org/conference/osdi26/presentation/chaudhry), [Span Queries](https://proceedings.mlsys.org/paper_files/paper/2026/hash/f5d77f1e501e0496377d8b68c8e81a48-Abstract-Conference.html), [ThunderAgent](https://github.com/ThunderAgent-org/ThunderAgent)
- **Strong mechanism-matched baselines.** A full-system “optimizer off” baseline is necessary but insufficient when each pass competes with a mature point solution. The evaluation needs routing, compression, cache, and concurrency baselines where those mechanisms contribute.
- **Applicability and negative regimes.** The current manuscript's natural-prose abstention and real-Hotpot boundary result are valuable. Promote them. Reviewers need to know when a rule fires, when it cannot, and when it harms quality.
- **Stochastic validity.** Single provider runs at default temperature and small (n) do not establish non-inferiority. The fresh routing analysis in Opportunity Is Not Realizability is especially relevant: choosing the apparent best policy and evaluating it on the same data creates selection bias. [Opportunity Is Not Realizability](https://arxiv.org/abs/2608.08265)
- **Reproducible claims.** Paid APIs complicate replay, but the artifact can still include pinned raw request/response traces where licensing permits, a deterministic no-key replay path, source outcomes, scripts, and a paid live mode. The official artifact process explicitly values validation instructions and expected results. [MLSys artifact guidance](https://mlsys.org/Conferences/2026/CallForAEs)

## Minimum viable MLSys main-track experiment package

This is the smallest package that could plausibly change the recommendation from “weak reject” to “borderline/weak accept.” It is still substantial.

### 1. Freeze the thesis, runtime, and analysis before the blind test

- Declare the supported boundary: closed-provider OpenAI/Anthropic-compatible calls, no serving-engine access, no required workflow DSL, and optional annotations only for explicitly marked rules.
- Choose the three or four rewrite families actually being claimed. A credible minimum is ContextCompress, ModelDowngrade, StateDrop when provenance is available, and one exact cache/output-budget mechanism. Remove ParallelBranch from headline claims unless the async executor lands before the freeze.
- Pre-register rule thresholds, hotness, composition order, guard budgets, model mappings, task splits, non-inferiority margins, and primary outcomes. Do not tune on the final tasks.
- Create a frozen calibration split and a held-out blind split. Report every attempted workload, including those where no rule fires.

### 2. Use three unengineered workload families

At minimum:

1. **Stateful tool use:** the current [τ³-bench repository](https://github.com/sierra-research/tau2-bench), pinned to a release, with airline/retail or banking-knowledge tasks. This exercises long policies, multi-turn state, tools, and real success criteria.
2. **Coding:** a fixed subset of [SWE-bench Verified](https://github.com/SWE-bench/SWE-bench) using an established agent such as mini-SWE-agent/OpenHands, without restructuring prompts to make rules fire. This tests long trajectories, repeated context, model routing risk, and external effects.
3. **Computer-use or retrieval:** an [OSWorld/OSWorld-Human](https://github.com/xlang-ai/OSWorld) subset or a fixed real retrieval agent with public tasks. OSWorld-Human is particularly aligned with efficiency because it provides human trajectory references and MLSys 2026 found leading agents used 2.7–4.3× excess steps. [OSWorld-Human paper](https://proceedings.mlsys.org/paper_files/paper/2026/hash/5edb57c05c81d04beb716ef1d542fe9e-Abstract-Conference.html)

Run at least two genuinely different orchestration stacks, not merely two thin wrappers around the same loop. Include at least two provider protocols and three model families/tiers where the mechanisms make sense. Pin exact model versions, temperatures, SDK versions, provider, date, region if relevant, and price table.

The primary result must aggregate *all* eligible tasks in each workload. Do not report only traces that cross a ContextCompress threshold or only call sites where the optimizer warms successfully.

### 3. Compare against strong per-mechanism and full-system baselines

Required minimum controls:

- Unmodified agent with provider-native prefix caching as actually deployed.
- Agentc all-on, each claimed rule only, each claimed rule off, best single rule, and naïve sequential composition.
- [AgentOpt](https://github.com/AgentOptimizer/agentopt) for transparent tracking/model selection/routing, using the same model pool and calibration budget.
- [RouteLLM](https://openreview.net/forum?id=8sSqNntaMr) or a clearly justified learned router plus a simple fixed-cheap/fixed-strong cascade for ModelDowngrade.
- [LLMLingua-2](https://aclanthology.org/2024.findings-acl.57/) plus cheap recency/BM25/extractive dropping for ContextCompress. Match achieved input-token reduction where possible rather than comparing unrelated operating points.
- [vCache](https://proceedings.iclr.cc/paper_files/paper/2026/hash/9559cd2116de7a8f5672eac3fcd232cc-Abstract-Conference.html) or, if its interface makes direct integration impossible, exact caching and a calibrated semantic-cache baseline with an explicit reason the official implementation cannot run.
- LLMCompiler or AsyncFC only if Agentc claims executed concurrency. Otherwise remove that axis from the paper.

ApproxMLIR, Murakkab, Agentix, and ThunderAgent may be impractical apples-to-apples baselines because they require different execution/serving control. For those, provide a capability comparison and, ideally, one orthogonality experiment rather than a misleading numerical contest.

### 4. Measure the whole system

For every task and configuration, retain source-level records sufficient to compute:

- task success/score and paired outcome flips;
- total billed dollars using a versioned price table, input and output tokens, model calls, tool calls, cache hits, and shadow calls;
- end-to-end latency and time-to-first/last useful action where the workload exposes it;
- p50, p95, and p99 latency, throughput and goodput at multiple concurrency levels;
- optimizer CPU time, memory, persistent-state size, embedding time/cost, lock contention, cold start, and steady-state overhead;
- per-rule eligibility, proposal, application, abstention reason, and natural fire rate;
- composition chosen, alternatives rejected, and actual marginal benefit of each pass.

Report both warm and cold behavior. Warmup may model a long-lived service, but a client-side agent process often restarts; hiding the first tasks behind a warm cost model weakens the deployment claim.

### 5. Make the guard a first-class experiment at its real operating rate

The guard is the most plausible novelty anchor and currently the least supportable headline. Run shadow rates of 0%, 1%, 2%, 5%, 10%, and 100% on:

- naturally occurring harmful rewrites found during the blind runs;
- injected but realistic regressions for every destructive rule family, not only StateDrop;
- benign rewrites that legitimately change wording/format;
- provider errors, process restarts, and model/version drift.

For each rate, report false disable, missed harmful rewrite, time/fires/cost until disable, cumulative task damage before disable, retained savings, extra billed cost, and p50/p95/p99 latency. Separate local guard bookkeeping from the synchronous counterfactual model call. Calibrate thresholds on a held-out set, then lock them. Audit the divergence metric against task labels and a blinded human or independently validated judge sample. The result should be a cost–quality–risk frontier, not one “97% prevented” point.

### 6. Use inference suited to stochastic agents

- Use repeated paired trials or a power analysis; five trials per task/config is a reasonable floor for the primary stochastic comparison, not a universal guarantee of power.
- Use hierarchical paired bootstrap intervals over tasks and repetitions, and exact paired tests where outcomes are binary.
- Declare a task-specific non-inferiority margin before looking at optimized results.
- Keep calibration/model-selection tasks disjoint from reporting tasks and account for selecting among rules/configurations.
- Report effect sizes and intervals, not only non-significant (p)-values. “No significant loss” is not evidence of equivalence at (n=20) or (n=30).

### 7. Ship a credible artifact

- One clean-clone command builds Rust, the Python extension, and the replay harness.
- A no-key “reproduce-lite” path reconstructs every table/figure from frozen raw results and trace schemas.
- A paid “reproduce-paper” path documents exact expected spend, rate limits, model availability, and allowable variance.
- Every figure and table maps to a canonical file; no stale or unrecoverable rows remain.
- Include anonymous documentation and a capability/compatibility test matrix. The paper must compile in the official MLSys style and fit the ten-page body without relying on the appendix for essential evidence.

### Minimum success bar

Proceed only if the blind package shows all of the following:

- at least two of the three unengineered workload families naturally activate more than one claimed mechanism;
- at least two workloads achieve a practically meaningful aggregate improvement—roughly **10–15% lower billed cost or 15% lower end-to-end latency** is a reasonable screening threshold—without crossing the predeclared quality-loss margin;
- the full system beats best-single and naïve composition, rather than deriving nearly all benefit from one ContextCompress case;
- at the actual 2% shadow rate, the guard limits harmful-rewrite damage to the declared budget with acceptable delay and benign-disable rate, while preserving most of the optimization benefit;
- Agentc remains competitive with mechanism-matched baselines or demonstrates a clear deployment/combination advantage they do not offer;
- the result survives repeated trials, held-out analysis, and complete artifact replay.

These numbers are go/no-go engineering thresholds, not claims about what MLSys formally requires.

## Stronger, ambitious package

If time or follow-on work permits, the main-track version becomes much stronger with four additions.

### A. Make the optimization boundary genuinely general

Move interception below provider SDK adapters to an HTTP/gateway boundary comparable to AgentOpt, with explicit support for subprocess and non-Python agents. Attribute calls to traces without leaking sessions under concurrency. Publish a compatibility matrix and failures. This turns “application-transparent” from rhetoric into a measured systems property.

### B. Turn the guard into a risk controller

Replace a fixed five-consecutive-violations rule with a sequential, uncertainty-aware controller that exposes an operator-set damage budget. Calibrate per-rule/per-call-site risk, adapt sampling to uncertainty and drift, persist state across restarts, and provide statistically interpretable error bounds. Preserve tool-call schemas and external-effect invariants. Borrow the *standard*, not necessarily the mechanism, from vCache's explicit error budget, the MLSys uncertainty/fallback paper, and emerging transactional runtimes. [vCache](https://proceedings.iclr.cc/paper_files/paper/2026/hash/9559cd2116de7a8f5672eac3fcd232cc-Abstract-Conference.html), [runtime uncertainty and fallback](https://proceedings.mlsys.org/paper_files/paper/2026/hash/96aca14d6c4dcd3adf54bc2c5ad7f138-Abstract-Conference.html), [Atomix](https://arxiv.org/abs/2602.14849), [Cordon](https://arxiv.org/abs/2606.17573)

### C. Prove orthogonality to serving systems

Run Agentc above a modern self-hosted stack such as SGLang/vLLM with ThunderAgent- or Agentix-style program-aware scheduling. Show whether semantic request rewriting produces multiplicative or merely substitutive benefit after KV reuse, batching, and scheduling. This would make the architectural split—the application layer chooses *what semantic work to request*, the serving layer chooses *how to execute it*—a concrete contribution.

### D. Release a trace/prevalence benchmark

An anonymized corpus of real or representative agent call traces, with ground-truth message provenance, dependency/effect labels, rule eligibility, and counterfactual outcomes, would be more durable than another benchmark fixture. Measure automatic structure inference precision/recall, the prevalence of each optimization opportunity, realized versus oracle savings, and the reasons opportunities are not safely realizable. A trace release also gives the field a common evaluation target and strengthens artifact impact.

The ambitious planner would optimize an explicit cost–latency–quality-risk objective under uncertainty, compare alternative compatible plans, and learn conservatively from live traffic. The key is not to add more rules. It is to make plan choice and safety a coherent mechanism with a contract that reviewers can reason about.

## Stop/go decision criteria

The 2027 deadline leaves 57 days from this assessment. Use staged, irreversible gates.

### Binary campaign gate: 2026-10-01

The MLSys campaign is a **GO only if every row below passes on frozen, held-out runs by October 1**. Any failed row is a **NO-GO for MLSys 2027 main**, not an invitation to retune on the test set.

| Gate | Pass condition |
|---|---|
| Natural usefulness | At least two of three unengineered workload families show at least 10% lower billed cost or 15% lower end-to-end latency, and the 95% interval for task-quality change stays above the pre-registered non-inferiority margin |
| Unified-system value | At least two independent rewrite families each contribute a measurable held-out gain, and all-on beats the best single rule plus naïve sequential composition on at least one primary metric without a quality trade |
| Safety at the advertised rate | At 2% sampling, at least 90% of injected harmful call sites are disabled before their predeclared cumulative damage budget; benign-rule disable rate is at most 5%; at least 80% of gross optimization savings remains after shadow cost |
| Competitive position | At matched quality, Agentc beats AgentOpt or the relevant point-solution baseline on a primary outcome on at least two workloads, or demonstrates a measured integration/composition capability that the baseline cannot provide |
| Statistical validity | Primary results use disjoint calibration/test tasks, repeated paired trials or a justified power design, selection-aware analysis, and intervals—not “non-significant” single-run deltas |
| Submission integrity | Manuscript, manifest, figures, prices, and raw outcomes agree; clean-clone replay works; the anonymous paper fits the official ten-page body |

The exact non-inferiority and cumulative-damage margins must be fixed per workload before the blind run. If the existing five-consecutive-shadow-violations controller cannot meet the 2% gate, that is evidence to redesign the controller or remove the 2% safety headline—not to evaluate it at 100% and extrapolate.

### Go to full MLSys submission

By approximately **2026-10-01**, continue only if:

- integrations for at least three unmodified workload families and two frameworks work without task-specific prompt engineering;
- a frozen blind pilot shows natural opportunity and aggregate Pareto improvement on at least two workloads;
- guard results at 2% sampling are empirically supportable and no longer extrapolated from dense shadowing;
- the central novelty sentence survives direct comparison to AgentOpt, ApproxMLIR, Murakkab, Agentix, Parrot, and Agent JIT Compilation;
- every headline number has a canonical raw source, exact model/price metadata, and a reproducible script;
- an anonymized ten-page MLSys draft can tell one mechanism-centered story rather than cataloguing nine rules.

### Conditional go, but change target or scope

If only one natural workload shows meaningful savings, or if the strongest result is the guard rather than the optimizer, write the paper around that narrower empirical contribution and target a venue/track where a careful negative/applicability result fits. If the runtime is solid but external validity is not, release the artifact and trace study rather than overclaiming a main-track optimizer.

### Stop the MLSys 2027 main-track push

Stop by the early-October gate if any of these remain true:

- meaningful savings occur only on purpose-built fixtures, while unengineered workloads mostly abstain;
- one compression rule supplies nearly all aggregate gain and the unified planner does not outperform that rule alone;
- the 2% guard misses damaging rewrites for too long, frequently disables benign ones, or erases savings/latency benefit;
- AgentOpt or point-solution baselines match the result with a simpler mechanism and Agentc cannot show a deployment or composition advantage;
- StateDrop still requires manual metadata while the paper claims zero-touch inference;
- claimed rules do not actually change end-to-end execution;
- the manifest/manuscript/figures cannot be reconciled, the artifact cannot build cleanly, or the paper cannot be anonymized and reduced to ten pages.

## Final judgment

The field has **surpassed the current broad story but not necessarily the constrained problem**. Agentc is relevant because existing agents increasingly run over opaque provider APIs and cannot all be ported into Parrot, SGLang, Murakkab, ApproxMLIR, or a custom serving engine. A safe application-side optimizer is a legitimate missing layer. But transparency alone is no longer enough after AgentOpt, and “JIT for agents” is no longer an ownable phrase after ICML 2026.

The main-track path is to make the paper about a principled rewrite contract and prove it under natural traffic: evidence before rewrite, explicit abstention, interference-aware plan choice, sampled counterfactual validation, and bounded damage. The current implementation contains pieces of that system. The current evaluation does not yet prove the system matters broadly or that the guard works under the operating rate advertised in the abstract.

**Net: relevant, not obsolete; broad novelty surpassed; MLSys 2027 is a high-risk conditional go, contingent on blind real-workload evidence and a safety-centered reframing.**

## Primary source list

### Venue and evaluation standard

- MLSys 2027 call for research papers: https://mlsys.org/Conferences/2027/CallForResearchPapers
- MLSys 2027 dates: https://mlsys.org/Conferences/2027/Dates
- MLSys 2026 artifact evaluation call: https://mlsys.org/Conferences/2026/CallForAEs
- OSWorld-Human, MLSys 2026: https://proceedings.mlsys.org/paper_files/paper/2026/hash/5edb57c05c81d04beb716ef1d542fe9e-Abstract-Conference.html
- When Machine Learning Isn't Sure, MLSys 2026: https://proceedings.mlsys.org/paper_files/paper/2026/hash/96aca14d6c4dcd3adf54bc2c5ad7f138-Abstract-Conference.html

### Runtime, compiler, serving, and workflow systems

- Parrot, OSDI 2024: https://www.usenix.org/conference/osdi24/presentation/lin-chaofan
- SGLang, NeurIPS 2024: https://proceedings.neurips.cc/paper_files/paper/2024/hash/724be4472168f31ba1c9ac630f15dec8-Abstract-Conference.html
- LLMCompiler, ICML 2024: https://proceedings.mlr.press/v235/kim24y.html
- AI Metropolis, MLSys 2025: https://proceedings.mlsys.org/paper_files/paper/2025/hash/4f31327e046913c7238d5b671f5d820e-Abstract-Conference.html
- Halo, preprint: https://arxiv.org/abs/2509.02121
- Agentix, NSDI 2026: https://www.usenix.org/conference/nsdi26/presentation/luo
- ApproxMLIR, MLSys 2026: https://proceedings.mlsys.org/paper_files/paper/2026/hash/bbd3e0e9913824bbc46e7e87b11461ae-Abstract-Conference.html
- Span Queries, MLSys 2026: https://proceedings.mlsys.org/paper_files/paper/2026/hash/f5d77f1e501e0496377d8b68c8e81a48-Abstract-Conference.html
- Murakkab, OSDI 2026: https://www.usenix.org/conference/osdi26/presentation/chaudhry
- ThunderAgent, authors' official repository: https://github.com/ThunderAgent-org/ThunderAgent
- Agent JIT Compilation, ICML 2026 paper: https://arxiv.org/abs/2605.21470
- FlowCompile, preprint: https://arxiv.org/abs/2605.13647
- SkVM, preprint: https://arxiv.org/abs/2604.03088
- AsyncFC, preprint: https://arxiv.org/abs/2605.15077
- Scepsy, preprint: https://arxiv.org/abs/2604.15186
- Cost-Aware Optimization for Agentic Query Execution, preprint: https://arxiv.org/abs/2606.03152

### Transparent optimization, routing, compression, and caching

- AgentOpt technical report: https://arxiv.org/abs/2604.06296
- AgentOpt official repository: https://github.com/AgentOptimizer/agentopt
- FrugalGPT, TMLR 2024: https://openreview.net/forum?id=cSimKw5p6R
- RouteLLM, ICLR 2025: https://openreview.net/forum?id=8sSqNntaMr
- Opportunity Is Not Realizability, preprint: https://arxiv.org/abs/2608.08265
- LLMLingua, EMNLP 2023: https://aclanthology.org/2023.emnlp-main.825/
- LongLLMLingua, ACL 2024: https://aclanthology.org/2024.acl-long.91/
- LLMLingua-2, Findings of ACL 2024: https://aclanthology.org/2024.findings-acl.57/
- vCache, ICLR 2026: https://proceedings.iclr.cc/paper_files/paper/2026/hash/9559cd2116de7a8f5672eac3fcd232cc-Abstract-Conference.html
- vCache official repository: https://github.com/vcache-project/vCache
- AgenticCache, MLSys 2026: https://proceedings.mlsys.org/paper_files/paper/2026/hash/c66a9db149261435664284a20b6f1d42-Abstract-Conference.html
- ContextPilot, MLSys 2026: https://proceedings.mlsys.org/paper_files/paper/2026/hash/b0131b6ee02a00b03fc3320176fec8f5-Abstract-Conference.html
- Cortex, NSDI 2026: https://www.usenix.org/conference/nsdi26/presentation/ruan-cortex

### Stateful runtime safety

- Atomix, preprint: https://arxiv.org/abs/2602.14849
- Cordon, preprint: https://arxiv.org/abs/2606.17573
- REVISE, preprint: https://arxiv.org/abs/2609.00643

### Candidate public workloads

- τ³-bench current repository: https://github.com/sierra-research/tau2-bench
- SWE-bench: https://github.com/SWE-bench/SWE-bench
- OSWorld: https://github.com/xlang-ai/OSWorld
