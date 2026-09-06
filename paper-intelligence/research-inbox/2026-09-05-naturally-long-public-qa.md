# Naturally long public QA: candidates after the current model×SD study

Date: 2026-09-05 (EDT). Bead: `bd-323l.6.13.8.6.20.14.4`.
Scope: primary documentation, repository metadata, static schemas, and published
aggregate statistics. No corpus download, candidate-ID query, task selection,
provider request, credential/ledger access, or change to the current 38 tasks.
This is a research recommendation, **not new acquisition authorization**.

## Recommendation

Prefer **QASPER train** for a later naturally long **single-paper,
multi-paragraph document workflow**: it has a small, versioned archive with a
published checksum and preserved paragraph structure. Do not call it
multi-document QA. If multiple independent documents are essential, investigate
**original MuSiQue-Ans train** next, but first pin its corpus bytes and measure
length/coverage without labels. **LongBench v2** is a poor immediate fit for the
current transport envelope and budget, despite its genuinely long contexts.

None supplies proof that AgentC will delete anything. Preserve the current
negative result: CC applied to **0/38** public HotpotQA tasks for both fixed
models. The current paid protocol remains the separately reviewed model×SD
study, not a replacement draw seeking a favorable CC result.
[Existing no-network coverage artifact](../../bench/repro/hotpot-train-typed-2026-09-05/coverage.json).

## What a suitable workload must actually satisfy

The [current CC implementation](../../crates/agentc-optimizer/src/rules/context_compress.rs)
first requires summed UTF-8 **message-content bytes >8192** and supplied scores.
A proposal additionally needs aligned per-message scores, at least 30% of
messages at/below the configured epsilon, and something removable after
user-input, follow-on-token, and role-preservation checks. The current adapter
uses epsilon 0.15 and the public lexical proxy. Long input alone is insufficient;
published token/word counts are not this byte predicate.

The code computes a **message fraction**, although comments call it a token
fraction. Documentation bug `bd-flh6` tracks that discrepancy; no frozen rule,
threshold, or runtime behavior was changed in this research.

The [current public adapter](../../bench/typed_public_workload.py) is deliberately
Hotpot-specific: ten paragraphs, Hotpot IDs, short natural answers, strict
string/null outputs, and 512 output tokens per stage. Every candidate below needs
a distinct workload/renderer/evaluator contract—not pretending its records fit
that interface. Retain actual native materialization and omit a fabricated
provider input bound.

## Candidate comparison

| Candidate | Native source units / genuine train partition | Fit for CC and 512-output workflow | Main limitation |
| --- | --- | --- | --- |
| QASPER | One research paper; ordered sections/paragraphs; 888 train papers | Natural fine-grained deletion units; short answers common, but full evidence quotations can exceed the cap | Single document, mixed answer types and figure/table dependence |
| MuSiQue-Ans | Twenty supplied Wikipedia paragraphs; 19,938 train questions | Closest multi-document filter→synthesis→answer fit; original contexts, not added padding | Native context-length distribution unverified; 2–4-hop tasks need more than a fixed 2–3-passage promise |
| LongBench v2 | 503 benchmark cases across six domains, including multi-document QA | Short multiple-choice final output; truly long input | A flattened context field, enormous tail, no genuine disjoint training partition established |

### 1. QASPER: strongest provenance, but a document-workflow claim

The authors report 5,049 questions over 1,585 NLP papers, with questions written
from titles/abstracts and answered from full papers. The published train split has
2,593 questions. Extractive and abstractive answers average 14.4 and 15.6 words,
respectively; these averages support investigating a short-answer cap, not a
guarantee for every answer or for intermediate quotations.
[Author paper, NAACL 2021](https://arxiv.org/abs/2105.03011v1),
[paper's aggregate description](https://arxiv.org/html/2105.03011v1).

The official AllenAI HF metadata pins:

- Repository `allenai/qasper`, revision
  `fdc9d8214fbab5dd782958601db4d678e6934a54` (2022-10-07).
- Data license **CC BY 4.0**; train has **888 paper records**, not 888 questions.
- Archive `qasper-train-dev-v0.3.tgz`, **10,835,856 bytes**, published SHA-256
  `a28fdf966db827bcee3d873107d6b6669864fb7ca8fbf73a192f5e39191bdb5a`.
  This checksum was read from pinned metadata, **not verified by downloading the
  archive in this task**. The archive bundles train and dev; any future extraction
  must use only the train member and must not inspect the dev member.

[Pinned metadata/checksums](https://huggingface.co/datasets/allenai/qasper/blob/fdc9d8214fbab5dd782958601db4d678e6934a54/dataset_infos.json),
[pinned loader/license](https://huggingface.co/datasets/allenai/qasper/blob/fdc9d8214fbab5dd782958601db4d678e6934a54/qasper.py),
[versioned archive location](https://qasper-dataset.s3.us-west-2.amazonaws.com/qasper-train-dev-v0.3.tgz).

Schema provides paper `title`, `abstract`, `full_text` sections with paragraph
strings, question records, and separate answer annotations. It also contains
figure/table captions and file references. Preserve all supplied text in natural
section/paragraph order; attach deterministic public section/position labels,
not evidence labels. Do not duplicate whole-paper titles on every paragraph just
to influence the lexical proxy. A text-only rendering cannot claim to preserve
unavailable visual information; do not remove questions by gold evidence type.
[Pinned schema](https://huggingface.co/datasets/allenai/qasper/blob/fdc9d8214fbab5dd782958601db4d678e6934a54/dataset_infos.json).

This review did not establish the original train distribution of rendered bytes
or CC applicability. The authors' long-document architecture supports inputs up
to 16K tokens; that is a model configuration, not a claim about every paper's
length. Do not substitute a repackaged benchmark's average for this corpus.
[Author paper](https://arxiv.org/html/2105.03011v1).

Answer types include unanswerable, extractive, yes/no, and freeform, with multiple
annotations. Dataset-level unanswerability must be distinct from protocol-failure
null; Hotpot's single-gold scoring is insufficient. Pin and parity-test the author
evaluator and establish the concise-answer contract before any paid pilot.
Author baseline/evaluator revision:
`afd0fb96bf78ce8cd8157639c6f6a6995e4f9089` (2021-10-27).
[Schema](https://huggingface.co/datasets/allenai/qasper/blob/fdc9d8214fbab5dd782958601db4d678e6934a54/dataset_infos.json),
[official evaluator](https://github.com/allenai/qasper-led-baseline/blob/afd0fb96bf78ce8cd8157639c6f6a6995e4f9089/scripts/evaluator.py).

### 2. MuSiQue-Ans: closest multi-document fit; acquisition pin incomplete

The original dataset has 2–4-hop questions, twenty context paragraphs per
question, and train/dev/test counts 19,938/2,417/2,459. Contexts combine supporting
paragraphs and retrieved distractors as part of the **published dataset**, not an
AgentC-added length extension. Questions were constructed through connected
single-hop composition; this is an external benchmark, not a claim of naturally
occurring user-agent traffic. MuSiQue-Full adds unanswerable contrasts and should
not be silently mixed with MuSiQue-Ans.
[Author paper, TACL 2022](https://arxiv.org/html/2108.00573v3),
[publication record](https://aclanthology.org/2022.tacl-1.31/).

Author repository revision `922ac98f19a201998dbdae6d7f2887a5258dbdeb`
(2024-06-12) states **CC BY 4.0**, provides all splits, and warns about leakage
through its seed QA datasets. Its official v1.0 download script points to a
Google Drive ZIP; the inspected script supplies **no content checksum**. The Git
commit pins the code/URL, not the remote ZIP bytes. Do not run that script
unreviewed or treat an arbitrary HF mirror as author-equivalent.
[Pinned author README](https://github.com/StonyBrookNLP/musique/blob/922ac98f19a201998dbdae6d7f2887a5258dbdeb/README.md),
[download definition](https://github.com/StonyBrookNLP/musique/blob/922ac98f19a201998dbdae6d7f2887a5258dbdeb/download_data.sh).

Public projection is the composed question and ordered paragraph
`idx/title/paragraph_text`. Keep `is_supporting`, decomposition answers/support
indices, gold answer, aliases, and answerability strictly evaluator-side. Preserve
each paragraph as one native unit; no artificial sentence splitting is needed.
[Official format conversion/schema](https://github.com/StonyBrookNLP/musique/blob/922ac98f19a201998dbdae6d7f2887a5258dbdeb/raw_data_to_official_format.py).

Twenty paragraphs do not establish >8192 bytes or removability. The original
length distribution remains unmeasured here. A four-hop question can need more
than the current filter's promised 2–3 passages; a reviewed future prompt must
preserve required evidence rather than keep that arbitrary cap. The 512-token
filter/synthesis cap could still be a bottleneck; final factoid output is the
easier part. Evaluate official aliases and path validity offline, without
decomposition/gold feedback to the application.

### 3. LongBench v2: genuine long-context stress, not a cheap train follow-up

The authors' ACL 2025 benchmark has 503 multiple-choice cases, contexts of
8K–2M **words**, and six domains including multi-document QA. The author README
loads the HF collection with `split='train'`, but describes it as an evaluation
benchmark. That loader label does not establish an independently held-out
training population; this review found no author-defined train/dev partition.
[Author paper](https://arxiv.org/abs/2412.15204),
[pinned author README](https://github.com/THUDM/LongBench/blob/2e00731f8d0bff23dc4325161044d0ed8af94c1e/README.md).

The redirected official HF namespace is `zai-org/LongBench-v2`, revision
`2b48e494f2c7a2f0af81aae178e05c7e1dde0fe9` (2024-12-20), with
**Apache 2.0** dataset-card license. Metadata identifies `data.json` as
465,490,535 bytes, LFS SHA-256
`15d61c22d92c96900b3c4948b6aeea218d3214b676a65df48e7b8555604c7fe2`.
No corpus bytes were fetched or checksum-verified here.
[Official HF metadata](https://huggingface.co/api/datasets/zai-org/LongBench-v2),
[pinned tree](https://huggingface.co/api/datasets/zai-org/LongBench-v2/tree/2b48e494f2c7a2f0af81aae178e05c7e1dde0fe9?recursive=true&expand=false).

Schema exposes one flattened `context` string, question, four choices, answer,
domain/sub-domain, difficulty, and length. Explicit document-boundary arrays are
not part of that schema; a faithful parser cannot be assumed without later
public-only inspection. Final choice output fits a small cap, but reducing huge
contexts to 512 tokens of selected quotations is a new semantic bottleneck.
Many cases can exceed the current 64-KiB serialized request envelope or target
provider context. Taking an allowed-size subset would change the target
population, not reproduce the full benchmark.
[Pinned dataset card/schema](https://huggingface.co/datasets/zai-org/LongBench-v2/blob/2b48e494f2c7a2f0af81aae178e05c7e1dde0fe9/README.md).

## Selection, provenance, and cost conditions for any later design

These are proposed requirements, not work authorized by this note:

1. Define the population **before corpus/ID inspection**. An explicitly named
   “long, text-renderable training documents within the existing transport
   envelope” population is legitimate; claiming it is an unconditioned dataset
   sample is not. A byte threshold must use the frozen renderer and distinguish
   native message-content bytes from complete serialized request bytes.
2. For a length-conditioned population, first approve a blinded indexer that
   emits only IDs and public structural/byte metadata—not question/body/label
   text. Freeze the eligibility predicate and index hash, then sample IDs before
   selected content is exposed. JSON archives need an audited projection step;
   do not claim Parquet-style column isolation from a whole-record JSON load.
   For QASPER, sample at paper level and predeclare question selection/clustering
   so repeated papers do not masquerade as independent tasks.
3. Preserve native document/paragraph boundaries, all original text/order, and
   source licenses. No oracle pruning, added distractors, padding, generated
   salience, or relaxed CC threshold. Measure CC coverage on **all** selected
   tasks; do not silently replace no-ops. Selecting on actual CC activation would
   be a separate optimizer-conditioned population, not merely length conditioning.
4. Freeze public/label separation, dataset-specific evaluator, typed schema,
   invalid-history propagation, and a small prospective 512-output format gate.
   A valid JSON shape is not evidence retention or semantic correctness. Refusal,
   truncation, and invalid paths remain visible failures.
5. Before any paid matrix, account for the pilot and every workflow stage/arm,
   uncached and observed-price forecasts, output caps, prior charges/holds, and
   the largest unresolved next-call allowance. The existing full-context monetary
   reserve is not an input-size certificate. No request-specific provider bound,
   native admission, or current remaining balance is established by this research.
   Finish the current model×SD protocol first.

Deferred bounded follow-up is `bd-323l.6.13.8.6.20.14.4.1` (QASPER ingestion and
contract design, requiring separate authority). No manuscript claim changes.

## Exposure and verification notes

No dataset viewer, corpus file, current task fixture, or candidate ID column was
opened. Broad search unexpectedly returned a published QA illustration and
third-party corpus-ID snippets; conservative incidental strings are recorded in
the Bead metadata for future exclusion/provenance, not treated as authoritative
dataset facts. Subsequent investigation used official metadata/schema files and
publication-statistics-only extraction. No candidate population was sampled.

QASPER `dataset_infos.json` bytes SHA-256:
`c5a5b7abb6a0a91b2d9b173cdf1224d3d6e400b3d658d322acb67e12305dd5bf`.
Corpus pin validation, original length distributions, native CC coverage, exact
text/visual availability, and a complete prospective spending plan remain
unverified. The strongest conclusion is a ranked research direction—not a
guarantee that another dataset will make CC work.
