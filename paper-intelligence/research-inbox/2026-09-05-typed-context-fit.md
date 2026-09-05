# Typed requests: context-fit evidence and an honest experimental path

Date: 2026-09-05. Research Bead: `bd-323l.6.13.8.6.20.5`.
Status: primary-documentation review and unauthenticated metadata GETs; no model
calls, credentials, token-count POSTs, or runtime changes. This is a provenance
note, not a claim that the proposed routing policy is implemented or validated.

## Finding

**No documented pre-dispatch certificate was found for the complete input size
of an OpenRouter schema-constrained request.** Anthropic exposes a schema-aware
counter, but explicitly describes its result as an estimate. This does not supply
the genuine upper bound required by the current native routing interface.
[Anthropic token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting),
[OpenRouter OpenAPI](https://openrouter.ai/openapi.json).

Continue the fixed-model typed pilot under its existing authorization and gates.
Production typed requests should keep omitting `input_tokens_upper_bound`, so the
existing model-downgrade rule abstains. A future joint experiment needs a separately
versioned, opt-in policy that owns capacity uncertainty and fallback costs—not a
different label attached to a client-byte estimate.

## What the documented interfaces establish

### OpenRouter: capability metadata, not a full-input measurement

The official OpenAPI inspected on this date contains no `count_tokens` operation
or `max_input_tokens` field. Its `max_prompt_tokens` occurrences describe endpoint
metadata; they do not establish a request-side input cap. The official documentation
index lists Messages creation, but no token-counting operation. This is a negative
documentation finding, not proof that an undocumented service route cannot exist.
In particular, `/api/v1/messages/count_tokens` remains unverified.
[OpenAPI](https://openrouter.ai/openapi.json),
[documentation index](https://openrouter.ai/docs/llms.txt).

OpenRouter documents `POST /api/v1/messages` with structured output configuration.
Messages compatibility does not establish that the separate Anthropic counting
endpoint is implemented, or that a direct Anthropic request reproduces the
Chat Completions adapter's exact upstream payload and headers.
[Messages creation](https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-a-message).

`require_parameters: true` filters provider support; it does not measure the
request. Provider pinning and disabling provider fallback are useful identity
controls, but do not close this measurement gap. OpenRouter also documents
Anthropic beta-header handling for schema output.
[Provider selection](https://openrouter.ai/docs/guides/routing/provider-selection).

Public endpoint metadata retrieved without authentication at approximately
2026-09-05 22:13:40 UTC:

| OpenRouter model; selected provider tag | Context limit | Maximum completion | Maximum prompt |
| --- | ---: | ---: | --- |
| `anthropic/claude-sonnet-4.5`; `anthropic` | 1,000,000 | 64,000 | `null` |
| `anthropic/claude-haiku-4.5`; `anthropic` | 200,000 | 64,000 | `null` |

The selected names identified dated upstream Sonnet `20250929` and Haiku
`20251001`. These are observations of mutable public metadata, not a persisted
response snapshot or universal guarantee. A context limit is not a measurement of
a specific request; `null` is unknown, not zero or unlimited. Sonnet's larger
limit does not certify fit on Haiku.
[Sonnet endpoints](https://openrouter.ai/api/v1/models/anthropic/claude-sonnet-4.5/endpoints),
[Haiku endpoints](https://openrouter.ai/api/v1/models/anthropic/claude-haiku-4.5/endpoints).

### Anthropic: schema-aware counting still has two gaps

The direct API documents `POST https://api.anthropic.com/v1/messages/count_tokens`
with Anthropic authentication and `anthropic-version: 2023-06-01`. Its request
accepts `output_config.format` containing `type: "json_schema"` and `schema`, as
well as model, messages, system, and tools. Its result is `input_tokens`.
[Count-tokens reference](https://platform.claude.com/docs/en/api/messages/count_tokens).

However, the guide says actual message-creation input counts may differ from the
estimate, without giving a numerical one-sided error bound. Counting is free and
separately rate-limited; it does not use prompt caching. Some automatically added
system-optimization tokens counted by this endpoint are not billed.
[Token-counting guide](https://platform.claude.com/docs/en/build-with-claude/token-counting).

Separately, JSON output formatting injects additional system instructions whose
tokens are billed. The structured-output documentation does not give a
schema-bytes-to-injected-tokens upper-bound formula. These billed instructions must
not be conflated with the counter guide's non-billed optimization tokens.
[Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).

Thus even an exact reproduction of the upstream payload would still receive an
estimate, not a certificate. And this investigation has not established that the
direct payload reproduces OpenRouter's transformation. Any future counter study
must verify dated-model identity, messages, schema, feature headers, and adapter
parity; counting an approximation does not settle either gap. This is an inference
from the documented contracts, not an observed gateway failure.

### Overflow is not always a rejected request

For Claude 4.5, input alone exceeding the context window produces a 400
`invalid_request_error`. When input fits but input plus requested output exceeds
the window, the request can be accepted and generation can instead stop with
`model_context_window_exceeded`. Therefore an output-token cap does not certify
input fit or complete generation.
[Context-window behavior](https://platform.claude.com/docs/en/build-with-claude/context-windows),
[stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons).

OpenRouter distinguishes normalized `finish_reason` from provider-native
`native_finish_reason`. Preserve both. A generic `length` stop is not sufficient
evidence to distinguish an output cap from context exhaustion; ambiguous outcomes
must remain ambiguous rather than being classified as verified overflow.
[Response contract](https://openrouter.ai/docs/api_reference/overview).

## Existing implementation boundary

At base commit `e0b98b7`,
[`RequestRequirements::from_call` and `ModelTarget::supports`](../../crates/agentc-optimizer/src/model_catalog.rs)
require a numeric input upper bound and compare it plus maximum output against
target capacity. The admission comparison does not consult the bound's basis
label. Changing that label cannot make a numeric estimate safe.

The [typed adapter](../../bench/typed_relational_workload.py) therefore omits the
production bound and retains client bytes only in `typed_client_size_diagnostic`,
explicitly excluding opaque provider schema instructions. This preserves the
existing fail-closed behavior. No provider capacity proof follows from native
tests that inject a hypothetical bound solely to exercise optimizer mechanics.

## Proposed executable seam: controlled attempts with unknown capacity

The following is a design proposal, tracked separately as
`bd-323l.6.13.8.6.20.5.1`. It requires implementation, tests, independent review, and
a new experiment manifest before use. It does not authorize changes or spending.

1. Keep the current proven-bound policy unchanged and default. Represent
   `PROVEN_BOUND` and `UNKNOWN` distinctly. Introduce an explicitly opt-in policy,
   for example `opaque-provider-attempt-v1`; never pass a fabricated number through
   the existing hard-bound interface. Preserve protocol, capability, credential,
   and output-cap checks.
2. Freeze the exact model/provider cohort, schema hash, adapter/parser version,
   call-site identity, and a small client-size operating envelope. That envelope
   limits experimental exposure; it is not a proof of full provider-prompt fit.
   Initially permit unknown-capacity calls only as protected counterfactuals with
   the source result retained. Primary selection also requires the existing
   semantic-admission checks and a separately reviewed availability policy.
3. Before a primary candidate attempt, reserve enough existing campaign budget
   for both that attempt and at most one original-source fallback. Give attempts
   distinct receipts. Do not silently add repair, compression, provider fallback,
   or other prompt transformations to manufacture a successful result.
4. Treat verified input overflow or a native context-exhaustion stop as a distinct
   capacity/availability failure. Retain raw partial outputs, usage, costs, and
   unresolved accounting; do not expose partial output to the application. Under
   a prospectively fixed conservative policy, the first verified overflow disables
   that exact candidate cohort until reviewed. Do not turn provider errors into
   fabricated paired semantic observations.
5. Allow one application-controlled source fallback only for verified capacity
   failure and only with valid remaining reservation. Preserve the original source
   request and valid stage history. Refusal, content filtering, ambiguous transport
   failure, and an unclassified 400 do not authorize this fallback. The source can
   also fail; surface and count that failure. Never use gold answers to decide
   whether to retry a structurally valid response.
6. Report every candidate and fallback attempt in cost, latency, failure, and
   exposure totals, alongside final task quality. Maintain capacity evidence
   separately from semantic-risk evidence. Version the plan/profile identity and
   do not pool old prompt-only or hard-bound observations into this new contract.

The resulting claim would be accountable, bounded experimental exposure with
fallback—not guaranteed context fit, guaranteed availability, or zero semantic
damage. A clean small pilot does not establish a future-overflow bound. Fallback
latency and cost remain part of the joint system's measured outcome.

## Empirical-overhead alternative and immediate decision

A frozen `estimated_fit_v1` policy could study a schema-aware count or empirical
overhead estimate plus a prespecified margin. Its validity would be an assumption
limited to the measured model/provider/schema/adapter cohort, with drift requiring
fresh validation. A pilot maximum is not a universal upper bound. This alternative
still needs explicit unknown-capacity failure accounting and cannot populate the
current production `input_tokens_upper_bound` field as proof.

For now: finish the fixed-model typed pilot; preserve native routing abstention.
The smallest credible route to joint experiments is the explicit uncertainty
policy above, not additional unsupported token arithmetic. Still unverified are
any undocumented OpenRouter counting operation, exact gateway-to-counter parity,
live overflow normalization for these endpoints, and the proposed fallback policy's
implementation and measured behavior.
