# FinQA offline application contract

This is tested application/evaluation code, **not a model experiment or an MLSys
efficacy result**. No real FinQA rows, answers, or provider calls were used.

## Application

[`finqa_workload.py`](../../finqa_workload.py) implements two model-call stages:
retrieve public evidence IDs, then generate a FinQA arithmetic program. A bounded
interpreter executes the program; execution is not a third model call. All ten
upstream operation names are supported. There is no Python `eval`, tool execution,
network transport, credential loader, native admission, or quality-feedback API.

The public projection allowlists `id`, `qa.question`, `pre_text`, `post_text`, and
`table`. It never reads gold programs, answers, supporting facts, or precomputed
retriever outputs. Each table row uses the same JSON serialization regardless of
relevance. The program stage receives only the selected evidence and table header.
Unselected report text is not retained as manufactured dead history. Consequently
this application does **not** supply an automatic StateDrop opportunity; actual
ContextCompress applicability still requires native, label-blind measurement.

`call(task, stage, history, attention, model=..., run_identity=...)` returns a
native-shaped request. The attention implementation is injected. There is no
claimed provider input-token bound: client bytes are not a capacity certificate.
Every state dependency is declared live. Each experimental arm must maintain its
own history; a future runner must enforce this and prevent probe substitution.

After validating a provider receipt, the runner must call `application_text` with
the **observed** finish reason and upstream validity. Keep raw receipts separately;
do not pass an unvalidated or truncated provider response directly as history.
`execute` requires both canonical stage states. Invalid JSON, null, unknown or
duplicate evidence IDs, non-normal finish, invalid upstream state, forward
references, nonfinite arithmetic, and programs beyond 32 steps cannot earn a score.

## Scoring and source provenance

The source is [FinQA](https://github.com/czyssrs/FinQA), pinned to
`0f16e2867befa6840783e58be38c9efb9229d742`. The upstream
[execution evaluator](https://github.com/czyssrs/FinQA/blob/0f16e2867befa6840783e58be38c9efb9229d742/code/evaluate/evaluate.py)
has raw SHA-256
`845cd131cab843eceff256cf6d392978cc470a7da4a80107beb56027fdca5c13`.
Its code is provided under the repository's
[MIT license](https://github.com/czyssrs/FinQA/blob/0f16e2867befa6840783e58be38c9efb9229d742/LICENSE),
copyright 2021 Zhiyu Chen. This code is an independent bounded implementation of
the execution semantics, not the upstream symbolic-program-equivalence scorer.

`score_inventory(tasks, histories, labels)` requires exactly every selected ID in
all three inventories. Invalid application outputs remain in the denominator.
It compares final five-decimal numeric execution results or `yes`/`no` to separate
`exe_ans` labels with exact equality, not substring matching or a fitted tolerance.

The metric is named `guarded-finqa-execution-accuracy-v1`, **not official leaderboard
accuracy**. Our application rejects malformed programs that upstream may accept,
requires `none` as a table operation's second argument, and rejects ambiguous
row labels. Like upstream, execution receives the entire original public table;
the model's program-generation input is still limited to retrieved evidence.
Correct final execution alone does not prove correct retrieval,
reasoning, or a reference model's semantic correctness.

An opt-in test fetches only SHA-pinned evaluator **code**, extracts three inspected
pure functions without executing upstream imports/CLI, and compares 1,007 invented
valid programs. All matched. This is finite test coverage, not a proof for all
possible programs or all official corpus formats.

## Prospective cohort selection

[`finqa_selection.py`](../../finqa_selection.py) accepts ID-only source inventories
and hashes IDs with an explicit seed. It excludes whole company/year reports for
previously exposed IDs, and keeps calibration/evaluation reports disjoint. Input
order cannot change selection. Insufficient candidates fail; no automatic shrink,
retry, or reseeding. Multiple questions from a report remain possible within a
partition: uncertainty must cluster by report, not question. Report separation
does not imply company separation or absence from model pretraining.

No actual cohort, seed, source-file hash, model set, risk threshold, or paid protocol
is frozen by these unit tests. Synthetic tests use negative seed `-2026090901` and
invented `INVENTED*/1900/...` IDs, not a future real evaluation cohort. Repository
MIT metadata is not treated as a separate license guarantee for underlying report
content; real-data acquisition/redistribution remains a tracked gate. Source-file
acquisition, independently reviewed ID checkpoint, separate sealed label files,
live driver integration, and predeclared full-cost policy comparisons are not
implemented by this module. The existing three-stage drivers intentionally do not
recognize it yet.

## Verification

From the repository root with the project's test environment:

```bash
python -m pytest tests/test_finqa_workload.py tests/test_finqa_selection.py tests/test_finqa_upstream_parity.py -q
FINQA_UPSTREAM_PARITY=1 python -m pytest tests/test_finqa_upstream_parity.py -q
ruff check bench/finqa_workload.py bench/finqa_selection.py tests/test_finqa_workload.py tests/test_finqa_selection.py tests/test_finqa_upstream_parity.py
```

Default: **87 passed, one intentional network-test skip**. With the explicit
upstream-code check: **88 passed**, including all 1,007 parity comparisons. No
third-party runtime dependency was added. No frozen experiment artifact changed.
