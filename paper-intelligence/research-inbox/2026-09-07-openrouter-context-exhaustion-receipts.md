# OpenRouter context-exhaustion receipts: prospective classifier boundary

Checked 2026-09-07. Bead `bd-323l.6.13.8.6.20.5.1.2.2.1`.
Local source inspected at `d6242ab0f69b00fb5823a9755a04514fc9638107`.
Primary documentation and code inspection only; no provider requests, credentials,
journal, labels, or runtime changes. This narrows the [existing UNKNOWN-capacity
design](2026-09-05-unknown-capacity-routing-design.md), not its admission policy.

## Finding

A narrowly attributed **generation-time context truncation** can support a future
classifier. It is not an input-token bound or proof that a different request fits.
There is no present fallback authorization. Actual HTTP-error receipts require a
separate transport/identity contract; recognizing their error category is not
enough to identify the exact failed endpoint.

Anthropic distinguishes two cases: input alone exceeding context produces HTTP
400 `invalid_request_error`; on Claude 4.5 and newer, an accepted generation that
fills remaining context returns `model_context_window_exceeded`. This differs
from `max_tokens`, which exhausts the requested output allowance, and from
`refusal`. Context-limited content is truncated, even if its JSON happens to parse.
Sources: [context overflow](https://platform.claude.com/docs/en/build-with-claude/context-windows#context-window-overflow-behavior),
[stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons#model_context_window_exceeded).

OpenRouter documents `choices[].native_finish_reason` as the provider's raw stop
string, separately from normalized `finish_reason`. This supports the cross-doc
inference that the exact native string is meaningful, but does not demonstrate
its current wire representation on either pinned Anthropic endpoint. No such
receipt was acquired here. [API response contract](https://openrouter.ai/docs/api_reference/overview#finish-reason).

OpenRouter also documents canonical `error.metadata.error_type`:
`context_length_exceeded` concerns combined context; `max_tokens_exceeded`,
`token_limit_exceeded`, and `string_too_long` mean different limits. Nonstream
failures can arrive inside HTTP 200. The page shows both top-level `error` and
`choices[0].error` envelopes; a diagnostic parser should inspect both and reject
conflicts. Native `provider_code` and human messages are not the canonical
category. Generic HTTP 400 or Anthropic `invalid_request_error` alone is
insufficient. [Typed errors and envelopes](https://openrouter.ai/docs/api_reference/errors-and-debugging#typed-error-codes).

## Endpoint identity is the limiting distinction

With `X-OpenRouter-Metadata: enabled`, successful responses expose `requested`,
`attempt`, `is_byok`, and `endpoints.available[].{provider,model,selected}`.
**Failed responses have no selected endpoint**; optional `attempts` may describe
provider/model/status. Some early errors omit metadata; HTTP 500 masks it.
`attempt=1` alone therefore does not prove successful endpoint selection or even
a completed provider call. Cache replays omit routing metadata. Sources:
[metadata fields](https://openrouter.ai/docs/guides/features/router-metadata#field-reference),
[error-specific semantics](https://openrouter.ai/docs/guides/features/router-metadata#error-responses).

The same page documents `X-Generation-Id` for some postmortem lookups. The
[generation endpoint](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation)
requires an ID and exposes model/provider, native finish, cancellation, and cost
fields. Neither an uncaptured ID nor a provider display name reconstructs an exact
failed endpoint revision. No guaranteed recovery for every failure was established.

## Minimal proposed v1 — fail closed, classification only

Return `verified_native_context_truncation` only for a newly versioned receipt
satisfying **all** of these proposed requirements:

1. Original-ledger origin/path, immutable request+metadata fingerprint, group,
   reserve, attempt, and one durable parsed response are exactly linked. Preserve
   the canonical response digest, actual HTTP status, generation ID, and failure
   record; reject missing, duplicate, contradictory, or later terminal activity.
2. HTTP 200; exactly one text choice; no top-level/choice error or refusal;
   `native_finish_reason` exactly `model_context_window_exceeded`; normalized
   `finish_reason` exactly `length`. The latter is a deliberately restrictive
   prospective policy, **not** an endpoint-specific mapping verified here.
3. Fixed requested model and returned model match the frozen contract; returned
   provider and the sole `selected=true` endpoint match its provider/endpoint-model
   strings. Require integer (not boolean) `attempt=1`, `is_byok=false`, no hidden
   attempts, and the existing strict pipeline policy. Missing attribution rejects.
   Bind fresh endpoint observation and schema/adapter/provider-policy hashes too;
   endpoint strings are not proof of immutable weights.
4. Preserve reported usage/cost with the original accounting checks; retain the
   entire failed-attempt liability, including any known-cost component. No fake
   completion, quality pair, partial-answer stitching, hold release, or admission
   update. Classification alone never dispatches anything.

Canonical `context_length_exceeded` errors remain
`context_category_seen_identity_unqualified` in v1, regardless of HTTP status.
A future error-envelope version would need recorded bodies/selected safe headers
and an independently reviewed **failed-attempt** identity proof; it must not
invent `selected=true`. These are proposed restrictions, not claims that all
legitimate context failures will qualify. A separately reviewed one-shot,
pre-reserved exact-source fallback state machine would still be required.

## Current implementation boundary

At the source commit above, [request_json](../../bench/openrouter_pilot.py) discards
actual HTTP-error bodies and does not durably capture their generation headers.
For parsed inference responses, `Ledger._perform` appends the complete parsed
body before validation. `schema_text_choice` rejects the native context-window
string; `Ledger.call` then records failure and retains its allowance. Thus an
HTTP-200 context receipt could remain auditable even though it is not a result.
Transport timeout/cancellation/malformed transport JSON has no such completed
response proof. Malformed **application** JSON under a normal stop is instead an
application-invalid outcome, not capacity evidence.

[Group replay](../../bench/openrouter_groups.py) enforces source-first policy:
candidate dispatch requires a durable source completion validated against its
response. It does not authorize candidate-first fallback after any failure.
Changing this requires a new policy and chronology tests, not adding one string
to the success validator. Historical receipts and frozen acquisitions stay intact.

## No-network acceptance matrix for that future implementation

| Synthetic receipt/event mutation | Required outcome |
| --- | --- |
| Exact v1 attributed context stop, partial text or parseable JSON | Context classification; no semantic success; full liability |
| Native `max_tokens`/`length`, normalized `length` alone, or native context with normalized `stop` | No context qualification |
| Canonical context error at either documented location; HTTP 400 or 200 | Category diagnostic only; no v1 fallback qualification |
| Conflicting errors, refusal/content filter, generic 400, message-only “context” text, other limit categories | No qualification |
| Timeout, cancel, invalid transport JSON, missing body, normal-stop invalid application JSON | No qualification; preserve the appropriate financial/application outcome |
| Wrong endpoint/model/schema, no selected endpoint, extra attempts, `attempt=true`, BYOK, cache-missing metadata, unknown/mutating pipeline | No qualification |
| Missing/duplicate response, changed reserve digest/cost, forged generation ID, later result/failure, replayed classification | Fail closed without dispatch or releasing liability |
| Future authorized fallback: combined allowance absent, exhausted cap, source slot already claimed, crash at any boundary | No extra dispatch; no duplicate charge authorization |

Positive synthetic fixtures prove parser/state-machine behavior only, not actual
OpenRouter endpoint delivery. Missing real wire confirmation remains explicit;
this research neither requests such a call nor enables primary UNKNOWN routing.
