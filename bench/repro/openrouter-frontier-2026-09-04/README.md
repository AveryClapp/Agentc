# Matched-context frontier experiment

Development evidence, not a paper result. The frozen acquisition manifest pairs
the original HotpotQA ten-paragraph context with the same question plus ten
extra distractor paragraphs. The two conditions share questions and must not
be counted as independent datasets.

The schedule contains 3 warmup, 20 calibration, and 160 heldout questions,
disjoint from the preceding pilot's 23 questions. Four pinned models cross
full and native ContextCompress inputs in each context (2,904 calls total).
Warmup runs only full inputs. The reinforced short-answer instruction and
512-token output cap were selected using the separate development screen,
before this manifest was frozen. Normalized token F1 is primary; exact match
is secondary. Neither scorer changes gold labels or accepts substrings.

## Calibration checkpoint, before heldout acquisition

All 344 warmup/calibration calls completed for $0.95105119. The shared ledger
then contained 615 completed calls, $2.12951515 spent, and no unresolved
reservations. No output was truncated. Two Sonnet extended-context outputs
(full and compressed on one question) exceeded 20 words; the improved format
instruction is therefore not a universal guarantee. All outputs remain scored.

Native compression rewrote all 20 extended calibration inputs per model and
none of the 20 natural inputs. Extended input-token reduction is approximately
36%. Calibration F1, full → compressed:

| Model | Extended F1 |
| --- | --- |
| Sonnet 4.5 | 0.7353 → 0.7486 |
| Haiku 4.5 | 0.7733 → 0.7333 |
| Gemini 2.5 Flash Lite | 0.5417 → 0.5417 |
| Qwen3 30B A3B Instruct | 0.6356 → 0.7289 |

These are development observations, not heldout estimates. Compression can
help, hurt, or abstain. In the natural condition full/compressed payloads are
identical, so any differences measure provider variability, not a rewrite.

`static_calibration_lock.json` freezes the cheapest nominal-cost candidate
within 0.02 mean F1 of full Sonnet on calibration. Fixed-model controls choose
full Haiku in both conditions. Static joint selection chooses Haiku/compress
(currently a no-op) for natural and Qwen/compress for extended. Every candidate
calibration call is charged in the static-control setup cost. This is an
empirical calibration constraint, not a certified probability-of-harm bound.

`replay-calibration.json` runs the real native guarded planner against exact
measured requests. All six configurations in both conditions retain the
source primary throughout calibration, with exploration charged separately.
Several alternatives are disabled after lexical disagreement. No gold labels
or unselected counterfactual answers reach the native controller. These are
offline selected-feedback trajectories, not a live deployment experiment.

## Reproduction

Use Python 3.13 and `PYTHONHASHSEED=0` with the exact native library and source
hashes recorded in `manifest.json`. Acquisition uses the shared append-only
pilot ledger, a $20 stage limit, and a $50 cumulative limit. A matching cached
request restores native observations without another provider call. Any
unresolved reservation stops further acquisition.

The modules `bench.openrouter_frontier`, `bench.openrouter_replay`, and
`bench.openrouter_frontier_analysis` expose their required paths through
`--help`. Freeze static selection with `calibrate` before acquiring heldout;
run `analyze` only after all scheduled outcomes exist. Replays use isolated
temporary profile stores and can separately test a calibration-boundary
restart without any new paid requests.

Actual billed cost is authoritative for spending. Provider caches are not
disabled: nominal uncached catalog-token estimates are reported separately.
Matrix acquisition warms caches, so replayed observed charges cannot establish
causal deployed-policy savings. Shared-host latency is diagnostic only.
Bootstrap intervals are paired within question, descriptive and unadjusted;
they do not account for calibration selection or repeated-provider uncertainty.
