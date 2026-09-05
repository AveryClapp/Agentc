---
title: Direct Anthropic controls for the Claude gateway comparison
status: provenance; documentation-verified; direct-experiment-unverified
last-updated: 2026-09-05
owner: paper-intelligence
bead: bd-323l.6.13.7.1
---

# Decision

**GO for a tightly capped, matched direct-Claude smoke using the two dated IDs below.** Current primary documentation supports the required API controls. Account access, returned IDs, actual cache counters, and accepted wire payload still require live verification by the parent experiment. This research used public documentation and official SDK source only: no credentials, provider API requests, inference, builds, or runtime edits.

This experiment can measure a direct-service condition on a fixed subset of existing inputs. It cannot attribute every difference exclusively to the gateway: message projection, sampling, serving infrastructure, request timing, and cache conditions remain possible contributors. Choose the subset before examining direct outputs and join each result to its frozen source task, context, model, and input hash.

Research date: 2026-09-05 UTC (2026-09-04 America/New_York). Local source: `f88a1b6c03418cb2c3f37876cd3276d3cd0107a8`. This single file is raw provenance; no paper-intelligence IDs or paper claims were created.

# Snapshots and availability

| Direct Claude API ID | Base context / output limit | USD per million uncached input / output tokens | Primary reference |
| --- | --- | --- | --- |
| `claude-sonnet-4-5-20250929` | 200K / 64K | 3 / 15 | [Sonnet 4.5](https://platform.claude.com/docs/en/models/sonnet-4-5/overview) |
| `claude-haiku-4-5-20251001` | 200K / 64K | 1 / 5 | [Haiku 4.5](https://platform.claude.com/docs/en/models/haiku-4-5/overview) |

Both IDs appear as Active in the current lifecycle table, with retirement commitments no sooner than September 29, 2026 and October 15, 2026, respectively. These are earliest-retirement commitments, not scheduled retirement dates. The Sonnet reference separately calls it Legacy while explicitly saying it remains available; the labels differ, but neither source says it is retired. Documentation does not prove this particular account's access. [Model deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations)

Use the complete dated IDs, not `claude-sonnet-4-5` or `claude-haiku-4-5` aliases. Anthropic guarantees a fixed underlying model for an ID, while explicitly allowing surrounding routing, safety-classifier, and sampling infrastructure to change. A matching date/model name therefore improves model control without proving identical behavior across requests or gateways. [Model IDs and versioning](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions)

# Wire request and message projection

Send `POST https://api.anthropic.com/v1/messages` with the documented `x-api-key`, JSON content type, and `anthropic-version: 2023-06-01`. Freeze the version header; versioning preserves existing parameters but permits new output values and error variants. [API versioning](https://platform.claude.com/docs/en/api/versioning), [Messages API](https://platform.claude.com/docs/en/api/messages/create)

Recommended control fields:

```json
{
  "model": "claude-sonnet-4-5-20250929",
  "max_tokens": 512,
  "temperature": 0,
  "stream": false,
  "service_tier": "standard_only",
  "thinking": {"type": "disabled"},
  "system": "<exact original leading system text>",
  "messages": [
    {"role": "user", "content": [{"type": "text", "text": "<exact first passage>"}]},
    {"role": "user", "content": [{"type": "text", "text": "<exact next passage>"}]},
    {"role": "user", "content": [{"type": "text", "text": "<exact reinforced final question>"}]}
  ]
}
```

The Messages contract accepts text strings or typed content blocks, uses top-level `system` for the system prompt, and combines consecutive same-role messages into one conversational turn. `max_tokens` bounds generation; `standard_only` selects standard rather than priority capacity. [Create a Message](https://platform.claude.com/docs/en/api/messages/create)

**Projection rule for this fixture:** validate exactly one leading system message followed by user text, move only that system text to `system`, and preserve every subsequent string and its order. Keep one source message per direct message/content block; do not add separators, role labels, synthetic assistant turns, or another instruction. The local [message builder](../../bench/openrouter_matrix.py) emits passages before the final question, and the [reinforced contract](../../bench/openrouter_contract.py) modifies only that final question. This projection preserves supplied content/order, not a claim about identical hidden tokenization or gateway serialization. Reject unsupported source shapes instead of silently rewriting them.

`temperature: 0` remains supported for these older models, but does not guarantee deterministic results. Do not also send `top_p`. Current Python SDK v1 removes sampling parameters from its normal interface; raw REST or a deliberately verified SDK path avoids confusing that client limitation with model incompatibility. [Sampling parameter reference](https://platform.claude.com/docs/en/api/cli/beta/messages/create), [parameter lifecycle](https://platform.claude.com/docs/en/about-claude/model-deprecations#api-parameter-deprecations), [Haiku migration details](https://platform.claude.com/docs/en/models/sonnet-5/migration-guide#migrating-to-claude-sonnet-5-from-claude-haiku-45)

Omit tools, cache directives, custom stop sequences, beta headers, and OpenRouter-specific `provider`, `plugins`, `transforms`, or `usage` controls. In particular, `inference_geo` is unsupported on 4.5 and returns 400. These are protocol choices for a bounded text-only experiment; they should be frozen in its manifest. [Pricing and inference geography](https://platform.claude.com/docs/en/about-claude/pricing#data-residency-pricing)

# Response acceptance and cache accounting

Persist the raw response and `request-id` before validation. Require `type: "message"`, `role: "assistant"`, the requested dated `model`, and inspect content blocks by `type`. Retain `id`, `stop_reason`, `stop_sequence`, and complete usage. Normal completion is `end_turn`; `max_tokens` means truncation. Refusals, context-limit stops, and unexpected blocks must remain visible rather than being repaired or silently dropped. A truncated completion remains a scored, billed result; do not automatically continue it. [Official Message schema, pinned SDK source](https://github.com/anthropics/anthropic-sdk-python/blob/62de60b27d04f0927a0ccf0f2610597fafcfab6a/src/anthropic/types/message.py)

Direct usage reports `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, optional cache creation by TTL, optional server-tool usage, and optional `service_tier` (`standard`, `priority`, or `batch`). It does **not** define OpenRouter's `usage.cost` field. `output_tokens` already includes billed thinking tokens; do not add its decomposition again. Preserve missing/null fields as missing rather than asserting observed zeros. [Official Usage schema, pinned SDK source](https://github.com/anthropics/anthropic-sdk-python/blob/62de60b27d04f0927a0ccf0f2610597fafcfab6a/src/anthropic/types/usage.py)

Total input is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`; plain `input_tokens` is not the total when caching is present. API transformations mean visible text and token counts need not correspond one-to-one. [Message usage contract](https://github.com/anthropics/anthropic-sdk-python/blob/62de60b27d04f0927a0ccf0f2610597fafcfab6a/src/anthropic/types/message.py)

Both documented caching mechanisms are opt-in: top-level `cache_control` enables automatic breakpoint placement; block-level `cache_control` sets explicit breakpoints. Automatic placement does not mean caching is on for a request without directives. Omit both for this control and verify zero cache creation/read usage when reported; unexpected positive counters fail the uncached-condition check after preserving cost evidence. The default five-minute TTL applies after caching is enabled. [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

# Pricing and pre-dispatch bounds

For these two models, five-minute writes cost 1.25 times base input, one-hour writes 2 times, and reads 0.1 times. With per-million rates `p_in`, `p_out`, reconstruct standard on-demand cost as:

```text
USD = [p_in * (uncached + 1.25 * write_5m + 2 * write_1h + 0.1 * read)
       + p_out * output] / 1,000,000
```

Use a verified TTL breakdown if writes occur; the clean uncached arm reduces to input plus output. Label this **tariff-reconstructed cost from provider usage**, not a provider-returned billed-dollar field or an invoice reconciliation. Record the pricing snapshot and any account-specific discounts separately. [Official pricing](https://platform.claude.com/docs/en/about-claude/pricing)

Anthropic's token-count endpoint is free but explicitly returns an estimate; actual generation input may differ slightly. It excludes billing for system-added optimization tokens. The documentation does not provide a hard UTF-8-bytes-plus-constant tokenizer bound. An estimated token count plus a margin is therefore an engineering assumption, not a documented strict spending guarantee. This research did not call that endpoint. [Token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting)

A conservative reservation derived from the documented 200K base context and 512-output cap is **USD 0.60768 for Sonnet** or **USD 0.20256 for Haiku** per attempted request: reserve 200,000 input tokens plus all 512 output tokens at the table's base rates. This deliberately over-reserves relative to the context's output allowance. It assumes standard capacity, no cache writes/tools/betas, and no path admitting a larger context. A 2x-input defensive reserve would be USD 1.20768 / 0.40256 if guarding against unexpected one-hour cache writes. These are derived experiment bounds, not quoted provider limits.

Hold reservations for unresolved attempts; reconcile successes using validated usage and the frozen tariff. A separate local stage limit and shared campaign limit still apply. Estimated direct spend and OpenRouter-returned billed spend should remain distinguishable in outputs even when summed for the campaign guard.

# HTTP failures, retries, and comparison limits

Anthropic documents 400 validation/spend-limit errors, 401 authentication, 402 billing, 403 permissions, 404 missing resources, 413 oversize requests, 429 rate limits, and 500/504/529 server failures. Official SDKs retry transient errors twice by default, with backoff. Disable automatic retries (`max_retries=0` in supported clients) or use a verified single-attempt transport. Record HTTP status, safe error type, and request ID; timeout/connection failure does not prove that no inference occurred. [API errors](https://platform.claude.com/docs/en/api/errors)

The experimental policy should stop and retain the reservation on ambiguous transport failure, response mismatch, or incomplete accounting. Do not replay automatically after restart. A manually authorized retry needs its own attempt identity and reservation. These policies provide accounting discipline; the cited docs do not promise exactly-once execution or document a billing-safe idempotency key for Messages.

Before the matched subset, smoke each dated snapshot and verify model, schema, `standard` tier when reported, cache counters, and tariff reconciliation. Original OpenRouter catalog names and returned endpoint date strings establish an intended mapping, not proof of an identical backend configuration. Preserve the original frozen endpoint metadata alongside each direct result. Noncontemporaneous cache warmth, load, serving updates, and stochastic outputs limit gateway-only causal claims and make shared-host latency diagnostic. No direct results were produced here; parent Bead `bd-323l.6.13.7` owns live verification and the experiment.
