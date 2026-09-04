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

**Joint model-routing plus semantic-rewrite optimization is not, by itself, a novel thesis.** Cognify searches combinations of model, code, prompt, and workflow-structure changes using workflow-level evaluation. ApproxMLIR co-tunes LLM approximation with context selection and non-ML approximations under an application QoS target, then emits runtime decision-tree choices. FrugalGPT explicitly identifies joint prompt and LLM selection as a composition, although its reported implementation study focuses on cascades. AgentOpt establishes that choices across pipeline roles must be evaluated as a full combination rather than as isolated calls. [Cognify paper](https://doi.org/10.1145/3711896.3736884), [Cognify repository](https://github.com/GenseeAI/cognify), [ApproxMLIR paper](https://proceedings.mlsys.org/paper_files/paper/2026/file/bbd3e0e9913824bbc46e7e87b11461ae-Paper-Conference.pdf), [FrugalGPT](https://openreview.net/forum?id=cSimKw5p6R), [AgentOpt technical report](https://arxiv.org/abs/2604.06296)

The narrower research opening is **online, call-conditional, interaction-aware constrained plan selection at an opaque API boundary**. That is a proposed direction, not a current Agentc result. The current CompositionPlanner sorts independently projected savings, greedily admits proposals using fixed `CostDriver` compatibility plus a safe-pair allowlist, and applies the survivors in a fixed order. It does not search a model-by-rewrite configuration space, learn cross-pass quality or cost interactions, or solve a quality/risk-constrained objective. The small ModelDowngrade-plus-ContextCompress orthogonality check establishes compatibility on one fixture; it does not establish joint optimization.

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

### Model-availability and price audit

The implementation audit on 2026-09-03 exposed a reproducibility failure in the prospective evaluation plan: its Together-hosted Llama 3.1 strong/cheap pair was already unavailable. Together's own deprecation ledger dates retirement of the 70B endpoint to 2026-02-25 and the 8B endpoint to 2026-03-06. Because no calibration, pilot, or confirmatory task had run, protocol revision 11 replaces that impossible cell with the currently cataloged `zai-org/GLM-5.3` and `zai-org/GLM-5.3-Flash` pair. This is an outcome-blind artifact correction, not model selection based on favorable results. [Together deprecations](https://docs.together.ai/docs/deprecations), [Together serverless catalog](https://docs.together.ai/docs/serverless/models)

The runtime catalog now records the dispatch protocol, credential namespace, exact model/revision form, context and output limits, required capabilities, output-token parameter convention, prices, observation date, and primary source together. OpenAI's frozen pair is `gpt-5.4-2026-03-05`/`gpt-5.4-mini-2026-03-17`; Anthropic's is `claude-sonnet-4-5-20250929`/`claude-haiku-4-5-20251001`; LiteLLM carries the `together_ai/` prefix for the Together pair. The observed list prices are $2.50/$15 and $0.75/$4.50 per million input/output tokens for the OpenAI pair, $3/$15 and $1/$5 for the Anthropic pair, and $1.40/$4.40 and $0.15/$0.50 for the Together pair. Cached-input categories are versioned separately. [OpenAI GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4), [OpenAI GPT-5.4 mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini), [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing), [LiteLLM](https://docs.litellm.ai/)

This strengthens artifact integrity but does not strengthen the novelty claim. A model catalog and price table are infrastructure. Their research relevance is that a persistent online planner cannot claim drift adaptation or reproducible cost optimization while silently routing aliases, conflating immutable snapshots, or retaining retired endpoints. Experimental manifests must therefore archive both the catalog version used for the decision and the exact model actually returned by the provider.

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
| [Cognify](https://doi.org/10.1145/3711896.3736884) | August 2025, KDD 2025 | Searches combinations of workflow-architecture, step, and prompt changes. Its implemented choices include task decomposition/ensembling, model selection, code rewriting, reasoning prompts, and few-shot examples; a user supplies training data, an evaluator, and a search budget | This is direct prior art for generic joint model-plus-workflow/prompt configuration search. Agentc can only distinguish online choice over observed opaque calls, lower adoption cost, and a runtime risk contract—not the idea of cross-lever joint optimization |
| [MESS+](https://papers.neurips.cc/paper_files/paper/2025/hash/4dd1d9b841712bd37b833559f041530c-Abstract-Conference.html) | December 2025, NeurIPS 2025 main conference | Learns per-model request-satisfaction probabilities online and combines them with a virtual queue in a per-request cost-minimization problem under a long-run satisfaction constraint; reports average 2× cost savings over routing baselines | It controls model choice only and relies on observed satisfaction feedback, but it owns online cost/SLA-aware routing. Any Agentc claim about online constrained planning needs MESS+ as an archival baseline or a precise explanation of why delayed agent outcomes and semantic rewrites make the problem different |
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

## Joint model-routing and semantic-rewrite planning audit

This section tests a more specific proposed thesis: **choose the model and the semantic rewrite plan together for each hot call site**. It separates source-backed capabilities from conclusions drawn for Agentc. A blank or “not reported” cell means only that the cited source does not report that capability; it is not proof that no unpublished implementation exists.

### Verified capabilities from primary sources

| Work and status | Model-choice unit and timing | Semantic or execution transformation | How choices are coordinated, trained, or constrained | Deployment boundary and reported evidence |
|---|---|---|---|---|
| [AgentOpt technical report](https://arxiv.org/abs/2604.06296) and [current official repository](https://github.com/AgentOptimizer/agentopt), April 2026 technical report/project | The report searches a fixed model assignment across pipeline roles; the current repository also exposes a per-call router using prompt, session history, or a user policy | The report evaluates model assignment. The repository's intercept supports tracking, exact request-body caching, and model-field replacement; it does not list a semantic prompt/state rewrite | Offline selection needs a dataset and evaluator and returns an accuracy/cost/latency frontier. The repository describes selection and online routing as composable stages, with learned feedback between them left to future versions | Patches `httpx` for in-process Python and uses an HTTPS proxy for subprocess agents. The report evaluates four benchmarks and ten search methods; UCB-E reduces evaluation budget 62–76% versus brute force |
| [RouteLLM](https://proceedings.iclr.cc/paper_files/paper/2025/hash/5503a7c69d48a2f86fc00b3dc09de686-Abstract-Conference.html), ICLR 2025 | A learned router sends each prompt to one strong or one weak model; a threshold converts predicted strong-model preference into the decision | No prompt, state, workflow, or execution rewrite is part of the reported action | Trained offline from human preference data with task-specific augmentation; the threshold sweeps the strong-call/quality trade-off | Single-request routing. Evaluated on MT-Bench, MMLU, and GSM8K; reports more than 2× cost reduction and generalization to model pairs absent from training |
| [FrugalGPT](https://openreview.net/forum?id=cSimKw5p6R), TMLR 2024 | A learned cascade chooses an ordered list of APIs and stops when a generated answer's reliability score crosses a learned threshold | The paper surveys prompt adaptation and completion caching and explicitly sketches joint prompt-plus-LLM selection, but its reported FrugalGPT experiments instantiate the LLM cascade | Learns cascade lists, scoring functions, and thresholds under a budget from labeled training examples; the paper states that train and test distributions should be similar | Black-box commercial APIs. Reports matching the best individual model with up to 98% lower cost or improving GPT-4 accuracy by 4% at matched cost |
| [MESS+](https://papers.neurips.cc/paper_files/paper/2025/hash/4dd1d9b841712bd37b833559f041530c-Abstract-Conference.html), NeurIPS 2025 main conference | Chooses among more than two models for every request while learning each model's request-satisfaction probability online | No semantic request rewrite is in the reported action space | A virtual queue records accumulated SLA deficit; each request minimizes model cost plus a queue-weighted predicted-satisfaction term. The guarantee is long-run and relies on stated assumptions, including predictor quality; the method obtains binary satisfaction feedback after responses and studies 20% feedback density | Self-hosted open-weight model zoos. Evaluated with three Llama sizes on eight zero-shot benchmarks, three random seeds, RouteLLM/RouterDC controls, and non-stationary and larger-zoo studies; reports about 2× average cost savings over adaptive routers |
| [Cognify paper](https://doi.org/10.1145/3711896.3736884) and [official repository](https://github.com/GenseeAI/cognify), KDD 2025 | Model selection is one step-level “cog”; the optimized result is a workflow configuration rather than a per-request router | Joint search covers task decomposition, ensembling, model selection, code rewriting, reasoning-prompt insertion, and few-shot examples | AdaSeek uses hierarchical TPE search over combinations of architecture, step, and prompt changes. Users supply a workflow, training data, evaluator, objectives/thresholds, and search budget; the output is a quality/cost/latency frontier | Paper supports LangChain, DSPy, and its own model; the current repository also advertises LangGraph. Six workflow types; reports up to 2.8× quality, 10× cost, and 2.7× latency improvements |
| [ApproxMLIR](https://proceedings.mlsys.org/paper_files/paper/2026/file/bbd3e0e9913824bbc46e7e87b11461ae-Paper-Conference.pdf), MLSys 2026 | “LLM approximation” selects among compiled model variants; the evaluation uses Gemma 3 1B and 4B | The same configuration space includes context selection/truncation, corpus subsetting, approximate term scoring, tool/kernel transformations, task skipping, and model approximation | Approximation knobs across ML and non-ML components are co-tuned with OpenTuner under an application QoS target. `approx.decision_tree` changes choices using user-provided runtime state; `approx.try` exposes a user-written check/recover contract | Requires source annotations, MLIR-compatible frontends, compiled artifacts, and a user QoS evaluator. Three compound systems, disjoint tuning/evaluation data, a 12-hour compound-system search, and up to 3.04× speedup at 9% QoS loss |
| [Agentix](https://www.usenix.org/conference/nsdi26/presentation/luo), NSDI 2026 | Routes calls among serving-engine replicas for load and KV locality; the evaluated model is fixed within each serving setup | Preemption, program-aware scheduling, KV movement, and replica placement; it does not report semantic content rewrites | Builds a dynamic program DAG and cumulative service/critical-path state from calls already completed, then applies PLAS or ATLAS scheduling without prior workload knowledge | Extends OpenAI/vLLM APIs and requires control of a modified vLLM serving stack. Four workloads and three model sizes; reports 4–15× program throughput at matched latency over vLLM |
| [Parrot](https://www.usenix.org/conference/osdi24/presentation/lin-chaofan), OSDI 2024 | Model choice is specified by the application/service setup rather than selected as a per-call optimization in the paper | Semantic Variables explicitly mark prompt inputs/outputs and dataflow; Parrot then performs contextual fill/generation, prefix/KV reuse, pipeline execution, and application-aware scheduling | The explicit Semantic Variable graph and requested performance objective expose dependencies and shared prefixes to the service | Requires Parrot's programming/API abstraction and serving system. Evaluates four application patterns and reports up to roughly order-of-magnitude latency/throughput gains |
| [Agent JIT Compilation](https://arxiv.org/abs/2605.21470), ICML 2026 | The experiments compare planner/agent models, but the optimized decision is a code plan or schedule, not per-call model routing | JIT-Planner generates several executable code plans, validates tool contracts, estimates control-flow cost, and selects a plan; JIT-Scheduler selects serial, parallel, or hedged execution from learned latency distributions | Candidate validity uses tool preconditions/postconditions; cost selection uses CFG analysis and Monte Carlo schedule estimates. Offline traces populate code and latency caches | Browser/web-agent environment. Thirty-seven tasks across five applications; reports 10.4× planner and 2.4× scheduler speedups over named agent baselines |
| [LLMLingua-2](https://aclanthology.org/2024.findings-acl.57/), Findings of ACL 2024 | No model routing | Removes prompt tokens using a bidirectional Transformer token classifier trained on an extractive-compression dataset distilled from an LLM | A requested compression ratio controls per-prompt token selection; it has no reported cross-call workflow planner or joint model-choice objective | Standalone prompt transformation evaluated in- and out-of-domain on MeetingBank, LongBench, ZeroScrolls, GSM8K, and BBH; reports 3–6× compression-method speedup and 1.6–2.9× end-to-end speedup at 2–5× compression |
| [vCache](https://proceedings.iclr.cc/paper_files/paper/2026/hash/9559cd2116de7a8f5672eac3fcd232cc-Abstract-Conference.html), ICLR 2026 | Uses a fixed response model on a cache miss; no model router is reported | Replaces a fresh inference with a semantically matched cached response | Learns a threshold per cached embedding online. Uncertain queries trigger a full inference that labels whether reuse would have matched; probabilistic exploration enforces a user-selected error bound under the paper's i.i.d. and sigmoid-likelihood assumptions | Three embeddings, two LLMs, five datasets/four released benchmarks; compares static and fine-tuned embedding baselines and reports up to 12.5× higher hit rate and 26× lower error |
| [Opportunity Is Not Realizability](https://arxiv.org/abs/2608.08265), August 2026 preprint | Diagnostic study, not a routing system: separates a full-outcome model oracle, a Bayes-optimal router restricted to declared pre-answer signals, and held-out learned-router gain | No rewrite mechanism | Provides selection-valid intervals after choosing a best fixed model or best policy family and a signal-information bound | Eight checkpoints from six families on four benchmarks. The strongest tested prompt router recovers only 7.5–14.4% of the oracle gap; the simultaneous lower interval for the best of eleven policies is zero on every task |

### Inferences for Agentc, not claims made by the cited papers

| Proposed claim | Assessment from the verified matrix | Consequence |
|---|---|---|
| “We jointly optimize model choice and semantic rewrites” | **Already occupied at the generic configuration-search level.** Cognify searches model and prompt/workflow changes together; ApproxMLIR co-tunes LLM and context/tool approximations and emits runtime decisions; FrugalGPT names joint prompt/LLM selection as a composition | Do not claim firstness for joint selection. Treat these as the conceptual predecessors even when their deployment boundaries differ |
| “We are the first transparent client-side optimizer” | **Unavailable.** AgentOpt intercepts at a broader HTTP/proxy boundary and already couples workflow-level model selection with per-call routing infrastructure and exact caching | Narrow the boundary claim to supported SDK traffic unless Agentc implements and measures a comparable wire-level path |
| “We provide online quality-constrained planning” | **Potentially differentiating only with a new mechanism.** MESS+ already provides online cost/SLA-constrained routing, and vCache provides an online error-bounded cache decision under explicit assumptions. Agentc's five-strike divergence circuit breaker is not an equivalent task-quality constraint | State the precise risk signal, horizon, feedback delay, assumptions, and damage budget. Compare against MESS+-style routing and vCache-style calibrated decisions where applicable |
| “We recover and optimize program structure without migration” | **Potentially differentiating.** Parrot receives explicit semantic variables; ApproxMLIR receives annotations; Agentix reconstructs runtime DAG state but controls a serving engine; Cognify reads a workflow it is allowed to rewrite | Measure how much structure Agentc can infer from untouched opaque calls, with precision/recall, abstention, and downstream utility—not anecdotes |
| “We perform JIT execution planning” | **Unavailable with the current implementation.** Agent JIT selects and executes code plans/schedules; Agentix executes scheduling decisions. Agentc's current planner rewrites a call and emits a concurrency certificate without an integrated asynchronous executor | Use “runtime rewrite selection” until chosen plans materially alter execution and are measured end to end |
| “Joint planning beats point solutions” | **Open and testable, but not shown.** No source in this focused set reports the exact combination of opaque-boundary per-call model routing, semantic message/state rewrites, and online counterfactual damage control | This is the useful experiment. A capability intersection is not a contribution until one integrated policy beats strong independent/sequential policies at matched information and quality |

The proposed research object should be an explicit plan

> `plan = (model, enabled rewrites, rewrite operating points, execution choice)`

chosen from information available **before** the optimized call: prompt features, call-site history, trace/provenance facts, observed cost/latency, and persisted guard state. The proposed objective should minimize billed cost and/or latency subject to a predeclared end-to-end quality-loss margin and cumulative damage budget. It should predict interactions, reject infeasible plans, and abstain when confidence is insufficient.

That proposal is materially deeper than the current CompositionPlanner. Today, ModelDowngrade changes the model after message rewrites have been admitted by static compatibility; projected savings are ranked independently; fixed ordering resolves execution; and the guard reacts after sampled outputs diverge. An interaction-aware planner would have to learn or conservatively bound facts such as whether a cheaper model is more sensitive to a particular compression level, whether rewriting changes the routing signal, whether two individually safe passes jointly cross the quality margin, and whether shadow cost erases the chosen plan's savings.

### Baseline roles and matching requirements

| Source | Baseline role for the joint thesis | Fair matching protocol |
|---|---|---|
| AgentOpt | **Runnable full-system baseline** for fixed role-level combinations, per-call model routing, HTTP interception, tracking, and exact caching | Use the same model pool, task split, evaluator, selection budget, cache policy, provider prices, and eligible traffic. Report AgentOpt fixed-combination selection and its per-call router separately; do not compare Agentc online routing only to AgentOpt's offline selector |
| RouteLLM | **Runnable point baseline** for prompt-conditioned strong/weak routing | Use the same strong/weak pair, exact prompt seen by the router, target quality point, and held-out distribution. Report the full strong-call/quality/cost curve and router overhead |
| FrugalGPT | **Runnable cascade baseline** when repeated model calls are permitted | Charge every attempted model response and scorer call; match the training set and total calibration budget. Report both quality and tail latency, since a cascade and a single-call router have different latency semantics |
| MESS+ | **Required online-controller baseline or controlled reimplementation** if Agentc claims online SLA/risk-constrained routing | Match the model zoo, request order, satisfaction signal and delay, feedback density, SLA target, exploration cost, and non-stationarity. If opaque APIs prevent energy-equivalent replication, compare billed cost and label the systems-boundary mismatch |
| Cognify | **Runnable offline joint-search baseline** on supported LangChain/LangGraph/DSPy workflows; otherwise capability comparison | Give Cognify the same candidate models, prompt/rewrite choices where expressible, training examples, evaluator, and search-dollar budget. Amortize tuning cost over an explicitly stated deployment volume |
| LLMLingua-2 | **Runnable semantic-rewrite baseline** for compression | Compare at matched achieved token reduction and matched quality, not at nominal compression settings; include compressor CPU/GPU latency and memory |
| vCache | **Runnable cache baseline** if semantic CacheHit remains claimed | Replay the identical ordered query stream, embedding model, response model, cache capacity, and user error budget; report false reuse, hit rate, convergence, and all exploration inferences |
| ApproxMLIR, Agentix, Parrot, Agent JIT | **Usually cite-only architectural controls** because they require compilation, explicit APIs, or serving-engine control | Do not manufacture apples-to-apples numbers. Use a capability table, or one explicit orthogonality/ported-workflow experiment that discloses migration and hardware differences |

### The experiment that would establish a joint-planning contribution

1. **Construct a bounded factorial oracle on calibration data.** For each natural workload, execute at least three model tiers against every safe combination of the two or three semantic rewrite families that naturally activate, including multiple compression/budget operating points. Measure final task outcome, all model/shadow calls, dollars, input/output tokens, and end-to-end latency. This is necessary to identify whether model-by-rewrite interactions exist; projected independent savings cannot answer it.
2. **Compare policy classes, not just features.** Include unmodified strong and cheap models, best fixed model, best fixed rewrite, RouteLLM or MESS+ route-only, AgentOpt fixed-combination and online routing, the best static model-by-rewrite configuration, route-then-rewrite, rewrite-then-route, the current greedy CompositionPlanner, the proposed joint planner, and the calibration oracle. `CacheHit` should remain a separate terminal action because it eliminates the model call rather than composing normally.
3. **Match information and optimization budgets.** Every learned policy must receive the same calibration examples, labels/feedback, model pool, price table, and search-dollar ceiling. Charge offline search, online exploration, embeddings, judges, and shadow calls, then show break-even volume. Distinguish policies using only the current prompt from policies that see trace history or explicit provenance.
4. **Extend the routing diagnostics to plans.** This is an inference from Opportunity Is Not Realizability, not a method claimed by that paper: report (a) the full-outcome model-by-rewrite oracle gap, (b) the best gain available from a declared pre-call signal, and (c) untouched-test gain of the selected planner. Use selection-valid simultaneous intervals after choosing among models, rewrites, thresholds, and policy families. A large oracle gap is not evidence that Agentc can realize it.
5. **Report interaction and regret directly.** For each model/rewrite pair, report the departure from the sum of their isolated effects on cost, latency, and quality; then report planner regret to the feasible oracle. The contribution requires nontrivial interaction: if the best joint plan is always “use the independently chosen router and then run the same rewrite,” a unified optimizer has not earned its complexity.
6. **Stress delayed and drifting feedback.** Evaluate immediate task labels, delayed end-of-trajectory labels, the current label-free divergence signal, sparse feedback, model/version drift, and restarts. MESS+'s immediate binary request feedback and vCache's fresh-response comparison are stronger signals than many live agents provide; the paper must not silently assume them.

For the joint thesis to survive, the proposed planner must beat the strongest independent/sequential policy at matched quality on at least two unengineered workload families, not merely approach a projected additive ideal. It must also retain the advantage after calibration, exploration, and shadow costs and under a held-out, selection-valid analysis. Until those conditions are met, joint model-by-rewrite planning belongs in future-work positioning, not the abstract or contribution list.

## What is commoditized and what may still be novel

| Status in 2026 | Capability | Implication for the paper |
|---|---|---|
| Commoditized substrate | SDK/HTTP interception, token/cost/latency tracing, exact replay caching, per-call policy hooks | Describe as engineering and deployment mechanism. AgentOpt is broader at the wire boundary than Agentc's current OpenAI/Anthropic Python SDK patching |
| Established research area | Strong/weak model routing, cascades, per-query model choice | ModelDowngrade is a pass inside the system, not a paper contribution by itself. Use RouteLLM/FrugalGPT/AgentOpt baselines |
| Established research area | Joint offline search over model, prompt, context, and workflow configurations | Cognify and ApproxMLIR make generic joint-selection firstness unavailable. The possible contribution is an online, call-conditional policy for opaque traces that models rewrite interactions and enforces an explicit risk contract |
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
| “Across three model providers” | The revised protocol now names and prices Z.ai models hosted by Together through LiteLLM, but a hosting route is not equivalent to a third closed-provider protocol | Name the exact models, hosts, API protocols, catalog observation, and pricing source | Run pinned identifiers on at least two closed-provider protocols plus one hosted or self-hosted open-weight stack, and report these categories separately |
| “Automatic label-free accuracy guard” | Output divergence is not task accuracy. Lexically different correct answers and lexically similar wrong answers defeat it | “A label-free sampled output-divergence circuit breaker” | Calibrated semantic risk, human/task-label audit, false-negative/false-positive curves, and non-inferiority at a declared damage budget |
| “Bounded 2% shadow-sampling cost prevents 97% of damage” | The showcased guard experiments use dense or effectively full shadowing; at 2%, five consecutive violations can require hundreds of eligible calls and each shadow is a synchronous billed request | State the measured sampling rate for every experiment and separately project—not claim—2% deployment overhead | Run 0/1/2/5/10/100% sampling with real stochastic timing, sequential detection analysis, cumulative loss before disable, retained savings, p95/p99 latency, and dollars |
| “Fail-open safety” | Fail-open protects availability, not semantic correctness or external tool effects | “Exceptions preserve baseline request execution” | Pair with effect-aware or task-aware safeguards; do not conflate operational fallback with safe agent behavior |
| “95.2% of additive ideal” | (n=20), projected cost arithmetic, and no powered quality result; this is mechanistic | “A small orthogonality check reaches 95.2% of the projected additive ideal” | End-to-end measured composition on natural workloads, multiple seeds, CIs, best-single and naïve-sequential controls |
| “Composition planner” | The implementation uses fixed driver compatibility and ordering; it is not a search/learned global optimizer | “A deterministic compatibility and ordering policy” | Cost-quality-risk objective, uncertainty, counterfactual alternatives, and evidence it chooses better plans than strong policies |
| “Joint model-routing and semantic-rewrite optimizer” | Cognify and ApproxMLIR are direct generic joint-configuration prior art, while the current Agentc planner does not implement or measure interaction-aware constrained selection | “A proposed online model-by-rewrite planning direction at an opaque API boundary” | A held-out factorial study; comparison with AgentOpt, Cognify, route-only, rewrite-only, sequential, static-joint, and current-greedy policies; selection-valid gains after tuning, exploration, and shadow cost |
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
| Joint-planner value | On at least two unengineered workloads, the selected joint model-by-rewrite policy beats route-only, rewrite-only, the best static joint configuration, both sequential orders, and the current greedy planner at matched quality after charging search, exploration, and shadow costs; its selection-valid 95% lower bound on the primary benefit is positive, and at least one workload exhibits a held-out model/rewrite rank reversal or other material non-separability |
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
- Cognify, KDD 2025: https://doi.org/10.1145/3711896.3736884
- Cognify official repository: https://github.com/GenseeAI/cognify
- FrugalGPT, TMLR 2024: https://openreview.net/forum?id=cSimKw5p6R
- RouteLLM, ICLR 2025: https://proceedings.iclr.cc/paper_files/paper/2025/hash/5503a7c69d48a2f86fc00b3dc09de686-Abstract-Conference.html
- MESS+, NeurIPS 2025: https://papers.neurips.cc/paper_files/paper/2025/hash/4dd1d9b841712bd37b833559f041530c-Abstract-Conference.html
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

### Provider model and price records

- OpenAI GPT-5.4: https://developers.openai.com/api/docs/models/gpt-5.4
- OpenAI GPT-5.4 mini: https://developers.openai.com/api/docs/models/gpt-5.4-mini
- Anthropic model overview: https://platform.claude.com/docs/en/about-claude/models/overview
- Anthropic pricing: https://platform.claude.com/docs/en/about-claude/pricing
- Together serverless model catalog: https://docs.together.ai/docs/serverless/models
- Together model deprecations: https://docs.together.ai/docs/deprecations
- LiteLLM provider routing: https://docs.litellm.ai/
