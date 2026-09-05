# Guard sensitivity protocol

This frozen, zero-API analysis crosses five lexical-divergence thresholds
(0, 0.05, 0.15, 0.5, 1) with the six original routing/rewrite/joint policies,
in both matched context conditions. Threshold 1 is an intentionally unguarded
ceiling, not a safety setting. The grid was fixed on calibration before the
heldout acquisition completed; no best-heldout threshold is selected.

Each trajectory uses a separate temporary native profile store and sees only
the outcome of its selected primary, leased exploration, and sampled reference
calls. Gold answers are available only to the post-policy evaluator. Both
context conditions share questions. All results are exploratory offline replay,
not causal live-policy cost or latency measurements.

`calibration.json` retains the calibration-only trajectories. Full trajectories
are published as `complete.json.gz` because the uncompressed report can exceed
Git hosting's per-file limit. Compression is lossless and deterministic:

```sh
gzip -n -k complete.json
gzip -t complete.json.gz
```

The raw JSON remains local and is ignored only in this artifact directory.
To restore it in a fresh checkout, use `gzip -d -k complete.json.gz`.
Do not truncate decisions or remove unfavorable configurations to reduce size.
The 0.05 grid cells are controls for the frozen primary replay, not additional
independent experiments. Restart variants use the same grid and are labeled
separately.
