# Realistic agent workload pilot

Actual coding-agent histories provide compression opportunities that the short
FinQA workflow did not. The pilot acquires 24 published mini-SWE-agent sessions
covering the same 12 real SWE-bench Verified issues in two model runs. The
supported GPT-4.1 histories contain 363 recorded requests; the unchanged native
ContextCompress rule proposes deletions on 287 (79.1%), across all 12 tasks.
This is an offline opportunity result. No new agent response, patch, test
outcome, token saving, provider bill, or learned-policy result was produced.

The next workload family is versioned tau customer service, with tool calls and
environment mutations. [Source research](sources.md) compares coding, service,
and retrieval alternatives and identifies their runnable implementations and
grading contracts. Beads owns remaining work: **bd-shi1.8.2** covers fresh coding
and service integration; **bd-kmeg** covers missing task-instruction protection
in raw SDK histories. FinQA's frozen negative result remains unchanged.

## What was acquired

- Submission index: SWE-bench/experiments
  `40f164d5b8f1d249bf95a6df8b74b577fd8e519d`.
- Declared mini-SWE-agent v1.0.0 source:
  `a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d`. This identifies the declared
  scaffold, not an attestation of each historical runner's exact configuration.
- Runs: `20250726_mini-v1.0.0_gpt-4.1-2025-04-14` and
  `20250726_mini-v1.0.0_claude-sonnet-4-20250514`.
- Both inventories contain all 500 trajectory IDs, with no unmatched IDs.
  Before inspecting trajectory bodies, seed `2026090902` and sample size 12
  were recorded in **bd-shi1.8.1**. IDs are ranked by
  `SHA256(str(seed) + "\n" + instance_id)` over the shared inventory.
  Success, prompt size, trajectory length, and rule activation do not affect
  selection. Unsupported traces remain in the selected denominator.
- [Selection](selection.json), [GPT inventory](inventory-gpt.json),
  [Sonnet inventory](inventory-sonnet.json), and [acquisition](acquisition.json)
  bind the sources. The 24 downloaded files total 1,808,847 bytes. S3 objects
  are checked against the frozen sizes and single-part MD5 ETags, then bound
  by SHA-256. The Git commit does not make the S3 objects immutable.

Raw trajectories remain outside Git in
`~/.cache/agentc/realistic-workloads-bd-shi1-8-1`, with private directory/file
permissions. Committed artifacts contain IDs, hashes, sizes, and rule receipts;
they do not contain task bodies, generated patches, or command output.

## Measured coverage

[census-v2.json](census-v2.json) is the complete result. All 24 trajectories were
parsed, comprising 807 archived assistant responses. The prefix preceding each
response is inspected independently. Archived commands are never executed,
and later archived responses are never treated as results of modified prompts.

| Source run | Selected trajectories | Recorded responses | Histories over 8,192 text bytes | Native-supported histories | CC proposals |
| --- | ---: | ---: | ---: | ---: | ---: |
| GPT-4.1 | 12 | 363 | 342 | 363 | 287 |
| Sonnet 4 | 12 | 444 | 426 | 0 | Unmeasured |

GPT request content ranges from 5,502 to 140,370 bytes. Sonnet archived-prefix
text ranges from 5,502 to 112,003 bytes; its representation is not a faithful
historical request log. Its Anthropic cache helper mutates earlier user
messages into content blocks and moves cache markers across calls. The current
native bridge also rejects those blocks. The pilot preserves them and records
`unsupported_archived_content_blocks`, without flattening or replacing tasks.
The unsupported Sonnet cells are not zero-applicability observations.

GPT trajectories contain 5 to 157 recorded calls (median 16.5); Sonnet
trajectories contain 21 to 71 (median 29). Calls within a task are dependent,
and the long GPT trajectory contributes heavily to pooled byte totals.

The native binary is pinned to
`b260a39b1baedbe0a4cc45895c6041ec5bfcfdbddb3c7f342c38ed3879fe0122`.
ContextCompress retains its default strict size threshold of more than 8,192
bytes and uses the existing SDK proxy epsilon, 0.10. It also checks attention
and retention conditions: crossing the size threshold alone is insufficient.
The pilot changes no rule implementation, threshold, task text, message order,
or archived tool-output limit.

## A useful defect exposed by real histories

Two explicitly labeled provenance profiles are evaluated on the same GPT
request contents:

| Provenance profile | CC proposals | Proposed removed content bytes | Proposals removing original task |
| --- | ---: | ---: | ---: |
| `raw_sdk`: untagged-string literal fallback | 287 / 363 | 2,371,672 | 1 |
| `task_tagged`: explicit task provenance | 287 / 363 | 2,364,529 | 0 |

The initial run revealed **bd-kmeg**: raw-SDK provenance lets one proposal
remove the original issue instruction at
`scikit-learn__scikit-learn-25102`, zero-based turn 21. The second profile was
added after that discovery. It tags message 1 as user input because the pinned
agent's `run` method constructs that message from the actual task. This is a
source-derived adapter control, not automatic current SDK behavior. All other
dependencies remain literal; no stale-state labels are invented.

There are 18,379,199 input-content bytes across the 363 GPT prefixes. The
task-tagged proposals remove about 12.9% of that summed content volume. This
is neither token savings nor end-to-end cost savings: original later histories
remain fixed, model outputs can change, and provider caching and overhead are
not measured. Per-trajectory proposed byte reductions range from 2.7% to 14.9%,
with a median of 8.5%. Protecting the instruction is not proof that other deletions
preserve task success.

StateDrop returns `not_applicable` on all 363 supported histories in both
profiles because the archive lacks state dependencies and read windows.
Transcript age is not evidence of dead state. Service tasks offer real state
transitions, but a future adapter still needs actual provenance before it can
evaluate StateDrop faithfully.

[census.json](census.json) retains the earlier restricted-format diagnostic
that rejected all Sonnet trajectories and tested only raw-SDK provenance.
It is superseded by the complete v2 denominator and both-profile result; its
source digest identifies that initial development version, which was not a
committed checkpoint. It supports no separate performance claim.

## Reproduce

The acquisition/source checkpoint is
`4b122ca0d5172651420125ec011f50b8984beb77`. The final census binds exact source
and acquisition hashes. The historical native binary is available locally at
the path below. A rebuilt binary requires its own measured hash and new result;
do not describe it as the historical binary.

```bash
python -m bench.realistic_workloads acquire \
  --cache /path/outside/git/to/new-private-cache

python -m bench.realistic_workloads census \
  --cache /path/outside/git/to/private-cache \
  --binary /Users/averyclapp/.worktrees/Agentc/bd-323l-6-13-8-6-20-11/target/native-snapshots/b260a39b1baedbe0a4cc45895c6041ec5bfcfdbddb3c7f342c38ed3879fe0122/typed_rule_materializer \
  --binary-sha256 b260a39b1baedbe0a4cc45895c6041ec5bfcfdbddb3c7f342c38ed3879fe0122 \
  --output /path/to/new-census.json
```

Acquisition refuses to replace an existing cache file or receipt. To reacquire,
copy the frozen inventories and selection into a fresh artifact directory and
pass it with `--artifact`; retain the committed acquisition receipt for hash
comparison. Native replay accepts the already frozen private cache directly.

Verification: 122 focused tests passed, one optional upstream-network test
skipped; Ruff and `git diff --check` passed. Independent **bd-shi1.8.1.2** review
passed and reproduced all 1,452 native receipts and the complete v2 census
byte-for-byte (SHA-256
`08deb8c48d537abc63e1f269a0a1633e9f04a245bdf3cf93ba92b4a01f4f533b`).
It also independently checked the ID ranking and future-answer exclusion.
There were zero provider calls and zero executed archived
commands. The Docker client is installed on this host, but its daemon was not
running when checked; no fresh container benchmark was launched.
