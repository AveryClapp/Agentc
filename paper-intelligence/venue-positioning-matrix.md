---
title: Venue Positioning Matrix
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Venue Positioning Matrix

This file tracks where the Agentc paper might fit. Venue data must be verified against official pages before submission planning.

## Current Status

Venue deep research was ingested as `DRP-002` on 2026-05-09. Top venue facts for MLSys 2026, ATC 2026, COLM 2026, and ICML 2026 Agents in the Wild were spot-checked against official pages on 2026-05-09. Treat future-cycle dates as watch items until official CFPs are posted.

## Primary Venue Shortlist

| ID | Venue | Recommendation | Best Framing | Current Readiness | What Must Improve | Verified Notes |
|---|---|---|---|---|---|---|
| `VEN-009` | ATC 2026 operational systems track | most actionable near-term main venue | practical runtime/optimizer with operational lessons | possible but needs concentrated work | overheads, failure modes, deployment/trace story, extended abstract | Official deadline: 2026-06-10. Long 12 pp, short 6 pp, required 2-page extended abstract, double-blind. |
| `VEN-001` | MLSys | best long-run topical fit | runtime optimizer for compound AI systems | promising but not yet comfortable | end-to-end optimizer result, artifact readiness, latency/overhead, baseline comparisons | 2026 scope includes compound AI systems, AI agent systems, efficient inference/serving, ML compilers/runtimes, and tooling/benchmarks; 2026 deadline already passed. |
| `VEN-010` | COLM | best LM-native fit | LM cost-quality optimizer and stochastic evaluation methodology | promising but next cycle | stronger LM-facing evaluation, cost-quality frontier, comparisons to routing/compression baselines | 2026 deadline already passed; 9-page main text; double-blind. |
| `VEN-011` | ICML 2026 Agents in the Wild workshop | strong workshop fit, but deadline passed | agent systems/infrastructure and evaluation | missed for 2026 | use as style/feedback reference, not current target | Official deadline was 2026-05-08 AoE; as of 2026-05-09 New York this is effectively closed. |

## Top-Line Decision

If the goal is a real 2026 main submission, build toward `VEN-009` first. If the goal is best paper fit after strengthening, build toward `VEN-001`. If the goal is language-model community fit, build toward `VEN-010`.

## Post-June Reality Check

The first venue search did find later opportunities, but most are weaker fits than ATC/MLSys/COLM. `DRP-003` adds a richer post-June venue plan; those facts are still raw and must be verified against official pages before submission planning.

| Window | Venue | Fit | Why Weak Right Now |
|---|---|---|---|
| July 2026 | AAAI-style broad AI deadlines/watch items | weak-to-possible | AgentC currently reads more like systems/runtime infrastructure than a broad AI-method paper. |
| August 2026 | CIDR 2027 | weak-to-possible for a visionary architecture piece | Needs a data-systems/query-optimizer framing, not the current empirical optimizer paper. |
| September 2026 | NSDI 2027 fall deadline | weak | Networked/distributed systems fit is awkward unless AgentC becomes a distributed agent-serving/control-plane paper. |
| September 2026 | EuroSys-style fall cycle/watch item | possible but hard | Needs deeper systems novelty, overhead, artifact, and end-to-end evidence. |
| Monthly into 2027 | VLDB/SIGMOD-style database venues | weak | Needs a clear data-management contribution; current paper is LLM runtime optimization. |

## DRP-003 Post-June Target Update

Raw post-June venue planning now suggests this working order:

| Lane | Status | Why It Matters | Evidence Needed |
|---|---|---|---|
| MLSys 2027 | strongest long-run topical target, CFP not yet verified for 2027 | Best match for runtime optimizer / agentic AI systems / ML systems framing. | artifact polish, end-to-end optimizer evidence, overhead/tail latency, baseline comparisons |
| AAAI 2027 | plausible broad-AI target, CFP not yet verified for 2027 | Could work if AgentC is framed as generally useful agent infrastructure. | broader evaluation, behavior-preservation story, clear rewrite model |
| ICLR 2027 | possible if conceptual story improves, CFP not yet verified for 2027 | Needs a reusable methodology/abstraction story beyond engineering savings. | stronger conceptual model, broad benchmarks, surprising optimizer insights |
| EuroSys 2027 | confirmed fall systems lane per raw research, needs verification | Strong systems audience if runtime evidence is mature. | overhead, scaling, multi-framework evidence, rule interactions, deployment realism |
| NSDI 2027 | high-risk systems lane per raw research, needs verification | Possible only with a networked/distributed/frontiers angle. | realistic multi-call orchestration, latency/cost tradeoffs, distributed-systems relevance |
| CIDR / ARR | short-paper fallback | Useful if the full systems story is not ready. | concise argument, narrow claim, strongest available evidence |

## Candidate Venue Families

| ID | Venue Family | Status | Audience | Likely Fit | Evidence Bar | Main Risks | Next Action |
|---|---|---|---|---|---|---|---|
| `VEN-001` | ML systems, e.g. MLSys | `strong-fit-watch` | ML systems and infrastructure | strong topical fit | strong systems framing, reproducibility, cost/latency/quality evaluation | novelty vs serving/inference work; benchmark breadth | watch for MLSys 2027 CFP |
| `VEN-002` | Systems, e.g. OSDI/SOSP/NSDI/ATC/EuroSys | `mixed` | systems reviewers | ATC strongest, OSDI/SOSP/NSDI weak right now | deep systems contribution, robust artifacts, scale | current evidence may look too application-level or small-scale | prioritize ATC/EuroSys over OSDI/SOSP/NSDI |
| `VEN-003` | NLP/LLM, e.g. ACL/EMNLP/NAACL/ARR/COLM | `possible` | NLP/LLM application and evaluation reviewers | COLM strongest | clear related work, LLM evaluation methodology, strong baselines | systems contribution may feel outside core NLP | keep COLM as LM-native target |
| `VEN-004` | AI/ML, e.g. NeurIPS/ICML/ICLR | `weak-main-possible-workshop` | broad ML audience | workshop path stronger than main venue | novelty, rigorous evaluation, strong positioning | may need more theory or broader benchmark evidence | monitor workshops; do not make main venue default |
| `VEN-005` | Agent/compound-AI workshops | `strong-workshop-fit` | agent systems and LLM workflow researchers | strong possible fit | clear agent-workload framing, practical results | deadlines/page limits vary and may be passed | maintain workshop watchlist |
| `VEN-006` | Efficient LLM / model routing / serving workshops | `strong-workshop-fit` | efficient inference and routing community | strong possible fit | cost-saving evidence, comparison to routing/compression/caching | must distinguish runtime layer from provider serving | collect current workshop shortlist |
| `VEN-007` | Software engineering / AI tooling | `possible` | developer tools and empirical SE | possible if framed as agentic engineering | transparent integration story, tool evaluation | may need user/developer study or broader tool tasks | consider ICSE agentic engineering workshops |
| `VEN-008` | HCI/tooling, e.g. CHI/UIST/IUI | `not-recommended-now` | human-centered AI tools | weak fit for current paper | user workflow evidence | current evidence is systems/evaluation, not user study | deprioritize unless framing changes |

## Venue Evaluation Fields

For each researched venue, add:

- official link
- current or most recent deadline
- page limit
- anonymity policy
- artifact policy
- contribution type expected
- reviewer persona
- required evidence
- current Agentc readiness
- missing experiments or writing work
- recommendation: `strong-fit`, `possible-fit`, `weak-fit`, `not-recommended`

## Immediate Follow-Up

- Decide whether the near-term lane is ATC 2026 or longer-run MLSys/COLM.
- Link `GAP-011`, `GAP-012`, `GAP-014`, and `GAP-015` into experiment planning.
- Verify every venue again before any actual submission decision.
