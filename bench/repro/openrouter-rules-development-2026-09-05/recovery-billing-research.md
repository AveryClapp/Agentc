# OpenRouter HTTP 429 recovery: billing evidence

Researched 2026-09-05 UTC for bd-323l.6.13.8.4.3.1. Official documentation only;
no credentials, inference requests, account queries, or live-ledger changes.

## Conclusion

A genuine outer HTTP 429 supports an expectation of no inference charge, but
the available evidence does not establish this particular request's final cost.
Retain its full **USD 0.127608 possible-failure allowance** against both the
USD 1 development-stage ceiling and USD 50 campaign ceiling. An explicitly
audited retry may resolve this one request's concurrency block without releasing
that financial allowance or fabricating a zero-cost provider result. This is a
conservative project decision, not an OpenRouter settlement guarantee.

## Documented boundaries

- **HTTP status is not an embedded error code.** OpenRouter commits HTTP 200
  when the provider accepts a request, before the first token. Later failures
  can appear inside a non-streaming HTTP 200 body, including an error-only body
  with a generation ID. Thus a witnessed *outer* HTTP 429 is materially stronger
  pre-generation evidence than seeing `error.code=429` alone. The docs support
  retrying rate limits after `Retry-After` when supplied; a fixed delay cannot
  establish compliance with a header that was not retained.
  [Errors and debugging](https://openrouter.ai/docs/api_reference/errors-and-debugging)
- **Error billing has a waiver, not a universal empty-response rule.** The
  June 14, 2026 support article says error-finish/error-output responses are
  waived regardless of processed tokens; zero-completion responses also qualify
  with blank/null/none finish reasons. Upstream prompt charges are absorbed.
  Reasoning/output tokens and separate search, parsing, or BYOK fees have
  exceptions. The general error guide still warns that some no-content requests
  incur prompt-processing charges. The more specific waiver criteria therefore
  matter; absent response content is not itself proof of eligibility.
  [Zero Completion Insurance](https://openrouter.zendesk.com/hc/en-us/articles/51693138951451-Was-I-charged-for-a-failed-errored-or-empty-response-Zero-Completion-Insurance),
  [no-content caveat](https://openrouter.ai/docs/api_reference/errors-and-debugging#when-no-content-is-generated)
- **Aggregate equality is corroboration, not a settlement watermark.** Credits
  expose `total_usage`; current-key metadata exposes usage aggregates. The FAQ
  describes live/real-time reporting, but these reviewed references specify no
  maximum billing-posting delay, finalized-through timestamp, or guarantee that
  two equal readings 60 seconds apart, 120 seconds after reservation, establish
  final request-level settlement. Those intervals are project checks only.
  [Credits API](https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits),
  [current-key API](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key),
  [FAQ](https://openrouter.ai/docs/faq)
- **Exact lookup needs provider identity.** `GET /api/v1/generation` requires
  the generation ID and returns cost/usage metadata. The support article also
  directs users to the Activity request detail or support. No reviewed public
  endpoint maps a local reservation ID or payload hash to that exact record.
  Without the captured generation ID, an account total is not a substitute;
  Activity/support attribution remains a possible later reconciliation route.
  [Generation API](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation),
  [request billing verification](https://openrouter.zendesk.com/hc/en-us/articles/51693138951451-Was-I-charged-for-a-failed-errored-or-empty-response-Zero-Completion-Insurance)

## Application to the witnessed failure

Operator-reported facts, not independently account-verified here: 60 completed
development calls cost USD 0.305325; the sole witnessed HTTP 429 reservation is
`rules-live-dev-v1-d3e81e86aafc129930be-d238bb46ad07bbad4318b7ba`.
No response body or generation ID was retained. Later aggregate usage of
USD 9.54065647 equals all 3,601 completed ledger charges, while the reservation's
earlier account reading was USD 9.49922947. Repeated later equality supports
“no additional charge observed,” not “provider-confirmed zero charge.”

The chosen receipt should bind this exact reservation, request fingerprint,
HTTP-status evidence, and timestamped snapshots; preserve every original ledger
record; and authorize only one separately reserved, unchanged-request retry.
Keep the old allowance in both ceilings alongside completed charges, every
other outstanding allowance, and the new retry reserve. Do not synthesize an
answer, usage, generation ID, or billed cost for the failed attempt. Keep
fallback disabled and stop on another failure. This documents bounded recovery;
it does not make an account-level snapshot a request-level billing receipt.
