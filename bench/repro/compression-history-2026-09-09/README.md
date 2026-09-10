# Assistant exchange retention in the coding development pilot

The fixed compression pilot sometimes retained a shell observation while
deleting the assistant message containing its command. Exact replay found the
latest action missing in **29 of 40 actual compressed requests**: 17 pytest
requests and 12 requests-library requests. Those inputs contained 314 partial
assistant exchanges in total, counting repeated exposure across prefixes.

The native rule now retains the latest assistant message and the entire suffix
after it. Earlier assistant exchanges, ending immediately before the next
assistant message, are retained or removed together. This conservative boundary
also covers shell results represented as user messages; it assigns no inferred
tool provenance. Prefix documents before the first assistant retain the previous
extractive behavior. Original-task, explicit UserInput, role, and follow-on-token
protections still apply. Scores, epsilon, applicability thresholds, and learned
admission thresholds are unchanged.

ContextCompress carries implementation version `assistant-exchanges-v1` in
complete-plan identity. Evidence from the former message-deletion behavior
cannot admit the new version, including composed plans. Other rules keep their
previous identities.

## Exact offline replay

Input: frozen live manifest
`3f0e329f0317b5520839c7b31babf58a0af49a958e0178d338a9c4c0da660e36`.
The original native binary
`9b5f4e3d5c3f7e71b2db0c765f0a3da43b2192c88b3626cebebc206002d48f23`
reproduces all 40 actual compressed dispatches exactly, including abstentions.
Candidate binary:
`2dce4ee1e2d807275d7e1141597903713e1393528bb798c53c12948105abfa81`.

| Observed history | Prefixes | Old replay rewrites | Candidate rewrites | Candidate partial exchanges | Candidate missing current messages |
|---|---:|---:|---:|---:|---:|
| pytest source | 20 | 13 | 0 | 0 | 0 |
| pytest compressed | 20 | 17 | 0 | 0 | 0 |
| requests compressed | 20 | 18 | 13 | 0 | 0 |
| requests source | 10 | 7 | 0 | 0 | 0 |

These are conditional rewrites of **already observed development histories**.
The candidate did not generate these histories. Source-arm replay rewrites were
never dispatched. The candidate removed 26,224 UTF-8 content bytes across these
prefixes; this is neither token savings nor cost savings. Original outcomes,
provider receipts, grading failures, and repeat counts remain unchanged. No
provider, grader, shell workload, or ledger operation ran during this audit.
Whether the change prevents loops requires a separately frozen fresh live run.

[replay.json](replay.json) records exact retained indices, source/binary/trace
hashes, original-history digests, and per-prefix checks. It contains no message
bodies. The retained private transcripts are necessary to reproduce it.

```bash
python -m bench.compression_history_audit \
  --cache /path/to/live-realistic-bd-shi1-8-2 \
  --manifest-sha256 3f0e329f0317b5520839c7b31babf58a0af49a958e0178d338a9c4c0da660e36 \
  --candidate /path/to/native-snapshots/2dce4ee1e2d807275d7e1141597903713e1393528bb798c53c12948105abfa81/typed_rule_materializer \
  --output /new/path/replay.json
```

Validation: 351 optimizer Rust tests across unit, integration, and example
targets; 28 focused Python tests; Clippy and Ruff. Regressions exhaust all 256
salience masks of a three-exchange history, cover empty observations, multiple
response roles, protected clarifications, current assistant prefill, and the
native materializer and protected joint-plan path.
