# OpenRouter prompt-length pricing threshold

Primary-source check, 2026-09-05. Review task `bd-323l.6.13.8.6.20.6`;
reporting defect `bd-yxqb`. No inference request or credential access.

OpenRouter explicitly defines `pricing.overrides[].min_prompt_tokens` as a
**strictly greater-than** condition on total prompt tokens. All conditions must
match; matching entries are applied in array order, with later entries winning
per price key. Prices are expressed per token/request/unit. These are documented
semantics, not an inferred inclusive-boundary convention.
[Official Models API documentation, Pricing Overrides](https://openrouter.ai/docs/guides/overview/models#pricing-overrides).

The frozen, unacquired typed pilot manifest `4b19a54de565efea193de90cee4708ac963efa42f7ffa68bef26553248d4c64c`
records Sonnet base input/output rates of 3/15 USD per million, with a
`min_prompt_tokens: 200000` override of 6/22.5. It is a captured endpoint
observation, not a guarantee of future rates.
[Public Sonnet endpoint metadata](https://openrouter.ai/api/v1/models/anthropic/claude-sonnet-4.5/endpoints).

Consequently, the frozen-rate uncached counterfactual uses base rates at exactly
200,000 prompt tokens and the override at 200,001. With zero output tokens those
examples cost 0.600000 and 1.200006 USD respectively (arithmetic from the cited
rates and threshold rule). Apply the selected rates to all relevant tokens, not
only tokens beyond the threshold. Preserve array order; do not sort overrides.

For this narrowly validated pilot, temporal and unknown pricing conditions are
rejected before dispatch. The nominal uncached figure is a frozen-rate
counterfactual: provider-reported `usage.cost`, retained failure allowances, and
the original shared campaign ledger remain the billing authority. This research
does not authorize native routing, change the 54-call feasibility gate, or turn
the nominal figure into a guaranteed invoice reconstruction.
