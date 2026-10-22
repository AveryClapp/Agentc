"""Prevent misleading environment admission from partial or broken controls."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bench import requests_fixture_preflight as preflight
from bench._legacy_pytest_reporter import pytest_runtest_logreport
from bench.requests_fixture_preflight import FixtureError, reference_verdicts


def test_owned_grading_requires_admission_and_an_active_session(tmp_path):
    session = preflight.RequestsGradingSession(
        tmp_path / "data", tmp_path / "out", tmp_path / "wheels"
    )
    for active, admitted in [(False, False), (True, False), (False, True)]:
        session._active, session.admitted = active, admitted
        with pytest.raises(FixtureError, match="admission"):
            session.grade(tmp_path / "submission")


def test_owned_grader_cannot_regrade_a_receipt(monkeypatch, tmp_path):
    session = preflight.RequestsGradingSession(
        tmp_path / "data", tmp_path / "out", tmp_path / "wheels"
    )
    session._active, session.admitted, session._grading = True, True, {}
    record = {
        "model_patch": "patch",
        "upstream_status": "Submitted",
        "model_name_or_path": "control",
        "serialization_changed": False,
        "patch_sha256": "a" * 64,
    }
    monkeypatch.setattr(
        preflight, "_load_candidates", lambda paths: [(record, "b" * 64)]
    )
    physical_grades = []
    monkeypatch.setattr(
        preflight,
        "_grade_patch",
        lambda **kwargs: physical_grades.append(kwargs) or {"status": "unit-control"},
    )
    session.grade(tmp_path / "submission")
    with pytest.raises(FixtureError, match="duplicate"):
        session.grade(tmp_path / "submission")
    assert len(physical_grades) == session.submitted_count == 1


@pytest.mark.parametrize("status", [None, "SKIPPED", "UNKNOWN", "ERROR", "FAILED"])
def test_preservation_failures_or_missing_results_cannot_admit_fixture(
    status: str | None,
) -> None:
    observed = {"regression": "FAILED"}
    if status is not None:
        observed["preservation"] = status
    verdict = reference_verdicts(
        observed, ["regression"], ["preservation"], True, False
    )
    assert not verdict["negative_control_valid"]
    assert not verdict["resolved"]


@pytest.mark.parametrize("status", [None, "SKIPPED", "UNKNOWN"])
def test_official_resolution_cannot_hide_incomplete_reference_coverage(
    status: str | None,
) -> None:
    observed = {"preservation": "PASSED"}
    if status is not None:
        observed["regression"] = status
    verdict = reference_verdicts(observed, ["regression"], ["preservation"], True, True)
    assert not verdict["complete"]
    assert not verdict["resolved"]
    assert not verdict["negative_control_valid"]


def test_controls_require_the_expected_regression_and_complete_preservation() -> None:
    negative = reference_verdicts(
        {"regression": "FAILED", "preservation": "PASSED"},
        ["regression"],
        ["preservation"],
        True,
        False,
    )
    assert negative == {
        "complete": True,
        "resolved": False,
        "negative_control_valid": True,
    }
    positive = reference_verdicts(
        {"regression": "PASSED", "preservation": "PASSED"},
        ["regression"],
        ["preservation"],
        True,
        True,
    )
    assert positive == {
        "complete": True,
        "resolved": True,
        "negative_control_valid": False,
    }
    # A solved unchanged checkout does not establish a regression control.
    assert not reference_verdicts(
        {"regression": "PASSED", "preservation": "PASSED"},
        ["regression"],
        ["preservation"],
        True,
        False,
    )["negative_control_valid"]


@pytest.mark.parametrize(
    "f2p,p2p,found",
    [([], ["test"], True), (["test"], [], True), (["test"], ["test"], False)],
)
def test_empty_reference_sets_or_unparsed_output_cannot_establish_admission(
    f2p: list[str], p2p: list[str], found: bool
) -> None:
    result = reference_verdicts({"test": "PASSED"}, f2p, p2p, found, True)
    assert not result["complete"]
    assert not result["resolved"]


@pytest.mark.parametrize("mutation", ["missing", "changed", "extra"])
def test_dependency_drift_is_rejected_before_container_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    wheel = tmp_path / "pytest.whl"
    wheel.write_bytes(b"locked dependency")
    monkeypatch.setattr(preflight, "PYTEST_WHEELS", {wheel.name: preflight._sha(wheel)})
    assert preflight._verified_wheels(tmp_path) == preflight.PYTEST_WHEELS
    if mutation == "missing":
        wheel.unlink()
    elif mutation == "changed":
        wheel.write_bytes(b"different code")
    else:
        (tmp_path / "unexpected.whl").write_bytes(b"other dependency")
    with pytest.raises(FixtureError, match="fixture lock"):
        preflight._verified_wheels(tmp_path)


@pytest.mark.parametrize(
    "when,outcome,expected",
    [
        ("setup", "passed", ""),
        ("call", "passed", "PASSED"),
        ("teardown", "passed", ""),
        ("setup", "failed", "ERROR"),
        ("call", "failed", "FAILED"),
        ("teardown", "failed", "ERROR"),
        ("setup", "skipped", "SKIPPED"),
        ("call", "skipped", "SKIPPED"),
    ],
)
def test_reporting_preserves_actual_call_and_lifecycle_failures(
    capsys: pytest.CaptureFixture[str], when: str, outcome: str, expected: str
) -> None:
    report = SimpleNamespace(
        when=when,
        nodeid="test.py::case",
        passed=outcome == "passed",
        failed=outcome == "failed",
        skipped=outcome == "skipped",
    )
    pytest_runtest_logreport(report)
    assert capsys.readouterr().out == (
        f"\n{expected} test.py::case\n" if expected else ""
    )
