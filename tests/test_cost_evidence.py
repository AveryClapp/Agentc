"""Cost labels compose conservatively independently of validator verdicts."""

from types import SimpleNamespace

import pytest

from agentc._output_validation import _add_usage


@pytest.mark.parametrize("old_source", [None, "unknown", "invalid", {}, False])
def test_missing_or_invalid_auxiliary_source_is_never_upgraded_by_a_reported_cost(
    old_source,
):
    plan = SimpleNamespace(
        additional_inference={
            "input_tokens": 2,
            "output_tokens": 1,
            "cost_usd": 0.01,
            **({"cost_source": old_source} if old_source is not None else {}),
        }
    )
    _add_usage(
        plan,
        {
            "input_tokens": 3,
            "output_tokens": 2,
            "cost_usd": 0.02,
            "cost_source": "provider_reported",
        },
    )
    assert plan.additional_inference == {
        "input_tokens": 5,
        "output_tokens": 3,
        "cost_usd": 0.03,
        "cost_source": "unknown",
    }


@pytest.mark.parametrize(
    "source", ["provider_reported", "estimated", "unknown", "no_inference"]
)
def test_first_auxiliary_cost_preserves_its_declared_source(source):
    plan = SimpleNamespace(additional_inference=None)
    _add_usage(
        plan,
        {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0, "cost_source": source},
    )
    assert plan.additional_inference["cost_source"] == source


def test_estimated_auxiliary_cost_remains_estimated_after_additional_reported_usage():
    plan = SimpleNamespace(additional_inference=None)
    _add_usage(
        plan,
        {
            "input_tokens": 2,
            "output_tokens": 1,
            "cost_usd": 0.01,
            "cost_source": "estimated",
        },
    )
    _add_usage(
        plan,
        {
            "input_tokens": 3,
            "output_tokens": 2,
            "cost_usd": 0.02,
            "cost_source": "provider_reported",
        },
    )
    assert plan.additional_inference["cost_source"] == "estimated"
