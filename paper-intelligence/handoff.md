---
title: Paper Intelligence Handoff
status: draft
last-updated: 2026-05-09
owner: paper-intelligence
---

# Paper Intelligence Handoff

## Current State

The paper-intelligence folder is useful now as a research map and gap-finding workspace. It is not a manuscript and should not be treated as final prose.

The core structure is in place:

- Results are summarized in `results-ledger.md`.
- Candidate claims are bounded in `claim-bank.md`.
- Open evidence gaps live in `paper-gap-register.md`.
- Reviewer objections live in `reviewer-risk-register.md`.
- Literature candidates live in `literature-ledger.md`, with checked source blurbs in `literature-verified-blurbs.md`.
- The whole-paper related-work shape lives in `literature-review-section-plan.md`.
- Venue strategy lives in `venue-positioning-matrix.md`.

The main paper framing is: **AgentC is a runtime optimizer for compound AI / multi-step LLM agent traces.** Routing is only one related-work subsection, not the whole paper.

## What Is Useful Already

- It prevents overclaiming by separating supported, promising, and unsafe claims.
- It shows the strongest current results: ContextCompress and ModelDowngrade, with StateDrop as promising but caveated.
- It names the closest related-work threats: Agentix/Autellix, Halo, Murakkab, AIOS, Cognify, DSPy, LMQL, SGLang, LLMCompiler, LLM-Tool Compiler, vCache, and the routing/compression/cache baselines.
- It makes the next evidence gaps concrete: end-to-end optimizer evidence, overhead/failure-mode evidence, cache/parallelism correctness, and stochastic evaluation.

## What Is Still Weak

- Literature blurbs are now primary-source checked in `literature-verified-blurbs.md`, but the ledger rows still need metadata cleanup.
- `nearest-neighbor-comparison.md` has the right threat set, but some rows still need final metadata/baseline cleanup from the verified pass.
- `CacheHit` and `ParallelBranch` should stay out of headline empirical claims unless new results are added.
- StateDrop needs stronger program-analysis/liveness support if we want to present it as principled.
- The folder has many files because it is ledger-based. Use the README fast-read list first; do not try to read every file linearly.

## Open Questions

- Should we finish StateDrop n=50 and real HotpotQA partial matrices?
- Which venue family should drive the first submission strategy?
- Where is the richer trace evidence for the Hotpot oracle ceiling?
- Which related work is closest enough to shape novelty claims?

## Next Best Action

Promote the verified literature pass:

1. Copy corrected metadata from `literature-verified-blurbs.md` into `literature-ledger.md`.
2. Update `bibliography-ledger.md`.
3. Tighten `nearest-neighbor-comparison.md` around the strongest threats.
4. Turn runnable baseline candidates into experiment tickets.
5. Update `paper-gap-register.md` for CacheHit, ParallelBranch, and stochastic-evaluation gaps.

After that, run one red-team pass from `red-team-review-prompts.md` and turn the findings into `weak-point-resolution-plan.md` items.
