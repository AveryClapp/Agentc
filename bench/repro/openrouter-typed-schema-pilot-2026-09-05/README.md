# Typed JSON procurement feasibility pilot

Status: **SUPERSEDED_BEFORE_ACQUISITION — zero paid calls.** Independent review
required an authoritative whole-prefix ledger check and a permanent stop after
provider failure before any live dispatch. Subsequent implementation also fixes
the frozen-rate long-context counterfactual. This manifest remains an unacquired
protocol record; it must not be resumed or promoted. A new source-bound freeze is
required to test those fixes, with the application, tasks and 54-call gate unchanged.

Prospective, development-only screen. Source `bd325e7`; canonical manifest
SHA-256 `4b19a54de565efea193de90cee4708ac963efa42f7ffa68bef26553248d4c64c`.
No native execution is enabled by this pilot, and no efficacy claim is implied.

This is a new application contract, not extraction, repair or re-scoring of the
three failed prompt-only pilots. The generator and six exposed seed `2026090501`
tasks are unchanged. Each stage returns a schema-constrained object with one
required `result`: three ordered IDs, a typed six-field synthesis, or an approver
string. The explicit null result is failure. Parsing consumes the complete JSON,
rejecting duplicate keys, malformed types, out-of-domain values and trailing
material. No gold labels enter requests or comparison. Exact parsed-value
equality ignores JSON formatting only; it does not certify task correctness.

There are 54 calls: six tasks × source/repeat/target × three own-history stages.
Both source and independent repeat use Sonnet 4.5; target uses Haiku 4.5, each
restricted to the Anthropic endpoint and its frozen dated identity. Stage schemas
are identical across arms. Responses are preserved raw; only validated normal
completion crosses into canonical application state. Refusal, length, invalid
JSON/domain or upstream state is failure, including length-terminated valid JSON.
There is no response healing, automatic retry or material router transformation.

The unchanged feasibility gate requires all 54 calls, all 18 valid workflows,
source/repeat each at least 5/6 exact final answers and target at least 4/6. Stop
at the first invalid application completion or provider failure. An incomplete
screen cannot pass. Independent reconstruction must match every payload, receipt,
own-history decision, schema, provider attribution, cost and reported gate.

The $7.90 stage ceiling provides reservation headroom, not a spending target.
Because injected schema instructions have no documented numeric overhead bound,
each new call reserves the entire freshly verified endpoint context at the
6/30-dollar-per-million input/output caps, plus zero permitted request fees:
Sonnet $6.015360 and Haiku $1.215360 with the 512-output-token cap. Every active
published price tier must fit those caps; inactive search/cache rates are ignored
only because neither tools/plugins nor `cache_control` can enter the request.
Unknown or missing usage retains the full failed-attempt allowance. First-schema
compilation may exceed the unchanged 120-second deadline and create such a hold.

Before this screen, the original $50 campaign has $17.76945847 known charges and
$0.248244 retained uncertainty: $18.01770247 committed and $31.98229753 remaining.
The shared ledger retains all old runs, failures and holds; no new budget exists.

Native model routing remains disabled. Client request bytes are diagnostic only,
not a bound on opaque provider-added schema instructions. The adapter therefore
omits the runtime's numeric input bound, and the native runner explicitly rejects
this workload until a separately reviewed capacity contract exists. The opt-in
native unit test uses an explicitly hypothetical capacity to check CC/SD schema
preservation only; it provides no live routing, admission or efficacy evidence.

Pre-acquisition verification: 561 tests plus 17 subtests pass; the separately
enabled existing-native check brings typed/legacy adapter tests to 151 passing.
Independent transport review closed pricing, grammar and finish-state findings.
Independent integrated-driver and frozen-manifest review is required before paid
dispatch. Grammar-cache warmth and shared-host latency remain uncontrolled.
