---
title: Agentc Paper Intelligence Workplan
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Agentc Paper Intelligence Workplan

**Status:** initial refocus plan  
**Purpose:** build a paper intelligence hub for Agentc: a place to collect evidence, find gaps, generate paper ideas, store literature-review/deep-research results, prioritize experiments, and prepare high-quality inputs for hand-written paper drafting.

This is not a manuscript-writing automation plan. The paper should be written by hand. Agents should help by making the evidence clearer, surfacing missing support, finding related work, pressure-testing claims, and organizing results so human writing is easier and sharper.

## North Star

The workspace should answer these questions quickly:

- What are the strongest paper claims Agentc can currently support?
- Which claims are promising but under-supported?
- Which experiment gaps are worth spending API tokens on?
- Which literature areas do we need to understand before writing?
- What are likely reviewer objections?
- Where in the repo is the evidence for each claim?
- What alternative paper angles are available?
- What results, quotes, papers, or ideas have we already collected?

## Naming Decision

Rename the concept from `paper-writeup` to `paper-intelligence`.

Recommended paths:

- `paper-intelligence/` for research, ledgers, claim maps, gap analysis, literature review, and agent briefings.
- `paper-intelligence/references/source/` for immutable source artifacts such as Avery's paper reference, feedback notes, response PDFs, and copied source docs.
- `paper-intelligence/research-inbox/` or `deep-research-inbox.md` for generated research drops before promotion.
- `paper-intelligence/section-briefs/` for compact human-writing briefs.
- `paper/agentc/` only later, if the repo owner explicitly approves a manuscript workspace. Until then, keep manuscript drafting out of scope.

Rationale:

- `paper-writeup` implies agents are drafting the paper.
- `paper-intelligence` says the actual job: make the human-written paper better by collecting, organizing, and stress-testing the material.

## Operating Rules

- Agents may summarize, map, audit, compare, and propose.
- Agents should not draft final manuscript prose unless explicitly asked.
- Every result number must point to a local artifact.
- Every literature claim must point to a paper, source, or pending verification item.
- Every idea should be tagged with status: `raw`, `promising`, `needs evidence`, `ready for writing`, or `discarded`.
- Every gap should say whether it needs code work, experiment work, literature review, figure/table work, or just clearer prose framing.
- New deep-research outputs should be stored as reusable notes, not lost in chat.

## Current State

Agentc already has meaningful results and implementation artifacts.

Important repo artifacts:

- `bench/paper_results/*.csv` contains canonical or partial paper-result CSVs.
- `bench/optimizer_ablation.py` runs the 11-config per-rule ablation matrix.
- `bench/scripts/run_paper_ablation.sh` runs paper-quality ContextCompress and StateDrop experiments.
- `bench/scripts/run_pushback_ablation.sh` runs reviewer-pushback experiments.
- `crates/agentc-optimizer/src/rules/*.rs` contains the rule implementations.
- `crates/agentc-optimizer/src/planner.rs` contains the hot-call, rule-ranking, safety-check, and pass-through planner behavior.
- `python/agentc/_intercept.py` and `python/agentc/_optimizer.py` contain the Python-side interception and FFI planning path.
- `specs/optimizer.md`, `specs/profiler.md`, and `specs/memoization.md` contain design and evaluation context.

Important local paper artifacts:

- `paper-intelligence/references/source/agentc-paper-reference-v2.md` is the high-level paper reference and table/figure plan copied from the Messages attachment.
- `paper-intelligence/references/source/agentc-feedback.md` is local feedback/research critique context.
- `paper-intelligence/references/source/agentc-response.pdf` is a local response artifact.
- `paper-intelligence/references/source/readme-local-before-upstream.md` may contain older local framing.

Current result posture:

- `ContextCompress`: strong headline result, about 34.5% cost savings on `long_context_qa`.
- `ModelDowngrade`: strong headline result, about 35.3% cost savings on `gaia_router`.
- `StateDrop`: smaller but real result, about 5.9-6.0% cost savings / 9.3-9.6% input-token savings on current `iterative_refiner` runs, with the n=50 matrix still partial.
- Real HotpotQA shows ContextCompress mostly declines at the activation boundary, which supports the gate/safety story.
- Oracle HotpotQA shows the manual-compression ceiling.

## Phase 0A: Bootstrap Hygiene

- [x] **P0A.1 Create canonical paper-intelligence directory**
  - **Goal:** establish `paper-intelligence/` as the canonical workspace.
  - **Done when:** the workplan and prompt-template files live under `paper-intelligence/`.

- [x] **P0A.2 Preserve local paper artifacts with checksums**
  - **Goal:** move important root-level paper artifacts into stable reference storage.
  - **Action:** move the current root artifacts to `paper-intelligence/references/source/`.
  - **Done when:** original checksums are recorded in `artifact-inventory.md`.

- [x] **P0A.3 Add frontmatter to paper-intelligence working files**
  - **Goal:** keep paper-intelligence working files machine-readable and easy to scan.
  - **Done when:** every new Markdown file has YAML frontmatter with `title`, `status`, `last-updated`, and `owner`.

- [x] **P0A.4 Define IDs and schemas before ledger growth**
  - **Goal:** prevent future Markdown drift.
  - **Done when:** `metadata-schemas.md` defines IDs, statuses, and row templates for all core artifacts.

- [x] **P0A.5 Add initial control surfaces**
  - **Goal:** make future ingestion safe before deep research arrives.
  - **Done when:** `README.md`, `AGENTS.md`, `artifact-inventory.md`, `repo-source-map.md`, `result-validation-checklist.md`, `results-ledger.md`, `literature-ingestion-workflow.md`, and `venue-positioning-matrix.md` exist.

## Phase 0: Refocus And Cleanup

- [x] **P0.1 Rename the planning workspace**
  - **Goal:** replace the old `paper-writeup` framing with `paper-intelligence`.
  - **Action:** use `paper-intelligence/` as the canonical home for this work.
  - **Also do:** remove or archive any stale `specs/paper-writeup/` files so future agents do not follow the wrong framing.
  - **Done when:** `paper-intelligence/` is the only active planning/intelligence workspace.

- [x] **P0.2 Create a directory README**
  - **Goal:** explain that this is a paper intelligence base, not a manuscript-writing directory.
  - **File:** `paper-intelligence/README.md`.
  - **Must include:** purpose, read order, file inventory, manual-writing boundary, and how new research/results should be added.
  - **Done when:** a fresh agent can open the directory and understand the difference between evidence support and manuscript prose.

- [x] **P0.3 Create an agent instruction file**
  - **Goal:** give research/gap-finding agents local rules.
  - **File:** `paper-intelligence/AGENTS.md` or `paper-intelligence/CLAUDE.md`.
  - **Must include:** do not write final paper prose by default; update ledgers; cite artifacts; tag ideas; separate evidence from interpretation; preserve uncertainty.
  - **Done when:** an agent can run a literature search, result audit, or claim review without drifting into unsourced paper drafting.

- [x] **P0.4 Normalize local paper artifacts**
  - **Goal:** move important local context out of the repo root and into the intelligence base.
  - **Suggested destination:** `paper-intelligence/references/source/`.
  - **Files to triage:** `agentc-paper-reference-v2 (1).md`, `agentc-feedback.md`, `agentc-response.pdf`, `README.local-before-upstream.md`.
  - **Decision labels:** `keep-tracked`, `keep-local`, `summarize-only`, `discard`, `defer`.
  - **Done when:** there is no unexplained paper context floating at repo root.

- [x] **P0.5 Write an import/skip plan for Pizza files**
  - **Goal:** import only useful paper-process patterns from `pizza_at_the_pentagon`.
  - **File:** `paper-intelligence/pizza-import-plan.md`.
  - **Source candidates:** paper review checklist, section workplan, academic prompts, citation plan, style guide, rendering checklist.
  - **Refocus rule:** adapt them as intelligence-gathering and review tools, not as manuscript automation.
  - **Done when:** each candidate Pizza file is marked `adapt`, `skip`, or `defer`, with a reason.

## Phase 1: Evidence And Repo Mapping

- [x] **P1.1 Create `repo-source-map.md`**
  - **Goal:** map paper-relevant repo locations.
  - **File:** `paper-intelligence/repo-source-map.md`.
  - **Must cover:** optimizer rule code, planner activation path, Python interceptor path, benchmark harness, scripts, paper results, specs, README, and local references.
  - **Suggested sections:**
    - What Agentc is
    - Runtime/interception path
    - Rule implementation paths
    - Planner/safety paths
    - Benchmark harness paths
    - Result CSV paths
    - Reproduction scripts
    - Existing paper/reference notes
  - **Done when:** future agents can find evidence without broad repo archaeology.

- [x] **P1.2 Create `results-ledger.md`**
  - **Goal:** make all experimental numbers durable and source-grounded.
  - **File:** `paper-intelligence/results-ledger.md`.
  - **For each result, record:** rule, workload, n, model, config, CSV path, headline cost savings, input-token savings, accuracy delta, row status, caveats, and likely paper use.
  - **Initial entries required:**
    - ContextCompress on `long_context_qa`, n=100
    - ModelDowngrade on `gaia_router`, n=127
    - StateDrop on `iterative_refiner`, n=30
    - StateDrop on `iterative_refiner`, n=50 partial
    - ContextCompress on real HotpotQA, n=300 partial
    - HotpotQA oracle baseline, n=300
  - **Status labels:** `headline-ready`, `canonical`, `partial`, `diagnostic`, `appendix-only`, `needs-rerun`, `do-not-use-yet`.
  - **Done when:** every number we might cite has a stable source and status.

- [x] **P1.3 Create `artifact-inventory.md`**
  - **Goal:** list all paper-relevant files, including local references and generated outputs.
  - **File:** `paper-intelligence/artifact-inventory.md`.
  - **Must include:** committed CSVs, scripts, specs, untracked Markdown/PDF artifacts, possible generated `bench/results/` outputs if present, and future deep-research notes.
  - **For each artifact:** path, type, owner/source, importance, status, and next action.
  - **Done when:** the project has a searchable inventory of paper material.

- [x] **P1.4 Create `reproduction-commands.md`**
  - **Goal:** collect exact commands for regenerating or extending results.
  - **File:** `paper-intelligence/reproduction-commands.md`.
  - **Must include:** fixture generation, `optimizer_bench`, `optimizer_ablation`, `run_paper_ablation.sh`, `run_pushback_ablation.sh`, `run_targeted_ablation.sh`, and oracle baseline.
  - **For each command:** required env vars, expected output path, likely API cost, runtime risk, and verification check.
  - **Done when:** a future experiment runner does not need to reverse-engineer shell scripts.

- [x] **P1.5 Create `result-validation-checklist.md`**
  - **Goal:** define when a result is trustworthy enough to enter the ledger.
  - **File:** `paper-intelligence/result-validation-checklist.md`.
  - **Checks:** row count, config completeness, baseline sharing, plausible costs, token accounting, model identity, sample size, partial-run labeling, accuracy uncertainty, and source command.
  - **Done when:** new CSVs cannot silently become paper claims.

## Phase 2: Claim, Gap, And Idea Engine

- [x] **P2.1 Create `claim-bank.md`**
  - **Goal:** collect possible paper claims separately from the paper itself.
  - **File:** `paper-intelligence/claim-bank.md`.
  - **For each claim:** text, status, evidence, caveat, likely reviewer objection, and paper location if used.
  - **Status labels:** `supported`, `promising`, `needs-analysis`, `needs-experiment`, `needs-citation`, `too-strong`, `discarded`.
  - **Initial claims:**
    - Agentc optimizes multi-step LLM agent workloads transparently.
    - Runtime rewrite rules can produce large cost savings on suitable call sites.
    - ContextCompress and ModelDowngrade show strong isolated savings.
    - StateDrop shows smaller but real input-token savings.
    - Activation gates matter and prevent inappropriate rewrites.
    - Some rules are characterized but not fully benchmarked end-to-end.
  - **Done when:** human writing can pull from a vetted bank instead of inventing claims from memory.

- [x] **P2.2 Create `paper-gap-register.md`**
  - **Goal:** identify what is missing between current evidence and a strong paper.
  - **File:** `paper-intelligence/paper-gap-register.md`.
  - **Gap categories:** result gap, method gap, explanation gap, citation gap, figure/table gap, reviewer-risk gap, implementation-evidence gap, positioning gap.
  - **For each gap:** description, severity, blocking claim, owner/action, evidence needed, and whether it needs tokens/API spend.
  - **Initial gaps to consider:**
    - StateDrop n=50 matrix is partial.
    - Real HotpotQA matrix is partial.
    - StateDrop accuracy metric is lenient.
    - Paired accuracy testing is not yet integrated.
    - Related work needs systematic collection.
    - CacheHit and ParallelBranch need careful positioning.
  - **Done when:** the next best contribution is obvious from the gap list.

- [x] **P2.3 Create `idea-bank.md`**
  - **Goal:** hoard paper ideas without forcing them into the manuscript too early.
  - **File:** `paper-intelligence/idea-bank.md`.
  - **Idea types:** paper angle, experiment idea, figure idea, title idea, related-work positioning, limitation framing, reviewer-response idea, future-work idea.
  - **For each idea:** one-sentence version, longer rationale, evidence needed, risk, and current status.
  - **Done when:** interesting thoughts from chats, deep research, and code reading have a durable home.

- [x] **P2.4 Create `paper-angle-matrix.md`**
  - **Goal:** compare possible paper narratives.
  - **File:** `paper-intelligence/paper-angle-matrix.md`.
  - **Candidate angles:**
    - Agent compiler/runtime for LLM workloads
    - Rule-based JIT optimizer for compound AI systems
    - Practical cost optimization layer for agent frameworks
    - Evaluation methodology for optimizer-style LLM runtimes
    - Runtime systems paper with ML evaluation
  - **For each angle:** central claim, strongest evidence, weakest evidence, audience, likely venue, figures/tables needed, and related-work neighborhood.
  - **Done when:** we can deliberately choose the paper framing instead of drifting into one.

- [x] **P2.5 Create `question-backlog.md`**
  - **Goal:** store questions for Avery, future agents, or deep research.
  - **File:** `paper-intelligence/question-backlog.md`.
  - **Question types:** implementation, experiment, result interpretation, paper positioning, literature, venue, collaboration.
  - **For each question:** why it matters, who can answer it, and what artifact should be updated afterward.
  - **Done when:** uncertainty is tracked rather than repeated across chats.

- [x] **P2.6 Create `decision-log.md`**
  - **Goal:** preserve key paper-planning decisions.
  - **File:** `paper-intelligence/decision-log.md`.
  - **Initial decisions to record:** manual writing boundary, `paper-intelligence` naming, which results are headline vs diagnostic, whether to finish partial experiments before prose, and whether to use Markdown or LaTeX later.
  - **Done when:** future collaborators can see why the structure exists.

## Phase 3: Literature Review And Deep Research Hoard

- [x] **P3.1 Create `literature-ledger.md`**
  - **Goal:** track all related work in one place.
  - **File:** `paper-intelligence/literature-ledger.md`.
  - **For each paper/source:** title, authors, year, link/DOI/arXiv, status, summary, relevance to Agentc, claim it supports, and notes for related work.
  - **Status labels:** `candidate`, `skimmed`, `read`, `verified`, `cited`, `discarded`.
  - **Topic buckets:** agent frameworks, model routing, prompt/context compression, semantic caching, KV/prefix caching, LLM inference systems, compound AI systems, tool-call parallelism, benchmark methodology, stochastic LLM evaluation.
  - **Done when:** related work does not live only in browser tabs or chat logs.

- [x] **P3.2 Create `related-work-map.md`**
  - **Goal:** organize literature by conceptual neighborhood.
  - **File:** `paper-intelligence/related-work-map.md`.
  - **Must answer:** what is closest to Agentc, what is adjacent, what is not actually comparable, and what gap Agentc occupies.
  - **Suggested structure:** one section per neighborhood, with "how Agentc differs" bullets.
  - **Done when:** a human can write related work with a clear map instead of a flat citation pile.

- [x] **P3.3 Create `deep-research-inbox.md`**
  - **Goal:** provide a landing zone for outputs from long web/deep-research sessions.
  - **File:** `paper-intelligence/deep-research-inbox.md`.
  - **For each research drop:** date, topic, source/model/tool used, summary, links, extracted claims, confidence, and follow-up actions.
  - **Rule:** inbox entries should later be promoted into `literature-ledger.md`, `idea-bank.md`, `claim-bank.md`, or `paper-gap-register.md`.
  - **Done when:** deep research is captured even before it is fully digested.

- [x] **P3.4 Create `quote-and-evidence-bank.md`**
  - **Goal:** store useful quotes, definitions, and precise source-backed formulations.
  - **File:** `paper-intelligence/quote-and-evidence-bank.md`.
  - **Must include:** source, exact quote or paraphrase, allowable use, relation to Agentc, and citation status.
  - **Use cases:** related work, motivation, definitions, reviewer-response framing.
  - **Done when:** useful language from papers is not lost.

- [x] **P3.5 Create `citation-gap-list.md`**
  - **Goal:** track claims that need citations.
  - **File:** `paper-intelligence/citation-gap-list.md`.
  - **For each gap:** claim needing support, likely literature area, search queries, candidate papers, and status.
  - **Done when:** every uncited background/motivation claim has a research path.

## Phase 4: Reviewer And Weakness Analysis

- [x] **P4.1 Create `reviewer-risk-register.md`**
  - **Goal:** predict objections early.
  - **File:** `paper-intelligence/reviewer-risk-register.md`.
  - **Initial risks:**
    - Purpose-built workloads may look too synthetic.
    - Some matrices are partial.
    - StateDrop metric is lenient.
    - StateDrop cost savings are smaller than input-token savings.
    - CacheHit and ParallelBranch are descoped from headline results.
    - OpenAI prompt caching complicates interpretation.
    - LLM stochasticity makes cost/accuracy deltas noisy.
  - **For each risk:** likely objection, current answer, evidence needed, whether to address in main text or limitations, and whether new experiments are needed.
  - **Done when:** weak spots are visible and actionable.

- [x] **P4.2 Create `red-team-review-prompts.md`**
  - **Goal:** give agents reusable prompts for adversarial paper review.
  - **File:** `paper-intelligence/red-team-review-prompts.md`.
  - **Prompt types:** skeptical systems reviewer, skeptical ML evaluation reviewer, related-work reviewer, reproducibility reviewer, clarity reviewer.
  - **Done when:** review passes can be consistent and high-quality.

- [x] **P4.3 Create `weak-point-resolution-plan.md`**
  - **Goal:** convert reviewer risks into concrete work.
  - **File:** `paper-intelligence/weak-point-resolution-plan.md`.
  - **For each weak point:** resolution options, cheapest fix, strongest fix, owner, and decision.
  - **Done when:** each serious weakness has a mitigation path.

- [x] **P4.4 Create `manual-writing-brief.md`**
  - **Goal:** give the human writer a compact briefing, not prose.
  - **File:** `paper-intelligence/manual-writing-brief.md`.
  - **Must include:** strongest claims, best results, key caveats, recommended narrative, figures/tables to include, and what not to say.
  - **Done when:** William can sit down to write with a two-to-four page briefing instead of scanning the whole repo.

## Phase 5: Experiment And Token Spend Planning

- [x] **P5.1 Create `experiment-priority-board.md`**
  - **Goal:** rank possible experiments by paper value per token/dollar/hour.
  - **File:** `paper-intelligence/experiment-priority-board.md`.
  - **For each experiment:** gap closed, expected paper value, API cost, runtime, implementation risk, exact command, output artifact, stop condition.
  - **Initial candidates:**
    - Finish missing StateDrop n=50 row.
    - Finish remaining real HotpotQA configs.
    - Add paired accuracy/McNemar testing.
    - Cleanly reproduce StateDrop temp=0.
    - Repeat ModelDowngrade with another seed or robustness pass.
  - **Done when:** extra token spending is deliberate.

- [x] **P5.2 Create `experiment-run-log.md`**
  - **Goal:** store what was run, when, with what settings.
  - **File:** `paper-intelligence/experiment-run-log.md`.
  - **For each run:** date, command, env vars, model, cost estimate, output path, completion status, notes, and ledger update link.
  - **Done when:** experiment history is recoverable without shell history.

- [x] **P5.3 Create `statistical-analysis-plan.md`**
  - **Goal:** plan uncertainty reporting and paired tests.
  - **File:** `paper-intelligence/statistical-analysis-plan.md`.
  - **Must cover:** standard error for accuracy deltas, paired binary tests, McNemar applicability, partial matrix caveats, cost vs input-token interpretation, and stochastic output variance.
  - **Done when:** result interpretation is defensible before manuscript writing.

- [x] **P5.4 Create `figure-idea-bank.md`**
  - **Goal:** hoard possible visual explanations.
  - **File:** `paper-intelligence/figure-idea-bank.md`.
  - **Ideas to include:** system architecture, two-gate rule pipeline, headline savings bar chart, StateDrop noise vs signal, rule activation map, experiment matrix overview.
  - **For each figure:** purpose, source data, sketch description, risk, and whether it belongs in main paper or appendix.
  - **Done when:** visual ideas are not lost and can be prioritized later.

## Phase 6: Human Writing Support

- [x] **P6.1 Create `section-briefs/`**
  - **Goal:** store source-grounded notes for each paper section, not final prose.
  - **Directory:** `paper-intelligence/section-briefs/`.
  - **Possible files:** `motivation.md`, `system.md`, `rules.md`, `methodology.md`, `results.md`, `related-work.md`, `limitations.md`, `future-work.md`.
  - **Each brief should include:** claims, evidence, caveats, related-work hooks, figure/table hooks, and open questions.
  - **Done when:** the human writer can open a section brief and write by hand.

- [x] **P6.2 Create `outline-options.md`**
  - **Goal:** compare possible paper outlines without committing to one.
  - **File:** `paper-intelligence/outline-options.md`.
  - **Options:** systems-first, results-first, compiler-analogy-first, methodology-first, workshop-short-paper format, full conference format.
  - **For each outline:** strengths, risks, required evidence, and best venue fit.
  - **Done when:** paper structure is a deliberate choice.

- [x] **P6.3 Create `title-and-abstract-idea-bank.md`**
  - **Goal:** collect title/abstract directions as ideas, not final prose.
  - **File:** `paper-intelligence/title-and-abstract-idea-bank.md`.
  - **For each idea:** title, one-sentence pitch, paper angle, risks, and required evidence.
  - **Done when:** good framing ideas are saved for the human writing pass.

- [x] **P6.4 Create front-door current-state docs**
  - **Goal:** provide a compact state summary for Avery/collaborators without adding another competing entry point.
  - **Files:** `paper-intelligence/README.md` and `paper-intelligence/current-fit-and-publishability.md`.
  - **Must include:** current result state, open gaps, best paper angle, open questions, next experiments, and what input is needed from collaborators.
  - **Done when:** a collaborator can understand the paper state in under ten minutes.

## Recommended First Batch

Do these first:

1. P0.2 create `README.md`.
2. P0.3 create local agent instructions.
3. P0A.4 create `metadata-schemas.md`.
4. P1.3 create `artifact-inventory.md`.
5. P0.4 move/reference the local paper artifacts.
6. P1.1 create `repo-source-map.md`.
7. P1.5 create `result-validation-checklist.md`.
8. P1.2 create `results-ledger.md`.
9. P3.1/P3.3 create `literature-ledger.md` and `deep-research-inbox.md`.
10. P5.1 create `experiment-priority-board.md`.

This creates the intelligence base. After that, agents can safely collect literature, find gaps, and propose experiments while the actual paper remains a human-written artifact.
