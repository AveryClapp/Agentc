# Structured procurement feasibility pilot

This is a controlled generated-workload screen, not a joint-system efficiency
result or real-world QA evidence. The three operations are record selection,
relational join/multiplication, and application of an approval policy. Twenty
tenant-scoped documents contain eight records each; unrelated tenants are real
distractors within this generated task, not arbitrary padding. Gold answers
remain evaluator-only. Malformed upstream output becomes explicit error state;
the application never repairs it from gold.

The frozen six cases use generator seed `2026090501`. Each runs on Sonnet,
Sonnet again, and Haiku through the pinned Anthropic endpoint: **54 calls**,
temperature zero, 512-token cap. Each arm consumes only its own preceding
outputs. The repeated source workflow measures observed variability; six cases
are not enough to establish a reliability or safety bound.

The required output grammars are sorted document IDs, a fixed-field procurement
fact, then a bare approver ID. Parsing rejects whitespace, missing/extra fields,
non-ASCII forms, and explicit error results; it does not validate arithmetic or
select the correct records. Whole-path strict scoring prevents a lucky valid
final ID after malformed upstream state from receiving credit.

## Frozen promotion rule

All 54 calls and 18 workflows must complete, every stage output must satisfy
the grammar, Sonnet and its repeat must each get at least five of six final
answers exactly correct, and Haiku must get at least four of six. A new native
study can be prepared only after reconstructing all pilot requests and histories,
recomputing this gate, and verifying charges against the authoritative ledger.
An incomplete, corrupted, or failing pilot cannot be promoted by editing its
summary. A passing pilot is a feasibility screen, not evidence of efficacy.

The pilot cap is **$1**, within the original **$50 campaign**, retaining all
prior uncertainty allowances. Known campaign spending before this pilot was
$17.44038747 plus $0.147306 in historical allowances. Every new request uses the
reviewed shared reservation, exact-request retry and total-deadline machinery.

Acquisition source: `f12e5aa`; independent checks: 398 tests plus 17 subtests.
Manifest: `ef2f66da6aaf39c80758016faa6389c3847a1a60572232bc13c57e2f6b62289f`.
The source/parser/prompt/feedback descriptor hashes bind the application call
sites. This pilot does not execute or train the native optimizer. The reviewed
follow-on native mode uses raw lexical comparison on valid outputs, maximal
disagreement for invalid output/upstream state, and unchanged 20-pair admission
floor and numerical risk settings. This stricter application validity check is
explicitly different from the earlier free-form QA workload.

Run `python -m bench.openrouter_structured_study run-pilot` with explicit
`--env-file`, original `--ledger`, `--output` and the `--manifest-sha256` above.
Use `PYTHONHASHSEED=0`; never replace the shared campaign ledger.
