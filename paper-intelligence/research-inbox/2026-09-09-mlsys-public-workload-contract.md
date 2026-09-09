# MLSys 2027 target and prospective FinQA workload contract

Date: 2026-09-09. Research task: **bd-8mz2**. Primary-source documentation,
evaluator code, and repository metadata only; no corpus rows, gold shards,
provider calls, or previous experiment outcomes were opened for selection.
This note authorizes neither data acquisition nor an experiment.

**Same-day clarification:** the [dated follow-up below](#2026-09-09-follow-up-local-research-versus-redistribution)
supersedes the blanket local-acquisition concern, not the separate redistribution
review. The initial finding is preserved here to make that change explicit.

## Decision

Proceed with a **synthetic-only, dependency-free FinQA adapter and execution
scorer**. Its natural pipeline is evidence retrieval → program generation →
deterministic execution: two LLM calls, not three. Corpus acquisition remains
blocked on a separate licensing decision (**bd-tprq**). TAT-QA is the explicit
dataset-license fallback if that gate cannot be resolved. The failed Hotpot
acquisition remains failed and unscored; this proposal does not resume it.

## Official next venue, not a guessed year

The current official target is **MLSys 2027**, research track. Its scope includes
agentic systems and ML runtimes. The CFP gives **October 30, 2026, 20:00 UTC**,
but the official dates page gives **12:00 PM PDT = 19:00 UTC**. Both also disagree
by one hour for October 10 opening. Plan against the earlier **19:00 UTC
(3:00 PM EDT)** until the organizers or submission system resolve this;
**bd-2r1p** records the discrepancy. This is not an AoE deadline.
([2027 CFP](https://mlsys.org/Conferences/2027/CallForResearchPapers),
[official dates](https://mlsys.org/Conferences/2027/Dates))

The research submission is double-blind, including affiliations and self-
identification; track cannot change after submission. Maximum: 10 two-column
pages excluding references, with every cited work's authors explicitly listed.
Use the linked 2025 style. Upload any appendix separately by the same deadline;
reviewers need not read it. Register conflicts and follow the dual-submission
policy. LLM consultation is allowed, but solely LLM-generated paper content is
prohibited except illustrative/experimental content. Artifact evaluation is
voluntary after acceptance. Reviews: January 18; response: January 21;
notification: February 28, 2027.
([CFP requirements](https://mlsys.org/Conferences/2027/CallForResearchPapers),
[style archive](https://media.mlsys.org/Conferences/MLSYS2025/mlsys2025style.zip))

The linked [OpenReview venue](https://openreview.net/group?id=MLSys.org/2027/Conference)
returned a loading shell during this check; it did not resolve the clock conflict.

## Why this workload, and the licensing boundary

FinQA consists of expert-written numerical questions over financial reports,
with annotated executable reasoning. That supports a meaningful retrieval-to-
program boundary and an objective endpoint without an LLM judge.
([original paper](https://aclanthology.org/2021.emnlp-main.300/))

The repository has an MIT license covering software and associated documentation.
The inspected README does not separately grant dataset/report rights. Do not
infer that every underlying report is public domain, or that public download
implies unrestricted redistribution. **This research does not establish that
FinQA is cleared for acquisition or redistribution.** Resolve that scope before
downloading real shards; synthetic implementation does not need them.
([pinned license](https://github.com/czyssrs/FinQA/blob/0f16e2867befa6840783e58be38c9efb9229d742/LICENSE),
[pinned README](https://github.com/czyssrs/FinQA/blob/0f16e2867befa6840783e58be38c9efb9229d742/README.md))

| Alternative | Primary-source facts | Decision for this bounded implementation |
| --- | --- | --- |
| TAT-QA | Explicit dataset CC BY 4.0; 16,552 questions over 2,757 report contexts. Raw train/dev metadata totals 14,483,078 bytes. Its metric handles normalized EM/F1, spans and scales. | Strong licensed fallback, but a broader answer contract than FinQA execution. Use `dataset_raw`, not derived TagOp facts/mappings. |
| Spider 1.0 | Dataset CC BY-SA 4.0; 200 databases; official test-suite accuracy; leaderboard closed and test set released in 2024. | Natural schema-link → SQL → execution, but requires database packaging and a read-only SQL sandbox. Download size not verified here. |
| BIRD-SQL | Official site states CC BY-SA 4.0 and 95 databases totaling 33.4 GB; execution-based evaluation, with versioned cleaned dev releases. | Full release is too large for the modest first implementation; mini variants require their own freeze and scorer review. |

Sources: [TAT-QA README/license](https://github.com/NExTplusplus/TAT-QA/blob/master/README.md),
[TAT-QA metric](https://github.com/NExTplusplus/TAT-QA/blob/master/tatqa_metric.py),
[TAT-QA tree metadata](https://api.github.com/repos/NExTplusplus/TAT-QA/git/trees/870accc41953dcde885aabeb963d94aabdc0fbc3?recursive=1),
[Spider official site](https://yale-lily.github.io/spider),
[BIRD official site](https://bird-bench.github.io/).
The tradeoffs in the last column are design judgments, not benchmark results.

## Exact FinQA source pin and size evidence

Official `refs/heads/main` resolved to commit
`0f16e2867befa6840783e58be38c9efb9229d742`. The following are GitHub Git-tree
metadata, not downloaded corpus contents. Git blob SHA-1 is **not** a raw-file
SHA-256; the latter must be computed after the acquisition gate.
([ref API](https://api.github.com/repos/czyssrs/FinQA/git/ref/heads/main),
[pinned tree API](https://api.github.com/repos/czyssrs/FinQA/git/trees/0f16e2867befa6840783e58be38c9efb9229d742?recursive=1))

| Pinned path | Bytes | Git blob SHA-1 |
| --- | ---: | --- |
| `dataset/train.json` | 78,216,616 | `a6c2c5a60f9abe3fba5a793cc4d9e1dee2e09209` |
| `dataset/dev.json` | 10,954,658 | `970f239f591c782b17df420c0d343b780737f3da` |
| `dataset/test.json` | 14,395,143 | `59958c7c3bb3b21f4dff6bc912a0fe0ae710aee0` |
| `code/evaluate/test.json` | 9,763,443 | `090dc7e7f1839d94b5be8fde1f2a88b34b6b878f` |
| `code/evaluate/evaluate.py` | 14,305 | `4829a4ca58a5725fb3f7a46b3b23b7f7e848a512` |

Train + dev = **89,171,274 bytes (~85.04 MiB)**. The two `test.json` paths are
different files; basename-only selection is unsafe. The inspected evaluator's
raw SHA-256 is `845cd131cab843eceff256cf6d392978cc470a7da4a80107beb56027fdca5c13`;
the license's is `92c0e67bd762c15c07ba60de09558338315a8ebf99aac4f51f487179809ac087`.

No token-length claim follows from these download sizes. After rights clearance,
measure public-projection bytes/tokens for the entire prospectively eligible
pool before fixing resource caps or paid budgets. Do not truncate examples or
select easy short programs using labels. A small ID-selected subset makes call
count manageable, but prompt cost and provider context fit remain unmeasured.

## Proposed implementation contract — not an acquired protocol

1. **Public projection:** retain only `id`, `pre_text`, `post_text`, `table`,
   and `qa.question`, preserving original text, table cells, and order. Never
   pass the entire `qa` object or derived retriever fields. The official README
   identifies `program`, `gold_inds`, `exe_ans`, and `program_re` as references,
   warns that provided gold retrieval is not end-to-end inference, and records
   a 2022 table-format label leak. Public test results are not private-challenge
   results. ([pinned data/inference documentation](https://github.com/czyssrs/FinQA/blob/0f16e2867befa6840783e58be38c9efb9229d742/README.md))
2. **Label-free selection:** prospectively freeze split names, seed/salt, count,
   exact raw hashes, and sorted hash-ranked IDs. For a modest initial design,
   six train format cases and 32 dev evaluation cases are reasonable proposals,
   not a power calculation or implemented selection. Exclude pilot annual-report
   groups from evaluation using company/year, not merely page filename. Prefer
   one evaluation question per report, selected by the same hash rule; otherwise
   cluster inference by report. Record public-only eligibility exclusions before
   seeing answers. Do not filter on question type, program length, gold evidence,
   correctness, model agreement, or estimated favorable optimization coverage.
3. **Retriever call:** expose the complete public document with positional
   evidence IDs; accept only a bounded, distinct set of valid IDs. Serialize
   chosen evidence in original order. Wrong evidence is an ordinary prediction,
   not a reason to replace the task. Null/invalid retrieval must propagate to
   the downstream contract and final failure accounting.
4. **Program call:** consume the question and that branch's selected evidence,
   predict typed DSL tokens, then execute without another LLM call. Freeze a
   program-step cap and numeric resource limits prospectively. Execution uses
   the original **public table**, matching the upstream executor's table lookup;
   labels never select rows. No Python `eval`, external tools, hidden retry,
   gold-supported retrieval, or branch-history substitution.

### Scoring semantics and strict deviations

The upstream executor supports `add`, `subtract`, `multiply`, `divide`, `exp`,
`greater`, and `table_max/min/sum/average`; prior-result references and constants;
comma/percent parsing; and table-cell currency/parenthetical cleanup. `greater`
returns `yes`/`no`. Final numeric results are rounded to five decimal places and
compared exactly with `qa.exe_ans`. Its symbolic program-equivalence metric is
separate. It strips the last token without checking EOF; table dictionaries
overwrite duplicate row labels. Critically, `evaluate_result` divides by the
number of **provided predictions**, without exact inventory validation
(**bd-ntst**).
([pinned evaluator](https://github.com/czyssrs/FinQA/blob/0f16e2867befa6840783e58be38c9efb9229d742/code/evaluate/evaluate.py))

Our proposed scorer instead requires exactly one prediction for every selected
ID: missing, extra, or duplicate records reject the report. Invalid programs
score zero in that fixed denominator. Require terminal EOF, complete steps,
backward-only references, finite real arithmetic, and explicit operation/size
limits. Preserve last-row-wins lookup for compatibility or explicitly version
any stricter duplicate-label policy; do not silently change it. Claim
**guarded execution accuracy**, not official leaderboard parity or symbolic
program accuracy. Successful supported finite programs should match upstream;
stricter malformed handling must be documented.

Synthetic acceptance tests should cover all ten operations, chaining, constants,
percent/comma/currency/parenthetical cells, five-place rounding, yes/no, row-label
collisions, missing EOF, malformed steps, forward/missing references, division by
zero, non-finite/complex results, resource limits, invalid upstream state, and
complete-inventory rejection. Add public-projection poison tests proving changes
to every excluded gold/derived field cannot change either request or selection.
No real answer is needed to implement or review those tests.

## Optimization and reporting limits

This pipeline may expose meaningful ContextCompress opportunities in the
retriever's public document; **actual native materialization coverage must be
measured without labels**. Zero coverage is a valid negative result. Program
generation already consumes a selected subset, so a second compression benefit
is not guaranteed. Two calls do not automatically create dead conversational
state: do not add a third call or irrelevant history to manufacture StateDrop.
Use only supported, observed opportunities and name the identifiable comparison.

Public development data can have appeared in model pretraining. Report-group
exclusion prevents our own within-study report leakage, not model contamination.
Freeze model/provider/schema identities, inventory, costs, failure policy, scorer,
and cluster analysis before evaluation. Score only after complete acquisition
and integrity checks; show invalids, failures and all acquisition costs rather
than a complete-case accuracy. An objective endpoint is useful evidence, not
automatic evidence of adaptive routing efficacy or MLSys readiness.

Research verification: official pages/source inspected; Git ref and file-size/blob
metadata checked through unauthenticated APIs; evaluator/license raw hashes
computed. No implementation tests were run by this research task. Open gates are
tracked in **bd-2r1p**, **bd-tprq**, and **bd-ntst**, not in a parallel task list.

## 2026-09-09 follow-up: local research versus redistribution

Task: **bd-tprq.1**. The initial review above stopped at the repository license
and README. That was insufficient grounds for blocking ordinary local research
acquisition: uncertainty about redistribution is not evidence that downloading
and privately evaluating the authors' published dataset is prohibited.

**New primary evidence.** The original paper's section 8, "Data Access and
Licensing" (PDF page 9), explicitly identifies the underlying FinTabNet reports
as CDLA-Permissive data and states that this permits the authors' additional
annotations and publication. This is a first-party statement about source-data
rights, not merely a generic MIT badge.
([FinQA paper, section 8](https://aclanthology.org/2021.emnlp-main.300.pdf#page=9))

There is a precise remaining inconsistency: footnote 6 labels the license
Permissive but links **Sharing 1.0**, and its cited Publish-section numbering
matches Sharing. **bd-wjbv** records this; do not silently choose a redistribution
license. The old [IBM FinTabNet page](https://developer.ibm.com/data/fintabnet/)
now redirects to the generic Data Asset eXchange, so it did not independently
resolve the variant during this check.
([paper footnote 6](https://aclanthology.org/2021.emnlp-main.300.pdf#page=9))

Both variants grant Use in section 2.1. Their definitions expressly encompass
accessing, copying, studying, analyzing, and evaluating data. Their publication
conditions differ: Permissive allows another outbound license with the required
notices; Sharing requires its own agreement. Both distinguish internal use from
making data available outside the entity, and exempt qualifying computational
results, which must not contain more than a de minimis portion of source data.
([CDLA-Permissive 1.0, sections 1–3](https://cdla.dev/permissive-1-0/),
[CDLA-Sharing 1.0, sections 1–3](https://cdla.dev/sharing-1-0/))

**Recommendation, not a legal guarantee:** a documented assumption relying on
the authors' official MIT repository release **and** their stated CDLA source
provenance is sufficient for this project's prospective **local-only research
download and evaluation**. No FinQA-specific prohibition of that use was found
in the inspected author sources; the README actively documents research
training/inference/evaluation. TAT-QA is not needed solely to avoid the earlier
blanket concern. Preserve the pinned source, hashes, MIT copyright/permission
notice, paper citation, and CDLA provenance in any local acquisition record.
([pinned MIT license](https://github.com/czyssrs/FinQA/blob/0f16e2867befa6840783e58be38c9efb9229d742/LICENSE),
[pinned research-use instructions](https://github.com/czyssrs/FinQA/blob/0f16e2867befa6840783e58be38c9efb9229d742/README.md))

Keep public redistribution of copied report text, tables, selected fixtures, or
response logs separately reviewed for the exact license, notices, and changed-
file obligations. Do not automatically classify sending report content to an
external model provider as local-only use; review that sharing arrangement
separately. Prefer releasing acquisition/selection/scoring code, source hashes,
IDs, and aggregate results without report excerpts until that scope is resolved.
Neither the MIT license nor the CDLA texts guarantees every upstream right or
replaces compliance with applicable law. The remaining uncertainty is specific
to those rights and sharing conditions, not a discovered private-use ban.

This follow-up examined documentation, paper licensing text, and license terms;
it downloaded no corpus, inspected no selected evaluation labels, contacted no
author, and made no provider call. It changes a research recommendation, not
execution authority, a frozen cohort, or any previous study's status.

**Exposure disclosure:** the PDF extractor also displayed published examples,
including Figure 1 and Figure 4, while opening the licensing source. These are
not unseen evaluation inputs. Future enrollment (**bd-lnai**) must exclude the
known **GRMN/2006** report and identify the other published-example reports by
matching public questions before any label access. Conservatively inventory all
paper examples, including appendices, rather than treating unmatched example
provenance as unexposed. No IDs, thresholds, or new outcomes were selected from
those examples during this licensing check.
