"""Prompt-cache cost model for context-removing changes (protocol Sections 5.3, 10).

Prices
------
Providers bill a cached-prefix read at ``r * p`` and a cache write at
``w * p`` per input token, where ``p`` is the base input price. Ratios in the
repo (``crates/agentc-optimizer/src/provider_tariff.rs:26-31``, observed
2026-10-12, nanodollars per token):

- Anthropic Sonnet/Haiku 4.5 ``standard``: ``[input, cache_read, write_5m,
  write_1h, output] = [3000, 300, 3750, 6000, 15000]`` (Haiku scaled by 1/3),
  i.e. ``r = 0.1``, ``w = 1.25`` (5-minute TTL) or ``2.0`` (1-hour TTL).
  ``data/pricing.json`` agrees for older Claude models (1.25x write,
  0.1x read).
- OpenAI ``gpt-4o`` / ``gpt-4o-mini`` default tier: ``[input, cached,
  output] = [2500, 1250, 10000]``, i.e. ``r = 0.5``; OpenAI caches
  automatically and has no write premium, so ``w = 1``.

These are presets only; every function takes the ratios as parameters.

Usage conventions differ: Anthropic's ``input_tokens`` excludes cache reads
and cache writes; OpenAI's ``prompt_tokens`` includes ``cached_tokens``. The
billing function takes the convention explicitly.

Break-even derivation
---------------------
Model one agent whose request at each step is the previous request plus ``g``
appended tokens (append-mostly context), served from a prefix cache that
holds the whole previous request. Prices are in units of ``p``; output
tokens are unaffected by the change and cancel.

*Baseline step* with context length ``L`` (of which ``L - g`` was the
previous request, hence cached)::

    B(L) = r (L - g) + w g

*Change at step s*: remove ``D > 0`` tokens starting at position ``k``
(tokens ``[0, k)`` are untouched), so the edited request has ``L - D``
tokens. Only the untouched part of the old cached prefix can be read,
``c = min(k, L - g)``, and the rest must be (re)written (``D = 0`` leaves the
request unchanged, ``c = L - g``)::

    C_s = r c + w (L - D - c)

*Each later step* (``m`` of them) appends ``g`` tokens to both contexts; both
read their whole previous request from cache and write ``g``. The changed
context is ``D`` tokens shorter, so each later step is cheaper by ``r D``.

Total cost difference, changed minus baseline::

    Delta(m) = [r c + w (L - D - c)] - [r (L - g) + w g] - r D m

The first bracket difference is the one-time *cache-invalidation penalty*
``P = w (L - D - c - g) - r (L - g - c)``; the saving accrues at ``r D`` per
remaining step. Removal lowers cost iff ``Delta(m) < 0``, i.e.::

    m > m* = P / (r D)

If ``P <= 0`` (edit falls inside the newly appended tokens, or no caching:
``r = w = 1`` gives ``P = -D``) removal pays immediately. Scale-free form with
``rho = D/L``, ``kappa = k/L``, ``gamma = g/L``, ``kappa' = min(kappa, 1 - gamma)``::

    m* = [w (1 - rho - kappa' - gamma) - r (1 - gamma - kappa')] / (r rho)

Example: Anthropic 5-minute cache (``r = 0.1``, ``w = 1.25``), a clearing
edit that removes 30% of a context at relative position 0.2 with 2% growth
per step needs ``m* = (1.25*0.48 - 0.1*0.78)/(0.1*0.3) = 17.4`` more steps
before it saves money. A repeated edit (clearing that fires again later)
pays the penalty again at each firing; apply the model per firing. Cache
expiry (TTL) is not modelled: every step is assumed to hit within the TTL.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from agentc.change_safety.records import TrajectoryRecord


@dataclass(frozen=True)
class CachePrices:
    """Per-token prices (any unit) for one model."""

    input: float
    cache_read: float
    cache_write: float
    output: float

    @property
    def read_ratio(self) -> float:
        return self.cache_read / self.input

    @property
    def write_ratio(self) -> float:
        return self.cache_write / self.input

    @classmethod
    def from_ratios(cls, input_price: float, output_price: float, *, read: float, write: float) -> CachePrices:
        return cls(input_price, read * input_price, write * input_price, output_price)


#: Nanodollars per token from provider_tariff.rs (2026-10-12 snapshot).
ANTHROPIC_SONNET_4_5_5M = CachePrices(3000.0, 300.0, 3750.0, 15000.0)
ANTHROPIC_SONNET_4_5_1H = CachePrices(3000.0, 300.0, 6000.0, 15000.0)
ANTHROPIC_HAIKU_4_5_5M = CachePrices(1000.0, 100.0, 1250.0, 5000.0)
OPENAI_GPT_4O = CachePrices(2500.0, 1250.0, 2500.0, 10000.0)
OPENAI_GPT_4O_MINI = CachePrices(150.0, 75.0, 150.0, 600.0)


def billed_cost(
    prices: CachePrices,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    convention: str = "anthropic",
) -> float:
    """Billed cost of one call (or summed usage) from provider usage fields.

    ``convention="anthropic"``: ``input_tokens`` is uncached, non-write input.
    ``convention="openai"``: ``input_tokens`` includes ``cached_input_tokens``
    (and there are no cache writes).
    """
    if min(input_tokens, output_tokens, cached_input_tokens, cache_creation_input_tokens) < 0:
        raise ValueError("token counts must be non-negative")
    if convention == "anthropic":
        uncached = input_tokens
    elif convention == "openai":
        if cached_input_tokens > input_tokens:
            raise ValueError("openai cached tokens exceed prompt tokens")
        uncached = input_tokens - cached_input_tokens
    else:
        raise ValueError(f"unknown usage convention {convention!r}")
    return (
        uncached * prices.input
        + cached_input_tokens * prices.cache_read
        + cache_creation_input_tokens * prices.cache_write
        + output_tokens * prices.output
    )


def trajectory_billed_cost(
    record: TrajectoryRecord, prices: CachePrices, *, convention: str = "anthropic"
) -> float | None:
    """Billed cost of a trajectory from its summed usage; ``None`` if usage is missing."""
    if record.input_tokens is None or record.output_tokens is None:
        return None
    return billed_cost(
        prices,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        cached_input_tokens=record.cached_input_tokens or 0,
        cache_creation_input_tokens=record.cache_creation_input_tokens or 0,
        convention=convention,
    )


def removal_penalty(*, context: float, growth: float, edit_position: float, removed: float, read: float, write: float) -> float:
    """One-time cache-invalidation penalty ``P`` (units of base input price)."""
    _check(context, growth, edit_position, removed, read, write)
    # With nothing removed the request is unchanged and its whole old prefix hits.
    cached = min(edit_position, context - growth) if removed > 0 else context - growth
    return write * (context - removed - cached - growth) - read * (context - growth - cached)


def removal_cost_delta(
    *, context: float, growth: float, edit_position: float, removed: float, remaining_steps: float, read: float, write: float
) -> float:
    """``Delta(m)``: changed minus baseline input cost over the edit step and ``m`` later steps."""
    penalty = removal_penalty(
        context=context, growth=growth, edit_position=edit_position, removed=removed, read=read, write=write
    )
    return penalty - read * removed * remaining_steps


def break_even_steps(*, context: float, growth: float, edit_position: float, removed: float, read: float, write: float) -> float:
    """``m*``: removal lowers cost iff more than ``m*`` steps follow the edit.

    Returns ``0.0`` when it pays immediately (penalty <= 0) and ``inf`` when
    it never pays (no removal or free cache reads with a positive penalty).
    """
    penalty = removal_penalty(
        context=context, growth=growth, edit_position=edit_position, removed=removed, read=read, write=write
    )
    if penalty <= 0:
        return 0.0
    saving = read * removed
    return penalty / saving if saving > 0 else math.inf


def break_even_steps_relative(*, removal_fraction: float, edit_fraction: float, growth_fraction: float, read: float, write: float) -> float:
    """Scale-free ``m*`` with ``rho = D/L``, ``kappa = k/L``, ``gamma = g/L``."""
    return break_even_steps(
        context=1.0,
        growth=growth_fraction,
        edit_position=edit_fraction,
        removed=removal_fraction,
        read=read,
        write=write,
    )


def _check(context: float, growth: float, edit_position: float, removed: float, read: float, write: float) -> None:
    if not (0 <= growth <= context and 0 <= edit_position and 0 <= removed <= context - edit_position):
        raise ValueError("need 0 <= growth <= context and 0 <= removed <= context - edit_position")
    if read < 0 or write < 0:
        raise ValueError("price ratios must be non-negative")


def simulate_prefix_cache_cost(requests: Sequence[Sequence[int]], *, read: float, write: float) -> float:
    """Brute-force input cost of a request sequence under an exact prefix cache.

    The cache holds the full previous request; request ``i`` reads its longest
    common prefix with request ``i - 1`` at ``read`` and writes the rest at
    ``write`` (the first request writes everything). Units of base input price.
    """
    total = 0.0
    previous: Sequence[int] = ()
    for request in requests:
        common = 0
        for a, b in zip(previous, request):
            if a != b:
                break
            common += 1
        total += read * common + write * (len(request) - common)
        previous = request
    return total


__all__ = [
    "ANTHROPIC_HAIKU_4_5_5M",
    "ANTHROPIC_SONNET_4_5_1H",
    "ANTHROPIC_SONNET_4_5_5M",
    "OPENAI_GPT_4O",
    "OPENAI_GPT_4O_MINI",
    "CachePrices",
    "billed_cost",
    "break_even_steps",
    "break_even_steps_relative",
    "removal_cost_delta",
    "removal_penalty",
    "simulate_prefix_cache_cost",
    "trajectory_billed_cost",
]
