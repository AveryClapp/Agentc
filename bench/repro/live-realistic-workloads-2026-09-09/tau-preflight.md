# Tau retail environment preflight

The official text retail environment is installed and executable locally. The
deterministic tool/state controls passed, including a failing negative control.
This establishes a working second workload environment, **not an agent success
rate or optimization benefit**. No agent, user simulator, judge, or provider call
was made; no benchmark task was executed.

## Version and acquisition

The repository is named `sierra-research/tau2-bench`; the selected revision is
branded tau3 and the installed package is `tau2==1.0.1`. It is explicitly pinned
to `672227c6b6676edc20d57ea53b7000262aae77b9`. This uses its current task fixes,
without presenting it as the original tau2 task version.
[Official source](https://github.com/sierra-research/tau2-bench/tree/672227c6b6676edc20d57ea53b7000262aae77b9)

Source and dependencies live outside Agentc under
`~/.cache/agentc/tau-service-bd-shi1-8-2-1/`. The successful environment is
`venv312`, using CPython 3.12.9 and the official frozen `uv.lock`. Agentc's
dependency files were unchanged. A prior core-only Python 3.13.1 installation
failed because tau imports the removed `audioop` module through voice helpers;
that untouched failed environment is retained as `venv` and tracked in `bd-atwb`.

[tau-preflight.json](tau-preflight.json) records all 75 installed distributions,
270 source/data file sizes and SHA-256 hashes, the executing harness hash, and
the control results. The source closure is
`d1c2963a8c25825c86abfdb8f860f524351f1e5e9b98fda81031267c735ad432`;
the official lock hash is
`62d3a8c4807b89e85703b3c03f2c21048a2da9736ca83d9af6f61adc74ac5314`.
Source revision and tracked-file cleanliness are checked before and after the
control. No task bodies, customer fixture bodies, or reference actions are
copied into Agentc Git.

## What executed

The script chooses the lexicographically first pending order from the public
simulated retail database. This choice does not consult task reference actions,
agent success, prompt size, or optimizer applicability. It then executes the
official tools:

1. Look up the user and the order: both leave the database unchanged.
2. Attempt cancellation with an invalid reason: the tool errors without mutation.
3. Record scripted confirmation and cancel with a valid reason: state changes.
4. Read the order again: its persisted status is cancelled.

All five tool calls retain their unique IDs and paired tool results, including
the error. The official LiteLLM projection is used unchanged; IDs, names, JSON
arguments, null assistant content, and tool-result content are checked. All
16 official tool schemas are retained. This checks the official request
representation, not provider wire bytes. The fixture includes artificial
invalid-input and confirmation controls; it is not a realistic model trajectory.

A separate environment remains unchanged, and loading a new environment restores
the initial state. Official strict trajectory replay reaches the same final
database hash. A deliberately corrupted **write** result is rejected; upstream
replay skips read-only tools, so this is not an assertion that it validates every
observation. [Official replay implementation](https://github.com/sierra-research/tau2-bench/blob/672227c6b6676edc20d57ea53b7000262aae77b9/src/tau2/environment/environment.py)

The official `EnvironmentEvaluator` grades a hand-authored DB control: the full
script receives 1 and its lookup-only prefix receives 0. The reference action is
used only to define that synthetic control's target state. It is not a ground
truth agent or a solution extracted from a benchmark task.
[Official environment evaluator](https://github.com/sierra-research/tau2-bench/blob/672227c6b6676edc20d57ea53b7000262aae77b9/src/tau2/evaluator/evaluator_env.py)

Two executions produced identical private trace bytes: 50,431 bytes, SHA-256
`3a594c179dd02fdc9df632134b3e440d8b56f9b2b87376da6a113b5d9919a13c`.
The retained traces are `control-trace.json` and `control-trace-v2.json` in the
private cache, mode 0600. The harness disables dotenv loading, suppresses tool
debug bodies, and installs a socket audit hook before importing tau. Execution
used an environment containing only PATH, HOME, TMPDIR and LANG, without API
credentials. Unit checks cover malformed/incomplete/duplicate tool IDs, unchanged
parallel-result serialization, exclusive private output, and the network guard.

## Reproduce the controls

The exact successful installation, from the pinned source checkout, was:

```bash
UV_PROJECT_ENVIRONMENT=/Users/averyclapp/.cache/agentc/tau-service-bd-shi1-8-2-1/venv312 \
  uv sync --frozen --python 3.12
```

From the Agentc worktree, use fresh output names; existing files are never
overwritten. This Python launcher reproduces the credential-free invocation:

```python
import os
import subprocess
from pathlib import Path

cache = Path.home() / ".cache/agentc/tau-service-bd-shi1-8-2-1"
env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ}
subprocess.run([
    str(cache / "venv312/bin/python"), "-m", "bench.tau_service_preflight",
    "--source", str(cache / "source"),
    "--private-output", str(cache / "control-trace-recheck.json"),
    "--report", str(cache / "control-report-recheck.json"),
], env=env, check=True)
```

## Live workload boundary

The pinned `retail/base` set contains 114 tasks. Six prospective development IDs
are frozen by SHA-256 ranking of `2026090903:<id>`: `31, 38, 90, 7, 63, 47`.
They were selected from all base IDs, without success, body, or applicability
filtering. They are integration/development candidates, not a held-out efficacy
cohort. No selected task has been run.

The task census found **112 tasks require DB plus natural-language assertions**;
only two specify DB alone. Full benchmark grading therefore needs a model judge
as well as the agent and user simulator. The source pins its default NL judge to
`gpt-4.1-2025-04-14`. A DB-only endpoint must be labeled auxiliary when the
original task requires additional factors; do not switch to the two DB-only
tasks to avoid this requirement.
[Evaluation semantics](https://github.com/sierra-research/tau2-bench/blob/672227c6b6676edc20d57ea53b7000262aae77b9/docs/evaluation.md)

The official CLI accepts `--domain retail --task-split-name base --task-ids 31
38 90 7 63 47 --num-trials 1 --seed 2026090903 --max-concurrency 1`, plus explicit
agent/user model IDs. This is a source-verified integration entry point, **not a
paid execution command already validated here**. Its defaults include retries
and cache behavior, and `--save-to` can resume existing output. A live adapter
must bind fresh output, exact model/request/response capture and all agent, user,
judge, retry and failure costs to the campaign ledger before dispatch.
[CLI implementation](https://github.com/sierra-research/tau2-bench/blob/672227c6b6676edc20d57ea53b7000262aae77b9/src/tau2/cli.py)

A malformed-judge control also reproduced upstream defect `bd-jcmv`: both `{}`
and `{"results": []}` received reward 1 for a task with one required assertion.
[tau-judge-negative-control.json](tau-judge-negative-control.json) records this
stubbed, no-model experiment. The future grading adapter must reject missing,
duplicated or mismatched assertion results before accepting full reward; that
adapter would be a documented fail-closed addition to official grading.
[Affected evaluator](https://github.com/sierra-research/tau2-bench/blob/672227c6b6676edc20d57ea53b7000262aae77b9/src/tau2/evaluator/evaluator_nl_assertions.py)

StateDrop eligibility remains unknown: genuine simulated database writes do not
automatically make old transcript messages dead state. No liveness tags, repeated
content, or optimizer proposals were manufactured by this preflight. Fresh
arm-specific agent/user executions and complete grading are still required to
measure optimization coverage, correctness, savings, or learned-policy efficacy.
