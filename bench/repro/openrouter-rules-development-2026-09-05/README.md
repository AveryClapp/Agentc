# Live joint-system development screen: aborted, not efficacy evidence

This run exercised real multi-step, joint rewrite/model probes, but was **aborted
after a provider error contaminated one controller's state**. Do not use it as a
joint-system speedup, quality, guard-safety, or admission result. Do not resume
its manifest. The frozen raw records are retained for incident reconstruction.

## What was exercised

Each arm executes its own filter → synthesize → answer workflow through pinned
Anthropic endpoints on OpenRouter. Subsequent prompts use that arm's actual
primary outputs, never another arm's output or an exploration answer. Gold and
supporting-fact labels are stripped before native planning; gold is used only by
offline evaluation and the explicitly labeled static-router calibration.

| Arm | Control |
| --- | --- |
| Original | Source Sonnet 4.5, no optimization |
| Historical rules | Safe non-routing subset, original greedy one-rule planner |
| Guarded rules | Same subset with independent evidence/guard state |
| Routing only | ModelDowngrade only, Sonnet/Haiku 4.5 |
| Sequential | Calibration-frozen global model followed by independent guarded rules |
| Joint | Joint selection over non-routing rewrites and model routing |

The non-routing whitelist is ContextCompress, StateDrop, PromptDedup,
OutputBudget, StructuredTruncation. This is **not every legacy rule**:
DeadOutputTruncation, CacheHit and ParallelBranch are excluded with reasons in
the protocol, and StructuredTruncation does not apply to this text workflow.
Unavailable/no-op rules must not be presented as measured zero-effect ablations.

The prospective schedule contained four calibration questions across two models,
then three warmup and five development questions across six arms. The fixture is
the existing 500-question extended-context Hotpot workload. IDs were fresh
relative to 206 questions exposed in this campaign; older repository exposure is
not ruled out. There is no confirmatory holdout here.

Native settings retained hot threshold 3, **20 pairs per exact execution plan**,
the default rule divergence budgets, lexical disagreement, maximum rewrite depth
3, and bounded exploration. Five development questions cannot establish a newly
learned 20-pair admission. This was an initial $1 protocol/mechanism screen, not
a sufficiently powered effectiveness experiment. Changing output caps changes
plan identity; rule-family activation counts are not evidence sample counts.

Static calibration selected **Sonnet**, not Haiku: mean F1 0.70588 versus 0.67647,
with a fixed 0.02 margin. Sequential therefore became its own guarded-rules
controller on the source model. We did not force a cheaper selection to create
an apparent routing contrast. All 24 calibration calls are charged to this arm.

## Incident and evidence boundary

1. The first attempt stopped after 60 result records on an outer HTTP 429.
   A separately reviewed retry retained the failed request's entire $0.127608
   bound. Two unchanged ledger/account observations were a conservative project
   check, **not a provider settlement guarantee or proof of zero failed-call cost**.
2. The exact request was retried successfully; prior successful generations were
   not repurchased. State was reconstructed in fresh, isolated per-arm stores
   from paid observations. This is not native crash-recovery evidence.
3. Call **130**, guarded-rules/filter on `hotpot_5ae376a35542990afbd1e163`, returned
   partial text after 103.07 seconds with `finish_reason=error`,
   `native_finish_reason=overloaded_error` and a choice-level provider 503 error.
   Its provider-reported cost was zero; nominal upstream repricing was $0.009735.
   The old transport incorrectly admitted this as a successful observation.
4. The experiment was interrupted when this was detected. The affected arm's
   same-question calls 130–135 and later calls 139–141 inherited invalid history
   or profile state. One in-flight exploration remains unresolved. No further
   inference or retry is authorized by this artifact.

Provider failures can arrive inside an HTTP 200 response after output begins;
partial text is not evidence of successful completion. See the
[official error contract](https://openrouter.ai/docs/api_reference/errors-and-debugging)
and [billing/recovery research](recovery-billing-research.md).

Independent audit matched all **141 ledger result records** to requests, response
bodies, billing and distinct generation IDs. These are not 141 successful model
completions. It also reconstructed 116 original request intents and 115 completed
decision records. The clean pre-error prefix has **129 result records, 108
decisions and 21 comparisons**, costing $0.603775; one length stop is retained as
a cap outcome. Only one question completed across all six arms before the error.
The unaffected arms do not make the full run a valid efficacy experiment.

The pre-error comparisons cover 16 arm/site/exact-plan identities, with only
1–2 observations each versus 20 required. Real joint probes included
ContextCompress+ModelDowngrade at filtering and OutputBudget+ModelDowngrade at
answering. None was admitted onto the joint primary path. This establishes live
mechanism execution, **not net benefit**. It cannot distinguish reliable guard
rejection from insufficient evidence or establish harmful cap fragmentation.

Repeated identical source requests also varied lexically. Those are disjoint,
post-hoc pairs sharing questions, not independent dataset examples or semantic
false-positive labels. Downstream exact repeats are conditional on previous
outputs producing identical prompts, not representative of every downstream call.

## Accounting at abort

| Quantity | USD |
| --- | ---: |
| Known charges for this screen, including setup/probes/partial work | 0.657259 |
| Retained first HTTP429 allowance | 0.127608 |
| New interrupted-request reservation | 0.019698 |
| Conservative total for this screen | **0.804565** |
| Known charges across the campaign | 9.89259047 |
| Conservative campaign total including both allowances | **10.03989647** |

The campaign ceiling remains $50. No allowance was silently released or counted
as a successful response. All other paid drivers remain frozen until shared
failure/hold accounting is repaired. The original $1 stage was stopped for
correctness, not because its budget was exhausted.

## Artifact interpretation and reproduction

- [manifest.json](manifest.json) freezes sources, prompts, IDs, schedule,
  endpoints, prices, policies and the reviewed native binary. Its digest is
  `d3e81e86aafc129930bee535c549ceb7f84c13eb76f797eba1ec5f6a497a3066`.
- [calls.json](calls.json), [intents.json](intents.json), and
  [decisions.json](decisions.json) are the original acquisition record. The native
  snapshot is `eb6d78a`; acquisition source was frozen at `ae4c583`.
- [summary.json](summary.json) is the **unmodified old summary**. It does not flag
  the provider-error contamination. Its two-question F1 means must not be cited
  as valid comparison results or used to select a policy.
- [comparison.json](comparison.json) explicitly sets `analysis_eligible=false`
  and `comparison_available=false`: quality means and matched comparisons are
  suppressed, while all recorded costs remain accounted for.
- [diagnostics.json](diagnostics.json) likewise sets `analysis_eligible=false`.
  Its 25 pairs/17 identities include contaminated feedback and are retained only
  as raw incident diagnostics, not certified guard/admission evidence.
- [recovery/status.json](recovery/status.json) and
  [recovery/receipt.json](recovery/receipt.json) preserve both spending bounds.
  `recovery/raw-pre-validity-*` are superseded forensic drafts, not valid reports.

The comparison and diagnostic modules can be rerun offline with `--artifacts`
pointing here and `--output` pointing to a new file. Comparison also requires
`--fixture` pointing to the original hash-matching `long_context_qa_n500.json`.
Neither analyzer loads credentials, dispatches inference or constructs native
stores. Review passed 24 focused validity/analysis tests and independent ledger,
history, exact-plan and unequal-prefix accounting checks.

## Consequence for joint-system proof

The next experiment must be a newly frozen prospective study, with transport
failures kept out of capability profiles, all uncertainty allowances enforced,
enough observations per stable exact plan, and untouched matched evaluation
questions. End-to-end benefit must include profiling and shadow costs and beat
both rules-only and routing-only controls at the declared quality contract.
This run neither proves nor disproves that result.

Implementation and follow-up state lives in Beads: `bd-mof8` (provider-error
observation), `bd-4tvy` (analysis validity), `bd-o4h3` (whole-request deadline),
`bd-vpm4`/`bd-323l.6.13.9` (shared failure/hold accounting), and
`bd-323l.6.13.8.5` (new prospective efficacy protocol). The main experiment
remains open; no efficacy claim was added to the manuscript.
