# OpenRouter model × ContextCompress pilot

Exploratory provider-backed measurements, **not paper evidence** and not an
evaluation of the learned joint-routing policy. All 172 scheduled calls completed
on September 4, 2026. The paired analysis validates identities, schedule coverage,
provider attribution, original gold labels, saved scores, and usage costs.

## Result

On 12 held-out questions, native ContextCompress reduced total input tokens by
37.0–37.2% and billed inference cost by 35.3–37.0% within each model. This is an
opportunity characterization, not a demonstrated routing improvement or safety
guarantee.

| Model | Strict exact matches, full → compressed | Paired losses / gains | Full cost | Compressed cost | Cost reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| Claude Haiku 4.5 | 7/12 → 7/12 | 0 / 0 | $0.054633 | $0.034971 | 36.0% |
| Claude Sonnet 4.5 | 0/12 → 2/12 | 0 / 2 | $0.176859 | $0.114423 | 35.3% |
| Gemini 2.5 Flash Lite | 9/12 → 9/12 | 0 / 0 | $0.0050068 | $0.0031536 | 37.0% |
| Qwen3 30B A3B Instruct 2507 | 6/12 → 7/12 | 1 / 2 | $0.0052715 | $0.0033317 | 36.8% |

A paired loss means the full response passes strict EM but the compressed
response does not; a gain means the reverse. Qwen's improved aggregate therefore
hides one regression. In the separate eight-question calibration split, Gemini
had one loss and one gain; Qwen had one loss and no gains. Do not combine the
splits to select an arm and then call that selection held-out performance.

The sample is too small to establish a low damage rate. For example, Gemini has
zero held-out losses among nine full-context exact matches, but its descriptive
95% Wilson interval still has an upper endpoint of 29.9%. Haiku's corresponding
upper endpoint is 35.4%. These intervals assume independent questions, are not
adjusted for multiple comparisons, and are not safety certificates.

## Answer-format limitation

The prompt asks for a short answer only, and the frozen output cap is 128 tokens.
Sonnet frequently provides an explanation instead. Ten of its twelve full-context
responses contain the normalized gold-token sequence despite failing exact match;
five hit the output cap. Haiku's five non-exact full-context responses also contain
the gold-token sequence. These observations flag formatting sensitivity, not a
replacement accuracy score: token presence also matches quotations, negations,
and contradicted answers.

**Do not interpret Sonnet's EM as zero reasoning accuracy, or use it to claim that
cheap-model routing preserves semantic quality.** Original EM/F1 scores remain
unchanged. A later protocol needs development-only answer-contract validation and
a new disjoint held-out sample before scaling. All 23 questions used here are now
development/exploration data for that later protocol.

## Protocol and cost

- Four fixed model/provider pairs; provider and model fallbacks disabled.
- Three disjoint warmup questions × four full-context arms: 12 billed calls.
- Eight calibration questions × four models × two rewrite arms: 64 billed calls.
- Twelve holdout questions × four models × two rewrite arms: 96 billed calls.
- Deterministic question selection and within-question arm ordering, independent
  of labels. One sample per model/arm/question; no repeated-run uncertainty study.
- Actual native `ContextCompress`, `current_greedy`, epsilon 0.15, hot threshold
  3, composition off. All 80 compression arms rewrote; no synthetic warmup
  observations, reference sampling, or quality-admission guard was used.
- The same short-answer prompt, temperature 0, and 128-token limit for all models.
- OpenRouter provider pins: Anthropic for both Claude models,
  `google-ai-studio` for Gemini, `nebius/fp8` for Qwen. Catalog snapshots describe
  observed serving cohorts, not immutable backend binaries.
- Host contention invalidates clean wall-clock performance claims. Recorded
  latencies are diagnostic only; no speedup is claimed.

| Billed phase | Cost |
| --- | ---: |
| Warmup | $0.0583989 |
| Calibration | $0.2613881 |
| Holdout | $0.3976496 |
| Entire matrix | **$0.7174366** |
| Earlier three accounting smokes | $0.0000519 |
| Cumulative ledger at completion | **$0.7174885** |

There were 175 completed calls including smokes, no unresolved reservations, a
$5 matrix-stage ceiling, and a $50 cumulative authorization ceiling. The
per-model table excludes setup because it compares matched held-out inference;
the table above includes all actual campaign charges. These are reported API
charges, not a claim about unused account balance or end-to-end policy cost.

## Artifacts and reproduction

- `manifest.json`: immutable schedule, model/provider snapshots, native catalog,
  dataset/native/source hashes, and generation settings.
- `results.json`: all 172 raw answers, billed usage, generation attribution,
  native plans, and frozen strict EM/F1 scores.
- `summary.json`: original per-phase/model/arm aggregates.
- `paired_analysis.json`: post-hoc paired analysis and integrity bindings,
  generated without API calls by `bench/openrouter_analysis.py`.

Canonical manifest SHA-256:
`fff7ec44bfd57c0d874e143eb23fe5c98e0fef0cf5d284663daba5d8b2e1accc`.
Canonical results SHA-256:
`118cd0f7e01178c65657a51464e1ed75ce30b5ab61c36da64f94ac6913dc6c84`.

The manifest was frozen before committing the new harness. Its `source_commit`
is base `7b93c54`; its exact `source_files` hashes correspond to **`d343d74`**, the
substantive pilot implementation commit. Use those hashes and the native-library
hash for provenance, not the base commit alone. Later bug fixes do not alter this
frozen manifest or silently relabel the results.

The initial 20-call stage was resumed by replaying successful ledger entries,
without another provider dispatch for those calls. In v1, prefix native plans
were regenerated during this replay, and prefix result artifacts could overwrite
longer ones (review bug `bd-uqeo`). The 172-row final artifact is complete; the
first 20 plan fields are replay reconstructions, not separately retained original
dispatch-plan records. Changed provider-visible payloads would have failed the
ledger fingerprint check. Preserve the final artifact before any legacy replay.

From the repository root, with the original local fixture available:

```bash
python -m unittest tests.test_openrouter_pilot tests.test_openrouter_matrix tests.test_openrouter_analysis
python -m bench.openrouter_analysis \
  --artifacts bench/repro/openrouter-pilot-2026-09-04 \
  --fixture bench/fixtures/long_context_qa.json
```

The analysis requires no credentials or network. The fixture hash must match the
manifest. Paid re-execution requires a separate budgeted stage; never discard the
shared ledger to rerun this identity. Credential files and the full shared ledger
are deliberately not published.

The research question still open is whether a learned, risk-controlled joint
planner beats fixed models, routing-only, rewrite-only, and AutoRouter once
calibration, reference calls, fallback, and adaptation costs are included. This
pilot does not evaluate those policies, direct-Anthropic gateway sensitivity,
composition, drift, or multiple workloads.
