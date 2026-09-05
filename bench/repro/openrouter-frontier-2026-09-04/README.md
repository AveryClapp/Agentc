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

## Complete heldout results

All 2,904 requests completed for $7.95189111. At this checkpoint the cumulative
campaign ledger contained 3,175 completed calls, $9.13035507 spent, and no
unresolved requests. The complete results digest is
`c1ed9f8c1ff8131d9257bf71b86d50bc6b1756311e20060b4ff4566b772999fc`.

Long-context compression, 160 paired heldout questions per model:

| Model | Nominal cost reduction | F1 change, percentage points | Descriptive paired 95% interval |
| --- | ---: | ---: | ---: |
| Sonnet 4.5 | 32.34% | +1.88 | [−1.31, +5.20] |
| Haiku 4.5 | 32.28% | +0.22 | [−2.69, +3.07] |
| Gemini 2.5 Flash Lite | 32.49% | −1.18 | [−5.97, +3.57] |
| Qwen3 30B A3B Instruct | 32.44% | +1.94 | [−2.61, +6.53] |

All extended prompts were rewritten. Unlike calibration, 28/160 natural
prompts per model crossed the rule's applicability threshold; 132/160 were
identical-payload repeats. Natural-arm differences therefore mix actual
rewrites and provider variability. Supporting paragraphs were removed on five
extended and four natural questions, identically across models. Retention is
not a correctness guarantee, and the rule is not proven semantically safe.
Sonnet still produced more than 20 words on 21 extended/full and 14
extended/compressed heldout answers; all remain in the frozen scorer.

The six native policies, each in both contexts, retained source Sonnet for
every one of their 2,196 decisions (including setup). They incurred 2,447
primary/exploration/shadow events. Setup-inclusive nominal overhead was
0.94–7.44% depending on policy/context, with no primary-quality change because
the selected request never changed. This is a negative result for the current
guarded controller, not evidence of successful adaptive routing.

The calibration-frozen fixed Haiku control saved 54.41% including setup on
extended contexts; its heldout F1 change was +2.85 points, with a wide paired
interval [−2.09, +8.12]. The static Qwen-plus-compression choice saved 71.43%
including all candidate calibration costs but lost 6.97 F1 points
([−13.27, −0.63]). Neither result establishes a certified risk contract. In
natural contexts, the fixed and static-joint controls lost 4.56 and 6.85 F1
points respectively.

Four-outcome interaction intervals are reported in `mechanisms.json`. The
extended-context intervals all cross zero: this study does not establish a
model-specific compression interaction. The natural Gemini contrast is only
an unadjusted exploratory result and mixes no-op repeats with exercised rules.

`replay-complete-restart.json` exactly reproduces the original replay's
selected outcomes, feedback and costs after a calibration-boundary restart.
The sensitivity grid's 0.05 cells likewise reproduce all 12 primary
trajectories. The separately reviewed refresh runtime generates eight extra
comparisons for expanded-budget extended rewrite-only, costing a nominal
$0.069141, but still changes no primary outcome. The other 11 trajectories
are unchanged. These tests do not establish adaptation to deployment drift.

The figure bundle is in
`../openrouter-frontier-figures-2026-09-05-v2/`; it labels this experiment
ContextCompress-only. The broader live multi-rule factorial is not measured
by this matrix. No other inactive rule is assigned a measured zero effect.
