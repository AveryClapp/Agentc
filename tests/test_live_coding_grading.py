from bench.live_coding_grading import complete_reference_tests, reference_verdicts
from bench.live_coding_grading import captured_patch, PilotError
import pytest


def test_empty_or_partial_parse_cannot_pass_negative_controls():
    assert not complete_reference_tests({}, ["regression"], ["existing"], False)
    assert not complete_reference_tests({}, ["regression"], ["existing"], True)
    assert not complete_reference_tests(
        {"regression": "FAILED"}, ["regression"], ["existing"], True
    )
    assert not complete_reference_tests({"existing": "PASSED"}, [], ["existing"], True)


def test_complete_reference_coverage_is_independent_of_outcome():
    for outcome in ("FAILED", "PASSED"):
        assert complete_reference_tests(
            {"regression": outcome, "existing": "PASSED"},
            ["regression"],
            ["existing"],
            True,
        )


def test_skipped_and_unknown_cannot_pass_even_if_official_report_says_resolved():
    for status in ("SKIPPED", "UNKNOWN"):
        verdict = reference_verdicts(
            {"f": status, "p": "PASSED"}, ["f"], ["p"], True, True
        )
        assert verdict == {
            "complete": False,
            "resolved": False,
            "negative_control_valid": False,
        }


def test_broken_environment_is_not_a_valid_negative_control():
    verdict = reference_verdicts(
        {"f": "ERROR", "p": "ERROR"}, ["f"], ["p"], True, False
    )
    assert verdict["complete"]
    assert not verdict["resolved"]
    assert not verdict["negative_control_valid"]


def test_conclusive_positive_and_negative_controls():
    negative = reference_verdicts(
        {"f": "FAILED", "p": "PASSED"}, ["f"], ["p"], True, False
    )
    assert negative == {
        "complete": True,
        "resolved": False,
        "negative_control_valid": True,
    }
    positive = reference_verdicts(
        {"f": "PASSED", "p": "XFAIL"}, ["f"], ["p"], True, True
    )
    assert positive == {
        "complete": True,
        "resolved": True,
        "negative_control_valid": False,
    }


def test_restore_newline_only_from_exact_captured_submission():
    patch = "diff --git a/x b/x\n context"
    events = [
        {
            "event": "tool",
            "result": {
                "returncode": 0,
                "output": "MICRO_SWE_AGENT_FINAL_OUTPUT\n" + patch + "\n",
            },
        }
    ]
    assert captured_patch(patch, events) == patch + "\n"
    events[0]["result"]["output"] += "another line\n"
    with pytest.raises(PilotError):
        captured_patch(patch, events)
    with pytest.raises(PilotError):
        captured_patch(patch, [])
