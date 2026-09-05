# Multi-stage rule protocol: structural preflight only

`structural.json` exercises the actual native planner over a clean
filter → synthesize → answer workflow, using synthetic outcomes and zero
provider requests. It is not a quality, latency, cost-saving, or guarded
promotion experiment. The three call sites have separate stable identities;
each arm's downstream calls receive its own prior-stage outputs. Gold/support
labels are removed at the planner seam. State consumption is an explicit
workload annotation, not an inferred semantic guarantee.

Five configurations are structurally exercised: original, historical greedy
non-routing rules, guarded non-routing rules, routing-only, and joint. The
non-routing whitelist contains ContextCompress, StateDrop, PromptDedup,
OutputBudget and StructuredTruncation. Actual applicability is narrower:
there is no ToolOutput JSON here, and no duplicates are injected. CacheHit,
ParallelBranch and DeadOutputTruncation have explicit blocking contracts in
the report; they are not evaluated as zero-effect ablations.

The unchanged 480-decision protocol completes 252 synthetic explorations on
runtime `eb6d78a`, independently reproduced. Only historical greedy primary
plans selected rules in this short preflight: 29 filter ContextCompress,
29 synthesis OutputBudget and 29 answer OutputBudget selections. This does
not imply StateDrop is absent from candidate exploration or that a lower
output cap reduces actual billed tokens.

Native snapshot SHA-256:
`3cb3ae2c92ca1402115aade8b973cbd11ded10d1c5b9e8c6cc689f64de87ab59`.
The library is retained under `bd-yrvb/target/native-snapshots/eb6d78a/` and
was built from clean branch `fix/exploration-lease-roundtrip` using the same
locked/offline maturin command as the refresh artifact. Its source changes
enable existing serde_json exact float round-trips and add two regressions;
305 optimizer tests and clippy pass independently.

The earlier runtime failed when the default ModelDowngrade threshold
`.03f32` promoted to f64 changed by one ULP during JSON decoding. The fix
preserves the exact number and keeps strict lease validation. It does not
relax the risk threshold or modify the frozen paid matrix/runtime.

A live six-arm study additionally requires an independently calibrated
route-then-rewrite baseline, durable paid dispatch/probe accounting, real
arm-specific intermediate answers, and a separately frozen question split.
Those measurements are not present in this preflight artifact.
