# Realistic workload sources, 9 September 2026

Use archived mini-SWE-agent v1 coding sessions for the immediate offline
opportunity pilot. They contain an agent's actual successive shell actions and
observations. Preserve both selected model cohorts, but restrict native replay
to supported plain-string histories: the Sonnet adapter mutates user messages
into content blocks that the current materializer does not support. Use a
versioned customer-service environment as the next candidate
for stateful execution. BrowseComp-Plus is a useful retrieval alternative, with
less complete request reconstruction in its published normalized traces. These
are engineering recommendations from the source inspection below, not claims
that any optimization preserves task success.

No model calls, test grading, benchmark-dataset downloads, or trajectory execution
were needed for this research. One coding trace was parsed only for keys and
role counts. Published submission metadata was consulted for provenance; its
aggregate outcome fields were not used to choose either coding cohort. Do not
execute commands found inside downloaded trajectories.

## Coding: SWE-bench and mini-SWE-agent v1

Pin the official submission index to
`40f164d5b8f1d249bf95a6df8b74b577fd8e519d`. It distinguishes submission entries
from artifacts: older submissions point to public S3; newer ones can point to a
submitter repository. Bash-only runs now reside under `evaluation/verified/`,
while the older S3 prefix remains `bash-only/`. The index documents both local
evaluation and verification from recorded test outputs. A trajectory is an
inference record; an evaluation report is separate evidence.
[Official index](https://github.com/SWE-bench/experiments/blob/40f164d5b8f1d249bf95a6df8b74b577fd8e519d/README.md)

The pilot candidates use the same declared agent version, `1.0.0`, and one
attempt per task, chosen for scaffold compatibility rather than reported
success:

| Submission | Pinned provenance | Public trajectory prefix |
| --- | --- | --- |
| `20250726_mini-v1.0.0_claude-sonnet-4-20250514` | [metadata](https://github.com/SWE-bench/experiments/blob/40f164d5b8f1d249bf95a6df8b74b577fd8e519d/evaluation/verified/20250726_mini-v1.0.0_claude-sonnet-4-20250514/metadata.yaml) | `s3://swe-bench-submissions/bash-only/20250726_mini-v1.0.0_claude-sonnet-4-20250514/trajs/` |
| `20250726_mini-v1.0.0_gpt-4.1-2025-04-14` | [metadata](https://github.com/SWE-bench/experiments/blob/40f164d5b8f1d249bf95a6df8b74b577fd8e519d/evaluation/verified/20250726_mini-v1.0.0_gpt-4.1-2025-04-14/metadata.yaml) | `s3://swe-bench-submissions/bash-only/20250726_mini-v1.0.0_gpt-4.1-2025-04-14/trajs/` |

The `v1.0.0` source tag resolves to
`a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d`. At that revision the Python package
is named `microsweagent`. The default agent passes its accumulated `messages`
to `model.query`, then appends the successful response as an assistant message.
Shell observations and recoverable formatting/timeout errors are appended as
user messages. The prefix before each recorded assistant message therefore
identifies the earlier message sequence associated with that successful call.
This is not necessarily its original serialized representation: adapters can
mutate earlier entries. A trailing termination message is not another request;
missing or retried provider attempts cannot be reconstructed from successful
messages.
[Agent implementation](https://github.com/SWE-agent/mini-swe-agent/blob/a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d/src/microsweagent/agents/default.py)

The Anthropic adapter provides a concrete mutation case. It calls a cache helper
that edits the shared message dictionaries: recent user strings become text
block arrays with cache markers. Subsequent calls remove old markers but leave
the arrays. An archived final Sonnet history therefore does not preserve the
historical cache markers or content representation at each earlier call. Keep
the stored blocks intact and report unsupported projection; do not silently
flatten them into strings. The plain LiteLLM adapter has no such explicit
preprocessor, but neither archive captures all dependency/provider transforms.
[Anthropic adapter](https://github.com/SWE-agent/mini-swe-agent/blob/a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d/src/microsweagent/models/anthropic.py),
[Cache mutation](https://github.com/SWE-agent/mini-swe-agent/blob/a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d/src/microsweagent/models/utils/cache_control.py),
[Plain model adapter](https://github.com/SWE-agent/mini-swe-agent/blob/a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d/src/microsweagent/models/litellm_model.py)

The save function writes `info` and `messages`, with additional run fields
permitted. `info` includes `submission`, `exit_status`, and aggregate model
statistics. These are not needed for prefix construction. The inspected public
sample additionally has `instance_id`; each message contains only `role` and
`content`. There is no embedded configuration in that sample, so the declared
version does not prove exact historical settings or provider wire bytes.
[Save implementation](https://github.com/SWE-agent/mini-swe-agent/blob/a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d/src/microsweagent/run/utils/save.py)

Concrete acquisition example:

```text
https://swe-bench-submissions.s3.amazonaws.com/bash-only/20250726_mini-v1.0.0_claude-sonnet-4-20250514/trajs/astropy__astropy-12907/astropy__astropy-12907.traj.json
```

The fetched sample was 60,121 bytes with SHA-256
`40be67260e739d6357c5681a1c9b9f7677b26f09ac29cb1ecca417adff14df72`;
its roles were one system, 23 user, and 22 assistant messages. This inspection
was for schema only, not a task-selection recommendation. S3 keys are mutable;
freeze downloaded bytes and a manifest instead of treating the URL as immutable.
[Sample artifact](https://swe-bench-submissions.s3.amazonaws.com/bash-only/20250726_mini-v1.0.0_claude-sonnet-4-20250514/trajs/astropy__astropy-12907/astropy__astropy-12907.traj.json)

The source configuration asks the agent to inspect a repository, reproduce a
problem, modify source files, and verify the change. Observations already have
the scaffold's own output limit: outputs of 10,000 characters or more are
represented by their first and last 5,000 characters plus a warning. Preserve
that recorded text. Adding repeated files, extending outputs, or selecting only
large histories would create a different workload.
[SWE-bench configuration](https://github.com/SWE-agent/mini-swe-agent/blob/a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d/src/microsweagent/config/extra/swebench.yaml)

Local feasibility: downloading frozen public traces and replaying native
materialization needs neither a model nor executing their shell commands.
Testing optimization quality requires new arm-specific agent executions in the
task environments, followed by the task tests. A recorded baseline patch's test
result cannot grade a modified prompt whose new response was never generated.

The practical live integration point is the pinned v1 batch runner:
`micro-extra swebench --subset verified --split test --filter <frozen-id-regex>
--workers 1 --model <pinned-model> --output <fresh-arm-directory>`.
The runner loads each problem statement, starts its Docker environment, and
writes the model patch to `preds.json`. Place Agentc at the model-call boundary
while retaining the scaffold and allowing each arm's new outputs to drive its
own subsequent commands. This is a proposed path, not a command executed here.
[Runner documentation](https://github.com/SWE-agent/mini-swe-agent/blob/a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d/docs/usage/swebench.md),
[Runner implementation](https://github.com/SWE-agent/mini-swe-agent/blob/a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d/src/microsweagent/run/extra/swebench.py)

Before live use, bind the task dataset and container image digests: that old
runner fetches the dataset without a revision and defaults to mutable `latest`
image tags. The currently inspected Verified dataset revision is
`c104f840cc67f8b6eec6f759ebc8b2693d585d4a`; this is a prospective source pin,
not a claim that the 2025 archived runs used it.
[Verified dataset](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified/tree/c104f840cc67f8b6eec6f759ebc8b2693d585d4a)

For grading, the inspected official harness revision is
`02e7a74ffd0b707aab73d203fe87bdc7c76afc8e`. Its evaluation runner applies the
submitted model patch in a container, executes the task evaluation script, and
captures test output. Its grader marks full resolution only when all designated
fail-to-pass and pass-to-pass tests succeed; absent parseable test output stays
unresolved. Preserve infrastructure failures separately. These are executable
task conditions, not agreement with a published leaderboard score.
[Evaluation runner](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/run_evaluation.py),
[Test-based grader](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/grading.py)

## Stateful service: tau2-bench's versioned text domains

The repository is still named `tau2-bench`, but its current README describes
tau3. Pin `672227c6b6676edc20d57ea53b7000262aae77b9` for this inspection. The
text domains include airline, retail, and telecom; the environment supplies
policies, tools, tasks, and optional user-side tools. These are simulated
customer-service workflows, not captured production customer conversations.
Core text installation uses `uv sync`, and a normal run needs both an agent
model and a user-simulator model. Version changes matter: the README explicitly
reports task/grading fixes and a banking-domain comparability break at 1.0.1.
[Repository overview](https://github.com/sierra-research/tau2-bench/blob/672227c6b6676edc20d57ea53b7000262aae77b9/README.md)

The ordinary text agent maintains a separate system prompt and message history,
appends user/tool inputs, and calls the model with the domain's structured
tools. It then adds the assistant response to history. The same source file also
implements an explicit ground-truth agent that receives resolution steps; that
debug agent is unsuitable as an ordinary policy-evaluation workload.
[Text agent](https://github.com/sierra-research/tau2-bench/blob/672227c6b6676edc20d57ea53b7000262aae77b9/src/tau2/agent/llm_agent.py)

Text results can be a JSON object containing `info`, `tasks`, and `simulations`.
Each simulation records its `task_id`, trial/seed, timing, termination reason,
messages, and optional rewards/costs/reviews. Voice uses a different tick-based
representation. An offline adapter should explicitly project text messages and
necessary public configuration, excluding `tasks` and `reward_info` from
opportunity analysis. The archive schema alone does not provide the exact
agent-visible policy or tool schemas.
[Result schema](https://github.com/sierra-research/tau2-bench/blob/672227c6b6676edc20d57ea53b7000262aae77b9/src/tau2/data_model/simulation.py)

The environment evaluator reconstructs the predicted environment from tool
history and a reference environment from the task's expected actions. It
compares both agent and user database hashes and can check explicit environment
assertions. Thus a different valid action sequence can reach the same passing
state; reference action order is not automatically a required execution path.
[Environment evaluator](https://github.com/sierra-research/tau2-bench/blob/672227c6b6676edc20d57ea53b7000262aae77b9/src/tau2/evaluator/evaluator_env.py)

Do not describe every tau reward as solely a database check. Each task's
`reward_basis` controls which factors count. The supported factors include
database state, environment assertions, required communication, specific
actions, and natural-language assertions with a model judge. The official
evaluation guide distinguishes outcome grading from optional action-match
diagnostics. Any pilot must freeze the domain/task version and evaluator mode.
[Evaluation semantics](https://github.com/sierra-research/tau2-bench/blob/672227c6b6676edc20d57ea53b7000262aae77b9/docs/evaluation.md)

Public traces are available, but check individual provenance rather than
assuming every leaderboard entry includes them. The pinned GPT-5.2 submission
names text trajectory files; the frontend documents how those names resolve.
On this research date the following public object returned HTTP 200 to HEAD,
with length 31,759,718 bytes. It was not downloaded or graded:

```text
https://sierra-tau-bench-public.s3.amazonaws.com/submissions/gpt-5-2_sierra_2026-02-26/trajectories/gpt-5.2_high_retail_gpt-5.2_4trials.json
```

This is a candidate acquisition endpoint, not an already frozen cohort. Its
submission metadata contains later domain-specific regrading notes; retain the
artifact's own version/configuration when acquiring it.
[Submission provenance](https://github.com/sierra-research/tau2-bench/blob/672227c6b6676edc20d57ea53b7000262aae77b9/web/leaderboard/public/submissions/gpt-5-2_sierra_2026-02-26/submission.json),
[Frontend artifact mapping](https://github.com/sierra-research/tau2-bench/blob/672227c6b6676edc20d57ea53b7000262aae77b9/web/leaderboard/src/components/TrajectoryVisualizer.jsx)

Local feasibility: the public text traces can support a no-call structural
census after acquisition. A live experiment needs a tool-preserving adapter,
environment resets per arm, a versioned user simulator, and all agent/user/judge
costs counted. Flattening structured tools into plain text would change the
benchmark interface.

## Retrieval: BrowseComp-Plus

Pin the code to `046949032b0328319cc9a02663a759ec601d9402`. BrowseComp-Plus
uses a fixed corpus of approximately 100,000 documents and supports repeated
search instead of a single preassembled context. It provides evidence-document
relevance judgments as well as answer evaluation. Its default answer evaluator
uses Qwen3-32B as a judge; retrieval recall is an objective auxiliary endpoint,
not a substitute for answer correctness.
[Project documentation](https://github.com/texttron/BrowseComp-Plus/blob/046949032b0328319cc9a02663a759ec601d9402/README.md)

The first-party public datasets resolve to these revisions as of this research:

| Artifact | Immutable revision |
| --- | --- |
| [Released trajectories](https://huggingface.co/datasets/Tevatron/browsecomp-plus-runs/tree/3c2b136c6382d2f7d43587bbc0a77f68b024c339) | `3c2b136c6382d2f7d43587bbc0a77f68b024c339` |
| [Document corpus](https://huggingface.co/datasets/Tevatron/browsecomp-plus-corpus/tree/b27b02bc3e45511b8b82a13e6f90ce761df726f6) | `b27b02bc3e45511b8b82a13e6f90ce761df726f6` |
| [Tasks and judgments](https://huggingface.co/datasets/Tevatron/browsecomp-plus/tree/144cff8e35b5eaef7e526346aa60774a9deb941f) | `144cff8e35b5eaef7e526346aa60774a9deb941f` |

The trajectory release contains `bm25/gpt5.jsonl`, `bm25/o3.jsonl`, and matching
`qwen3-embed-8b` runs. The official downloader decodes the published obfuscated
outputs locally. Preserve the benchmark's instruction against publishing
plain-text data online: commit only acquisition metadata and aggregate results.
The downloader currently uses an unpinned Hugging Face fetch, so a reproducible
acquirer must supply the explicit data revision above.
[Release download script](https://github.com/texttron/BrowseComp-Plus/blob/046949032b0328319cc9a02663a759ec601d9402/scripts_build_index/download_run_files.sh),
[Decoding implementation](https://github.com/texttron/BrowseComp-Plus/blob/046949032b0328319cc9a02663a759ec601d9402/scripts_build_index/download_and_decrypt_run.py)

The OpenAI search loop appends response items and corresponding function-call
outputs before its next request. Saved files normalize these into `result`
entries such as `tool_call`, `reasoning`, and `output_text`, plus aggregate
usage, tool counts, query ID, and retrieved document IDs. This loses request
grouping and some tool identifiers: the normalized release is useful for a
behavior census, but it is not an exact provider-request log. Replay would
require an explicitly bounded reconstruction or fresh request capture.
[Search loop and persisted format](https://github.com/texttron/BrowseComp-Plus/blob/046949032b0328319cc9a02663a759ec601d9402/search_agent/openai_client.py)

Tools support top-k search with snippets and optional full-document retrieval.
Their outputs arise from actual queries and retrieved documents, providing a
natural source of accumulating context. Whether the unmodified traces expose
useful compression opportunities still requires measurement; do not increase
snippet limits or repeat documents to force activation.
[Search tools](https://github.com/texttron/BrowseComp-Plus/blob/046949032b0328319cc9a02663a759ec601d9402/searcher/tools.py)

## Interpretation for Agentc

Real context accumulation and state provenance are different requirements.
An old tool output is not automatically dead state. The current StateDrop rule
requires state dependencies and checks reads in the relevant window; an archive
without that provenance cannot establish it. Preserve unknown dependency
provenance as the normal literal fallback, which honestly leaves StateDrop
inapplicable. Any later stateful adapter must capture actual application state
writes/reads, rather than infer them from transcript age or a desired result.
[Agentc StateDrop](../../../crates/agentc-optimizer/src/rules/state_drop.rs)

For the immediate coding pilot, retain all eligible histories from a selection
fixed using IDs, keep source body text unchanged, use current production
materialization settings, and report malformed/missing cases. Count native
applicability and byte changes separately from tokens, cost, correctness, and
learned-policy effectiveness. Positive offline coverage establishes that a live
study has something to test; it does not establish safe deletion or savings.
