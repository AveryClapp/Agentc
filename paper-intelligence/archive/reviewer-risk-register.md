---
title: Reviewer Risk Register
status: active
last-updated: 2026-05-08
owner: paper-intelligence
---

# Reviewer Risk Register

This register predicts objections before manuscript drafting. It turns weak spots into visible work items.

## Risk Levels

- `blocker`: likely to break a central claim if unresolved.
- `high`: likely reviewer objection; must be answered or scoped.
- `medium`: important but can be handled in limitations or appendix.
- `low`: useful polish or future hardening.

## Risks

| ID | Level | Likely objection | Current answer | Evidence needed | Mitigation path | Linked gap |
|---|---|---|---|---|---|---|
| `RR-001` | `high` | Workloads may look purpose-built for each rule. | The paper can frame workloads as targeted stress tests for common agent inefficiencies. | Related-work support and workload rationale. | Add workload taxonomy and state why each task isolates a realistic pattern. | `GAP-007` |
| `RR-002` | `high` | Accuracy preservation is under-tested. | Current CSVs include accuracy, but uncertainty and paired tests are not fully integrated. | Paired tests, row-level joins, confidence intervals. | Execute `EXP-003`; keep strong claims tied to cost with quality guardrails. | `GAP-004` |
| `RR-003` | `medium` | StateDrop savings are smaller and accuracy metric may be lenient. | Treat StateDrop as promising but not equal headline evidence. | Better metric or rerun; completion of n=50 matrix. | Finish `EXP-001` and possibly `EXP-004`. | `GAP-002`, `GAP-005` |
| `RR-004` | `medium` | Real HotpotQA near-zero savings weakens ContextCompress. | It actually supports activation-boundary behavior: the rule declines when compression is not profitable. | Clear explanation and result status. | Present as diagnostic/gating evidence, not headline savings. | `GAP-003` |
| `RR-005` | `high` | Related work may already have close analogs. | Verified blurbs found major threats: Agentix/Autellix, Halo, Murakkab, AIOS, Cognify, DSPy, LMQL, SGLang, LLMCompiler, LLM-Tool Compiler, vCache, and single-rewrite baselines. | Final nearest-neighbor comparison and runnable-baseline decisions. | Tighten `nearest-neighbor-comparison.md`; narrow novelty wording. | `GAP-007`, `GAP-010` |
| `RR-006` | `medium` | OpenAI prompt caching and pricing can confuse savings interpretation. | Report both cost and input-token savings; separate input-token reduction from billed-cost effects. | Pricing assumptions and accounting notes. | Add pricing appendix note if manuscript begins. | `STAT-003` |
| `RR-007` | `medium` | Rule activation policy may sound heuristic. | It is intentionally conservative: hot threshold, rule-specific preconditions, safety check, pass-through default. | Code references and diagrams. | Link planner and rule code in system brief; include rule activation map figure. | `FIG-002` |
| `RR-008` | `medium` | CacheHit and ParallelBranch may distract if not benchmarked. | Do not present them as headline empirical contributions until results exist. | Explicit descoping language. | Put in future work or implementation inventory with status labels. | `CLM-006` |
| `RR-009` | `high` | ModelDowngrade may look like ordinary model routing/cascading. | AgentC routes internal call sites in traces, not just user queries. | RouteLLM/FrugalGPT/LLMSelector comparison and call-site policy evidence. | Present routing as one runtime pass inside a broader optimizer. | `GAP-010`, `GAP-012` |
| `RR-010` | `high` | ContextCompress may look like LLMLingua/LongLLMLingua with a new name. | AgentC performs runtime message-trace rewriting, not standalone text compression. | Compression baseline comparison or sharp conceptual distinction. | Decide whether to run baseline or cite as discussion-only comparator. | `GAP-010`, `GAP-012` |
| `RR-011` | `medium` | CacheHit may be unsafe in multi-turn or stateful contexts. | CacheHit should require call-site/state-aware keys and conservative invalidation. | Context-aware semantic-cache citations and implementation evidence. | Keep CacheHit out of headline claims until correctness story is explicit. | `CIT-004` |
| `RR-012` | `medium` | ParallelBranch independence may be unsound with side effects or hidden dependencies. | The paper should not claim broad safe parallelization without dependency model evidence. | LLMCompiler/ReWOO/ALTO comparison and side-effect policy. | Treat as future work unless strong detector evidence exists. | `CIT-005` |
| `RR-013` | `high` | Agentix/Halo/Murakkab/Cognify or serving systems may subsume the runtime story. | AgentC works above the model server/API layer and rewrites application semantics; serving systems optimize execution internals. | Final orthogonality comparison using verified blurbs. | Make `application-level call semantics` and multi-rule trace rewriting the core distinction. | `GAP-010`, `GAP-016` |
| `RR-014` | `high` | Single-run evaluation is underpowered for stochastic LLM behavior. | Current result ledger is useful but needs paired uncertainty or repeated-run framing. | Evaluation-source-backed protocol plus row-level analysis. | Elevate `GAP-014`; avoid “proves behavior preservation” wording. | `GAP-004`, `GAP-014` |

## Review Output Format

When an agent reviews new results, literature, or draft briefs, it should report:

- highest-risk unsupported claim;
- which `RR` and `GAP` IDs it affects;
- cheapest mitigation;
- strongest mitigation;
- whether API tokens are needed.
