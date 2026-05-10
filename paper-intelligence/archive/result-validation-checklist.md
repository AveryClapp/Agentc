---
title: Result Validation Checklist
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Result Validation Checklist

Every new result starts as `quarantined`. A result may enter `results-ledger.md` as `canonical` or `headline-ready` only after validation.

## Required Metadata

- Result ID: `RES-###`
- Source artifact ID: `ART-###`
- Experiment/run ID: `EXP-###` if generated locally
- Git commit SHA
- `git status --short` snapshot
- Command or script path
- Script checksum if result was generated locally
- Dataset/fixture path and hash if available
- Model name and provider
- Pricing date or pricing source
- Temperature / random seed / sampling settings
- Start/end timestamp for new runs
- Expected row count and observed row count

## CSV Checks

- Header matches expected schema.
- Row count matches planned config count.
- All expected configs are present.
- Partial files are explicitly labeled `partial`.
- Numeric fields parse cleanly.
- Costs are nonnegative and plausible.
- Input-token deltas make sense for the rule.
- Accuracy deltas include sample size and caveat.

## Promotion Gates

| Status | Allowed Paper Use |
|---|---|
| `quarantined` | None |
| `partial` | Diagnostic only; no headline claim |
| `diagnostic` | Methods/limitations/pushback discussion |
| `canonical` | Results table or appendix |
| `headline-ready` | Headline result if claim caveats also pass |
| `needs-rerun` | Gap register only |
| `do-not-use-yet` | Do not cite |

## Failure Handling

If any required check fails:

1. Keep or set status to `quarantined`, `partial`, or `needs-rerun`.
2. Create or update a `GAP-###` entry.
3. Do not update claim status to `supported`.
4. Record the missing evidence or rerun command.

## Current Validated Baseline

The committed CSVs under `bench/paper_results/` are accepted as existing source artifacts, but the ledger still records their caveats. Newly generated replacements must pass this checklist before replacing those entries.
