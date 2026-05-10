---
title: Drafted paper edits — paragraphs ready to drop in
status: draft
last-updated: 2026-05-10
owner: paper-intelligence
---

# Drafted paper paragraphs

These are intended to be dropped into the existing LaTeX without
restructuring. Each section heading below maps to a target location
in the .tex file.

## 1. §1 Introduction — drop "novel"

**Find** (in the contributions list):

> "A JIT optimization runtime ... featuring a novel two-gate
> applicability/proposal rule design and a novel two-gate
> applicability/proposal pipeline ..."

**Replace with**:

> "A JIT optimization runtime ... featuring a two-stage
> applicability/proposal rule design and an IDF-weighted online
> attention proxy ..."

The word "novel" is a no-go phrase per `evidence-and-sources.md`
until verified — the two-gate design is a contribution either way,
just call it that. Also drop the second "novel" earlier in the
sentence about the same pipeline.

## 2. §3.2 Two-Gate Rule Pipeline — strengthen the safety paragraph

**Append to the end of the safety-check paragraph**:

> "This separation between proposal and safety check is intentional:
> rules can claim arbitrary projected savings, but provenance
> invariants — including unconditional preservation of
> \texttt{UserInput}-tagged messages and per-role retention floors —
> are enforced by the planner, not by the rule. A rule cannot opt out
> of provenance constraints by claiming a higher savings projection."

## 3. §4 Rewrite Rules — append after the StateDrop block

**Append** (after the existing StateDrop paragraph, before CacheHit):

> StateDrop's correctness rests on a runtime read-window model, not
> sound program slicing. A \texttt{State}-tagged message with key
> $k$ is removable iff $k$ is absent from the per-call
> \texttt{window\_state\_reads} set populated by
> \texttt{agentc.state\_read}. This is conservative runtime pruning
> informed by classical liveness analysis~\cite{allen1976dataflow,
> weiser1981slicing,tip1995survey}, not dependency-graph slicing
> over a static program. The 50\% retention floor and unconditional
> system-message preservation provide additional defensive bounds:
> StateDrop will never empty the message list, and the read window
> alone never decides removal — the floor and provenance constraints
> override the rule.

## 4. §6 Evaluation — add overhead paragraph after the spend note

**Insert** (between the existing "Total API spend" sentence and §6.1):

> Optimizer overhead, measured across 1,818 plan decisions captured
> in the \texttt{plan\_audit} table during the experiments below,
> has median 76 \textmu s on the pass-through path (the common case
> when no rule fires) and median 120 \textmu s on the rewrite path
> (when a rule fires and produces a new plan). The pass-through
> distribution is bimodal: cold-start cost-model loads on first
> call-site invocation account for the p99 = 21 ms tail, while
> warm-state pass-through stays below 100 \textmu s. The rewrite
> path is tighter (p95 = 0.35 ms, p99 = 1.2 ms). All overhead is at
> least three orders of magnitude below typical LLM round-trip
> latency.

## 5. New §6.5 — Multi-rule activation (template, partial data in)

**Insert** as a new subsection after §6.4 Oracle Compression Ceiling.

> \subsection{Multi-Rule Activation}
> \label{sec:eval-multirule}
>
> The four prior subsections evaluate each rule on a workload built
> to isolate that rule. To test whether multiple rules can fire on
> the same trace under a single runtime control plane, we evaluate
> an additional workload designed to activate both ContextCompress
> and StateDrop preconditions.
>
> \textbf{Setup.} \texttt{multirule\_qa.json} reuses the
> \texttt{long\_context\_qa} fixture (20 paragraphs per task,
> ~13--18 KB raw context). For each of $n=20$ tasks, the agent issues
> three LLM calls: an initial answer pass, then two refinement
> passes that each see the same long-context document plus prior
> revisions. Prior revisions are state-tagged via
> \texttt{agentc.state\_write} but are not re-read into the current
> call's read window, so their state keys are absent from
> \texttt{window\_state\_reads} and they are eligible for StateDrop.
> The long-context structure remains eligible for ContextCompress at
> every step. ModelDowngrade is left inactive: its
> \texttt{gpt-4o} $\rightarrow$ \texttt{gpt-4o-mini} route requires a
> baseline model whose Tier-1 30 K-tokens-per-minute ceiling cannot
> support a 12-configuration ablation; we discuss its composition
> separately in §\ref{sec:limitations}.
>
> \textbf{Results.} Per-call audit data shows that both rules
> activate on this workload: in all-on configurations,
> ContextCompress fires on 54 of 60 LLM calls (90\%) because its
> projected savings exceed StateDrop's on every call where both
> apply. With ContextCompress disabled, StateDrop fires on 12 of 60
> calls (20\%) — the refinement steps where state-tagged prior
> revisions are out of the read window. Savings, however, are
> dominated by ContextCompress: removable distractor paragraphs
> ($\sim$3 KB each) dwarf state-tagged refinement revisions
> ($\sim$10 tokens each) in token volume. Table~\ref{tab:multirule-matrix}
> shows that all-on saves [TODO]\% cost / [TODO]\% input tokens, and
> ContextCompress-only matches that within [TODO]\%, while
> StateDrop-only contributes [TODO]\% on this workload.
>
> \textbf{Reading.} The multi-rule activation result establishes
> that a single trace can route through different rewrite families
> across calls, with the planner selecting the highest-projected
> savings per call (Algorithm~\ref{alg:planner}). It does not
> establish that rules \emph{stack} additively on the same call ---
> by design, at most one rule rewrites each call. On this specific
> workload, the volume asymmetry between distractor paragraphs and
> state-tagged revisions makes ContextCompress dominant; workloads
> with comparable per-rule volume would exhibit more visible
> per-call rule turnover.

(Numeric placeholders filled by `summarize_paired.py` once
`bench/paper_results/multirule_qa-cc_sd-n20-paired.csv` finishes.)

## 6. §6 Methodology — add paired analysis paragraph

**Insert** (after the existing "Standard errors on accuracy are
binomial" sentence):

> For workloads where per-task pass/fail outputs are available, we
> additionally report McNemar's exact test on discordant pairs and a
> 95\% bootstrap confidence interval on the accuracy delta. McNemar
> tests whether baseline-pass / treatment-fail and baseline-fail /
> treatment-pass counts are symmetric; failure to reject indicates
> that the optimization does not systematically degrade accuracy on
> tasks the baseline solved. Bootstrap intervals are computed by
> resampling task indices with replacement (5,000 iterations,
> percentile method).

## 7. Table additions — McNemar columns (after EXP-003 finishes)

For each ablation table that has paired data
(`iterative_refiner-statedrop-n50-paired.per_task.csv`,
`multirule_qa-cc_sd-n20-paired.per_task.csv`), append two columns to
the existing table headers:

```
... & Acc Δ & McNemar p & 95% CI \\
```

Numbers will be filled by `paired_analysis.py` after both ablations
finish.

## 8. §8 Limitations — add baseline / Tier-1 / MD paragraph

**Append** to the existing Limitations section:

> \textbf{Multi-rule ModelDowngrade composition.} The multi-rule
> evaluation in §\ref{sec:eval-multirule} excludes ModelDowngrade
> because the gpt-4o $\rightarrow$ gpt-4o-mini route requires
> \texttt{gpt-4o} as the baseline model, and the resulting input-token
> volume saturates Tier-1's 30 K-tokens-per-minute ceiling on bursty
> 12-configuration ablations. A throttled focused run with MD active
> on a smaller workload remains as future work; the current evidence
> for ModelDowngrade composition is the §\ref{sec:eval-md} matrix
> showing a 35.3\% saving on its own purpose-built routing workload.

## 9. (Optional) Title softening

Current: ``Agentc: Just-in-Time Optimization for Multi-Step LLM Agent Workloads''

Conservative alternative: ``Agentc: Toward Just-in-Time Optimization
for Multi-Step LLM Agent Workloads''

Aggressive alternative (drops JIT analogy entirely):
``Agentc: A Runtime Optimizer for Multi-Step LLM Agent Workloads''

Recommendation: keep the JIT framing in §3 (the analogy is well-
defended there), but soften the title with "Toward" since the
evidence does not yet match the most ambitious reading of "JIT."
