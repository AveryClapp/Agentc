# FinQA local development cohort

Task **bd-lnai**. This directory contains acquisition code provenance, exposure
fingerprints, IDs, and aggregate diagnostics. Corpus reports and selected answers
stay in private directories outside every Git checkout. No provider call or
quality result is produced by this acquisition.

## Prospective selection

Before download or selection, Beads recorded seed `2026090901`: 70 hash-ranked
train cases, with the first six reserved for format checks and the remaining 64
for calibration; 64 dev cases form the evaluation partition. The existing
`finqa-report-disjoint-id-selection-v1` selector excludes whole exposed
company/year reports and prevents a report appearing in both partitions.
Multiple questions from a report can occur within one partition; analysis must
cluster by report. These development sizes are not a power calculation.
No reseeding, shrinking, label-based selection, or coverage-based substitution.

The original source is pinned to
[`czyssrs/FinQA` at `0f16e2867befa6840783e58be38c9efb9229d742`](https://github.com/czyssrs/FinQA/tree/0f16e2867befa6840783e58be38c9efb9229d742).
Exact file sizes and Git blob hashes are checked before parsing, and raw SHA-256
hashes are bound into the selection. Only train/dev can be enrolled. The test
file supports public-field lookup of reports exposed by published examples;
its labels and outcomes are not consulted, and its IDs cannot be enrolled.
The original README and MIT notice are retained with the private raw sources.

The [paper](https://aclanthology.org/2021.emnlp-main.300.pdf) was reviewed across
all 14 pages, including image-only annotation screenshots. Figures 1-3 repeat
GRMN/2006; Figures 5-6 identify AWK/2014 and K/2013. Three Figure 4 questions
require exact normalized public-question matching. Figure 7 additionally exposes
a PNC report: public text must corroborate the screenshot, and every matched
report is excluded. Figure 8 contains blank annotation fields. The versioned
[exposure inventory](exposures.json) stores question hashes and one short public
fragment; unmatched exposures stop enrollment. Exclusions address our recorded
exposure, not possible model pretraining contamination.

## Separate acquisition stages

Run from the repository root. `CACHE` and `BUNDLE` below must be private local
directories outside Git; `BUNDLE` must not already exist. The CLI prints only
operation/result hashes. It never prints reports or answers.

```bash
python -m bench.finqa_acquisition download --cache "$CACHE"
python -m bench.finqa_acquisition select --cache "$CACHE" \
  --output bench/repro/finqa-cohort-2026-09-09/selection.json
```

Commit and independently review the selection and exact acquisition source
closure before passing its full commit ID to materialization:

```bash
python -m bench.finqa_acquisition materialize --cache "$CACHE" \
  --selection bench/repro/finqa-cohort-2026-09-09/selection.json \
  --checkpoint "$REVIEWED_COMMIT" --output "$BUNDLE"
python -m bench.finqa_acquisition census --cache "$CACHE" \
  --output bench/repro/finqa-cohort-2026-09-09/public-census.json
python -m bench.finqa_acquisition native-census --bundle "$BUNDLE" \
  --binary "$MATERIALIZER" --binary-sha256 "$MATERIALIZER_SHA256" \
  --output bench/repro/finqa-cohort-2026-09-09/native-census.json
```

Materialization verifies the committed selection and source bytes, replays the
selection from verified source bytes, and writes the entire public allowlist
before accessing selected `exe_ans` fields. Answers are written separately as
`labels.sealed.json`, hashed, and changed to mode `000`. This is a procedural
seal, not encryption or a defense against the file owner. A future reviewed
runner/evaluator must enforce completion and integrity gates before unsealing
and scoring. No evaluation authorization is implied by possessing the bundle.

The public census includes all train/dev rows and reports schema failures
without replacing tasks. Byte sizes are not model token counts or certified
provider capacity. Native coverage measures actual forced retrieve-stage
ContextCompress/StateDrop proposals with a pinned binary; it does not run a
planner or establish safe admission, answer quality, or cost savings. Actual
program-stage coverage requires each arm's observed retrieval output and is
left to the separately reviewed two-stage runner, **bd-sjrg**.

Local research use follows the recorded **bd-tprq.1** source-provenance decision.
External-provider uploads and redistribution remain separate **bd-tprq** scope;
the paper's CDLA variant discrepancy remains **bd-wjbv**. Preserve source notices.

## Synthetic verification

`tests/test_finqa_acquisition.py` checks exact source and checkpoint failures,
published-example exclusion, selection without gold access, report separation,
public-before-label ordering, private sealed outputs, no silent overwrite or
resampling, and public-only census operation without a label file. Existing
FinQA selector/application tests remain the application contract.
