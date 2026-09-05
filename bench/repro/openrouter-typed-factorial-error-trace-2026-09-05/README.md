# Where the primary factorial's errors occurred

Post-hoc diagnosis, 2026-09-05, Bead `bd-323l.6.13.8.6.20.11.5`.
Inputs are the completed artifacts committed at `acd3334`, with original manifest
`4e30338f35205059a2b3d5b32fe41ee2b771b4f35eeea18bc194b5730b2ac148`.
The [machine-readable trace](trace.json) retains all 32 tasks × four cells,
not just errors, with public row witnesses, actual stage values, receipt IDs,
input-pair checks, and source/artifact hashes.

## Result

All 128 document selections are correct. There are two incorrect joins, both on
T19 in the Haiku cells. Twelve final errors occur despite correct upstream facts;
the remaining final error faithfully propagates an incorrect join. One additional
Haiku-original final answer is correct while contradicting its own synthesis.

Counts below are out of 32 workflows per cell. Stage correctness is independently
derived from public fixture records and policy, not inferred from final scores.

| Cell | Correct filter | Correct synthesis | Correct final answer | Final answer follows its actual synthesis | All stages correct |
| --- | ---: | ---: | ---: | ---: | ---: |
| S0: Sonnet original | 32 | 32 | 30 | 30 | 30 |
| SR: Sonnet CC + SD | 32 | 32 | 29 | 29 | 29 |
| H0: Haiku original | 32 | 31 | 25 | 24 | 24 |
| HR: Haiku CC + SD | 32 | 31 | 31 | 32 | 31 |

There are 13 responses inconsistent with their authoritative synthesis's policy:
2 S0, 3 SR, and 8 H0. Every one chooses the **middle tier of the correct region**
when its supplied total requires a different tier. This is an observed output
pattern, not evidence of the models' internal reasoning.

## Eight tasks with any final error

Task numbers follow the frozen fixture order, not outcome sorting. All four
filters are correct on every row. All syntheses are correct except the two
explicit T19 exceptions below. A dagger marks a wrong final answer.

| Task | Public correct calculation and policy | Expected | S0 | SR | H0 | HR |
| --- | --- | --- | --- | --- | --- | --- |
| T05 | 17 × 88 = 1496; US, above 1340 | M49 | M49 | M49 | M63† | M49 |
| T06 | 14 × 89 = 1246; US, above 1181 | M79 | M90† | M90† | M90† | M79 |
| T08 | 19 × 89 = 1691; US, above 1019 | M58 | M58 | M58 | M63† | M58 |
| T09 | 5 × 46 = 230; US, at most 426 | M59 | M59 | M59 | M78† | M59 |
| T13 | 17 × 78 = 1326; US, above 1106 | M50 | M50 | M70† | M70† | M50 |
| T17 | 20 × 96 = 1920; US, above 1324 | M42 | M42 | M42 | M17† | M42 |
| T19 | 20 × 57 = 1140; EU, 468 < total ≤ 1169 | M80 | M80 | M80 | M80* | M56†* |
| T31 | 19 × 92 = 1748; US, above 1396 | M48 | M41† | M41† | M41† | M48 |

T19 is `structured-procurement-v1-e6ab87570090b17cc4f57368`. Its public join is:

- D13, sentence 2: requisition R003 → SKU K147, quantity 20, region EU.
- D15, sentence 8: SKU K147 → vendor V805.
- D06, sentence 5: vendor V805 → unit price 57, giving total 1140.

Both H0 and HR instead emit vendor V805 with unit 11 and total 220. Arithmetic
is internally consistent, but the offer lookup is wrong. Unit 11 exists in D06,
sentence 2, for **vendor V759**, not V805; this match does not prove which row the
model actually consulted.

The answer-stage contract treats the synthesis as authoritative and forbids
repair from stale selected records. Given total 220, EU's required answer is M56.
HR emits M56: locally correct policy application, globally wrong due to the join.
H0 emits M80: globally correct, locally inconsistent with its supplied synthesis.
We do **not** infer that H0 read the stale records or deliberately repaired the
join. Its observed errors compensate at the final-output metric; the original
25/32 score is preserved unchanged.

## What can be localized—and what cannot

For every task and each model separately, the two cells have equal parsed filter
values, identical recorded synthesis payloads, and equal parsed synthesis
values. Their transmitted answer payloads differ only by deleting the selected
records message in the rewritten cell. The audit checks all 64 within-model
pairs, including the two identical wrong T19 syntheses.

Thus the observed final-answer differences are not explained by different
authoritative synthesis values between the original and rewritten cells. This
locates the observed divergence at the final policy stage; it does not establish
a causal StateDrop effect or isolate provider randomness. These are single
realizations, with uncontrolled provider/time/cache effects. CC and SD were
assigned as one fixed workflow treatment, not as independently randomized
stage interventions. No adaptive model choice or native admission was tested.

The diagnostic uses labels only after acquisition. Public-fact derivation ignores
the evaluator label and then checks agreement with it. No solved facts enter
runtime feedback, prompt changes, or the already predeclared repeat. The original
protected held-out tasks remain outside this analysis.

## Reproduction and verification

From a checkout with the original 77 frozen acquisition-source hashes:

```bash
python -m bench.openrouter_typed_error_trace \
  --input bench/repro/openrouter-typed-factorial-2026-09-05 \
  --manifest-sha256 4e30338f35205059a2b3d5b32fe41ee2b771b4f35eeea18bc194b5730b2ac148 \
  --output bench/repro/openrouter-typed-factorial-error-trace-2026-09-05/trace.json
python -m pytest tests/test_openrouter_typed_error_trace.py -q
```

The audit checks complete ordering, frozen fixture/source hashes, saved artifact
digests, unique generation IDs, all own-history request reconstructions, recorded
native deletion projections, transmitted payloads, and agreement with the original
final scores. It does not rerun the native rule proposal or provider-ledger audit;
the completed primary's independent verification remains the authority for those.
No credentials, network calls, campaign ledger reads/locks, or input edits occur.
Nine tests pass, including label blindness, inclusive policy thresholds, invalid
history retention, the T19 compensation distinction, and no-network/no-ledger
execution. Ruff passes.
