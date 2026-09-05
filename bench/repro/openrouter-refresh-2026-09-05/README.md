# Reviewed runtime refresh ablation

This is an offline engineering ablation, not a new provider experiment or a
replacement for the frozen original-runtime results. It reuses the exact paid
matrix and six predeclared policies; only bounded refresh eligibility changes.

Runtime source: `355f099c5db8ca08b9df3fa44f3bb3db90479df8`, independently reviewed
under `bd-05sr.1`. The 303 optimizer tests pass, including failures reproduced on
the original code for stale-profile and divergence-bound recovery. The replay
adapter and its provenance guard were reviewed under `bd-323l.6.13.5.5.1` and
`bd-6cod`.

The new native artifact was built in that clean checkout with:

```sh
CARGO_TARGET_DIR=/Users/averyclapp/.worktrees/Agentc/bd-05sr/target \
CARGO_BUILD_JOBS=2 \
/Users/averyclapp/Documents/Coding/GitProjects/Agentc/.venv/bin/maturin build \
  --release --locked --offline --interpreter /opt/homebrew/bin/python3.13 \
  --out /Users/averyclapp/.worktrees/Agentc/bd-05sr/target/wheels
```

Retained artifact: `bd-yrvb/target/native-snapshots/355f099/lib_native.dylib`,
SHA-256 `ae232d0c54450c3c66c021360163031a55a41b42bab0ae69a90a8f0b30f70931`.
The original artifact remains unchanged at
`bd-yrvb/target/release/lib_native.dylib`, SHA-256
`42d3aa206e632c0de9192ddad92f6743845625d28116ff353f302d7ed9c78b95`.
Neither binary is committed; the manifest binds both source provenance and the
patched binary. The independent build used approximately 500MB of temporary
cache and produced a 12MB retained library.

The frozen manifest verifies all 84 acquisition source files in both checkouts,
permits exactly the reviewed four Rust-file differences, and binds the unchanged
analysis sources, policies, and model catalog. Gold is evaluator-only. Every
primary, exploration, and sampled-reference request must match a measured
payload and is charged in the replay.

`calibration.json` reports 12 policy/context trajectories with 23 decisions each.
All 276 decisions match the original calibration baseline in primary outcome,
revealed feedback, and cost. Independent replay reproduced that equality. This
is an isolation check, not evidence that refresh improves quality or savings.

Full held-out replay and restart comparisons are separate outputs and remain
tracked in `bd-323l.6.13.5.5`. Even after the fix, exploration can exhaust its
budget or remain disabled for excessive divergence. No deployment drift is
injected or measured by this experiment.
