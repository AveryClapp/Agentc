---
title: OpenRouter Auto Router controls for a bounded service baseline
status: provenance; documentation-verified; live-auto-unverified
last-updated: 2026-09-04
owner: paper-intelligence
bead: bd-323l.6.13.6.1
---

# Decision

**GO for a budgeted smoke of the existing payload, followed by a bounded default-service baseline only if response attribution passes.** The documentation supports the requested controls. It does not establish that the four allowed models are all eligible for every prompt, that this account can execute the combined request, or that its results are comparable to a four-way optimizer with identical internal information. No inference requests, credentials, builds, runtime changes, or manuscript edits were used for this research.

Suggested experiment label: **OpenRouter Auto, default cost policy, four-model allowlist, restricted providers, standard service tier**. Keep the omitted cost setting and default session behavior visible in the manifest. Treat this as a live service comparison, with its own acquisition time and actual route distribution. Do not call it an optimal router or an exactly matched candidate-policy comparison.

Inspection: 2026-09-04 America/New_York, approximately 2026-09-05 00:05–00:07 UTC. Local source: `8ffe27016af767578921c78c1322b7b0ec9b90be`. This is a single provenance drop, not a new active paper ledger; no paper-intelligence IDs were created.

# Request contract

The existing [request builder](../../bench/openrouter_pilot.py) sends this control subset, plus bounded text messages, `max_tokens`, `temperature: 0`, `stream: false`, `transforms: []`, and `service_tier: "default"`:

```json
{
  "model": "openrouter/auto",
  "plugins": [{
    "id": "auto-router",
    "allowed_models": [
      "anthropic/claude-sonnet-4.5",
      "anthropic/claude-haiku-4.5",
      "google/gemini-2.5-flash-lite",
      "qwen/qwen3-30b-a3b-instruct-2507"
    ]
  }],
  "provider": {
    "only": ["anthropic", "google-ai-studio", "nebius/fp8"],
    "allow_fallbacks": false,
    "require_parameters": true,
    "data_collection": "deny",
    "max_price": {"prompt": 6, "completion": 30}
  }
}
```

`auto-router` is the correct plugin for `openrouter/auto`; exact model strings are supported allowlist patterns. The current router filters ranked candidates through a cost band. Omitting a cost setting behaves roughly like `low`; even `max` is a band, not an all-model ceiling. Account defaults can apply, and an account setting that prevents overrides can defeat request-level preferences. `provider.max_price` explicitly filters endpoints of resolved models. An empty eligible set can produce 404. Thus the allowlist bounds selection but does not enumerate an assured candidate set. [Auto Router documentation](https://openrouter.ai/docs/guides/routing/routers/auto-router)

This is the service introduced on August 10, 2026: prompt classification and trailing seven-day task-specific market-spend rankings, with model/provider restrictions on both request and account honored. Its live rankings and classification fallback make historical fixed-policy assumptions inappropriate. The launch post and current guide disagree about precedence when both deprecated `cost_quality_tradeoff` and `cost_tier` are supplied; this payload supplies neither, so that disagreement is immaterial here. [Official launch announcement](https://openrouter.ai/blog/announcements/introducing-the-new-auto-router/)

| Control | Documented meaning and experiment consequence |
| --- | --- |
| `provider.only` | Allowed-provider list intersects account restrictions. It is not a model-to-provider mapping. |
| Provider slugs | Base slugs match ordinary variants/regions; service-tier endpoints require opt-in. `nebius/fp8` targets the named variant. |
| `require_parameters: true` | Filters providers that cannot support requested LLM parameters; it does not prove identical model behavior. |
| `data_collection: "deny"` | Filters according to OpenRouter's provider-policy information. This is separate from `zdr: true`; do not equate it with a universal no-retention guarantee. |
| `max_price` | Prompt/completion values are USD per million tokens, not a total-request dollar ceiling. The existing ledger still supplies the campaign bound. |

These meanings come from [Provider Selection](https://openrouter.ai/docs/guides/routing/provider-selection). With `allow_fallbacks: false`, the SDK contract says an unavailable primary/custom provider returns an error instead of using the next provider. It does not independently document every interaction with Auto's internally generated model fallbacks; verify actual attempt metadata before accepting a run as single-attempt. [Pinned official ProviderPreferences schema](https://github.com/OpenRouterTeam/go-sdk/blob/b44b834138f008025dc499e7b47c8ad8c570f90f/models/components/providerpreferences.go)

Standard-tier routing is supported. Non-default service tiers require explicit admission; returned `service_tier` is `default`, `flex`, `priority`, or null when unavailable. The Google base slug does not itself opt into flex/priority. [Service Tiers](https://openrouter.ai/docs/guides/features/service-tiers)

# Public endpoint observations

Unauthenticated GETs returned these desired endpoints. Prices below are base prompt/completion USD per million tokens, converted from API per-token strings. Each listed endpoint advertised `max_tokens` and `temperature` support. These observations establish catalog presence, not account eligibility, current health, Auto rank inclusion, or enforcement of all controls together.

| Allowed model | Desired tag; provider name | Endpoint model name suffix | Base input/output price | Source |
| --- | --- | --- | --- | --- |
| `anthropic/claude-sonnet-4.5` | `anthropic`; Anthropic | `anthropic/claude-4.5-sonnet-20250929` | 3 / 15 | [Public endpoints](https://openrouter.ai/api/v1/models/anthropic/claude-sonnet-4.5/endpoints) |
| `anthropic/claude-haiku-4.5` | `anthropic`; Anthropic | `anthropic/claude-4.5-haiku-20251001` | 1 / 5 | [Public endpoints](https://openrouter.ai/api/v1/models/anthropic/claude-haiku-4.5/endpoints) |
| `google/gemini-2.5-flash-lite` | `google-ai-studio`; Google AI Studio | `google/gemini-2.5-flash-lite` | 0.10 / 0.40 | [Public endpoints](https://openrouter.ai/api/v1/models/google/gemini-2.5-flash-lite/endpoints) |
| `qwen/qwen3-30b-a3b-instruct-2507` | `nebius/fp8`; Nebius | `qwen/qwen3-30b-a3b-instruct-2507` | 0.10 / 0.30 | [Public endpoints](https://openrouter.ai/api/v1/models/qwen/qwen3-30b-a3b-instruct-2507/endpoints) |

Sonnet also advertised a price override above 200,000 prompt tokens; retain full pricing metadata, not just this base-price table. Nebius advertised `fp8`. Google advertised separate flex and priority entries with the same provider/model display names. These observations reinforce why a provider display name alone is insufficient. The [Auto endpoints GET](https://openrouter.ai/api/v1/models/openrouter/auto/endpoints) returned an empty `endpoints` array; it did not expose a four-model Auto candidate pool.

# Attribution and accounting

The transport already opts in with `X-OpenRouter-Metadata: enabled` and saves the raw successful response before validation. Router metadata reports `requested`, `strategy`, successful `attempt`, `is_byok`, candidate endpoints and their `selected` flags, and optional `attempts`/`pipeline`. Match structured fields rather than parsing `summary`. Cache replays omit router metadata, and some errors omit it; absence is not a successful endpoint attestation. [Router Metadata](https://openrouter.ai/docs/guides/features/router-metadata)

The selected endpoint schema contains only `model`, `provider`, and `selected`: no provider tag, immutable endpoint ID, or quantization. Match its provider/model pair against the frozen catalog mapping above, retaining request restrictions and service-tier evidence. This can substantiate the serving provider/model, but cannot independently prove the internal backend or FP8 execution. [Pinned official EndpointInfo schema](https://github.com/OpenRouterTeam/go-sdk/blob/b44b834138f008025dc499e7b47c8ad8c570f90f/models/components/endpointinfo.go)

Top-level `model` is the selected completion model. The current ChatResponse schema does not declare top-level `provider`, although the local pilot requires it. Its presence for Auto therefore needs a smoke. A selected Anthropic endpoint can expose a dated endpoint model while top-level `model` remains the allowed catalog slug. [Chat Completions schema](https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion)

Usage is returned automatically, including native-token counts, account charge in `usage.cost`, reasoning tokens, and cache details when available. Preserve `cached_tokens` and `cache_write_tokens` separately; missing fields are not verified zero. The router has no extra fee, but actual billed cost includes the serving model's accounting behavior. [Usage Accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting), [Auto pricing](https://openrouter.ai/docs/guides/routing/routers/auto-router#pricing)

Use generation `id` for reconciliation. The documented authenticated `/generation?id=...` response includes `model`, `provider_name`, `router`, `service_tier`, `session_id`, `total_cost`, native token fields, cache discount, and timing. Public research did not call it. Treat it as supplementary accounting evidence, not an independent cryptographic backend attestation. [Generation metadata API](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation)

# Stickiness and caching

Without an explicit session key, provider stickiness hashes the first system/developer message and first non-system message, scoped by account/model/conversation. It activates after a cache hit, expires after ten idle minutes, and can reuse Auto's resolved model while still eligible. Explicit `session_id` starts stickiness on any success; manual `provider.order` takes priority over provider stickiness. Gemini 2.5 supports implicit prompt caching without markers. [Prompt Caching and Provider Sticky Routing](https://openrouter.ai/docs/guides/best-practices/prompt-caching)

**Local implication:** [messages_for](../../bench/openrouter_matrix.py) constructs system + passages + final question. Distinct final questions can share the first passage and therefore the inferred session key. Full/compressed and natural/extended variants may preserve or change that opening. Unique task IDs do not establish independent Auto decisions. Preserving this default is acceptable for the named service baseline; report request order, opening-message identity, and observed cache usage. Adding a later isolated-session control would define a separate condition.

Response caching is distinct: it replays whole successful answers with zero billed counters. It defaults off without a cache header or caching preset, and `X-OpenRouter-Cache: false` can disable it. The current transport supplies neither a cache-enabling header nor a preset. Preserve a missing-metadata/zero-cost response for reconciliation instead of assuming a fresh route. [Response Caching](https://openrouter.ai/docs/guides/features/response-caching)

# Smoke acceptance and limits

Before a multi-request successor stage, inspect one already-exposed development prompt through the unchanged Auto payload and ledger. The research recommendation is to require:

1. Successful response with selected catalog model inside the four-string allowlist and valid text/usage/account charge.
2. `requested == "openrouter/auto"`, `strategy == "auto"`, `attempt == 1`, `is_byok == false`, and exactly one selected endpoint matching that model's frozen provider/model mapping. Retain all candidate and attempt records. Capture task classification if emitted; the Auto guide permits it to be absent when classification is unavailable.
3. Standard service tier when reported, interpretable cache accounting, and no unexplained pipeline stages. Preserve null/missing evidence explicitly.
4. Stop before another dispatch on a mismatch or unsupported response shape. A 404 establishes that this combined request is infeasible for the observed account/prompt/time; do not silently widen the pool or loosen provider/privacy/price controls.

These are proposed acceptance criteria derived from the contracts above, not observed outcomes. A successful smoke checks one served route only: it cannot establish eligibility of all four models, behavior under provider failure, absence of future routing drift, or equivalence to AgentC's search space. Retain failures and billed work in the same accounting ledger. Report the actual selected-model distribution, quality, billed cost, nominal uncached cost where reconstructible, cache counts, and separately measured service latency.

For the planned full-prompt comparison after fixed-arm acquisition, matched question/context inputs do not make the calls contemporaneous. Cache warmth, provider load, and routing state can differ across acquisition windows. Billed savings therefore describe the observed service conditions; latency is diagnostic, and nominal uncached cost is a supplementary normalization with explicit assumptions. This is an experimental interpretation constraint, not a claim that the API eliminates those differences.

The current [pilot](../../bench/openrouter_pilot.py) enforces Auto model membership and preserves metadata, but its optional `dispatch_contract` is a single fixed provider/model pair. The successor runner must validate the dynamically selected model against the corresponding frozen mapping before continuing; this can initially be an inspection step. It also suppresses HTTP error bodies, so failed-request router metadata would need a separately designed diagnostic path. Neither limitation was changed during frozen acquisition.

Outstanding live verification belongs to parent Bead `bd-323l.6.13.6`. This note does not promote a paper claim, alter existing result status, or establish perfect experimental fairness.
