---
title: Research Prompts
status: active
last-updated: 2026-05-08
owner: paper-intelligence
---

# Research Prompts

**Purpose:** reusable prompts for collecting literature, positioning, venue fit, reviewer-risk evidence, and red-team feedback for the AgentC paper intelligence workspace.

These prompts are for research support, not final manuscript writing. The expected output should be notes, maps, citation candidates, gap lists, and recommendations that a human can use while writing the paper by hand.

Supersedes:

- `deep-research-prompt-templates.md`
- `red-team-review-prompts.md`
- `idea-generation-protocol.md`

## Prompt Index

| Prompt | Use |
|---|---|
| Prompt 1 | Broad literature map. |
| Prompt 2 | Closest related work and differentiation. |
| Prompt 3 | Rule-specific literature review. |
| Prompt 4 | Evaluation methodology and reviewer risk. |
| Prompt 5 | Literature review for paper positioning. |
| Prompt 6 | Citation gap filler. |
| Prompt 7 | Conference and venue fit. |
| Prompt 8 | Workshop finder. |
| Prompt 9 | Post-June venue sweep. |
| Prompt 10 | Full paper literature review sweep. |
| Prompt 11 | Routing comparators and similar papers. |
| Prompt 12 | Deep research digest to repo notes. |
| Prompt 13 | Skeptical systems reviewer. |
| Prompt 14 | Skeptical ML evaluation reviewer. |
| Prompt 15 | Related-work reviewer. |
| Prompt 16 | Reproducibility reviewer. |
| Prompt 17 | Clarity reviewer. |
| Prompt 18 | Idea generation pass. |

## General Rules For All Research Prompts

Use these instructions at the top of any deep-research request:

```text
You are helping with paper intelligence for Agentc, a JIT optimization runtime for multi-step LLM agent workloads. Agentc intercepts LLM calls and applies rewrite rules such as ContextCompress, ModelDowngrade, StateDrop, CacheHit, and ParallelBranch to reduce cost while preserving task behavior.

Do not write final manuscript prose. Produce research notes that a human author can use.

Requirements:
- Prefer primary sources: papers, official proceedings pages, official CFPs, official project docs, arXiv/OpenReview/ACM/USENIX/ACL Anthology pages.
- Provide links for every paper, venue, or factual claim.
- Separate confirmed facts from your interpretation.
- Mark uncertainty explicitly.
- Avoid broad citation dumps. Prioritize work that changes how we should position, evaluate, or defend Agentc.
- For every recommended paper/source, explain exactly what claim it supports or what risk it creates for Agentc.
- End with concrete follow-up actions for the Agentc repo's paper intelligence files.
```

## Prompt 1: Broad Literature Map

Use this when starting the literature review from scratch.

```text
I am preparing a paper about Agentc, a runtime optimizer for multi-step LLM agent workloads. It intercepts LLM API calls from agent frameworks and applies cost-saving rewrite rules:

- ContextCompress: drops low-salience prompt/context messages when large prompts have enough unused context.
- ModelDowngrade: routes hot, simple/structured call sites to cheaper models when quality risk is within budget.
- StateDrop: removes stale state-tagged messages that the current execution window does not read.
- CacheHit: reuses prior responses from a semantic memoization cache.
- ParallelBranch: identifies independent sibling calls / parallelizable branches.

Please build a literature map for this topic.

Focus areas:
1. LLM agent frameworks and compound AI systems.
2. Runtime optimization for LLM applications.
3. Model routing / cascade / model selection.
4. Prompt or context compression.
5. Semantic caching / memoization / request deduplication.
6. Tool-call scheduling and parallel execution.
7. Evaluation methodology for stochastic LLM systems.
8. Systems papers on serving/inference optimization that are relevant but not identical.

For each cluster, provide:
- 5-10 most relevant papers/sources.
- One-paragraph summary of the cluster.
- How Agentc is similar.
- How Agentc is different.
- What claim this cluster can support in the paper.
- What reviewer objection this cluster may create.
- Which sources are must-cite versus optional.

Output format:
1. Executive map: 8-12 bullets.
2. Cluster-by-cluster literature table.
3. Closest-work ranking: top 10 closest works to Agentc.
4. Citation gaps: claims Agentc probably cannot make without more citations.
5. Suggested updates to:
   - paper-intelligence/literature-and-nearest-neighbors.md
   - paper-intelligence/claims-gaps-and-risks.md
```

## Prompt 2: Closest Related Work And Differentiation

Use this when we need to know what Agentc is *not* allowed to claim.

```text
Find the closest existing work to Agentc and pressure-test novelty.

Agentc is a runtime optimizer for multi-step LLM agent workloads. It intercepts LLM calls, observes call-site behavior, and applies rewrite rules to reduce cost: context compression, model downgrade/routing, stale state dropping, semantic cache hits, and parallel branch detection.

Research questions:
1. What existing systems are closest to this?
2. Has anyone already built "a compiler/runtime optimizer for LLM agents"?
3. Which works combine multiple optimization rules under one runtime?
4. Which works optimize only one dimension, such as model routing or context compression?
5. Which works operate inside the model/provider layer rather than at the application/runtime layer?
6. Which works require application code changes, and which are transparent?

Produce:
- A ranked list of closest related works.
- A differentiation matrix with rows as papers/systems and columns:
  - transparent interception
  - multi-step agent workload
  - runtime call-site profiling
  - model routing
  - context compression
  - semantic caching
  - state/memory pruning
  - parallel scheduling
  - empirical cost/accuracy evaluation
- A "claims we can safely make" section.
- A "claims to avoid" section.
- A "novelty risk" section with specific reviewer objections.
- A list of must-read papers with links.
```

## Prompt 3: Rule-Specific Literature Review

Use this separately for `ContextCompress`, `ModelDowngrade`, `StateDrop`, `CacheHit`, or `ParallelBranch`.

```text
Conduct a rule-specific literature review for Agentc's [RULE_NAME] rule.

Rule description:
[PASTE RULE DESCRIPTION AND ACTIVATION CONDITIONS]

Agentc context:
- The rule is implemented as a runtime rewrite over intercepted LLM calls.
- It is selected by a planner only after a call site is hot.
- It must pass safety checks and stay within an accuracy/divergence budget.
- It is evaluated using benchmark workloads and ablation matrices.

Please research:
1. Closest papers/systems for this specific optimization.
2. How prior work measures quality preservation or accuracy loss.
3. What activation gates or safety checks prior work uses.
4. Whether prior work operates at prompt level, model level, application level, runtime level, or serving-provider level.
5. What baselines and metrics reviewers will expect.
6. Whether Agentc's current evaluation is strong enough for this rule.

Output:
- Short summary of the literature area.
- Table of 10-20 relevant papers/sources.
- What Agentc can cite each source for.
- Evaluation expectations created by this literature.
- Gaps in Agentc's current evidence.
- Suggested experiments or analyses to close the gaps.
- Suggested updates to the claim bank and paper-gap register.
```

## Prompt 4: Evaluation Methodology And Reviewer Risk

Use this to harden the paper's experiment methodology.

```text
Evaluate Agentc's experimental methodology against expectations in LLM systems / ML evaluation papers.

Current Agentc method:
- Purpose-built workloads are used to trigger specific optimizer rules.
- An 11-config shared-baseline ablation matrix is used:
  - all-on
  - each rule off
  - each rule only
- Metrics include cost savings, input-token savings, accuracy delta, baseline cost, optimized cost, and task pass counts.
- Some results are complete and some are partial.
- LLM stochasticity is a known issue, so input-token savings are used as a more deterministic attribution signal.

Research tasks:
1. Find papers discussing evaluation under stochastic LLM outputs.
2. Find papers using ablations for runtime/system optimizers.
3. Find papers discussing paired tests or uncertainty reporting for LLM task accuracy.
4. Identify what reviewers may expect for cost/latency/accuracy reporting.
5. Identify whether purpose-built workloads are acceptable and how to frame them.

Output:
- Evaluation-methodology best practices relevant to Agentc.
- What Agentc already does well.
- What Agentc should add before submission.
- Statistical tests or uncertainty reporting worth adding.
- Reviewer-risk table.
- Concrete updates to:
  - paper-intelligence/results-experiments-and-repro.md
  - paper-intelligence/claims-gaps-and-risks.md
```

## Prompt 5: Literature Review For Paper Positioning

Use this when deciding the paper's main angle.

```text
Help choose the best paper positioning for Agentc.

Candidate paper angles:
1. Agentc as a compiler/runtime for LLM agents.
2. Agentc as a practical cost optimization layer for agent frameworks.
3. Agentc as a rule-based JIT optimizer for compound AI systems.
4. Agentc as an evaluation methodology paper for optimizer-style LLM runtimes.
5. Agentc as a systems paper about transparent LLM-call interception and rewrite planning.

For each angle:
- Identify closest related work.
- Explain what audience would care.
- Explain what evidence the paper needs.
- Explain what the current Agentc results support.
- Explain what the current Agentc results do not support.
- List likely venues/workshops.
- List biggest reviewer risks.
- Give a recommendation score from 1-10.

Output:
- A positioning matrix.
- Best primary angle.
- Best backup angle.
- Claims to emphasize.
- Claims to avoid.
- Literature clusters needed for the chosen angle.
```

## Prompt 6: Citation Gap Filler

Use this after the claim bank exists.

```text
I have the following Agentc paper claims that need citation support:

[PASTE CLAIMS FROM paper-intelligence/claims-gaps-and-risks.md]

Find high-quality citations for each claim.

Rules:
- Prefer peer-reviewed or widely recognized sources.
- Include arXiv only when it is the best or only available source.
- Do not provide fake citations.
- Link every source.
- For each citation, explain whether it supports the claim directly, indirectly, or only as background.
- If no good citation exists, say so and suggest a safer claim.

Output table columns:
- Claim
- Recommended citation
- Link
- Support type: direct / indirect / background / weak
- How to use it
- Safer wording if support is weak
- Follow-up needed
```

## Prompt 7: Conference And Venue Fit

Use this to find where Agentc should be submitted. This prompt intentionally asks for current CFP/deadline verification because venue details change.

```text
Research which conferences, workshops, or journals make sense for a paper about Agentc.

Agentc summary:
Agentc is a runtime optimizer for multi-step LLM agent workloads. It transparently intercepts LLM calls from agent frameworks and applies rewrite rules such as ContextCompress, ModelDowngrade, StateDrop, CacheHit, and ParallelBranch. Current evidence includes benchmark ablations showing cost savings for ContextCompress, ModelDowngrade, and StateDrop, plus pushback experiments on real HotpotQA and oracle compression.

The paper could be framed as:
- LLM systems / runtime optimization
- agent infrastructure
- compiler/runtime for compound AI systems
- practical cost optimization for LLM applications
- evaluation methodology for optimizer-style LLM runtimes

Please identify suitable venues.

Requirements:
- Verify current or most recent official CFP/deadline/page-limit/anonymity/artifact-policy information from official venue pages.
- Distinguish main conferences from workshops.
- Do not rely on stale deadline aggregators unless also verified against official pages.
- Include links to official CFPs or venue pages.
- If exact dates are not available yet, say so and use the most recent cycle as evidence.

Evaluate these venue families at minimum:
- ML/AI: NeurIPS, ICML, ICLR, COLM, AAAI, IJCAI
- NLP/LLM: ACL, EMNLP, NAACL, Findings, ARR, COLING
- Systems/ML systems: MLSys, OSDI, SOSP, NSDI, EuroSys, ASPLOS, ATC
- Data/infra if relevant: SIGMOD, VLDB, CIDR
- HCI/AI tools if relevant: CHI, UIST, IUI
- Workshops on agents, compound AI systems, efficient LLMs, model routing, LLM serving, AI agents, and ML systems

For each venue, assess:
- Topical fit
- Contribution fit: systems / ML / NLP / workshop / demo / short paper
- Evidence expected
- Novelty bar
- Risk of rejection based on current Agentc evidence
- Whether current results are enough
- What extra experiment or writing work would improve fit
- Deadline/page-limit/anonymity/artifact notes
- Recommendation: strong fit / possible fit / weak fit / not recommended

Output:
1. Executive recommendation: top 3 primary venues and top 5 workshops.
2. Venue fit table.
3. Deadline/action calendar using verified dates.
4. "What must be true before submission" checklist for each top venue.
5. Suggested paper framing for each top venue.
6. Sources/links section.
7. Updates to add to:
   - paper-intelligence/strategy-and-venues.md
   - paper-intelligence/claims-gaps-and-risks.md
```

## Prompt 8: Workshop Finder

Use this when the main-conference target is unclear and we want a near-term place to submit.

```text
Find active or recent workshops that would be good homes for Agentc.

Topic:
Runtime optimization for LLM agents / compound AI systems, including transparent LLM-call interception, model routing, context compression, state pruning, semantic caching, and cost-aware execution.

Search for workshops affiliated with:
- NeurIPS
- ICML
- ICLR
- COLM
- ACL / EMNLP / NAACL
- MLSys
- USENIX / ACM systems venues
- Agent, efficient LLM, LLM serving, compound AI, AI infrastructure, and model routing workshops

For each workshop:
- Official link
- Parent venue
- Most recent or current CFP
- Submission format/page limit
- Deadline status
- Fit for Agentc
- What framing would work
- Whether results need to be complete or can be work-in-progress
- Artifact/demo expectations

Output:
- Ranked workshop shortlist.
- Calendar of near-term opportunities.
- Best workshop for a first Agentc paper.
- Best workshop for feedback before a larger submission.
- Risks and missing evidence.
```

## Prompt 9: Post-June Venue Sweep

Use this when an earlier venue search over-focused on near-term deadlines. This prompt deliberately looks for opportunities after June 2026 and into the next cycle.

```text
Research venues for an AgentC paper with a strict focus on deadlines AFTER June 10, 2026 and opportunities later in 2026 or early 2027.

AgentC summary:
AgentC is a runtime optimizer for multi-step LLM agent workloads. It intercepts LLM calls from agent frameworks and applies rewrite rules including model downgrade/routing, context compression, state dropping, semantic caching, and parallel branch detection. Current evidence includes rule-level cost-saving ablations, but the paper still needs stronger end-to-end, overhead, baseline, and stochastic-evaluation evidence.

Important instruction:
Do not stop after finding ATC, MLSys, COLM, or an already-passed workshop. Search specifically for late-summer, fall, winter, and next-cycle venues.

Search target window:
- July 2026 through March 2027 submission deadlines.
- Include official deadlines that are already posted.
- If a 2027 CFP is not posted, use the most recent official cycle only as a pattern and mark it as unverified for 2027.

Venue families to check:
- Systems / ML systems: EuroSys, NSDI, ASPLOS, OSDI, SOSP, ATC, MLSys, HotOS, CIDR, SIGMOD, VLDB.
- ML / AI / LM: AAAI 2027, ICLR 2027 if posted, NeurIPS 2027 if posted, COLM next cycle if posted, ICML workshops if still open.
- NLP / LLM: ACL Rolling Review, EMNLP/ACL/NAACL workshops, COLING if active.
- Workshops: agent systems, compound AI systems, LLM serving, efficient LLM inference, model routing, agent infrastructure, software engineering for agents.

For each venue or workshop:
- Official link.
- Deadline and whether it is after June 10, 2026.
- Page limit.
- Anonymity policy.
- Artifact or reproducibility expectations.
- Fit for AgentC.
- What framing would work.
- Why current AgentC evidence is weak or strong for that venue.
- What minimum extra evidence would make submission plausible.
- Recommendation: strong / possible / weak / not recommended.

Output:
1. Calendar sorted by deadline after June 10, 2026.
2. Top 5 realistic post-June targets.
3. Top 5 workshops or short-paper venues.
4. Venues to ignore and why.
5. What AgentC must build by each target deadline.
6. Updates to add to:
   - paper-intelligence/strategy-and-venues.md
   - paper-intelligence/claims-gaps-and-risks.md
```

## Prompt 10: Full Paper Literature Review Sweep

Use this when we want a complete literature review map for the whole AgentC paper, not just one rewrite rule.

```text
Build a full-paper literature review map for AgentC.

AgentC summary:
AgentC is a runtime optimizer for multi-step LLM agent workloads. It intercepts LLM calls from agent frameworks and applies multiple rewrite classes under one runtime control plane:
- ModelDowngrade: routes hot/simple/structured call sites to cheaper models.
- ContextCompress: drops low-salience context from large prompts.
- StateDrop: removes stale state-tagged messages not read by the current execution window.
- CacheHit: reuses prior responses from a semantic memoization cache.
- ParallelBranch: identifies independent sibling calls or parallelizable branches.

Important framing:
Routing is only one subsection. Do not make the literature review mainly about model routing. The main paper frame is "runtime optimization for compound AI / multi-step LLM agent systems."

Research the whole space:
1. Compound AI systems and agent frameworks.
2. Runtime optimization for LLM applications and LM programs.
3. Model routing, cascades, and model selection.
4. Prompt/context compression and context pruning.
5. State/memory pruning, liveness, data-flow analysis, program slicing, and compiler analogies.
6. Semantic caching, memoization, request deduplication, and cache correctness.
7. Tool-call scheduling, parallel function calling, graph execution, and dependency/side-effect analysis.
8. Serving/inference systems such as vLLM, SGLang, Orca, DistServe, Prompt Cache, and why these are orthogonal.
9. Evaluation methodology for stochastic LLM systems: repeated trials, paired comparisons, pass^k/reliability, judge bias, uncertainty, and cost-quality frontiers.

For each cluster:
- 5-10 strongest papers/sources, with primary links.
- Venue/year and citation metadata.
- What claim the cluster supports for AgentC.
- What reviewer risk the cluster creates.
- What AgentC shares with this work.
- What AgentC does differently.
- Which sources are must-cite, optional, or only background.
- Whether any source is a direct baseline we may need to run.

Special focus:
- Find systems that combine multiple rewrite classes, not just one trick.
- Find papers closest to "runtime optimizer for agent traces."
- Find papers that threaten the novelty claim.
- Find papers that justify StateDrop through compiler/program-analysis concepts.
- Find papers that say single-run LLM evaluation is weak or unreliable.

Output:
1. Executive summary: 10 bullets on what the literature means for AgentC.
2. Cluster-by-cluster literature map.
3. Top 15 closest works to AgentC, ranked.
4. Full related-work section outline for a human author.
5. Claims AgentC can safely make.
6. Claims AgentC should avoid.
7. Citation gaps and missing evidence.
8. Baselines that are runnable versus citation-only.
9. Updates to add to:
   - paper-intelligence/literature-and-nearest-neighbors.md
   - paper-intelligence/claims-gaps-and-risks.md
```

## Prompt 11: Routing Comparators And Similar Papers

Use this to expand the literature review around `ModelDowngrade` and adjacent routing/cascade systems.

```text
Find papers and systems similar to AgentC's ModelDowngrade rule and explain how each compares to AgentC.

AgentC context:
- AgentC is a runtime optimizer for multi-step LLM agent workloads.
- ModelDowngrade routes hot, simple, or structured internal call sites to cheaper models when quality risk is within budget.
- ModelDowngrade is only one rewrite pass inside a broader optimizer that also includes context compression, state dropping, semantic caching, and parallel branch detection.
- AgentC's novelty claim should not be "model routing is new"; the question is how routing changes when it is embedded in a framework-intercepting runtime over agent traces.

Start with these known comparators:
- FrugalGPT
- RouteLLM
- Optimizing Model Selection for Compound AI Systems
- Language Model Cascades

Then search for additional similar work:
- LLM routing
- model cascades
- model selection for compound AI systems
- cost-aware inference
- confidence-based routing
- multi-agent model routing
- router robustness/safety
- benchmark papers for model routers

For each source:
- Link to primary source.
- Venue/year and citation metadata.
- What is being routed: user query, subtask, component, tool call, agent step, or call site.
- Whether routing is online/runtime or offline/static.
- Whether it uses uncertainty, preference data, confidence, task features, call-site history, or learned router models.
- Cost metric and quality metric.
- Whether it supports or threatens AgentC's ModelDowngrade claim.
- How AgentC should compare or contrast it.

Output:
1. Executive summary: what routing literature means for AgentC.
2. Table of at least 15 related routing/cascade/model-selection papers.
3. The 5 closest routing frameworks and exact compare/contrast language.
4. Claims AgentC can safely make.
5. Claims AgentC should avoid.
6. Baselines that are runnable versus citation-only.
7. Updates to add to:
   - paper-intelligence/literature-and-nearest-neighbors.md
   - paper-intelligence/AGENTS.md
   - paper-intelligence/claims-gaps-and-risks.md
```

## Prompt 12: Deep Research Digest To Repo Notes

Use this after any long research session to convert raw findings into durable repo artifacts.

```text
Convert the following raw deep-research output into structured Agentc paper intelligence notes.

Raw research:
[PASTE RAW OUTPUT]

Create updates for these files:
- literature-and-nearest-neighbors.md
- claims-gaps-and-risks.md
- strategy-and-venues.md
- evidence-and-sources.md

Rules:
- Do not write manuscript prose.
- Deduplicate papers and ideas.
- Preserve links.
- Separate confirmed facts from interpretation.
- Flag anything that needs verification.
- Mark which items are high priority.

Output:
1. Summary of what changed.
2. Proposed entries for each target file.
3. Items that should not be imported.
4. Follow-up research tasks.
```

## Prompt 13: Skeptical Systems Reviewer

```text
Review the AgentC paper intelligence materials as a skeptical systems reviewer.

Read:
- paper-intelligence/README.md
- paper-intelligence/current-fit-and-publishability.md
- paper-intelligence/results-experiments-and-repro.md
- paper-intelligence/claims-gaps-and-risks.md
- paper-intelligence/literature-and-nearest-neighbors.md

Find the strongest technical objections to the current paper story. Focus on workload representativeness, runtime overhead, planner correctness, safety checks, reproducibility, and whether the optimization analogy is overclaimed.

Return:
- top 5 findings, ordered by severity;
- exact files or IDs involved;
- what evidence would answer each objection;
- whether the fix needs code, experiment tokens, literature review, or framing.
```

## Prompt 14: Skeptical ML Evaluation Reviewer

```text
Review the AgentC result story as a skeptical ML evaluation reviewer.

Read:
- paper-intelligence/results-experiments-and-repro.md
- paper-intelligence/claims-gaps-and-risks.md
- paper-intelligence/current-fit-and-publishability.md

Look for statistical, methodological, and benchmark-design weaknesses. Pay attention to sample sizes, partial matrices, paired comparisons, stochasticity, metric choice, accuracy preservation, and cost-accounting assumptions.

Return:
- which results are headline-safe;
- which results are appendix-only or diagnostic;
- what exact analysis should be run before paper drafting;
- which claims should be softened or blocked.
```

## Prompt 15: Related-Work Reviewer

```text
Review the AgentC positioning against related work.

Read:
- paper-intelligence/literature-and-nearest-neighbors.md
- paper-intelligence/claims-gaps-and-risks.md
- paper-intelligence/current-fit-and-publishability.md

Identify missing comparator families and likely nearest-neighbor papers for runtime optimization of LLM applications, prompt/context compression, model routing, agent memory/state management, caching, and agent frameworks.

Return:
- missing literature areas;
- candidate papers or venues to verify;
- claims that need citations before prose;
- where AgentC appears meaningfully different.
```

## Prompt 16: Reproducibility Reviewer

```text
Review AgentC's paper reproducibility posture.

Read:
- paper-intelligence/results-experiments-and-repro.md
- paper-intelligence/evidence-and-sources.md
- paper-intelligence/claims-gaps-and-risks.md

Check whether a future researcher could reproduce the reported CSVs and understand which runs are canonical, partial, diagnostic, or local-only.

Return:
- missing commands or environment variables;
- missing artifact checksums;
- unclear output locations;
- run-log fields that should be required before new results are cited.
```

## Prompt 17: Clarity Reviewer

```text
Review the paper intelligence base for reader orientation.

Read:
- paper-intelligence/README.md
- paper-intelligence/current-fit-and-publishability.md
- paper-intelligence/literature-and-nearest-neighbors.md
- paper-intelligence/claims-gaps-and-risks.md
- paper-intelligence/results-experiments-and-repro.md
- paper-intelligence/strategy-and-venues.md

Ask whether a smart technical collaborator can understand the current paper state in under ten minutes.

Return:
- confusing names;
- missing definitions;
- duplicated or contradictory status labels;
- the shortest path from "new contributor" to "I know what to do next."
```

## Prompt 18: Idea Generation Pass

```text
Generate paper-improvement ideas for AgentC as a structured pass, not a brainstorm.

Use these lenses:
- literature-gap ideas: what missing related-work distinction suggests a new framing?
- reviewer-objection inversions: can a weakness become a measured contribution?
- figure-first ideas: what visual would make the system obvious?
- benchmark-extension ideas: what experiment would close a high-severity gap?
- venue-specific reframings: what changes if targeting systems vs LLM agents vs workshop?
- negative-result ideas: what non-firing or partial result actually supports safety?

Score each idea from 1-5 on novelty, evidence cost, venue fit, reviewer risk, and narrative leverage.

Return:
- top ideas with scores;
- which `GAP`, `RR`, `EXP`, or `FIG` IDs each idea affects;
- which ideas should be promoted to the paper-intelligence docs.
```
