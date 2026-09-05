# Bounded default Auto service control

All 366 planned calls completed: 3 warmup, 20 calibration and 160 heldout
questions in each matched context. The stage cost $0.10497640. Campaign totals
at completion were 3,541 calls and $9.23533147, with no unresolved reservations.

The request uses `openrouter/auto`, the same four-model allowlist as the
frontier, a constrained provider list, no fallback, and the unchanged
reinforced answer contract with a 512-token cap. No native optimizer runs.
Default cost-policy filtering and service session inference remain enabled.
This is a named external service configuration, not a four-model oracle or
an implementation of the paper's joint selector.

Every response selected Qwen3 30B A3B Instruct through Nebius. The returned
router pipeline, selected endpoint/model, requested model, usage and service
tier missingness are preserved in each row. Model/provider names are validated;
response metadata does not independently prove backend quantization tags.

| Context | Auto heldout F1 (%) | Source Sonnet F1 (%) | Paired change, points | Descriptive 95% interval |
| --- | ---: | ---: | ---: | ---: |
| Natural | 68.41 | 79.57 | −11.15 | [−17.09, −5.41] |
| Extended | 64.30 | 73.22 | −8.92 | [−15.19, −2.67] |

Auto's nominal setup-inclusive totals were $0.0351009 and $0.0698755 for
natural and extended respectively, compared with $1.089567 and $2.179578 for
source-only. Thus the very low cost comes with a measured answer-score loss.
The separate acquisition times and cache histories prevent a causal live
router latency/cost claim. These observations do not establish the quality of
Auto with another model pool, cost policy or workload.

`analysis.json` validates complete provenance and reports six comparisons:
source-only, calibration-frozen fixed model, and calibration-frozen static
model-plus-compression, in both contexts. Static controls are charged for all
candidate calibration requests. The paired interval fields refer to heldout
cost/quality; setup-inclusive totals are reported separately. Both contexts
reuse the same 160 heldout questions and are not independent datasets.

There are 357 distinct observable selected-model/opening-prefix groups; 18
calls occur in repeated groups of size two. These are not reconstructed or
verified private server session identities. The figure pack remains based on
the original frozen model × ContextCompress matrix; this service control is
additional evidence, not a replacement for the missing live full-rule study.

Reproduce analysis without API access using `python -m
bench.openrouter_auto_analysis --help` and the exact fixture files and frontier
artifacts. The existing `run` command resumes completed ledger entries without
redispatch; do not change the frozen manifest or delete the ledger.
