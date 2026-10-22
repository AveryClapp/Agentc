---
title: Direct Provider Cost Bounds Research
status: draft
last-updated: 2026-10-12
---

# Direct provider cost bounds

This research supports integration review; it does not change the model catalog,
receipt schema, or admission policy. Sources were read on 2026-10-12. All examples
below are arithmetic fixtures, with no inference or authenticated account queries.

## Finding

**A successful direct-provider response can support a valuation of its reported
usage under a pinned public tariff. It does not, by itself, establish the final
amount debited from the account.** Preserve those quantities separately.

Inference from the sources below: a positive lower bound is defensible for a
specified *list-price token charge*, when every billed token category and pricing
modifier is covered. Public documentation does not establish a positive lower
bound on every customer's incremental cash payment. A credit-funded execution,
capacity commitment, or account adjustment needs separate accounting evidence.
An all-inclusive upper bound on arbitrary execution/account cost is also not
established by two token counts. Unknown tools, pricing modifiers, retry sends,
and contractual charges must remain outside a qualified token-only scope.

## OpenAI Chat Completions

The GPT-4o model page lists USD/MTok input, cached input, and output rates of
2.50, 1.25, and 10.00; GPT-4o-mini lists 0.15, 0.075, and 0.60. Both accept text
and image input and emit text. These are current observations, not proof of a
historical tariff or negotiated account rate. [GPT-4o](https://developers.openai.com/api/docs/models/gpt-4o),
[GPT-4o-mini](https://developers.openai.com/api/docs/models/gpt-4o-mini).

For a qualified text call, let `P = usage.prompt_tokens`,
`C = usage.prompt_tokens_details.cached_tokens`, and
`O = usage.completion_tokens`. With the appropriate tier's rates `ri`, `rc`, `ro`,
the published-tariff valuation of reported usage is:

```text
T = ((P - C) * ri + C * rc + O * ro) / 1_000_000
```

Validate nonnegative integral counts and `0 <= C <= P`. `completion_tokens`
already includes reasoning and rejected prediction tokens; do not add their
breakdowns again, and do not count only visible output. A streamed call can lose
the final whole-request usage chunk if interrupted. Missing final usage cannot
be promoted to complete evidence. [Chat Completions reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create),
[Predicted Outputs](https://developers.openai.com/api/docs/guides/predicted-outputs).

If only `C` is missing and the model, tier, and scope are otherwise qualified,
`P*rc + O*ro <= 1_000_000*T <= P*ri + O*ro`. This is a tariff interval, not a
reported bill. Earlier OpenAI models have no additional cache-write charge;
cached-token reporting excludes hidden system tokens and rounds down to a token
block. Use the returned billing-oriented counters rather than reconstructing
them from prompt text. Do not generalize this rule to GPT-5.6 and later, whose
cache-write pricing differs. [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching).

For example, mini at the standard tariff with `P=10000`, `C=4096`, `O=1000`
values at **$0.0017928**. With unknown `C`, the interval is
**[$0.00135, $0.00210]**. This example demonstrates only tariff arithmetic.

### Tier and revision restrictions

Omitting `service_tier` selects `auto`; a project setting can select Fast mode.
The response's tier identifies the mode actually used, including downgrades to
`default`. On these older models, Fast/priority requests return `priority`.
Inspect the actual response tier; request omission is not proof of the standard
rate. [Fast mode](https://developers.openai.com/api/docs/guides/fast-mode).

The current Fast tariff is GPT-4o **4.25 / 2.125 / 17.00** and mini
**0.25 / 0.125 / 1.00**, per MTok input/cached/output. GPT-4o's May 2024 snapshot
has a distinct higher tariff. Never price all `gpt-4o-*` strings as the current
alias. The current pricing table includes neither GPT-4o nor mini in its Flex
table; do not invent a supported Flex tariff. The page also describes endpoint
uplifts. [Pricing](https://developers.openai.com/api/docs/pricing.md),
[Flex processing](https://developers.openai.com/api/docs/guides/flex-processing).

Snapshots stabilize model identity; the GPT-4o page lists
`gpt-4o-2024-08-06` and `gpt-4o-2024-11-20`, and mini lists
`gpt-4o-mini-2024-07-18`. Preserve requested identity and returned identity.
An unknown alias/snapshot relationship, fine-tuned model, or unrecognized tier
does not acquire a tariff through suffix matching. [GPT-4o snapshots](https://developers.openai.com/api/docs/models/gpt-4o#snapshots),
[GPT-4o-mini snapshots](https://developers.openai.com/api/docs/models/gpt-4o-mini#snapshots).

Image input has model-specific tokenization. The documented tile token counts
differ substantially between GPT-4o and mini; a local text tokenizer is not a
cost meter. Image generation, search, containers, and storage can have separate
charges. A text Chat Completions qualification must exclude other endpoints,
audio variants, built-in server tools, and unknown extensions unless their full
accounting is added. Ordinary client function execution also has costs outside
the provider's token charge. [Images and vision](https://developers.openai.com/api/docs/guides/images-vision),
[Tool pricing](https://developers.openai.com/api/docs/pricing.md#built-in-tools).

## Direct Anthropic Messages

For the repository's Claude 4.5 family, current USD/MTok rates are:

| Model | Uncached input | 5m write | 1h write | Cache read | Output |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sonnet 4.5 | 3.00 | 3.75 | 6.00 | 0.30 | 15.00 |
| Haiku 4.5 | 1.00 | 1.25 | 2.00 | 0.10 | 5.00 |

[Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing),
[Haiku 4.5](https://platform.claude.com/docs/en/models/haiku-4-5/overview).

Unlike OpenAI's total prompt counter, `usage.input_tokens` excludes cache writes
and reads. Let `U = input_tokens`, `R = cache_read_input_tokens`,
`W = cache_creation_input_tokens`, `W5` and `W1` be the creation TTL breakdown,
and `O = output_tokens`. Validate `W5 + W1 = W` when the breakdown is supplied.
Total model input is `U + R + W`. For the qualified 4.5 standard tariff:

```text
T = (U*ri + R*0.1*ri + W5*1.25*ri + W1*2*ri + O*ro) / 1_000_000
```

If only the TTL split is missing, bound the write contribution between
`W*1.25*ri` and `W*2*ri`. If the read/write totals are absent, `input_tokens`
alone does not bound all billed input. [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching#tracking-cache-performance).

Example: Haiku 4.5, `U=10000`, `R=20000`, `W=5000`, `O=1000`, unknown write TTL:
the tariff interval is **[$0.02325, $0.02700]**. Pricing the receipt's uncached
`input_tokens` alone would yield $0.015 and undercount even the interval's lower
endpoint. Folding `U+R+W` into one counter loses the categories needed to compute
a tight bound. A 2x rate on that *known total* gives a looser token-input upper
bound for this family; a 2x rate on `U` alone does not.

`output_tokens` is the inclusive billed output total, including internal thinking;
summarized or omitted thinking text is not a token counter. Do not add a thinking
breakdown on top of the total. [Thinking costs](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost).

### Tier, feature, and revision restrictions

`service_tier="auto"` can consume Priority capacity; `standard_only` selects
standard capacity. The response reports `usage.service_tier`. Priority is tied
to a capacity commitment and cannot be assigned a standard per-call net cost
from public token prices. Its costs are explicitly absent from the Cost API.
[Service tiers](https://platform.claude.com/docs/en/api/service-tiers),
[Usage and Cost API](https://platform.claude.com/docs/en/manage-claude/usage-cost-api).

Cache modifiers stack with Batch discounts and geography pricing. Claude 4.6
and later support a US-only 1.1x token multiplier; earlier models reject that
parameter. Partner endpoints have their own rates. Keep Batch, partner routes,
fast mode, expanded context, geography changes, and unknown betas outside a
4.5 direct-standard qualification until individually covered. Client tool
definitions and results consume tokens; server tools can add fees beyond tokens.
[Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing).

Web search adds a fee per search, reported under `usage.server_tool_use`; token
counts alone omit it. Code execution can be charged by container duration and
can begin billing when files preload, even without a tool call. A tool-call count
does not substitute for duration accounting. Image understanding consumes input
tokens rather than a universal fixed image fee. [Web search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool#usage-and-pricing),
[Code execution pricing](https://platform.claude.com/docs/en/about-claude/pricing#code-execution-tool),
[Vision](https://platform.claude.com/docs/en/build-with-claude/vision#calculating-image-costs).

Claude 4.5 dateless names are convenience aliases for dated snapshots; later
4.6-generation dateless canonical IDs are themselves pinned snapshots. Do not
apply one naming heuristic to every generation. [Model IDs and versioning](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions).

Fallback-credit beta can shift a retry's reported cache-creation tokens into
cache-read tokens. Charge the returned categories and retain every physical
attempt; do not infer those credits for an unrelated 4.5 call or silently remove
the failed/refused predecessor. [Fallback credit](https://platform.claude.com/docs/en/build-with-claude/fallback-credit).

## Public usage is not a per-request invoice

Anthropic documents API credits that can fund direct API calls without adding a
payment method. Inference: positive list-price usage does not imply a positive
incremental cash payment for that call. It can still consume credits, which is a
separate economically meaningful quantity. Do not assume this account has credits
or a discount, and do not infer zero credit consumption. [API credits](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers).

OpenAI's Costs endpoint exposes daily buckets grouped by project, user, line
item, API key, or API source, not by individual request ID. Anthropic's Cost API
also has daily buckets and omits Priority costs. Inference: neither documented
aggregate endpoint provides an ordinary execution's unique bill without an
additional, justified attribution procedure. Do not allocate a busy account's
daily cost evenly across calls or claim authenticated usage is billing attestation.
[OpenAI Costs](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs),
[Anthropic Usage and Cost API](https://platform.claude.com/docs/en/manage-claude/usage-cost-api).

The sources inspected do not establish a universal contractual discount range,
tax allocation, credit adjustment policy, final rounding rule, or allocation of
prepaid capacity to each response. Without such evidence, preserve a zero lower
bound on incremental net payment, or mark that quantity unknown. A token-only
list valuation is not an all-inclusive account-debit upper bound.

## Integration implications

These recommendations are engineering deductions from the evidence above:

1. Name the economic quantity in every bound: public token tariff, consumed
   credits, or net billed USD. Keep tariff evidence distinct from provider bills.
2. Bind a bound to the exact owned SDK completion, direct endpoint, actual model,
   actual tier, request feature scope, usage categories, and dated tariff snapshot.
   A successful response is required; retries with no complete usage remain held.
3. Retain a content-free projection of cache categories, TTL split, actual tier,
   geography, and server-tool usage before response objects disappear. Validate
   type/range/cross-field identities. Preserve immutable historical journals.
4. Exclude unknown pricing dimensions. An upper bound for known tokens does not
   repair missing cache counters or prove that undisclosed tools cost zero.
5. For cost-sensitive admission use the same economic quantity and compare the
   candidate's upper bound, including acquisition/failure/probe/fallback costs,
   against the reference's lower bound. A zero reference lower bound cannot
   certify positive USD savings. Keep task-quality evidence independent.
6. Account-specific evidence can narrow net-cost bounds only through a separate
   validated tariff/contract or billing mechanism; a user-entered assertion cannot
   become an authenticated invoice.

Local inspection on 2026-10-12 found that `provider_receipts::Completion` retains
only input/output counters, cost provenance, identity hashes, latency, and
attempts. `DispatchObservation.finish` extracts Anthropic `input_tokens` directly,
without cache reads/writes, TTL split, or actual tier. Consequently, those existing
receipts cannot acquire complete direct-provider tariff evidence after restart.
Sources: `crates/agentc-optimizer/src/provider_receipts.rs` and
`python/agentc/_provider_receipts.py`. This research makes no runtime edits.

## Source observation fingerprints

The following unauthenticated official Markdown page bodies were independently
fetched at approximately 2026-10-12T17:04Z. Hashes identify the observed material;
they do not certify that a tariff applied to any execution. Claude HTML pages
were read through the web tool; no authenticated endpoints were queried.

| Official source | SHA-256 |
| --- | --- |
| [OpenAI pricing](https://developers.openai.com/api/docs/pricing.md) | `e0767b5009dcc929a2f9eeb9ce6096a57ff12e5d8c011de9d69ff145aef2fc28` |
| [GPT-4o](https://developers.openai.com/api/docs/models/gpt-4o.md) | `f3c708a3ba44098a5086c550dabed6f0eeb262d51c97261a97b5315f93dc08c9` |
| [GPT-4o-mini](https://developers.openai.com/api/docs/models/gpt-4o-mini.md) | `e860cd14c7aa256ab6151f302fc0c7d0597a5a25fed3af09e8de64e3884daa3e` |
| [Priority/Fast guide](https://developers.openai.com/api/docs/guides/priority-processing.md) | `254e31b60552520d7f8567ca3f0de086f3a64f8d53c5be3a04292b55bbd2d720` |
| [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching.md) | `74dc45350d5de0e7eef80b0b09d67b733a73170275f1bacb0760e066a4254480` |
