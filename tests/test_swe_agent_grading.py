"""Submission/image/accounting joins and incomplete official reference coverage."""

import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from bench import swe_agent_grading as grade
from bench.campaign_accounting import digest
from bench.coding_submission import private_write
from bench.guard_evidence import file_sha
from bench.swebench_cohort import CohortError


def test_fixture_cleanup_failure_restores_grader_scope_and_releases_lock(
    fixture, monkeypatch
):
    from bench.swe_requests_fixture import FixtureOwnershipError

    args, submission, _, _, _, _, runner = fixture
    task_id = submission["task_id"]
    monkeypatch.setattr(grade, "REQUESTS_TASK", task_id)
    monkeypatch.setattr(
        grade,
        "verify_environment_report",
        lambda *a: {
            "checks": [{"task_id": task_id, "environment_admitted": True}],
            "http_fixtures": {task_id: {"fixture": True}},
        },
    )

    class FailingFixture:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def close(self):
            raise FixtureOwnershipError("controlled cleanup failure")

    monkeypatch.setattr(grade, "RequestsHTTPFixture", FailingFixture)
    previous = runner.RUN_EVALUATION_LOG_DIR
    with pytest.raises(FixtureOwnershipError):
        grade.grade_task(**args)
    assert runner.RUN_EVALUATION_LOG_DIR == previous
    assert not grade._GRADER_LOCK.locked()
    assert not (args["output"] / "grade.json").exists()


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    task_id, model = "fixture__task-1", "openai/fixture"
    task = {"instance_id": task_id, "base_commit": "a" * 40}
    pin = {
        "task_id": task_id,
        "config_digest": "sha256:" + "b" * 64,
        "immutable_image": "fixture/image@sha256:" + "c" * 64,
    }
    cohort = {
        "tasks": [
            {
                "task_id": task_id,
                "public_task": task,
                "public_task_sha256": digest(task),
            }
        ]
    }
    env = tmp_path / "environment"
    env.mkdir()
    private_write(env / "summary.json", {"literal": "frozen fixture"})
    source = tmp_path / "task"
    source.mkdir()
    submission_path = source / "submission.json"
    private_write(submission_path, {"literal": "receipt-backed fixture"})
    private_write(
        source / "cell-accounting.json",
        {"committed_usd": "0.001", "pending_upper_usd": "0", "budget_exhausted": False},
    )
    provenance = {
        "public_task_sha256": digest(task),
        "cohort_sha256": digest(cohort),
        "image_id": pin["config_digest"],
        "immutable_image": pin["immutable_image"],
        "execution_version": "e" * 64,
        "environment_report_sha256": file_sha(env / "summary.json"),
        "transport_session_id": "fixture-session",
        "cell_id": "fixture-cell",
        "arm": "joint_guarded",
    }
    submission = {
        "task_id": task_id,
        "base_commit": task["base_commit"],
        "provenance": provenance,
        "model_name_or_path": model,
        "model_patch": "literal exact patch\n\n",
        "patch_sha256": "d" * 64,
        "budget_exhausted": False,
    }
    inputs = SimpleNamespace(
        verify=lambda: None,
        cohort=cohort,
        pins={"checks": [pin]},
        private_rows={task_id: dict(task, patch="private gold must not be used")},
        evaluator=tmp_path / "evaluator",
        task_assets=tmp_path / "assets",
        sources={"literal": "frozen source"},
    )
    monkeypatch.setattr(
        grade,
        "verify_environment_report",
        lambda *args: {"checks": [{"task_id": task_id, "environment_admitted": True}]},
    )
    monkeypatch.setattr(grade, "load_submission", lambda path: deepcopy(submission))
    monkeypatch.setattr(grade, "execution_version", lambda bundle: "e" * 64)
    snapshot = SimpleNamespace(
        sha256="f" * 64,
        events=[
            {"event": "campaign_session", "session_id": "fixture-session"},
            {
                "event": "reserve",
                "id": "call",
                "session_id": "fixture-session",
                "metadata": {
                    "cell_id": "fixture-cell",
                    "task_id": task_id,
                    "arm": "joint_guarded",
                    "requested_model": model,
                    "actor": "agent",
                },
            },
        ],
        state={"results": {"call": {"cost_usd": "0.001"}}},
    )
    monkeypatch.setattr(grade, "read_snapshot", lambda path: snapshot)
    bundle = SimpleNamespace(verify=lambda: None)
    spec = SimpleNamespace(
        instance_id=task_id,
        image=pin["config_digest"],
        eval_script="literal evaluation\n",
        FAIL_TO_PASS=["regression"],
        PASS_TO_PASS=["preservation"],
    )
    monkeypatch.setattr(grade, "load_evaluator", lambda path: lambda row: spec)
    observed = {"regression": "FAILED", "preservation": "PASSED"}
    official = {"resolved": False}
    parser = SimpleNamespace(
        get_logs_eval=lambda *args: (observed, True),
        get_eval_report=lambda *args: {task_id: official},
        _resolve_case=lambda test, values: test if test in values else None,
        test_passed=lambda test, values: values.get(test) == "PASSED",
        test_failed=lambda test, values: values.get(test) == "FAILED",
        parse_test_exit_code=lambda text: 1,
    )
    calls, drained = [], []
    runner = SimpleNamespace(
        RUN_EVALUATION_LOG_DIR=tmp_path / "prior",
        CONTAINER_WORKDIR="/testbed",
        LOG_TEST_OUTPUT="test_output.txt",
    )

    class Containers:
        def __init__(self, *args):
            self.created = []

        def drain(self):
            drained.append(True)

    monkeypatch.setattr(grade, "_OwnedContainers", Containers)

    def run(spec, prediction, client, run_id, **kwargs):
        calls.append((deepcopy(prediction), kwargs))
        client.containers.created.append(SimpleNamespace(base_verified=True))
        directory = (
            runner.RUN_EVALUATION_LOG_DIR / run_id / model.replace("/", "__") / task_id
        )
        directory.mkdir(parents=True)
        (directory / "test_output.txt").write_text("literal captured test log\n")
        return task_id, {task_id: official}

    runner.run_instance = run
    monkeypatch.setitem(sys.modules, "swebench.harness.run_evaluation", runner)
    monkeypatch.setitem(sys.modules, "swebench.harness.grading", parser)
    monkeypatch.setattr(
        grade.importlib, "import_module", lambda name: sys.modules[name]
    )
    monkeypatch.setitem(sys.modules, "docker", SimpleNamespace())
    client = SimpleNamespace(
        images=SimpleNamespace(
            get=lambda identity: SimpleNamespace(
                id=identity, attrs={"Architecture": "amd64", "Os": "linux"}
            )
        )
    )
    args = dict(
        inputs=inputs,
        environment_report=env,
        bundle=bundle,
        ledger=tmp_path / "ledger",
        submission_path=submission_path,
        output=tmp_path / "grading",
        client=client,
    )
    return args, submission, observed, official, calls, drained, runner


def test_grader_receives_exact_reported_patch_without_gold_or_newline_repairs(fixture):
    args, submission, observed, official, calls, drained, runner = fixture
    original = runner.RUN_EVALUATION_LOG_DIR
    result = grade.grade_task(**args)
    assert calls[0][0]["model_patch"] == submission["model_patch"]
    assert calls[0][0]["model_patch"] != "private gold must not be used"
    assert calls[0][1]["skip_patch"] is False
    assert result["reference_tests_complete"] and result["official_score"] == 0
    assert result["all_provider_cost_usd"] == "0.001"
    assert result["actual_test_exit_code"] == 1
    assert runner.RUN_EVALUATION_LOG_DIR == original and drained
    assert grade._GRADER_LOCK.acquire(blocking=False)
    grade._GRADER_LOCK.release()


@pytest.mark.parametrize(
    "field",
    [
        "image_id",
        "immutable_image",
        "cohort_sha256",
        "execution_version",
        "environment_report_sha256",
    ],
)
def test_foreign_execution_identity_is_rejected_before_grading(fixture, field):
    args, submission, observed, official, calls, drained, runner = fixture
    submission["provenance"][field] = "other"
    with pytest.raises(CohortError):
        grade.grade_task(**args)
    assert not calls and not args["output"].exists()


def test_missing_reference_tests_remain_unscored(fixture):
    args, submission, observed, official, calls, drained, runner = fixture
    observed.pop("preservation")
    result = grade.grade_task(**args)
    assert result["reference_tests_complete"] is False
    assert result["official_score"] is result["protocol_score"] is None


def test_budget_failure_retains_official_outcome_and_scores_protocol_failure(fixture):
    args, submission, observed, official, calls, drained, runner = fixture
    submission["budget_exhausted"] = True
    submission["model_patch"] = ""
    observed["regression"] = "PASSED"
    official["resolved"] = True
    result = grade.grade_task(**args)
    assert result["official_score"] == 1 and result["protocol_score"] == 0
    assert calls[0][1]["skip_patch"] is True


def test_unreconciled_call_charges_do_not_reach_grader(fixture):
    args, submission, observed, official, calls, drained, runner = fixture
    import json

    (args["submission_path"].parent / "cell-accounting.json").write_text(
        json.dumps({"committed_usd": "0", "pending_upper_usd": "0"})
    )
    with pytest.raises(CohortError):
        grade.grade_task(**args)
    assert not calls


def test_resource_capture_failure_keeps_cleanup_and_marks_counters_unavailable():
    from types import SimpleNamespace
    from bench.swe_agent_grading import _MeasuredContainer

    stopped = []

    def unavailable(**kwargs):
        raise RuntimeError("counters unavailable")

    raw = SimpleNamespace(
        id="owned",
        stats=unavailable,
        stop=lambda **kwargs: stopped.append("stopped"),
        remove=lambda **kwargs: stopped.append("removed"),
    )
    container = _MeasuredContainer(raw)
    container.stop()
    container.remove()
    assert stopped == ["stopped", "removed"]
    assert container.measurement["cpu_seconds"] is None
    assert container.measurement["peak_memory_available"] is False
