# Model × ContextCompress ablation figures

These are reproducible exploratory figures, not a full-rule factorial or
publication-ready safety claim. They use the complete frozen 2,904-call
matrix: 160 heldout questions, four models, two matched context conditions,
full/compressed requests. The contexts reuse question identities.

- `01-model-rewrite-opportunity.svg` / `.png`: per-answer nominal cost and
  token F1, with compression activation/no-op counts. No setup is included.
- `02-policy-cost-quality-ablation.svg` / `.png`: setup-inclusive costs and
  paired F1 changes. Every static calibration candidate and every native
  primary, exploration and sampled-reference call is charged.
- `03-model-rewrite-interactions.svg` / `.png`: paired four-outcome
  difference-in-differences, with unadjusted descriptive intervals.

The primary native policies never left the source request in this experiment;
their zero quality deltas are exact reuse, not independent evidence of safety.
The cheapest calibration-frozen joint control loses quality on heldout.
Large savings alone are therefore not an adequate system result.

`plot_data.json` binds matrix, replay, mechanism and plotting-source hashes.
`rendering.json` binds the numeric data, Matplotlib version and all six export
hashes. Intervals are conditional on fixed calibration/realized trajectories;
they do not rerun the adaptive policy or estimate training/provider uncertainty.
Prices are nominal uncached repricing of observed tokens, not causal live
deployment bills. See the acquisition README for actual budget spending.

Reproduce from the experiment checkout with its declared `viz` dependencies:

```sh
python -m bench.openrouter_figures \
  --artifacts bench/repro/openrouter-frontier-2026-09-04 \
  --natural /path/to/hotpot_distractor.json \
  --extended /path/to/long_context_qa_n500.json \
  --replay bench/repro/openrouter-frontier-2026-09-04/replay-complete.json \
  --mechanisms bench/repro/openrouter-frontier-2026-09-04/mechanisms.json \
  --output /new/empty/output-directory
```

The generator refuses incomplete policy coverage, changed settings, mismatched
task chronology/provenance, and existing outputs. The earlier `...-2026-09-05`
bundle is retained for audit; this v2 changes only legend placement and adds
visible unique-question labeling, not the data or comparisons.
