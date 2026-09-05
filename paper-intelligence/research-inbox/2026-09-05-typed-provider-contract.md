---
title: Typed provider contracts for a prospective procurement experiment
status: provenance; documentation-verified; live-typed-unverified
last-updated: 2026-09-05
owner: paper-intelligence
bead: bd-323l.6.13.8.6.19
---

# Decision

Proceed to implementation and independent review of a **new, typed application contract**, then a small development-only provider smoke. Do not promote the failed prompt-only pilots, reinterpret their outputs, or spend against a newly reset budget. This note establishes API feasibility, not model correctness or joint-system efficacy. No inference calls, credentials, native builds, heldout labels, or acquisition-code edits were used.

The recommended boundary is: accept one complete JSON value, validate its exact application type, and compare that value with the paired source value. This is a prospective change in what the application consumes, not a forgiving extractor for the old prose/pipe-string contract. Keep the old results and their risk metric unchanged.

Inspected base: `1d06e1fdce026e8650383bbc7aa3c55410b3b3cf`. Public documentation and unauthenticated endpoint GETs checked September 5, 2026, approximately 21:55–21:59 UTC. No paper-intelligence IDs are created; this is one provenance note, not another task tracker.

# Endpoint evidence and wire contract

Both public endpoint GETs still advertise `structured_outputs`, `response_format`, `tools`, and `tool_choice` for the `anthropic` tag. Base prices below are USD per million input/output tokens. Catalog support is an observation, not an inference success or proof of enforcement.

| Requested model | Selected endpoint name to verify | Base price | Public source |
| --- | --- | --- | --- |
| `anthropic/claude-sonnet-4.5` | `anthropic/claude-4.5-sonnet-20250929` | 3 / 15 | [Sonnet endpoints](https://openrouter.ai/api/v1/models/anthropic/claude-sonnet-4.5/endpoints) |
| `anthropic/claude-haiku-4.5` | `anthropic/claude-4.5-haiku-20251001` | 1 / 5 | [Haiku endpoints](https://openrouter.ai/api/v1/models/anthropic/claude-haiku-4.5/endpoints) |

GET response byte hashes, observed at 21:56:55 UTC: Sonnet `33e38c48431a7fcf59a2a247c43200793027b9f8b8bd38ec219dc02317433296`; Haiku `e301a2506516fb834cabfb850484c456a799584c936b6a58bcbe0c8709cff844`. These hashes identify ephemeral GET observations; their complete bodies were not persisted. The retained V3 manifest at `bench/repro/openrouter-structured-local-pilot-2026-09-05/manifest.json` in worktree `bd-323l-6-13-8-6-18` has byte SHA-256 `ac8663a21b5c35388774bb678e8cb880bc0c487569912a4f6056a493a2267d0a` and corroborating endpoint fields observed at 21:49:37 UTC. Its *runtime catalog* deliberately advertises `structured_outputs: false`: the previous workload did not enable this capability. Do not mistake endpoint metadata for that old runtime capability setting.

OpenRouter's documented schema mode is `response_format.type = "json_schema"` with a named `json_schema`, `strict: true`, and `schema`. Plain `json_object` is not the requested schema contract. Support is endpoint-specific, can change, and strict enforcement varies between providers. [OpenRouter structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs), [API parameters](https://openrouter.ai/docs/api_reference/parameters)

Proposed filter fragment to combine with the existing bounded messages and transport controls:

```json
{
  "model": "anthropic/claude-sonnet-4.5",
  "provider": {
    "only": ["anthropic"],
    "allow_fallbacks": false,
    "require_parameters": true,
    "data_collection": "deny",
    "max_price": {"prompt": 6, "completion": 30}
  },
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "procurement_filter_v1",
      "strict": true,
      "schema": {
        "type": "object",
        "properties": {
          "ids": {
            "type": "array",
            "items": {"type": "string", "pattern": "^D(0[1-9]|1[0-9]|20)$"}
          }
        },
        "required": ["ids"],
        "additionalProperties": false
      }
    }
  }
}
```

This is a request fragment, not an executed request. Use the Haiku model string for its matched arm; retain `temperature: 0`, `max_tokens: 512`, `stream: false`, `transforms: []`, and standard service tier from the reviewed driver. In the prompt, explicitly request the matching JSON contract and exactly three sorted IDs. Do not embed the correct three IDs into the schema.

`require_parameters: true` rejects unsupported providers instead of silently ignoring parameters. Base `anthropic` restricts the provider but is not an immutable backend snapshot; verify the returned dated endpoint identity too. OpenRouter automatically supplies the Anthropic structured-output beta for JSON-schema response format. In contrast, its strict-tool route currently requires `x-anthropic-beta: structured-outputs-2025-11-13`; without that header it strips tool `strict`. These are gateway-specific rules even though Anthropic's direct API has graduated the feature. [Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)

Prefer JSON output over introducing tools here. Strict tools constrain function arguments and use a separate tool-call response shape; that would expand the adapter and refusal/finish-state surface without an application need. [Anthropic strict tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use)

# Provider constraints that remain load-bearing

Anthropic lists Sonnet 4.5 and Haiku 4.5 support. Direct requests use `output_config.format = {type: "json_schema", schema: ...}`; the old beta shape is transitional. Objects require `additionalProperties: false`. Basic types, enums, and simple anchored regexes are supported. Numeric bounds, string-length bounds, and array constraints beyond `minItems` 0 or 1 are unsupported; unsupported schemas return 400. Thus do not send `minItems: 3`, `maxItems`, or `uniqueItems`.

Refusals can return HTTP 200 with billed tokens and non-schema text. `max_tokens` termination can leave incomplete JSON. Even string enum/const capitalization can differ on ordinary completion. Do not silently case-fold.

New schemas incur grammar-compilation latency. Compiled grammars cache for 24 hours after last use; structural/tool-set changes invalidate them, while name/description-only changes do not. JSON output adds an injected, billed system prompt; changing output format invalidates the conversation prompt cache. The docs provide no numeric bound for that injected overhead. Prefilling and citations are incompatible. [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)

# Proposed application types, not a gold oracle

Use stable object envelopes at every stage. Top-level scalar/array handling through this exact gateway combination was not live-tested; an answer object with one scalar field avoids depending on it. The following are proposed schemas, using the supported subset above.

Synthesis schema:

```json
{
  "type": "object",
  "properties": {
    "sku": {"type": "string", "pattern": "^K[0-9]{3}$"},
    "vendor": {"type": "string", "pattern": "^V[0-9]{3}$"},
    "qty": {"type": "integer"},
    "unit": {"type": "integer"},
    "total": {"type": "integer"},
    "region": {"type": "string", "enum": ["EU", "US"]}
  },
  "required": ["sku", "vendor", "qty", "unit", "total", "region"],
  "additionalProperties": false
}
```

Answer schema:

```json
{
  "type": "object",
  "properties": {"id": {"type": "string", "pattern": "^M(0[1-9]|[1-9][0-9])$"}},
  "required": ["id"],
  "additionalProperties": false
}
```

Recommended local validation and comparison:

| Stage | Validated application value | Checks that must not be outsourced to a provider guarantee |
| --- | --- | --- |
| Filter | Ordered three-ID list | Exactly three, distinct, already sorted, byte-exact case, all IDs present in public task documents. Reject, do not sort or deduplicate. |
| Synthesis | Six-field typed record | Exact fields and types; retain existing public domain bounds. Boolean is not an integer. No string-to-number coercion. Do **not** repair or validate the factual join/total against hidden answers. |
| Answer | Scalar ID from the sole `id` field | Exact public ID grammar and valid upstream history. No extracting an ID from explanatory prose. |

JSON admits insignificant whitespace and object-member ordering differences; arrays are ordered. It does not define a unique byte serialization. Thus schema compliance is not the old byte-format contract. [RFC 8259, sections 2–5](https://www.rfc-editor.org/rfc/rfc8259)

For the new contract, parse the *entire* response as one JSON document, reject duplicate keys, trailing material, non-finite numbers, and coercions, then validate exact types. Preserve raw content separately. Compare the validated application values with exact strings, exact integers, ordered arrays, and order-insensitive object fields. A deterministic serialization of those validated values is acceptable as an implementation of equality; do not use it to repair invalid output. This is a design recommendation, not a provider promise.

Define paired disagreement prospectively as 0 only for equal valid values with valid upstream histories, otherwise 1. Neither validation nor disagreement reads gold labels. Downstream evaluation may still use heldout gold for the predeclared final score. Freeze the new metric/parser/schema identity, require fresh profiles for every arm, retain numeric admission/exposure settings, and never blend these samples into raw-lexical histories. This evaluates a different application contract; its results cannot retroactively validate the prompt-only pilots.

# Failure states, routing evidence, and money

OpenRouter exposes normalized `finish_reason` values including `stop`, `length`, `content_filter`, and `error`, plus `native_finish_reason`. Native token usage drives charging, and generation IDs support later accounting queries. HTTP success is therefore not sufficient application success. [OpenRouter response contract](https://openrouter.ai/docs/api_reference/overview)

Recommended fail-closed acceptance: require normal completion, no refusal/error signal, exactly one text response, intact endpoint attribution, and a valid complete application value. A length result remains paid failure evidence even if its text happens to parse. A filtered/refused result must never become a valid value or native learning observation. Preserve missing usage as missing and retain unresolved financial holds. Do not retry a refusal with a rewritten prompt, fabricate zero billing, or release a reservation because there was no usable answer.

Keep `X-OpenRouter-Metadata: enabled`; verify the selected endpoint/model, requested model, attempt count, and non-BYOK status against the frozen mapping. Inspect pipeline metadata for transformations. Metadata identifies serving/routing observations, not cryptographic proof that every schema keyword was enforced internally. [Router metadata](https://openrouter.ai/docs/guides/features/router-metadata)

Do not enable response healing: it repairs syntax and extracts JSON from surrounding text, which would introduce the exact post-processing treatment this experiment excludes. Reject unexpected healing pipeline evidence. [Response healing](https://openrouter.ai/docs/guides/features/plugins/response-healing)

Published base Sonnet/Haiku prices match the observed 3/15 and 1/5 rates. Client tools add schema/system/input/output tokens rather than a separate client-tool execution charge; that is not evidence of a fixed JSON-schema surcharge or an upper bound on its overhead. [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)

Keep grammar warmth distinct from paid prompt-cache reuse. Anthropic prompt caching through OpenRouter requires a top-level or block `cache_control`; omit both for the uncached condition and inspect reported read/write counters. Do not infer a grammar-cache hit from a prompt-cache counter or promise cold grammar compilation: another request may already have warmed it. [OpenRouter prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching)

Local implementation blockers are explicit: [make_request and Ledger.execute](../../bench/openrouter_pilot.py) currently reconstruct a text-only request and reject extra `response_format`; `upper_cost` uses serialized bytes plus fixed overhead. A typed request needs a reviewed, narrow schema parameter, exact request reconstruction/fingerprinting, matching replay validation, capability-safe routing, and an independently defensible reserve covering schema injection. Adding schema bytes alone does not prove the old overhead allowance sufficient. No such changes are made by this note.

For the smoke, use already-exposed development cases, identical stage schemas across models and treatments, no healing/tools/prefill, and the original shared campaign ledger. Charge compilation-bearing calls, failed calls, retries, and all holds. Record latency as observed end-to-end service latency; no cold/warm causal claim without a separately controlled design. Promote only after independently reconstructing every intended call, value, endpoint, cost, and the unchanged feasibility gate. Remaining verification belongs to the parent continuation Bead: actual combined-payload acceptance, refusal/native-finish mapping, accounting overhead, and matched schema compliance on both endpoints.
