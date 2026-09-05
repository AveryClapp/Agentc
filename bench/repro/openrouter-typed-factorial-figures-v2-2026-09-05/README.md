# Verified fixed-factorial development figures

Primary evidence: [complete acquisition and interpretation](../openrouter-typed-factorial-2026-09-05/README.md).
All 384 raw calls were reconstructed before rendering; no partial or selected
subset enters these figures. Bead `bd-323l.6.13.8.6.20.11.4`.

- [Cost and exact quality](01-cost-quality.svg): all four fixed configurations.
- [Paired interactions](02-paired-interactions.svg): all 32 task values, sample
  counts, mean and descriptive whole-task intervals.
- [Every paired task](03-all-paired-tasks.svg): correctness and paid cost relative
  to S0, in the frozen task order, with no outcome-based sorting.

PNG copies accompany every SVG. `plot_data.json` retains all values and full task
IDs. `rendering.json` records Matplotlib version and each image's file hash. Source
is `bench/openrouter_typed_factorial_figures.py` at commit `dd12c03`, SHA-256
`a9c81851641561602b23b5d95bca9828bf58e991334074fd7424cf8d8be9afca`.
Canonical input analysis SHA-256:
`962b12a8e2f1f27ab9322c42e19f5758986875df777cb79ebf5b5d6d2f5adc74`.

This v2 render fixes `bd-cxih`: it uses the canonical analysis bootstrap seed,
`joint-input-study-question-bootstrap-v1`, instead of the initial figure-only
seed. The exact quality interval now agrees with the analysis: +21.875 percentage
points, [3.125, 43.75]. The original render is preserved in the sibling directory
as superseded provenance. No input data, estimator, task membership or outcome
changed. The cost-interaction axis also uses a data-scaled range.

These are 32 generated development tasks and one realization per cell. They do
not establish generalization, native admission, adaptive policy superiority or
safety. Interval degeneracy is not certainty. Model-price times token reduction
explains the observed combined cost reduction; the interesting quality interaction
requires repeated and fresh-workload evaluation.
