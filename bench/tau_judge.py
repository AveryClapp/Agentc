"""Complete NL assertion coverage before accepting pinned Tau judge rewards.

The upstream parser defaults a missing results list to [], then all([]) awards
full reward. This guard validates the raw judge JSON before Pydantic coercion,
and verifies the official result list afterward. It does not rewrite prompts,
assertions, verdicts or rewards. Upstream files remain unchanged; this is an
explicit grading adapter, not unmodified leaderboard grading.
"""

from __future__ import annotations

from collections import Counter
from contextvars import ContextVar
from functools import wraps
from typing import Any

from bench.campaign_accounting import AccountingError, json_object

POLICY = "tau-nl-complete-coverage-v1"


class JudgeCoverageError(BaseException):
    """Invalid grading must not trigger an automatic judge or task retry."""


def validate_assertion_response(expected: list[str], body: Any) -> None:
    """Match exact assertion multiplicity; accept both true and false verdicts."""
    if not isinstance(expected, list) or any(
        not isinstance(value, str) for value in expected
    ):
        raise JudgeCoverageError("invalid required assertion inventory")
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise JudgeCoverageError("judge omitted its assertion results")
    results = body["results"]
    observed = []
    for result in results:
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("expectedOutcome"), str)
            or type(result.get("metExpectation")) is not bool
            or not isinstance(result.get("reasoning"), str)
        ):
            raise JudgeCoverageError("judge returned an invalid assertion verdict")
        observed.append(result["expectedOutcome"])
    if Counter(observed) != Counter(expected):
        raise JudgeCoverageError(
            "judge assertion coverage is missing, duplicated or mismatched"
        )


class TauJudgeGuard:
    """Scoped installation on the pinned evaluator's actual class/generate alias."""

    def __init__(self, module: Any):
        self.module = module
        self.evaluator = module.NLAssertionsEvaluator
        self._expected: ContextVar[list[str] | None] = ContextVar(
            "agentc_tau_judge_assertions", default=None
        )
        self._installed = False
        self._invalid = False

    def assert_valid(self) -> None:
        if self._invalid:
            raise JudgeCoverageError("worker has an invalid NL judge outcome")

    def __enter__(self) -> TauJudgeGuard:
        if self._installed:
            raise JudgeCoverageError("NL judge guard is already installed")
        original_generate = self.module.generate
        original_method = self.evaluator.__dict__["evaluate_nl_assertions"]
        if not isinstance(original_method, classmethod):
            raise JudgeCoverageError("NL judge evaluator differs from the pinned seam")
        self._original_generate = original_generate
        self._original_method = original_method

        @wraps(original_generate)
        def generate(*args, **kwargs):
            self.assert_valid()
            expected = self._expected.get()
            if expected is None:
                self._invalid = True
                raise JudgeCoverageError(
                    "judge call is outside its required assertion scope"
                )
            response = original_generate(*args, **kwargs)
            try:
                content = response.content
                if not isinstance(content, str):
                    raise JudgeCoverageError("judge returned no text verdict")
                try:
                    body = json_object(content.encode("utf-8"))
                except (AccountingError, UnicodeError):
                    raise JudgeCoverageError(
                        "judge returned invalid JSON; contents suppressed"
                    ) from None
                validate_assertion_response(expected, body)
            except JudgeCoverageError:
                self._invalid = True
                raise
            return response

        @wraps(original_method.__func__)
        def evaluate(cls, trajectory, nl_assertions):
            self.assert_valid()
            if not isinstance(nl_assertions, list):
                self._invalid = True
                raise JudgeCoverageError("invalid required assertion inventory")
            expected = list(nl_assertions)
            token = self._expected.set(expected)
            try:
                results = original_method.__func__(cls, trajectory, expected)
                projected = {
                    "results": [
                        {
                            "expectedOutcome": result.nl_assertion,
                            "metExpectation": result.met,
                            "reasoning": result.justification,
                        }
                        for result in results
                    ]
                }
                validate_assertion_response(expected, projected)
                return results
            except JudgeCoverageError:
                self._invalid = True
                raise
            finally:
                self._expected.reset(token)

        self._generate = generate
        self._method = classmethod(evaluate)
        self.module.generate = self._generate
        try:
            self.evaluator.evaluate_nl_assertions = self._method
        except BaseException:
            self.module.generate = original_generate
            raise
        self._installed = True
        return self

    def __exit__(self, *exc) -> None:
        if self._installed:
            self.module.generate = self._original_generate
            self.evaluator.evaluate_nl_assertions = self._original_method
            self._installed = False
