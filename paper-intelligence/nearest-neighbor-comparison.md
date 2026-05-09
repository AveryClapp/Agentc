---
title: Nearest Neighbor Comparison
status: draft
last-updated: 2026-05-09
owner: paper-intelligence
---

# Nearest Neighbor Comparison

This matrix will compare Agentc against the closest related systems after literature ingestion.

| LIT ID | Work/System | Optimization Target | Intervention Point | Transparent/No-Code-Change | Runtime vs Offline | Agent/Workflow Scope | Savings Metric | Quality Metric | Agentc Distinction |
|---|---|---|---|---|---|---|---|---|---|
| `LIT-024` | Agentix / Autellix | agentic-program execution/scheduling | serving/runtime scheduler with program context | partial; intercepts agent programs but is serving-centered | runtime | agent programs | throughput/latency | task quality / scheduling impact | closest serving-layer threat; AgentC must show semantic rewrite rules above API/server scheduling |
| `LIT-025` | Halo | batch agent-workflow DAG/query-plan optimization | workflow/query optimizer | no clear public artifact verified | runtime/batch | agent workflows | shared computation/cache/GPU placement | task quality | close systems framing; AgentC distinction depends on online SDK/API interception and concrete rewrite passes |
| `LIT-007` | FrugalGPT | model/API cost | routing/cascade controller | no, separate policy | runtime/query-time | mostly single-query | cost | task quality | ModelDowngrade prior art; AgentC wraps routing as one pass inside trace optimizer |
| `LIT-008` | Optimizing Model Selection for Compound AI Systems | per-component model choice | compound-system model selector | needs verification | likely offline/online mixed | compound systems | cost/quality | quality | direct model-selection comparison; AgentC spans more rewrite classes |
| `LIT-021` | LLMCompiler | parallel function/tool execution | planner/compiler | no, changes execution plan/prompting | compile/planning time | tool-call graphs | latency/cost | task success | ParallelBranch prior art; AgentC claims transparent trace-level pass |
| `LIT-009` | RouteLLM | model routing | query router | no, router layer | runtime | query-level | cost | preference/quality | direct ModelDowngrade baseline; not full agent-runtime optimizer |
| `LIT-011` | Language Model Cascades | composed LM calls/control flow | probabilistic-programming style cascade framework | no, explicit framework | runtime/query-time | LM call programs | call efficiency | task quality | useful trace-composition background, but not a direct ModelDowngrade/routing baseline |
| `LIT-013` | LLMLingua | prompt token compression | prompt compressor | no, explicit compressor | pre-call/runtime | prompt/text | token reduction | task accuracy | direct ContextCompress baseline; not message-trace/state-aware runtime rewrite |
| `LIT-017` | GPTCache | repeated prompt reuse | semantic cache | yes-ish at app layer | runtime | prompt/query | latency/cost | cache correctness | direct CacheHit baseline; not multi-rule optimizer |
| `LIT-022` | ReWOO | reasoning/observation interleaving | prompt/program structure | no, changes prompting pattern | planning/runtime | tool-using reasoning | token usage | task success | adjacent execution-pattern rewrite, not transparent interception |
| `LIT-023` | ALTO | compound pipeline execution | orchestrator/scheduler | needs verification | runtime | compound pipelines | latency/throughput | TBD | systems comparison for orchestration, not semantic rewrite suite |
| `LIT-040` | Towards Resource-Efficient Compound AI Systems / Murakkab | compound-AI workflow resource efficiency | declarative workflow + adaptive runtime | not transparent to arbitrary existing code | runtime/adaptive scheduling | compound systems | speed/energy/resource use | task quality | major threat to broad runtime-optimizer claim; AgentC must emphasize online trace rewrite and concrete multi-rule passes |
| `LIT-043` | AIOS | agent execution runtime | agent OS scheduler/context/memory/tool layer | no, uses AIOS abstractions | runtime | agent systems | scheduling/context/resource metrics | task quality | closest OS/runtime analogy; AgentC is narrower and focused on trace rewrite/control |
| `LIT-044` | Cognify | Gen-AI workflow autotuning | hierarchical autotuner over structure/operator/model/prompt choices | no, workflow must be optimized through Cognify | mostly offline/autotuning plus deployment | workflows | quality/latency/cost | task quality | close optimizer threat; AgentC distinction is runtime interception and trace rewrites rather than evaluator-driven autotuning |
| `LIT-041` | LMQL | language-model programs | query language and optimizing runtime | no, author uses LMQL | runtime/compiler | LM programs | latency/cost/control constraints | task output | predecessor for LM-program runtime language; AgentC does not require rewriting apps into a new language |
| `LIT-060` | LLM-Tool Compiler | tool-call fusion and parallel function calling | compiler/planner | no, changes planning/tool representation | compile/planning time | tool-call graphs | latency/token cost | task success | direct ParallelBranch threat; AgentC must distinguish runtime trace interception |
| `LIT-055` | vCache | verified semantic prompt caching | cache layer with correctness/error controls | app/serving cache layer | runtime | prompts/queries | cache hit/cost/latency | verified error bounds / false-hit control | raises correctness bar for CacheHit; AgentC needs false-hit and context-key story |
| `LIT-036` | SGLang | structured language-model program execution | LM-program language + optimized runtime | no, workloads are written/ported into SGLang | runtime/compiler | structured LM programs | throughput/latency/KV reuse | task output | serious systems neighbor; AgentC should emphasize framework-emitted trace optimization without rewriting into SGLang |

## Use

This artifact protects the paper from weak novelty claims. Do not claim novelty without checking this matrix.
