# Configuration

Every environment variable the Agentc runtime and benchmark harness read, in one
place. This is the source of truth; the README and specs link here rather than
re-declaring knobs.

Agentc has three config layers, by design:

1. **Profiler config** (`python/agentc/_config.py`) — the only typed surface.
   Precedence is **kwargs > environment > `~/.agentc/config.toml` > defaults**.
   Unknown TOML keys warn. Covers the four `capture_*` / `fail_open` /
   `storage_path` knobs.
2. **Optimizer config** (`crates/agentc-optimizer/src/config.rs`) — a parallel
   Rust struct with its own `[optimizer]` TOML section and env overrides.
3. **Ad-hoc reads** — memoization, guard, provider, and every `bench/` knob are
   read inline at their point of use.

Unifying these into one registry is future work; this document is the single
place they are all *described*.

## Runtime knobs (`AGENTC_*`)

These change shipped runtime behavior. Set them in the environment or (for the
profiler and optimizer) in the resolved storage directory's `config.toml`.
`AGENTC_CONFIG_PATH` selects an explicit shared bootstrap file. Python and native
bootstrap use that same file even when it relocates runtime storage. Optimizer
precedence is defaults, TOML, then environment. Resolve once per lifecycle;
invalid optimizer controls disable optimization and exploration and expose a
content-free diagnostic through `_native.optimize_configuration()`.
An invalid `AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION` override disables only
optional probes, preserving independently admitted plans.

Use `[optimizer]` for planner windows, enablement, composition, overhead and
`shadow_rate`; use `[optimizer.selection]` for `objective`, `min_plan_evidence`,
`plan_profile_freshness_hours`, `max_rewrite_depth`, `divergence_exposure_budget`
and optional `global_divergence_threshold`. `[optimizer.exploration]` accepts
`enabled`, `allocation`, `calls_per_site_24h` and `max_concurrent_counterfactuals`.
Plan envelopes carry the resolved sampling rate used by SDK shadow dispatch.

`[optimizer.rules.RuleName]` accepts `enabled` for each registered rule.
`ContextCompress` also accepts `min_prompt_bytes` and `min_dead_fraction`;
`ParallelBranch` accepts `max_fanout`. `[optimizer.accuracy_budget]` maps known
rule names to finite divergence fractions in `[0,1]`. Rule construction and
identity both use these values; changes require fresh evidence only for plans
containing the affected rule. `AGENTC_ENABLED_RULES` overrides file enable flags
as a complete whitelist while retaining tuning. Custom routing arrays and
unknown fields are rejected; routing uses the versioned model catalog.

| Variable | Default | Purpose |
|---|---|---|
| `AGENTC_STORAGE_PATH` | `~/.agentc` | Data dir for `traces.db`, `cost_model.db`, `optimizer_audit.db`. |
| `AGENTC_CONFIG_PATH` | storage directory's `config.toml` | Shared profiler/native optimizer bootstrap file. |
| `AGENTC_CAPTURE_CONTENT` | `true` | Capture prompt/response text in spans. |
| `AGENTC_CAPTURE_EMBEDDINGS` | follows `capture_content` | Capture embeddings in spans. |
| `AGENTC_FAIL_OPEN` | `true` | Fail-open boundary. `false` re-raises Agentc-internal errors (debugging opt-in). |
| `AGENTC_MEMOIZE` | `true` | `0/false/no/off` turns every `@memoize` into a pass-through. |
| `AGENTC_MEMOIZE_SIMILARITY` | `0.92` | LSH similarity threshold for a cache hit. |
| `AGENTC_MEMOIZE_TTL` | `3600` | Cache TTL, seconds. |
| `AGENTC_OPTIMIZE` | `true` | Master optimizer switch. `0/false` → always `PassThrough`. |
| `AGENTC_OPTIMIZE_HOT_THRESHOLD` | `3` | Observations before a call site is "hot" enough to optimize. |
| `AGENTC_OPTIMIZE_COST_MODEL_WINDOW` | `50` | Rolling per-call-site cost-model sample window. |
| `AGENTC_OPTIMIZE_DIVERGENCE_WINDOW` | `50` | Exact newest shadow samples retained per call-site/rule divergence estimate. |
| `AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW` | `50` | Independent exact execution-outcome and paired-divergence windows per complete plan. |
| `AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE` | `20` | Paired observations required for complete-plan admission. |
| `AGENTC_OPTIMIZE_OBJECTIVE` | `cost` | Complete-plan selection objective: `cost` or `latency`. |
| `AGENTC_OPTIMIZE_PLAN_PROFILE_FRESHNESS_HOURS` | `24` | Maximum age of paired evidence for admission. |
| `AGENTC_OPTIMIZE_MAX_REWRITE_DEPTH` | `3` | Maximum semantic rewrite depth, in `[1,3]`. |
| `AGENTC_OPTIMIZE_DIVERGENCE_EXPOSURE_BUDGET` | `1` | Rolling exact-plan exposure limit. |
| `AGENTC_OPTIMIZE_EXPLORATION_CALLS_PER_SITE_24H` | `20` | Persisted rolling counterfactual attempt cap per call-site version. |
| `AGENTC_OPTIMIZE_MAX_CONCURRENT_COUNTERFACTUALS` | `1` | Native counterfactual concurrency cap per call-site version; SDK process worker cap also applies. |
| `AGENTC_OPTIMIZE_MAX_OVERHEAD_MS` | `5` (release) / `50` (debug) | Plan-overhead kill-switch budget. |
| `AGENTC_OPTIMIZE_EXPLORATION` | `true` | Reference-visible calibration for cold plans, competitors of an admitted plan, and stale or divergence-rejected evidence. Provider candidates issue a second, **real and potentially billed** call; cached candidates decode their stored SDK response. `0/false/no/off` disables both. The persisted default cap is 20 attempts per call-site version per 24h, with one active lease per site and four workers process-wide. |
| `AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION` | `completion_first` | Cold-probe allocation: finish one evidence cohort rather than spreading the default 20 attempts across plans. `least_evidence` retains the historical allocator for comparisons. Resolved once per optimizer lifecycle; invalid values disable optional exploration. |
| `AGENTC_OPTIMIZE_SHADOW` | `0.02` | Bernoulli shadow-sampling rate. A sampled call issues a second, **real and billed** un-rewritten call inline to measure divergence. |
| `AGENTC_COMPOSE` | `true` | V2 composition (`1`) vs V1 first-match (`0`). |
| `AGENTC_EVAL_PLANNER_MODE` | `joint_guarded` | Evaluation control: `current_greedy` selects projected-savings composition while retaining complete-plan guard/context checks and disabling counterfactual exploration. Resolved once per optimizer lifecycle; invalid values pass through. This comparator does not use the joint planner's paired-evidence admission floor. |
| `AGENTC_OPTIMIZE_SELECTION_AUDIT` | `false` | Opt-in acquisition telemetry, resolved once per optimizer lifecycle. `1/true` adds the actual joint selector's reference, alternatives, estimates, admission state, policy and rejection reasons to `Plan.raw_json`. Cold, interrupted and unavailable searches are explicit; missing counts remain null. The greedy comparator reports that it does not enumerate complete plans. Exploration preserves the policy choice while recording the effective reference primary separately. Audit serialization adds measured planning work; default envelopes and canonical plan identities remain unchanged. |
| `AGENTC_SHADOW_DIVERGENCE_MODE` | `lexical` | Guard divergence metric: `lexical` Jaccard, `normalized` Jaccard after case/article/punctuation normalization, or `embedding` cosine distance. `embedding` falls back to `normalized` if the embedder is unavailable. |
| `AGENTC_SHADOW_DIVERGENCE_BUDGET` | per-rule budget, else `0.05` | Overrides the divergence threshold above which a rule is disabled for a call site. |
| `AGENTC_PROVIDER` | `openai` | Canonicalization provider hint. |
| `AGENTC_ENABLED_RULES` | unset = all | Comma-separated rule whitelist (ablation use). |
| `AGENTC_BIN` | auto-discovered | Path to a prebuilt `agentc` binary for the micro-benchmark. |

`AGENTC_SHADOW_DIVERGENCE_MODE`, `AGENTC_SHADOW_DIVERGENCE_BUDGET`, and
`AGENTC_OPTIMIZE_DIVERGENCE_WINDOW` define the accuracy-guard measurement
surface.

The default exploration allocator first favors eligible plans with fewer failed
or abandoned attempts in the rolling window, then more exact paired observations,
then greater projected savings. A fixed seeded identity ordering breaks ties.
This concentrates acquisition on one reliable cohort, including across restarts.
The projected savings are an allocation prior; they cannot admit a plan. The
20-pair admission floor, 20-attempt site cap, concurrency cap, compatibility
checks, and divergence-exposure budget still apply. Changing allocation preserves
the site's durable attempt count. Failed probes or changing plan identities can
still prevent a cohort from reaching admission within the cap.

When an admitted plan has an eligible cold competitor, acquiring a lease selects
the immutable original request for that invocation. The SDK returns its response
and runs one candidate off-path; it does not execute the incumbent on that probe.
Source observations update the reference profile, and paired evidence updates
only the leased complete-plan identity. If exploration is disabled or no lease
can be acquired, the admitted plan remains selected. Candidate failures consume
an attempt without retrying the source; a source failure closes the lease before
candidate dispatch. OpenAI and LiteLLM requests for client function tools can
calibrate when the adapter declares the `typed-actions-v1` comparator and a
versioned tool-settings digest. The digest includes full tool definitions,
tool choice and the parallel-call setting; both declarations bind the call-site
identity. Legacy clients, streaming, hosted tools, multiple choices and
unsupported payload shapes abstain. Opaque but supported tool histories remain
unchanged through parameter-only rewrites. Background probes call only the
provider backend; they never run generated tool actions or replace the returned
reference response.

Reaching 20 paired observations stops probing a current, admitted plan. A plan
rejected by the same selector for evidence older than 24 hours or an excessive
upper-p95 divergence can collect bounded refresh pairs beyond that count. Refresh
retains the actual counts and adverse samples in the existing newest-50 paired
window; the ordinary admission checks decide whether the updated profile qualifies.
Freshness uses the latest paired timestamp, so one fresh pair can restore an
otherwise qualifying stale profile. This does not require a new set of 20 fresh
pairs. Non-positive benefit and future-dated evidence alone do not request refresh.
Disabled plans, exposure limits, and the existing rolling attempt cap still block
probes; refresh does not reset them.

Historical serialized exploration policies without an allocation field retain
`least_evidence`; new runtime policies explicitly default to `completion_first`.
Existing frozen experiments retain their original sources and artifacts.

Structured assistant actions use an exact semantic comparison for shadow and
exploration feedback. Tool names, ordered arguments, accompanying text and
refusals participate; volatile tool-call IDs and JSON object key order do not.
Different actions have divergence 1, identical actions have divergence 0, and
malformed envelopes remain unknown. This applies in every divergence mode.

Normalized comparisons use `normalized-jaccard-v2`; embedding comparisons bind
`embedding-cosine-normalized-jaccard-v2`, including their fallback. Containment
no longer turns added negation or conflicting alternatives into exact agreement.
These versions cold-start older normalized and embedding plan evidence while
preserving the lexical contract. The SDK checks the native metric declaration
before planning. Token or embedding similarity remains a proxy and does not
validate semantic correctness or the frozen damage target.
Declared tool requests carry the `typed-actions-v1` guard marker in their
call-site identity, so previous empty-text tool comparisons cannot admit them.

The model catalog is `agentc-model-catalog-2026-10-12-r3`, with composite price
table `agentc-price-table-2026-10-12-r2`. Direct OpenAI, Anthropic and Together
targets retain their existing facts and observation dates. OpenRouter's
`openai/gpt-4o` and `openai/gpt-4o-mini` slugs use a separate namespace and an
October 12 public catalog observation. These are catalog cohorts, not immutable
provider binaries. SDK endpoint digests participate in call-site identity, so
router, direct-provider and unqualified endpoints cannot borrow admission.
LiteLLM's OpenAI backend adds its adapter prefix: for example, an OpenRouter
request uses `openai/openai/gpt-4o-mini` and sends `openai/gpt-4o-mini` on wire.
The catalog and price-table changes cold-start previous profile lookups.

ContextCompress retains the latest assistant message and every message after it.
Earlier assistant exchanges, ending at the next assistant message, are retained
or removed together. Prefix context before the first assistant can still be
compressed; the original user instruction and explicitly protected inputs survive.
This contract uses message roles rather than guessing tool provenance from text.
Opaque native tool-call histories remain ineligible for this text rewrite.

The rewrite implementation version is `assistant-exchanges-v1`. In the profiled
selector, older ContextCompress evidence cannot admit the updated rewrite,
including routed and composed plans; it needs evidence under the new identity.
Plans without ContextCompress retain their implementation identity. The explicit
`current_greedy` comparison mode still uses its documented admission behavior.

Complete-plan profiles also bind the adapter's `provider-request-config-v1`
declaration. Seed, reasoning/thinking controls, tool choice, penalties, output
controls and service tier contribute to a canonical settings digest. Prompt
content, credentials and caller annotations stay outside this declaration.
Known extension controls are supported explicitly; unknown or opaque controls
use the original provider request and receive no complete-plan observation.
New declarations cold-start legacy profiles, and the SDK verifies that the
native extension supports this identity contract before planning.

Use `with agentc.application_version("support-prompts-v2"):` around calls whose
prompt templates share an application revision. Change the stable identifier
whenever template semantics change, including edits that preserve message roles
and provenance. The identifier accepts 1–128 ASCII letters, digits, `.`, `_`,
`:`, or `-`; use a release identifier rather than prompt or user data. Only its
versioned SHA-256 digest enters the native call declaration.

A new declared revision cold-starts reference and alternative evidence for that
call site. It cannot reuse undeclared profiles or another revision's admission,
guard state, provider-alias resolution or exploration allowance. Previously
learned cohorts remain available under their original declaration after restart.
Unscoped calls preserve their existing identities; structural inference cannot
detect an undeclared semantic template edit. Nested contexts restore the prior
revision, and asyncio tasks, `asyncio.to_thread`, `traced_executor.submit` and
`parallel_map` propagate it. Separate process workers must declare their revision.
The SDK verifies native support whenever a revision is declared; malformed or
unsupported declarations abstain from planning and complete-plan observation.

Inside an application revision, `parallel_map` records an ordered batch profile
when every intercepted SDK call has a complete provider-reported outcome. The
profile binds the callback, input slots, per-slot call order, worker count and
every branch's plan and runtime versions. It sums physical tokens and reported
cost while using measured map wall time for latency. A second branch's model or
price revision invalidates the retained window just as a first branch's does.
`on_observation=callback` receives a content-free `ParallelObservation` with the
profile identity and totals, or an explicit abstention with unknown cost.

This path currently profiles original calls and observe-only parallel
certificates. Estimated billing, missing outcomes, streaming or bypassed SDK
calls, nested maps, rewrites, fallback and auxiliary probes abstain; their partial
cost is never promoted to complete batch economics. The synthetic peer in a
parallel certificate is neither dispatched nor counted. Ordinary batch
observations carry economics without reference pairs.

For an explicit comparison of runs you already execute, pass `comparison=True`
and collect their `ParallelObservation` receipts. Inputs and final results must
be ordinary JSON values; opaque results retain ordinary economics and receive
no comparison token. `compare_parallel_observations(reference, candidate)`
compares a one-worker original-request reference with a concurrent run of the
same ordered inputs, request graph, callback revision and runtime vector. It
computes exact agreement of the entire ordered result list, records one paired
sample and strict guard feedback, and reports the measured cost and latency of
both executions. This is an output-agreement contract, not a task-quality score.

Receipt bindings are durable. A receipt belongs to one pair; an exact retry
repairs interrupted writes without creating evidence or charging twice. Runtime
drift, retired receipts, reordered inputs and receipt reuse abstain. The journal
retains lifetime completed execution spend even for incompatible or unpaired
runs and unsupported result projections; failed or unbilled work remains
unknown in the owning provider ledger. Comparison mode has its own plan
identity, so it cannot borrow evidence from ordinary batch profiles.

Neither collection nor comparison authorizes learned scheduling or callback
replay. Scheduling admission still requires a verified independence contract
and benefit after charging all acquisition and validation work.

`completion_batch` provides that contract for prepared provider requests. Pass
`CompletionRequest` objects with complete text-message payloads and standard
synchronous SDK clients; SDK retries must be disabled and application hooks,
custom headers/query controls, streaming and remote storage are excluded. The
driver freezes every payload before dispatch, validates it against the SDK
method, checks the client again before invocation and owns exactly one original
completion per item. It preserves SDK response types and input order. Its
execution contract and versioned SDK output projection form separate profile
identities from callback maps.

Declare `application_version` and a stable `site` for the batch's semantic call
site. `BatchPolicy(remaining_batches=..., objective="latency")` supplies the
remaining deployment volume used to amortize all completed acquisition cost
and latency in that cohort. Guarded mode starts with a serial reference and
admits a concurrent schedule only after the native evidence floor, exact output
agreement, runtime/freshness, exposure, serving limits and positive net benefit
checks pass. Unresolved model aliases and planning-budget overruns use the
reference. Model revisions, application releases, SDK versions and provider base URLs
cannot borrow the previous cohort's admission.

Explicit `mode="reference"` and `mode="candidate"` execute the calibration
batches you request; collect their observations and use
`compare_parallel_observations` afterward. The driver never reruns either batch
or adds calibration calls automatically. `on_schedule` reports the chosen
worker count and acquisition accounting. This path currently schedules original
requests; model/rewrite alternatives and their auxiliary/fallback accounting
remain outside this contract.

Prepared LiteLLM requests support its OpenAI backend with an explicit standard
OpenAI client in `CompletionRequest.client`. The client and its endpoint are
bound before dispatch; implicit clients, hooks, fallback chains, caching and
global request transformations are rejected. Anthropic requests use the direct
standard Anthropic client.

Batch observations also expose a content-free provider dispatch journal ID and
a foreground accounting snapshot. The native journal records each SDK provider
invocation before dispatch and resolves it once, retaining primary, fallback,
validation, shadow and exploration roles with their logical owners. HTTPX send
instrumentation measures actual HTTP attempts; unsupported transports, retries,
incomplete billing, failures and cancellation retain uncertainty. A late probe
can append its completion after the foreground is sealed. Prepared original-call
profiles require a complete matching journal and cannot publish a second
execution from the same journal. Native content-free publication inputs are
reserved durably, then profiles, acquisition totals, comparison tokens and the
result commit atomically. Startup or an exact retry recovers interrupted writes
without repeating provider requests. Changed runtime/cohort predecessors or
physical journals retire the pending publication; legacy reservations without
recovery inputs remain quarantined. Journals supply accounting evidence; they
do not supply task-quality validation or permission to optimize.

Prepared batches also enroll a durable economic owner before dispatch. All
modes require enrollment to succeed and raise before SDK dispatch if it fails.
Uncataloged original requests receive a content-free accounting descriptor,
keep their original payload and remain ineligible for quality admission or
optimized scheduling. Unsupported SDK/HTTP client variants, transport retries
and unknown endpoints retain their uncertainty. Acquisition
charges include every physical call in calibration, unpaired or unprofiled
runs, and validation, shadow, exploration and fallback work. Ordinary deployment
primaries remain serving cost; auxiliary or uncertain deployment work enters
the acquisition ledger. Foreground time includes observation publication; late
dispatch durations add a conservative time charge. Unknown billing, missing
receipts, observer failures and unfinished foreground accounting block favorable
scheduling estimates. Materialized cohort totals update atomically with receipts,
survive runtime/window retirement and do not add already-covered comparison
summaries again. Historical uncovered summaries retain their known charges and
remain incomplete accounting. Schedule callbacks expose completeness and unknown
charge counts alongside the known acquisition cost.
Prepared cost/time ceilings include acquisition amortized over the declared
remaining batches, as do the net-benefit estimates.

Terminal SDK receipts also retain hashes of the billing endpoint, generation ID
and request ID when available. For OpenRouter, keep the raw response generation
ID locally and explicitly call `reconcile_openrouter_generation` with the same
standard synchronous OpenAI client and a receipt from
`provider_billing_receipts(observation.provider_receipt_batch_id)`. The helper
reads authenticated generation metadata; it never sends another completion.
It checks the original endpoint and identity, terminal status, model and native
token counts, rejects BYOK billing, and records an immutable bill overlay.
One generation bill can settle only one physical invocation across all cohorts.
Known reported charges cannot decrease, and already-complete reported costs
cannot be repriced without separate profile invalidation.

A prepared batch with complete original SDK results but missing reported USD
returns an observation with `status="pending_billing"`. Its content-free
checkpoint retains the original graph, runtime vector, ordered result
fingerprints and foreground metrics. After verifying all its costs, call
`refresh_parallel_observation(observation)` to obtain that same execution's
recorded observation and comparison token. Startup also recovers ready
checkpoints. The resulting cost source is `provider_billing_verified`; retained
scalar estimates keep their original provenance and are neither repriced nor
counted again. Retirement of the profile predecessor or physical journal
prevents publication. Bills without a retained original checkpoint supply no
quality evidence.

Scalar execution samples also retain their cost source and any owned physical
receipt identities. Profile summaries report counts of reported costs, verified
bills, estimates, unknown costs and executions with no inference in the retained
window. Cost-sensitive admission requires measured costs throughout both the
candidate and reference windows, including auxiliary work. Estimates still
contribute timing, usage and alias observations. Missing historical labels
migrate to unknown; a later bill preserves an estimated scalar sample's original
source and sequence rather than repricing or recording it again. Direct provider
paths without reported USD or supported verified billing therefore remain
unqualified for cost-sensitive admission until a validated cost-bound path is
implemented.

New terminal SDK receipts retain a versioned, content-free billing usage
projection. OpenAI evidence includes the actual response service tier and
cache, audio, image, reasoning and prediction token categories. Anthropic
evidence keeps uncached input separate from cache reads and writes, preserves
the five-minute/one-hour write split, and records reported tier, geography and
server-tool counts. Missing fields remain missing; unfamiliar fields mark
incomplete schema coverage without retaining their contents. Counts must agree
with the original receipt and protocol. These fields provide evidence for a
future scoped tariff-bound verifier; they do not turn estimates into bills or
enable cost-sensitive admission. Historical receipts cannot acquire this
projection after the fact, and terminal retry/restart preserves their bytes.

`tariff_accounting()` explicitly predeclares the runtime's dated public token
tariff before SDK dispatch. Its `reports()` method returns integer nanodollar
intervals for original invocations, plus lifetime acquisition intervals when
the native prepare path enrolled an owner and phase. Ordinary standalone SDK
calls also receive a content-free journal. Supported scope covers direct global
text requests and client function tools on the pinned GPT-4o/mini and Claude 4.5
targets; unknown tiers, endpoints, features or token categories retain holds.
Missing OpenAI cache counts and Anthropic cache TTL splits widen the interval;
missing Anthropic read/write totals cannot establish an upper bound. Journal
loss, missing roots and unfinished work prevent complete batch/cohort bounds.
The quantity is `published_token_tariff_usd`, distinct from reported or verified
provider bills. Original estimated scalar rows, billed-USD uncertainty and
quality/admission gates remain unchanged. Tariff arithmetic uses reference
lower, candidate upper and acquisition upper in the same snapshot; this numeric
calculation supplies no quality certificate or plan release.

Verified billing inputs are checkpointed before the accounting transaction.
Startup can finish an interrupted accounting write without another metadata
lookup. An interrupted lookup without verified inputs retains its pending hold.
Lookup time, including failed verifications, enters acquisition accounting once.
Original SDK receipts, failures and quality observations stay unchanged; billing
does not repair missing receipts, unsupported transports or foreign ownership.
Actual bills remain distinct from token-price estimates. Other providers,
missing generation IDs and conservative cost-bound resolution are unsupported
and retain their uncertainty. Billing lookup does not release benchmark campaign
reservations or authorize paid inference.
The verifier and provider-prefixed catalog path are covered by local SDK
fixtures, including the LiteLLM bridge and uncataloged original requests.
This engineering qualification supplies no live billed workload or task-quality
evidence. Account metadata and admitted workload paths still require their own
campaign qualification.

Provider-expanded model aliases have a persisted resolution cohort per call-site
version, protocol, requested alias, catalog declaration and price-table version.
Planning uses the provider-observed revision for profile lookup while dispatch
preserves the original alias. A changed revision invalidates sibling plan lookups;
an opaque alias response invalidates a previously resolved revision. Immutable
targets retain strict version matching, and fallbacks do not resolve aliases.

In `joint_guarded`, cooldown-expired plans remain blocked from user-visible
execution while bounded counterfactuals collect a fresh evidence cohort. The
profile boundary is persisted, preserves lifetime accounting, and rejects
delayed feedback from the previous cohort. Re-admission requires the normal
20-pair floor, divergence, freshness, compatibility, net savings and exposure
checks. Cooldown expiry does not erase guard exposure or exploration limits.

Production complete-plan identities include rule-owned implementation and
configuration descriptors. Changing a participating rule's thresholds, safety
budget, routing catalog or cache policy requires fresh evidence for that plan;
unchanged rules retain their own identities. Descriptors are captured at
registration and contain no prompts, state values or storage paths. Custom
rules must implement `RewriteRule::identity`; cache rules additionally declare
the key-builder and backend policy. Missing, ambiguous or invalid declarations
cannot obtain a production observation identity. Internal declarations stay
inside the native call representation and are not sent to provider APIs.

Cached responses have separate exact-match and LSH evidence identities. The
identity includes the source class and LSH acceptance threshold, without cached
content, age or per-hit similarity. Successful hits report zero current provider
tokens and spend; historical usage remains attached to the cached response and
is recorded separately in trace attributes. Sampled hits receive `CacheHit`
divergence attribution, while decoder fallbacks record the actual provider call.
Sync and async OpenAI, Anthropic and LiteLLM adapters load cached payloads from
the active or merged store through the native SQLite connection. Cached model
revisions do not update the provider-alias resolution cohort.

SDK response keys use the `optimizer-sdk-response-cache-v1` namespace and a
shared native builder for lookup and seeding. They bind the original message
shape, requested model, call site, provider protocol and namespace, actual output
cap, supported generation controls, tools, output schemas and declared task
contracts. Tracing and provenance metadata do not change these keys. Original
SDK message and shape declarations contain only digests; unsupported shapes,
streaming and image requests do not obtain a cache key. Generic memoized values
and legacy entries keep their existing namespace. SDK decoders require a
matching version, provider protocol, namespace and response shape.

In `joint_guarded`, a cached candidate can acquire paired evidence through a
durably leased background decode after the original reference call. Sync and
async SDK adapters compare the actual cached payload with that reference without
another provider request. The user keeps the reference response until the normal
20-pair, divergence, freshness, compatibility, net savings and exposure checks
pass. Cache comparisons report zero candidate inference spend and their actual
decode latency; reference calls retain their own cost. Missing, corrupt or
incompatible payloads fail the lease without adding a pair, and completion replay
does not duplicate evidence. Restart preserves admission and exploration limits;
cooldown recovery requires 20 fresh pairs. Async decoding runs off the event loop.
Native tool-message histories are eligible when the cache key builder explicitly
binds their original shape; this capability contributes to the rule identity.
Other key builders retain the conservative native-message gate.
Custom cache key builders return `None` when a request cannot be represented
safely. These admission mechanics do not establish workload efficacy or the
frozen guard's damage target.

The experimental `agentc._output_validation.experimental_validation_scope`
accepts a `ValidationContract` with a stable validator ID, configuration digest,
pinned model version, cost cap and sync callback (optionally an async callback).
The `pre-release-validation-v2` declaration changes optimized plan identity while
preserving the reference cohort. Actual SDK request inputs stay local to the
validator; credentials and transport objects are excluded. Multi-completion,
streaming, image and unrepresentable requests abstain.

Sync and async OpenAI, Anthropic and LiteLLM adapters validate rewritten and
cached candidates before release. Unknown, stale, mismatched, over-cap or negative
verdicts run the original request once; fatal stops and cancellation propagate.
Validator usage and rejected-candidate usage are recorded separately from the
returned response, so cache normalization cannot erase validation spend.
Background exploration runs the same validator and still obtains its divergence
from the actual reference comparison. Verdicts alone add no reference pairs.
Missing validator billing excludes the execution from profile calibration.
Two distinct rejected executions in the rolling 24-hour window disable the
exact plan through a separate durable rejection journal. Replay does not add a
signal, and rejection records add neither divergence exposure nor reference
pairs. Restart retains the disable; cooldown recovery still requires fresh
paired admission. A rejection that cannot be committed stops the dispatch before
a reference retry. Unusable cached payloads and ordinary provider failures also
reject a candidate; unknown provider spend is excluded from calibration.

Execution samples retain reference fallback spend and measured retry duration
already included in their totals. The selector subtracts this charged work before
projecting future fallback and shadow work, preventing a retry from being charged
twice. Cost and latency caps use the resulting full projected execution estimate.
Legacy samples receive zero credit for unrecorded fallback components.

This callback interface is an engineering prototype. A declared cost cap checks
the receipt after execution. For owned campaigns, `bench.output_validator` binds
the serving contract, prompt, output cap and single-attempt funding policy to the
validator configuration digest. Its `OwnedOutputValidator` uses the enrolled
campaign transport to reserve the full serving bound before a provider POST and
refuses a bound above the declared cap. It never opens or resumes a campaign.
Verdict parsing requires one strict boolean field; malformed, duplicate, refused
or truncated verdicts retain their actual billed usage and reject the candidate.

Owned async cancellation signals its HTTP worker, drains settlement, and preserves
any issued reservation. Failure accounting reports known spend separately from
unresolved budget holds. SDK error spans retain these values across all six
provider paths, and incomplete spend cannot enter calibration. Fatal stops and
cancellation do not run a reference retry. Generic callbacks can provide a
`ValidationFailureUsage` accounting hook; their own dispatch and funding remain
the application's responsibility.

Validator accuracy and actual workload economics still require qualification
before campaign admission. The separate rejection controller is
an engineering mechanism; it does not establish the frozen damage, benign-disable
or retained-savings targets on actual workloads.

## Secrets and provider selection

API keys live in `.env.example` (copy to `.env`), not here:
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `TOGETHER_API_KEY`, `GROQ_API_KEY`,
`HF_TOKEN` (or `HUGGINGFACE_API_KEY`). `BENCH_OPENAI_BASE_URL` /
`OPENAI_MAX_RETRIES` / `ANTHROPIC_MAX_RETRIES` tune the clients.

## Benchmark & reproduction knobs

Experiment-driver parameters, not shipped config. Read inline by `bench/`.

| Family | Variables | Purpose |
|---|---|---|
| Common | `BENCH_BASELINE_MODEL`, `BENCH_CHEAP_MODEL`, `BENCH_MAX_TASKS`, `BENCH_TASK_OFFSET`, `BENCH_FIXTURE_OVERRIDE`, `BENCH_STUB_MODE`, `BENCH_CONCURRENCY`, `BENCH_LLM_RETRIES` | Model ids, task window/count, fixture override, stubbed (no-spend) mode, concurrency, retries. |
| Guard sweep | `GSWEEP_N`, `GSWEEP_THRESHOLDS` | Task count and divergence thresholds for `run_guard_sweep.py`. |
| Guard eval | `GE_AGENT` (required), `GE_FIXTURE`, `GE_W`, `GE_N`, `GE_TAG`, `GE_CONFIGS` | `run_guard_eval.py` agent/fixture/warmup/measured/tag/configs. |
| Autogen | `AB_W`, `AB_N`, `AB_CONFIGS` | Autogen warmup/measured/configs. |
| Research-planner | `RP_W`, `RP_N`, `RP_CONFIGS` | Research-planner warmup/measured/configs. |
| Analyst-QA | `AQ_W`, `AQ_N`, `AQ_CONFIGS` | Analyst-QA warmup/measured/configs. |
| Cascade baseline | `CASCADE_THRESHOLD` | Confidence threshold for the frugal-cascade escalation baseline. |

`GE_W=0` in some sweep drivers means **no warmup** — those cells are behavioral
(rule-retention) only, not savings-comparable. See the reproduction docs.
