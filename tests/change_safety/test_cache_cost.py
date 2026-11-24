from __future__ import annotations

import itertools
import math

import pytest

from bench.change_safety.cache_cost import (
    ANTHROPIC_SONNET_4_5_5M,
    OPENAI_GPT_4O,
    billed_cost,
    break_even_steps,
    break_even_steps_relative,
    removal_cost_delta,
    simulate_prefix_cache_cost,
    trajectory_billed_cost,
)
from tests.change_safety.conftest import make_trajectory


def _brute_force_delta(L: int, g: int, k: int, D: int, m: int, r: float, w: float) -> float:
    """Changed minus baseline cost from explicit token sequences."""
    previous = list(range(L - g))  # identical cached request before the edit step
    base = [previous, list(range(L))]
    changed_step = list(range(k)) + list(range(k + D, L))
    changed = [previous, changed_step]
    next_id = L
    for _ in range(m):
        new = list(range(next_id, next_id + g))
        next_id += g
        base.append(base[-1] + new)
        changed.append(changed[-1] + new)
    return simulate_prefix_cache_cost(changed, read=r, write=w) - simulate_prefix_cache_cost(base, read=r, write=w)


@pytest.mark.parametrize("r,w", [(0.1, 1.25), (0.1, 2.0), (0.5, 1.0), (1.0, 1.0)])
def test_closed_form_matches_brute_force(r: float, w: float) -> None:
    L = 40
    for g, k, D, m in itertools.product((1, 4, 10), (0, 7, 33, 38), (0, 2, 5), (0, 1, 6, 30)):
        if D > L - k:
            continue
        expected = _brute_force_delta(L, g, k, D, m, r, w)
        got = removal_cost_delta(context=L, growth=g, edit_position=k, removed=D, remaining_steps=m, read=r, write=w)
        assert got == pytest.approx(expected, abs=1e-9), (g, k, D, m)


def test_break_even_separates_loss_from_saving() -> None:
    r, w = 0.1, 1.25
    L, g, k, D = 1000, 20, 200, 300
    m_star = break_even_steps(context=L, growth=g, edit_position=k, removed=D, read=r, write=w)
    assert m_star == pytest.approx(17.4)
    below, above = math.floor(m_star), math.floor(m_star) + 1
    assert _brute_force_delta(L, g, k, D, below, r, w) > 0
    assert _brute_force_delta(L, g, k, D, above, r, w) < 0
    assert break_even_steps_relative(
        removal_fraction=0.3, edit_fraction=0.2, growth_fraction=0.02, read=r, write=w
    ) == pytest.approx(m_star)


def test_break_even_edge_cases() -> None:
    # Without caching, removing tokens pays immediately.
    assert break_even_steps(context=100, growth=5, edit_position=0, removed=10, read=1.0, write=1.0) == 0.0
    # Removing only newly appended tokens never invalidates the cache.
    assert break_even_steps(context=100, growth=10, edit_position=95, removed=5, read=0.1, write=1.25) == 0.0
    # No removal: the request is unchanged and costs nothing extra.
    assert break_even_steps(context=100, growth=10, edit_position=0, removed=0, read=0.1, write=1.25) == 0.0
    # Free cache reads: the rewrite penalty is never recovered.
    assert break_even_steps(context=100, growth=10, edit_position=0, removed=10, read=0.0, write=1.25) == math.inf
    with pytest.raises(ValueError):
        break_even_steps(context=100, growth=10, edit_position=90, removed=20, read=0.1, write=1.25)


def test_billed_cost_conventions() -> None:
    anthropic = billed_cost(
        ANTHROPIC_SONNET_4_5_5M,
        input_tokens=100,
        output_tokens=10,
        cached_input_tokens=1000,
        cache_creation_input_tokens=200,
    )
    assert anthropic == pytest.approx(100 * 3000 + 1000 * 300 + 200 * 3750 + 10 * 15000)
    openai = billed_cost(OPENAI_GPT_4O, input_tokens=1100, output_tokens=10, cached_input_tokens=1000, convention="openai")
    assert openai == pytest.approx(100 * 2500 + 1000 * 1250 + 10 * 10000)
    assert ANTHROPIC_SONNET_4_5_5M.read_ratio == pytest.approx(0.1)
    assert ANTHROPIC_SONNET_4_5_5M.write_ratio == pytest.approx(1.25)
    with pytest.raises(ValueError):
        billed_cost(OPENAI_GPT_4O, input_tokens=5, output_tokens=0, cached_input_tokens=6, convention="openai")
    record = make_trajectory("w", "m", "t", 0, "baseline", 1.0, 3)
    assert trajectory_billed_cost(record, ANTHROPIC_SONNET_4_5_5M) == pytest.approx(1000 * 3000 + 100 * 15000)
