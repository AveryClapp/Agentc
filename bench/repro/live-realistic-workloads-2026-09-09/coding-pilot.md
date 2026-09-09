# Fresh coding workload pilot

Four fresh mini-SWE-agent executions made 70 provider calls for $0.0474188.
The baseline requests run submitted a patch after 10 calls; both compression
runs and the pytest baseline reached the 20-call limit without submitting.
These results do not establish an optimization benefit. Compression runs
repeated tool commands and spent more in aggregate than the baseline runs.

| Task | Arm | Calls | Rewritten requests | Charged USD | Agent outcome |
|---|---|---:|---:|---:|---|
| pytest-dev__pytest-7324 | source | 20 | 0 | 0.0129640 | Step limit |
| pytest-dev__pytest-7324 | compress | 20 | 17 | 0.0118064 | Step limit |
| psf__requests-1766 | compress | 20 | 18 | 0.0165764 | Step limit |
| psf__requests-1766 | source | 10 | 0 | 0.0060720 | Submitted |

All four prospective arms remain in the denominator. No task was replaced,
continued beyond its declared limit, or retried. A submission alone is not a
correctness result: grading is recorded separately in `coding-results.json`.

The official pytest control produced the expected three failing regression tests
and 80 passing tests. Neither pytest arm submitted a patch. The requests control
produced 46 failures and 45 passes, including failures among preservation tests;
it is not a valid grading environment for this task. The requests submission
also failed strict patch application. Requests correctness therefore remains
unscored, and the paired quality comparison is invalid. These infrastructure
results are retained rather than counted as model repair failures or successes.

## Prospective selection and execution

The two development tasks were selected before task-body inspection, using
SHA-256 ranking of `2026090903\n<instance_id>`, one each from pytest and requests.
The repository restriction was chosen for local environment feasibility.
The 12 prior archived-pilot IDs were excluded. Selection did not filter on
success, input length, or optimizer coverage. This small development pilot is
not a held-out efficacy cohort.

- Dataset: SWE-bench Verified revision
  `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`; parquet SHA-256
  `a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd`.
- Agent: original mini-SWE-agent v1 commit
  `a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d`, unchanged default agent and
  SWE-bench prompts. Its historical import package is `microsweagent`.
- Dispatch: OpenRouter `openai/gpt-4.1-mini`, OpenAI-only provider routing,
  dated endpoint `openai/gpt-4.1-mini-2025-04-14`, temperature zero,
  maximum 2,048 output tokens per call.
- Frozen runner commit: `ed92dd10a92a1b42b290a6033158bdbb37c9f8b5`.
- Frozen manifest SHA-256:
  `3f0e329f0317b5520839c7b31babf58a0af49a958e0178d338a9c4c0da660e36`.
- Executed native materializer SHA-256:
  `9b5f4e3d5c3f7e71b2db0c765f0a3da43b2192c88b3626cebebc206002d48f23`.

Every arm used a new digest-pinned Linux container, with no host mounts or
network, and its own actual model responses and shell results. Image preparation
commits were replaced by the selected task commit only after checking for a
clean worktree; exact commit and cleanliness were checked again before dispatch.
Gold patches, test-oracle fields, host credentials, and archived model answers
were never exposed to the agent container.

The compression arm applied fixed ContextCompress proposals to the next model
input. It did not replace the agent's accumulated history, reuse another arm's
outputs, or exercise learned-policy admission. No StateDrop provenance was
invented. An independent audit verified all 70 histories and exact retained-index
deletions, including retention of system and original task instructions.

## Budget and observed limitations

The existing campaign journal and original $50 authorization were retained.
The live account preflight plus all retained holds left approximately $0.63,
so the fresh stage was capped at $0.50. Provider requests used the existing
conservative reservations and account checks; no prior charges or holds were
cleared. All 70 calls completed, with no unresolved call reservations.

The two source arms cost $0.0190360; the two compression arms cost $0.0283828.
Different trajectories, step counts, and provider cache behavior prevent a
causal savings estimate from these totals. The pytest compression arm repeated
one command nine times and recorded 16 tool errors. The requests compression
arm also repeated one command nine times. Original instruction retention alone
does not prevent loss of useful action history.

The runtime instruction-retention fix passed independent native probes. The
reviewer's first integration run had one intermittent null-plan failure under
machine pressure; the same existing binary then passed all 10 integration tests.
That failure remains tracked as `bd-49vq`, rather than being omitted from the
verification record.

## Grading and retained evidence

The grading adapter uses unchanged official SWE-bench 3.0.15 test-script
construction and parsers at `b524f150d5d76f188c741d75669025f718c89c2e`.
That release accepts the selected Verified schema; the current upstream schema
requires different task fields. Container lifecycle is local and isolated.
Model patches must apply with strict `git apply`; fuzzy fallback is not used.

The adapter adds conclusive reference-test checks. Missing, skipped, or unknown
results cannot establish success. An unchanged negative control also requires
passing preservation tests and an observed failing regression test. The official
report is preserved separately from this stricter verdict. Review found and
tracked the missing-test control bug (`bd-jg14`) and upstream skipped-test
false-positive behavior (`bd-b4ug`) before accepting grading results.

Exact private requests, responses, tool receipts, predictions, and evaluator
logs are retained under `~/.cache/agentc/live-realistic-bd-shi1-8-2/`. Public
results contain hashes, counts, outcomes, and cost totals rather than task,
oracle, or model-response bodies. The tau companion report describes the
independently reproduced service-environment controls and the remaining full
agent/user/judge integration boundary.

## Submission serialization and cleanup

The requests patch application error was caused by mini-SWE-agent's final-output
parser dropping the last newline (`splitlines()` followed by `join()`). The raw
450-byte prediction fails strict application; the original captured 451-byte
terminal diff passes against the exact base source file. The adapter now restores
only that final newline, and only when the successful tool receipt proves every
byte. Prediction and history files remain unchanged. This resolves serialization,
not the failed requests preservation control; no new model calls or test run were
made for this diagnostic. See `coding-patch-serialization.json`.

The original grader source was retained privately as `grading-source-original.py`: its
SHA-256 exactly matches the original grading receipt. It was reconstructed by
reversing only the subsequent newline-helper edit, then checked against that hash.
The original failed grading result is retained alongside the later diagnosis.

Safe cleanup removed only the two reviewed Cargo development builds (1.4 GiB and
1.0 GiB reported by Cargo) and the two pinned, unreferenced task images. Docker
image storage decreased from 30.8 GB to 26.87 GB; other containers, volumes, and
build cache were unchanged. Host free space was 9.33 GB immediately after image
cleanup. All worktrees, the verified native binary, exact traces, predictions,
grading logs, and original budget journal were preserved. See `coding-cleanup.json`.

Validation includes 79 focused Python tests plus six subtests across the runner,
ledger, grading adapter, and tau controls; Ruff; independent native/runtime review;
all 70 request-history audits; exact official test-log replay; and the real patch
serialization check. Remaining workload defects are tracked in Beads, including
compression loops (`bd-guif`) and requests grading (`bd-efzo`).
