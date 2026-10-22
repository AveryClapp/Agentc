"""Strict judge coverage, with no model or corpus calls."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from bench.tau_judge import (
    JudgeCoverageError,
    TauJudgeGuard,
    validate_assertion_response,
)


def verdict(assertion, met=True):
    return {"expectedOutcome": assertion, "metExpectation": met, "reasoning": "fixture"}


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"results": None},
        {"results": []},
        {"results": [verdict("a"), verdict("a")]},
        {"results": [verdict("a"), verdict("other")]},
        {"results": [verdict("a")]},
        {"results": [verdict("a"), verdict("b"), verdict("other")]},
        {"results": [dict(verdict("a"), metExpectation="true"), verdict("b")]},
        {"results": [dict(verdict("a"), metExpectation=1), verdict("b")]},
        {"results": [dict(verdict("a"), reasoning=None), verdict("b")]},
        {"results": [dict(verdict("a"), expectedOutcome=" a"), verdict("b")]},
    ],
)
def test_missing_duplicate_mismatched_and_coerced_results_fail_closed(body):
    with pytest.raises(JudgeCoverageError):
        validate_assertion_response(["a", "b"], body)


@pytest.mark.parametrize(
    "results",
    [[verdict("a"), verdict("b", False)], [verdict("b", False), verdict("a")]],
)
def test_complete_false_verdicts_and_arbitrary_order_are_preserved(results):
    body = {"results": results}
    validate_assertion_response(["a", "b"], body)
    assert body["results"] == results


def test_expected_duplicate_occurrences_require_exact_multiplicity():
    validate_assertion_response(
        ["a", "a"], {"results": [verdict("a"), verdict("a", False)]}
    )
    with pytest.raises(JudgeCoverageError):
        validate_assertion_response(["a", "a"], {"results": [verdict("a")]})


def module_fixture(body):
    module = SimpleNamespace()
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        value = body(kwargs) if callable(body) else body
        return SimpleNamespace(
            content=json.dumps(value) if not isinstance(value, str) else value
        )

    class Evaluator:
        @classmethod
        def evaluate_nl_assertions(cls, trajectory, nl_assertions):
            response = module.generate(assertions=nl_assertions)
            return [
                SimpleNamespace(
                    nl_assertion=row["expectedOutcome"],
                    met=row["metExpectation"],
                    justification=row["reasoning"],
                )
                for row in json.loads(response.content).get("results", [])
            ]

    module.generate, module.NLAssertionsEvaluator = generate, Evaluator
    return module, calls


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"results": []},
        {"results": [verdict("other")]},
        '{"results":NaN}',
        '{"results":[],"results":[]}',
    ],
)
def test_guard_rejects_raw_json_before_official_parser_and_stops_later_judges(body):
    module, calls = module_fixture(body)
    generate = module.generate
    method = module.NLAssertionsEvaluator.__dict__["evaluate_nl_assertions"]
    with TauJudgeGuard(module) as guard:
        with pytest.raises(JudgeCoverageError):
            module.NLAssertionsEvaluator.evaluate_nl_assertions([], ["required"])
        with pytest.raises(JudgeCoverageError):
            guard.assert_valid()
        with pytest.raises(JudgeCoverageError):
            module.NLAssertionsEvaluator.evaluate_nl_assertions([], ["required"])
    assert len(calls) == 1
    assert module.generate is generate
    assert module.NLAssertionsEvaluator.__dict__["evaluate_nl_assertions"] is method


def test_guard_preserves_official_result_objects_and_restores_exact_aliases():
    module, calls = module_fixture({"results": [verdict("required", False)]})
    original = module.generate
    with TauJudgeGuard(module) as guard:
        results = module.NLAssertionsEvaluator.evaluate_nl_assertions([], ["required"])
        assert results[0].nl_assertion == "required"
        assert results[0].met is False
        guard.assert_valid()
    assert module.generate is original and len(calls) == 1


def test_parallel_judges_use_distinct_assertion_scopes():
    module, calls = module_fixture(
        lambda kwargs: {"results": [verdict(value) for value in kwargs["assertions"]]}
    )
    with TauJudgeGuard(module) as guard, ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [
            pool.submit(
                module.NLAssertionsEvaluator.evaluate_nl_assertions, [], [value]
            )
            for value in ("a", "b")
        ]
        assert [job.result()[0].nl_assertion for job in jobs] == ["a", "b"]
        guard.assert_valid()
    assert len(calls) == 2


def test_generate_without_expected_assertions_never_calls_a_model():
    module, calls = module_fixture({"results": []})
    with TauJudgeGuard(module):
        with pytest.raises(JudgeCoverageError):
            module.generate()
    assert calls == []
