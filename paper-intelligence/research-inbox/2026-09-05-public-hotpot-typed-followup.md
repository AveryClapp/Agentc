# Public HotpotQA follow-up: a bounded, label-blind design

Date: 2026-09-05. Bead: `bd-323l.6.13.8.6.20.14`. Inspected repo base:
`351cd64`. Status: research/design only; no dataset shards downloaded, candidate
tasks selected, adapter changed, provider calls made, or campaign ledger accessed.
The protected 48 QA question contents and existing QA fixture contents were not
opened. This note is not acquisition approval or manuscript prose.

## Recommendation

Use 32 prospectively sampled **HotpotQA distractor/train** questions for a new
fixed-model development experiment. Preserve the original three research steps,
placing each natural-text result inside a strict JSON envelope. Compare the same
two pinned models with and without actual CC/SD rule materialization; add the
single-rule controls only under a prospectively defined cost gate. This improves
external validity beyond generated procurement tasks, but is neither an official
test-set result nor evidence of adaptive routing, safe semantic substitution, or
an MLSys main-track contribution by itself.

## Exact source and evaluation pins

HotpotQA is the authors' EMNLP 2018 multi-hop QA dataset; its distractor setting
provides ten paragraphs. The authors distribute the data under **CC BY-SA 4.0**
and their code under **Apache 2.0**. Retain attribution, license notices, source
identity, and transformation provenance with any redistributed fixture.
[Author site](https://hotpotqa.github.io/),
[pinned author README](https://github.com/hotpotqa/hotpot/blob/3635853403a8735609ee997664e1528f4480762a/README.md).

Use the HF-hosted representation `hotpotqa/hotpot_qa`, revision
`1908d6afbbead072334abe2965f91bd2709910ab` (repository observation:
2025-08-11T10:16:27Z), config `distractor`, split `train`: **90,447 rows**.
The two train shards total 331,786,656 bytes. These are pinned HF artifact
identities, not a claim that we verified byte/row equivalence with the authors'
original `hotpot_train_v1.1.json` archive.
[HF repository metadata](https://huggingface.co/api/datasets/hotpotqa/hotpot_qa),
[pinned metadata tree](https://huggingface.co/api/datasets/hotpotqa/hotpot_qa/tree/1908d6afbbead072334abe2965f91bd2709910ab/distractor?recursive=true&expand=false),
[pinned dataset card](https://huggingface.co/datasets/hotpotqa/hotpot_qa/blob/1908d6afbbead072334abe2965f91bd2709910ab/README.md).

| Path under the pinned revision | Bytes | LFS content SHA-256 |
| --- | ---: | --- |
| `distractor/train-00000-of-00002.parquet` | 165624177 | `76d3bb3048a7cc73c1958107c0c5872a00d7e7d00c105b81e92f6769e7822e68` |
| `distractor/train-00001-of-00002.parquet` | 166162479 | `713661628434fbb19fff7392e2e321e4ed107e3c7c7784d0690946e5f722763f` |

HF uses `id` rather than the original `_id`, and represents `context` as aligned
title/sentence arrays and `supporting_facts` as aligned title/sentence-index arrays.
Preserve paragraph order, sentence order, and exact text; do not sort contexts or
construct relevance from supporting labels. The public runtime whitelist is
question plus context titles/sentences. Answers, supporting facts, and type/level
annotations belong only to separately stored offline evaluation data.
[Pinned dataset card](https://huggingface.co/datasets/hotpotqa/hotpot_qa/blob/1908d6afbbead072334abe2965f91bd2709910ab/README.md),
[original field definitions](https://github.com/hotpotqa/hotpot/blob/3635853403a8735609ee997664e1528f4480762a/README.md).

Pin the official evaluator at author-repo commit
`3635853403a8735609ee997664e1528f4480762a` (2019-02-14), file
`hotpot_evaluate_v1.py`, raw SHA-256
`d35fc91a6db21d791dbdda11daf3856e9359f5701d54e3eefba20d88fecc02c0`.
Use its normalized answer EM and token F1, including its yes/no/noanswer behavior;
test any local port against that exact source. Its punctuation/article/whitespace
normalization is an **offline metric**, not application repair. Official supporting
fact and joint metrics require explicit predicted sentence references: do not
invent them by matching freeform quotes against gold.
[Pinned evaluator](https://github.com/hotpotqa/hotpot/blob/3635853403a8735609ee997664e1528f4480762a/hotpot_evaluate_v1.py).

## Freeze IDs before reading candidate text or labels

The existing [fixture builder](../../bench/build_hotpot_fixture.py) downloads an
unrevisioned validation shard, then selects the first examples passing context
length and supporting-label filters. It is not the proposed sampling procedure.
Bug `bd-dnft` tracks a **separate** pinned train pipeline; leave frozen legacy
fixtures unchanged.

Proposed deterministic selection, to be implemented and independently reviewed:

1. Fetch only the two pinned train shards; verify each byte count and SHA. Read
   only their `id` columns initially; validate unique IDs and total row count.
2. Exclude the union of IDs from historical manifest schedules/exclusion lists
   (including all protected 48), plus Appendix A. Read **manifest IDs only**, not
   legacy fixture bodies. Normalize the repo's `hotpot_` prefix explicitly.
3. Rank eligible raw IDs by SHA-256 of compact UTF-8 JSON
   `["hotpot-train-typed-v1", revision, 2026090504, raw_id]`, then raw ID as tie
   breaker. Proposed seed `2026090504` is declared here before selection. Take
   the first six for a disjoint format pilot and the next 32 for development.
4. Commit the source pins, full exclusion set/hash, ID-universe hash, algorithm,
   seed, and ordered selected IDs before reading their questions or labels.
   Then materialize public columns and freeze that fixture hash; store evaluation
   labels separately. Do not reject/resample by answer, difficulty, support count,
   length, model success, or CC applicability. A malformed selected record stops
   preflight for review; it does not silently become a replacement draw.

“Fresh” means unexposed in this experiment, not absent from model pretraining.
This is a public **training-split development** sample, not protected confirmation.

## Minimum typed workflow and failure contract

Use a distinct workload/version and prompt/schema/source-bound call-site identity.
All three stages can use the same provider-supported minimal schema:

```json
{"type":"object","properties":{"result":{"anyOf":[{"type":"string"},{"type":"null"}]}},"required":["result"],"additionalProperties":false}
```

Send it through `response_format: {type: "json_schema", json_schema:
{name: <stage-bound-name>, strict: true, schema: <above>}}`, preserving fixed
Anthropic endpoint pinning, `require_parameters: true`, no provider fallback or
healing plugins. OpenRouter documents this request envelope. Anthropic supports
the core schema constructs but has constraint limitations and refusal/truncation
exceptions. A schema is not proof of truth or guaranteed successful completion.
[OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs),
[Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).

Do **not** send unsupported `minLength`/`maxLength`, exact sentence counts, or
semantic constraints. Retain `max_tokens: 512` for each call; locally cap decoded
result text at a prospectively fixed 8192 UTF-8 bytes and require non-whitespace
text for a successful result. This is a proposed application limit, not a token
conversion claim. Null is explicit application failure. The 512-token cap includes
JSON syntax/escaping and can truncate relevant quotes; test it in the pilot, and
count truncation rather than reconstructing partial JSON or quietly raising caps.

Preserve the [AST-extracted original prompts and message topology](../../bench/openrouter_rules_protocol.py):

| Stage | Natural operation | Original message/state structure |
| --- | --- | --- |
| Filter | Select 2–3 relevant paragraphs and quote key sentences with titles | System + ten public paragraphs + question |
| Synthesis | Produce one-sentence draft from filtered passages | System + own filter result + question |
| Answer | Short answer using synthesis only | System + stale own filter result + own synthesis + question; declared read is synthesis only |

Add JSON instructions to system messages; retain the final question as the salient
user message. Do not import the old planner module: its prompt constants can be
read using the existing AST/literal extraction without executing gold stubs.
Decode the envelope and pass its string **verbatim** downstream; do not extract
answers from prose, change quoted facts, or test truth against gold at runtime.
No exact sentence-count or paragraph-relevance oracle is introduced.

Require full JSON parsing, exactly the expected key/type, no duplicate keys,
nonfinite values, trailing prose, or coercion, and normal completion with no
refusal. Preserve raw receipts/statuses. Invalid, null, or length-capped output
marks the workflow failed, propagates only a canonical null state into its own
remaining history, and cannot be rescued by a later plausible answer. Continue
those scheduled stages for comparable charged accounting. Provider/transport
failure or ambiguous accounting instead stops the acquisition, retains the full
unresolved allowance, and permits neither manual retry nor resume. These are
distinct application-loss and acquisition-stop policies, not selective deletion
of difficult examples.

## Fair rule materialization, not native admission

Every arm starts with the same full ordered public context and schema. No
support-label pruning, padding, hand-selected salient sentences, or fake attention
scores. Compute the existing public lexical attention proxy from actual messages
with no trace DB; report it as a proxy, not model attention.
[Proxy implementation](../../python/agentc/_attention.py).

Use the existing direct [native materializer](../../crates/agentc-optimizer/examples/typed_rule_materializer.rs):
actual `applies`, `propose`, and `safety_check(original)`; filter CC, answer SD.
Verify retained-message subsequence and exact schema/model/request parameters,
and remap only aligned dependency/attention metadata. Ambiguous provenance
abstains to the original call. Preserve actual CC thresholds, including its
8192-byte applicability gate, and SD's declared read-set semantics. Report no-op
coverage over **all selected tasks**; do not select only tasks where a rule fires.
Add offline parity tests for full context, duplicate titles/messages, invalid
upstream histories, abstention, and label blindness.
[CC](../../crates/agentc-optimizer/src/rules/context_compress.rs),
[SD](../../crates/agentc-optimizer/src/rules/state_drop.rs).

Unlike legacy `workflow_call`, omit production `input_tokens_upper_bound` entirely.
Client bytes and a monetary full-context reservation do not certify provider input
fit. Native model-downgrade admission remains disabled until the separately
reviewed UNKNOWN-capacity contract (`.20.5.1`) exists.
[Context-fit evidence and limitations](2026-09-05-typed-context-fit.md).

## Prospective acquisition and budget gates

First finish and independently audit the already approved repeat. No third repeat
or public paid launch follows from this note. The parent's roughly $22 remaining
estimate is provisional; only authoritative accounting can establish spendable
headroom after known charges, unresolved holds, and outstanding repeat allowances.

Recommended allocation:

| Group | Tasks / arms / stages | New calls | Purpose |
| --- | --- | ---: | --- |
| Format feasibility | 6 disjoint tasks × 2 full-context models × 3 | 36 | New schema/prompt compatibility, charged token estimate |
| Primary | 32 tasks × 2 models × {neither, CC+SD} × 3 | 384 | Public model × rule-bundle opportunity |
| Single-rule controls, only if cost gate passes | Same 32 × 2 models × {CC-only, SD-only} × 3 | 384 | Complete model × CC × SD factorial |

Freeze the 36-call pilot before dispatch: all must finish normally with valid
non-null application outputs, no provider failure, and no unresolved accounting
to advance. This is a format gate, **not an accuracy gate**; pilot answer quality
is reported without using it to pick questions or tune success thresholds. One
failure retires this version; any revision needs explicit review, not automatic
churn through more pilots. Offline materializer parity is also required.

Before examining primary outcomes, use public input sizes plus the pilot's
per-model/per-stage observed billed input overhead and output usage to make a
documented **forecast**, including 512 output tokens for every planned call in a
conservative forecast variant. Include pilot, all selected arms, every repeated
stage, parsing failures, and any approved future group allowance—not just answer
calls or nominal token savings. Quote observed-price and price-cap variants and
cache assumptions. No pilot-derived overhead is a provider-context certificate.

The existing [typed monetary reserve](../../bench/openrouter_pilot.py) uses each
endpoint's full context limit at request price ceilings, plus 512 output tokens:
Sonnet **$6.015360**, Haiku **$1.215360** per unresolved call. These are facts of
the inspected frozen accounting code, not new estimates of actual input size.
Do not lower them. With serialized dispatch, known charges release resolved
reserves; unresolved holds remain charged. Do not sum all 768 full-context
reserves as if calls were concurrent, or ignore one because the payload is short.

A concrete **proposal**, subject to accounting and independent approval, is a
shared public-group envelope of $11, with known-charge progress ceilings $0.50
for the pilot and $4.00 for primary plus controls. The combined $4.50 plus the
largest next-call reserve is $10.515360; maintain both group and original $50
campaign checks, including every preexisting hold. This does not authorize
changing existing frozen stage caps. Choose the eight-arm design only if its
predeclared conservative forecast fits the $4 primary/control allowance;
otherwise freeze just the four-arm primary if that fits, or stop. No branch may
depend on quality. Acquire selected arms interleaved in deterministic task blocks,
with independent histories; do not append controls after seeing an effect.
Any budget stop leaves a partial artifact, never a complete-matrix claim.

## What the experiment can establish

Report end-to-end official answer EM/F1 (invalid paths score zero), parse/finish
failures, observed and nominal cost, latency, caching, actual rule coverage, and
paired task-level contrasts. Bootstrap the 32 whole task clusters across every
included cell; this does not measure repeated-generation stability. The eight-arm
version separates CC, SD, their interaction, and model interactions at the
**workflow** level. Independent histories intentionally allow upstream effects;
they do not isolate answer-stage behavior on a fixed synthesis. Such a replay
would require another explicitly budgeted experiment, not uncounted reuse.

This fixed experiment needs no online semantic feedback. Labels stay offline.
For a later controller, neither string equality nor official normalized EM between
two model outputs establishes semantic fidelity of freeform quotes/summaries;
reference disagreement needs a separately validated contract and paid calibration.
Better cost/quality on 32 train tasks would be useful evidence of opportunity,
not competitive benchmark supremacy, adaptive joint-system proof, a damage bound,
or publication assurance.

## Appendix A: conservative metadata-research exposure exclusions

The HF landing page automatically displayed a training preview during metadata
research. No candidate draw had occurred. A subsequent **ID-only** extraction
yielded 36 distinct page hex IDs: 30 preview examples plus six metadata IDs.
Exclude all 36 conservatively. Their intersection with the protected 48 manifest
IDs was empty; no protected question or existing QA fixture body was opened.
Persisted in `bd-dnft` metadata; canonical sorted compact JSON-list SHA-256:
`3950ad79cf2ae3cf55f566bca3080a3e18e91d591345bdecf4a788851789b220`.

```text
5a7272eb5542997f827839d7
5a76a401554299373536012b
5a7722d655429966f1a36c99
5a7a06935542990198eaf050
5a7d0db955429909bec76924
5a7e36045542991319bc9440
5a82171f5542990a1d231f4a
5a83168855429966c78a6b2e
5a832c3455429954d2e2ec41
5a84dd955542997b5ce3ff79
5a879ab05542996e4f30887e
5a89372855429951533612e6
5a8b63755542997f31a41cfe
5a8d7341554299441c6b9fe5
5a8efd3c55429918e830d179
5ab345db55429969a97a8122
5ab381b155429969a97a816b
5ab698885542995eadef002a
5aba66c855429939ce03dcdb
5abd90545542996e802b47d7
5ac2a5d455429921a00ab01b
5ac2a912554299218029dae8
5ac2c3545542990b17b1548b
5adc1309554299438c868d3b
5ade025e5542997dc790711e
5adf44985542993a75d2646d
5adf732a5542993a75d264e9
5ae0d91e55429924de1b7198
5ae3918b5542994393b9e709
5ae3b4d05542992f92d82349
5e9ecfc04957053f60648a3e
62de689d86220b5cb895acea
665ec5b0490242930eb94440
66f8caead3186746f4524419
69625a973527f984b1d0cec1
6e519c9e41327dd543db817b
```

Verification remaining: implement/review `bd-dnft` selection and public/label
separation through `.20.14.1` (fixture/adapter); review official-metric parity,
real native parity, and `.20.14.2` (harness/grouped accounting) before any
prospective pilot. No source, manuscript,
protected fixture, or previous experimental artifact is changed by this note.
