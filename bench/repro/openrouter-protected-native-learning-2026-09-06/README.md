# Protected native learning experiment

Prospective configuration frozen at `2026-09-06T02:09:58.755187+00:00`.
This file documents the acquisition design, not a completed result.

## Frozen identity

- Acquisition source commit: `4ee146ca2576a474d0fbb19fb84b4a6f86479179`.
- Manifest canonical SHA-256: `c5a38945224decfbc1ea03b582213f992b7742c7179121529bc7a3f5de85d477`.
- Manifest file SHA-256: `5d8ed2f4be4bd6eb21594bcd12b7b76fc6bebe96d474cfc1da8f357dbdecc5ae`.
- Native bridge SHA-256: `3803d2d0c8c6c804f4144a8d369f3bdd955d638f744d73db4d3f4486a3ac9d30`.
- Fixed rewrite materializer SHA-256: `b260a39b1baedbe0a4cc45895c6041ec5bfcfdbddb3c7f342c38ed3879fe0122`.

The manifest binds all 84 acquisition source files, current endpoint observations,
the original journal prefix, the offline control report, and the unchanged public
fixture. Its external hash is required by the runner. No credentials are archived.

## Ordered acquisition

1. Six fixed pilot tasks: plain Sonnet and plain Haiku, each with its own three-stage
   history. All 36 calls must return valid, non-null, nontruncated application values.
2. Sixty-three fixed source workflows, including the actual cold-start arrivals:
   189 source calls and at most 140 completed native probes across seven stable plans.
3. The same 63 tasks under fixed Haiku plus context compression at filtering and
   state dropping at answering: 189 calls, using this arm's own history.

Every request has a prospectively fixed 256-token output cap. The experiment alone
sets the native exploration allowance to 60 calls per site; the product default is
still 20, and its default output cap remains 512. The per-plan evidence target of
20 and all other native guards are unchanged. Offline zero-loss controls reached
seven plans with 20 observations each under this configuration; fake responses do
not establish real-model quality or admission.

The UNKNOWN-capacity primary always remains the reference model. Probe answers
never enter application history. Pair counts are not admission or damage
certificates. The source block precedes the fixed comparison block, so cache and
time effects are not randomized away.

## Financial and failure contract

The sole financial authority is the original campaign journal at
`/Users/averyclapp/Documents/Coding/GitProjects/Agentc/.git/openrouter-pilot-budget.jsonl`.
Its initial 19,341-event prefix contains $22.27594747 in known charges and
$12.275019 in retained uncertainty, leaving $15.44903353 within the original $50
authorization. No prior failed-call liability is removed.

The label-free historical scenario estimates $0.312176 for the pilot, $3.087615
for source acquisition plus probes, and $0.472196 for the fixed comparison.
The full largest-pair reservation is $12.01536. Pilot plus modeled training plus
that reservation totals $15.415151: this narrowly fits the initial remainder but
is not a spending guarantee. The complete audited pilot must supply a fresh
resource forecast before study acquisition is eligible.

Each phase has a single durable original-journal claim and a six-hour dispatch
deadline, rechecked after preflight and immediately before sending. Calls already
sent can finish recording their receipts after the deadline. Any provider or
protocol failure permanently stops the phase; no retry, resume, reseeding, or
selective rerun is permitted. Existing pair/attempt liabilities remain reserved.

## Interpretation and label access

This is one generated development draw, not an unseen-input benchmark. Historical
fake-response tests generated eight overlapping public contexts and internal
expected scalars; assertions did not use those scalars. The exact chronology and
limitations are bound by the manifest's exposure disclosure. All 69 original IDs
are preserved; none were replaced to remove the exposure.

Labels remain sealed until the independently audited pilot, entire source block,
and entire fixed comparison block are complete. Incomplete acquisition permits
integrity, resource, and learning-count diagnostics only. Any subsequent workflow
comparison is fixed-source versus fixed-combined-arm evidence, not adaptive joint
system efficacy. Probes are not independent workflow clusters.

Use `python -m bench.openrouter_protected_study run` with the exact manifest hash,
original journal, both pinned binaries, public bundle, output directory, explicit
credential-file path, and `--phase pilot` or `--phase study`. The same module's
`audit` command performs read-only replay and does not load credentials. The
runner itself refuses an already-claimed phase and refuses study acquisition
without a complete, freshly re-audited pilot and matching resource gate.

## Completed pilot checkpoint

The one-shot pilot completed all 36 calls with valid application values, no new
failure or unresolved charge, and $0.211568 in known cost. This verifies format
and resource feasibility only: no correctness labels were opened. The original
journal now contains 19,450 events, with $22.48751547 known, $12.275019 still held,
and $15.23746553 available within the original campaign cap.

The fresh pilot-derived resource scenario estimates $3.080928 for source plus
probes and $0.471566 for the fixed comparison. Training plus the largest full pair
reservation totals $15.096288, below the available remainder by $0.14117753.
Actual reservations remain authoritative; this estimate is not a guarantee.

- Pilot summary file SHA-256: `9c28ba5ff488c546eb08b5719fed49bf02920dc30a6c808fa35f66d586cf39e1`.
- Pilot audit file SHA-256: `6a831c512f185d2e911e4118c869a66e92c7bde838305c4380cf5b8864162203`.
- Postpilot gate canonical SHA-256: `e3d94479aa6afac2447ed06344021cbc36d1f6109ef9eb3fd1608f5d489d316a`.
- Postpilot gate file SHA-256: `02dcf5a7df824e5ffc4a88b8309dd505568b47c8e46e0468fa5281badbee7900`.
- Original journal checkpoint file SHA-256: `fc3c7444264e86e390ea64f53fc4328dd068ac0039c97ef0523db2f4f59f78ad`.
- Original journal checkpoint canonical SHA-256: `470c549a72c428af2ec43653b83e1c85f107f21841ee641ab9fc87d2d70981f7`.

The saved pilot summary binds its raw calls, intents, and protected-event subset.
The study runner independently reconstructs this gate again before claiming the
study; a changed financial snapshot or incomplete pilot cannot be bypassed using
the saved gate's `passed` field.
