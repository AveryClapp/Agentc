# Public model × StateDrop development factorial

Frozen before primary acquisition on 2026-09-06 UTC; no outcomes are reported
by this prospective archive.

Manifest canonical SHA-256:
`b5b1769028a2b81d509907a1097838802b65e1efb214c3bffda0238a5a8750f3`.
Acquisition source remains `51a47a9562143b43f9efeb346cc387200eee709c`;
the manifest pins all 80 source files, fixture/coverage, provider facts/prices,
schemas/prompts, original ledger origin and immutable native materializer.

## Design and scope

The exact 32 primary tasks are disjoint from the six pilot tasks. They were
selected from pinned HotpotQA training IDs before selected text/labels were
read; all ten original paragraphs and their ordering are retained. See
[dataset provenance and licenses](../hotpot-train-typed-2026-09-05/README.md).

| Cell | Fixed model | Attempted rewrites |
| --- | --- | --- |
| S0 | Sonnet 4.5 | None |
| SR | Sonnet 4.5 | CC at filter; SD at answer |
| H0 | Haiku 4.5 | None |
| HR | Haiku 4.5 | CC at filter; SD at answer |

Every cell runs its own three-call filter/synthesize/answer workflow, for
384 scheduled calls. Arm order within each task is frozen independently of
labels and outcomes. Histories are never copied between cells. Schema, output
cap of 512, temperature zero, endpoint constraints, and parser are fixed.

**CC applies to zero of the 38 selected public inputs.** The four-cell decision
was made before any public paid call, retaining all selected tasks and native
thresholds. The actual intervention is model choice × answer-stage StateDrop;
this cannot establish a CC effect or CC×SD composition effect. SD may abstain
on ambiguous/invalid state, which remains in the measured population.

This uses direct native rule materialization, not native planner admission or
adaptive routing. The tasks are public training-set development data, not an
untouched confirmatory evaluation. There is one realization per cell and exactly
32 task clusters. Official offline answer EM/F1 and task-cluster descriptive
intervals do not constitute an online semantic-risk certificate.

## Pilot, forecast and budget

The [format/resource pilot](../openrouter-public-typed-pilot-2026-09-06/README.md)
completed 36/36 valid non-null calls for USD0.095678. Its gate used no labels
or answer accuracy. Exact raw-artifact hashes are embedded in this manifest.

The frozen four-cell planning forecasts are:

| Pricing basis | Expected | Conservative, 512 output tokens/call |
| --- | ---: | ---: |
| Observed endpoint/tier prices | USD1.052340 | USD2.780544 |
| Universal request price caps | USD3.082536 | USD8.322336 |

The conservative observed-price forecast passes the predeclared USD7 gate.
Input estimates use the largest pilot effective billed-input/byte rate and
largest same-model pilot history per stage. They do not identify provider
overhead separately, certify context fit, or guarantee spending. No caching
or rewrite savings are credited in these forecasts.

The hard primary stage allowance is USD13.904322 (USD14 family allowance minus
the exact pilot charge). Stop starting calls at USD7 known primary charges.
Every next call still requires its full contextual allowance; original campaign
charges, pending attempts, and all historical uncertainty remain counted against
USD50. At freeze USD22.02610353 remained conservatively available. These are
limits, not spending targets.

## Failure and analysis contract

The full selection/phase has a permanent claim beside the original private
ledger. No provider retry, partial resume, resampling, or replacement is allowed.
Application-invalid completions count as losses and propagate canonical null;
all scheduled later stages remain charged, and plausible later answers cannot
repair a failed path. Provider failure or budget stop preserves a partial
artifact without a complete-case efficacy contrast.

Only after the complete 384-call schedule passes exact own-history, native,
provider, source, and financial reconstruction may analysis open the separately
stored labels. Report every cell, application-invalid paths, actual rule
activation, paid resources, and paired model×rules contrasts. Cost interactions
include log-cost ratios so price multiplication is not mislabeled as learned
synergy. Cache and observational latency are disclosed, not treated as controlled
causal measurements.

Use [the frozen driver](../../openrouter_public_factorial.py) with this output,
the exact primary and pilot manifest hashes, the same bundle/immutable bridge,
and the original ledger/key paths. `run` is one-shot. `audit` reconstructs the
evidence; `analyze` produces the derived report. Do not edit any pinned source
while acquisition or reconstruction is active.
