---
title: Deep Research Drop - Full Literature Review Map V2
status: active
last-updated: 2026-05-09
owner: paper-intelligence
---

# Deep Research Drop - Full Literature Review Map V2

## Source

User pasted a second full-paper literature-review response in chat on 2026-05-09. The response used model-internal citation handles, so all sources below are still candidates until checked against primary sources.

## Extracted Takeaway

This second literature pass strengthens the same central framing: AgentC is not mainly a routing paper. It should be positioned as a runtime optimizer for compound AI systems and multi-step LLM agent traces. The new result adds several stronger nearest-neighbor threats and more specific baselines than the first pass.

## New Or Strengthened Novelty Threats

- **Murakkab / Towards Resource-Efficient Compound AI Systems**: workflow/runtime co-design for resource-efficient compound AI; major threat to broad "runtime optimizer" claims.
- **AIOS**: agent operating-system framing with scheduling, context management, memory, and access control.
- **Cognify**: workflow optimization across quality, latency, and monetary cost.
- **DSPy / LMQL / SGLang**: LM programs plus compiler/runtime optimization language.
- **LLMCompiler / LLM-Tool Compiler**: direct threat to ParallelBranch and compiler-inspired tool-call parallelization.

## New Or Strengthened Rewrite-Family Sources

- **Routing**: Large Language Model Routing with Benchmark Datasets, plus stronger emphasis on FrugalGPT, RouteLLM, RouterBench, and compound-system model selection.
- **Compression**: LLMLingua-2, tool-using context compression, TACO-RL, and prompt-compression surveys.
- **StateDrop**: program dependence graphs, SSA/control dependence, program-slicing survey, and MemGPT as memory-management context.
- **CacheHit**: vCache, semantic-cache eviction/adaptation, classical semantic caching, and self-adjusting computation.
- **ParallelBranch**: LLM-Tool Compiler and LLMOrch-style function orchestration.
- **Serving orthogonality**: Sarathi-Serve is added alongside Orca, vLLM, DistServe, Prompt Cache, and SGLang.
- **Evaluation**: pass@k, LLM-judge bias papers, JudgeBench, Leaderboard Illusion, Don't Pass@k, one-run reproducibility, and SWE-rebench.

## Main Paper Implications

- Narrow the novelty claim around online trace-time control and multi-rewrite runtime integration.
- Treat individual rewrite families as established prior art.
- Prioritize verification of Murakkab, AIOS, Cognify, DSPy, LMQL, SGLang, LLMCompiler, LLM-Tool Compiler, RouteLLM, LLMLingua family, vCache, ContextCache, and stochastic-evaluation sources.
- Add runnable-baseline decisions for routing, compression, caching, and parallelization.
- Strengthen StateDrop with compiler/program-analysis language and explicit caveats.

## Promotions Made

- Add new `LIT` rows for sources surfaced only in this second pass.
- Update `literature-blurb-todo.md` with addendum blurbs and scoring for the new candidates.
- Update related-work and nearest-neighbor maps with the strongest new threats.

