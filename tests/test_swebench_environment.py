"""Reject incomplete controls and retain all tasks while owning local containers."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from bench import swebench_environment as env
from bench.swebench_cohort import CohortError


def grading():
    def resolve(test, observed):
        if test in observed:
            return test
        if test.count("[") > test.count("]"):
            matches = [t for t in observed if t.startswith(test)]
            if (
                matches
                and len({observed[t] in {"PASSED", "XFAIL"} for t in matches}) == 1
            ):
                return matches[0]
        return None

    def passed(test, observed):
        return observed.get(resolve(test, observed)) in {"PASSED", "XFAIL"}

    return SimpleNamespace(
        _resolve_case=resolve,
        test_passed=passed,
        test_failed=lambda test, observed: not passed(test, observed),
    )


@pytest.mark.parametrize("status", [None, "SKIPPED", "UNKNOWN", "FAILED", "ERROR"])
def test_preservation_failures_or_missing_tests_do_not_admit(status):
    spec = SimpleNamespace(FAIL_TO_PASS=["regression"], PASS_TO_PASS=["preservation"])
    observed = {"regression": "FAILED"}
    if status is not None:
        observed["preservation"] = status
    result = env.reference_verdict(spec, observed, True, {"resolved": False}, grading())
    assert result["negative_control_valid"] is False
    assert result["positive_control_valid"] is False


def test_controls_require_actual_reference_coverage_and_official_outcome():
    spec = SimpleNamespace(FAIL_TO_PASS=["regression"], PASS_TO_PASS=["preservation"])
    negative = env.reference_verdict(
        spec,
        {"regression": "FAILED", "preservation": "PASSED"},
        True,
        {"resolved": False},
        grading(),
    )
    assert negative["negative_control_valid"] and not negative["positive_control_valid"]
    positive = env.reference_verdict(
        spec,
        {"regression": "PASSED", "preservation": "PASSED"},
        True,
        {"resolved": True},
        grading(),
    )
    assert positive["positive_control_valid"] and not positive["negative_control_valid"]
    assert positive["required_test_count"] == 2
    assert positive["missing_reference_tests"] == 0
    for missing in ({}, {"preservation": "PASSED"}):
        assert not env.reference_verdict(
            spec, missing, True, {"resolved": True}, grading()
        )["positive_control_valid"]
    assert not env.reference_verdict(
        spec,
        {"regression": "PASSED", "preservation": "PASSED"},
        False,
        {"resolved": True},
        grading(),
    )["positive_control_valid"]


def test_truncated_ids_follow_pinned_grader_resolution_semantics():
    spec = SimpleNamespace(FAIL_TO_PASS=["case[param"], PASS_TO_PASS=["preservation"])
    observed = {
        "case[param-one]": "PASSED",
        "case[param-two]": "XFAIL",
        "preservation": "PASSED",
    }
    assert env.reference_verdict(spec, observed, True, {"resolved": True}, grading())[
        "positive_control_valid"
    ]
    observed["case[param-two]"] = "FAILED"
    result = env.reference_verdict(spec, observed, True, {"resolved": True}, grading())
    assert (
        result["missing_reference_tests"] == 1 and not result["positive_control_valid"]
    )


@pytest.fixture
def docker_stub(monkeypatch):
    class DockerException(Exception):
        pass

    class NotFound(DockerException):
        pass

    class ImageNotFound(NotFound):
        pass

    module = SimpleNamespace(
        errors=SimpleNamespace(
            DockerException=DockerException,
            NotFound=NotFound,
            ImageNotFound=ImageNotFound,
        )
    )
    monkeypatch.setitem(sys.modules, "docker", module)
    return module


@pytest.fixture
def cohort_inputs(monkeypatch, tmp_path, docker_stub):
    tasks = [
        {
            "task_id": f"repo__task-{i}",
            "stage": "C" if i < 30 else "P" if i < 60 else "T",
            "public_task_sha256": f"{i:064x}",
        }
        for i in range(150)
    ]
    pins = [{"task_id": t["task_id"], "status": "image_unavailable"} for t in tasks]
    for i in (0, 1):
        pins[i].update(
            status="immutable_image_verified",
            config_digest="sha256:" + str(i) * 64,
            immutable_image="swebench/task@sha256:" + str(i) * 64,
        )
    calls = []

    def local_image(reference):
        calls.append(reference)
        if reference.endswith("1" * 64):
            raise docker_stub.errors.ImageNotFound("not local")
        return SimpleNamespace(
            id=reference, attrs={"Os": "linux", "Architecture": "amd64"}
        )

    client = SimpleNamespace(images=SimpleNamespace(get=local_image))
    verify_calls = []
    inputs = SimpleNamespace(
        cohort={"tasks": tasks},
        pins={"checks": pins},
        bridge={},
        sources={},
        verify=lambda: verify_calls.append(True),
        evaluator=tmp_path / "evaluator",
    )
    runner = SimpleNamespace(
        RUN_EVALUATION_LOG_DIR=tmp_path / "original", CONTAINER_WORKDIR="/testbed"
    )
    monkeypatch.setattr(env, "load_evaluator", lambda source: None)
    original = env.importlib.import_module
    monkeypatch.setattr(
        env.importlib,
        "import_module",
        lambda name: (
            runner
            if name == "swebench.harness.run_evaluation"
            else grading()
            if name == "swebench.harness.grading"
            else original(name)
        ),
    )

    def valid_control(**kwargs):
        return {
            "status": "control_graded",
            "negative_control_valid": kwargs["name"] == "unchanged",
            "positive_control_valid": kwargs["name"] == "gold",
            "complete": True,
        }

    monkeypatch.setattr(env, "control", valid_control)
    return inputs, client, runner, calls, verify_calls


def test_all_150_tasks_remain_when_only_one_image_is_local(cohort_inputs, tmp_path):
    inputs, client, runner, calls, verified = cohort_inputs
    prior = runner.RUN_EVALUATION_LOG_DIR
    report = env.check_cohort(inputs, tmp_path / "output", client=client)
    assert len(report["checks"]) == report["assigned_task_count"] == 150
    assert report["environments_admitted"] == 1
    assert report["checks"][0]["environment_admitted"] is True
    assert report["checks"][1]["status"] == "local_image_unavailable"
    assert all(
        c["status"] == "immutable_image_unavailable" for c in report["checks"][2:]
    )
    assert all(c["official_score"] is None for c in report["checks"])
    assert report["environment_admission_completed"] is False
    assert report["provider_acquisition_ready"] is False
    assert report["physical_provider_calls"] == 0
    assert len(calls) == 2 and len(verified) == 2
    assert runner.RUN_EVALUATION_LOG_DIR == prior
    enrollment = json.loads((tmp_path / "output/enrollment.json").read_text())
    assert enrollment["cohort"] == inputs.cohort
    assert (tmp_path / "output/summary.json").stat().st_mode & 0o777 == 0o600
    assert not env._GRADER_LOCK.locked()


def test_bounded_cache_failures_preserve_cohort_and_release_admitted_lease(
    cohort_inputs, tmp_path
):
    inputs, client, runner, calls, verified = cohort_inputs
    acquired, released = [], []

    def acquire(pin):
        acquired.append(pin["task_id"])
        if pin["task_id"] == "repo__task-1":
            raise env.ProvisioningError("session pull limit")
        return {"ownership": "borrowed", "image_id": pin["config_digest"]}

    cache = SimpleNamespace(
        enrollment={"bounded": True},
        acquire=acquire,
        release=lambda: released.append(True),
    )
    report = env.check_cohort(
        inputs, tmp_path / "bounded", client=client, image_cache=cache
    )
    assert acquired == ["repo__task-0", "repo__task-1"]
    assert released == [True]
    assert report["assigned_task_count"] == len(report["checks"]) == 150
    assert report["checks"][0]["environment_admitted"] is True
    assert report["checks"][1]["status"] == "image_provisioning_failed"
    assert all(not c["environment_admitted"] for c in report["checks"][1:])


def test_uncertain_cache_cleanup_stops_grading_session(cohort_inputs, tmp_path):
    inputs, client, runner, calls, verified = cohort_inputs
    original = runner.RUN_EVALUATION_LOG_DIR

    def release():
        raise env.ImageOwnershipError("foreign image reference")

    cache = SimpleNamespace(
        enrollment={},
        acquire=lambda pin: {"image_id": pin["config_digest"]},
        release=release,
    )
    with pytest.raises(env.ImageOwnershipError):
        env.check_cohort(
            inputs, tmp_path / "uncertain", client=client, image_cache=cache
        )
    assert runner.RUN_EVALUATION_LOG_DIR == original
    assert not env._GRADER_LOCK.locked()
    assert not (tmp_path / "uncertain/summary.json").exists()


@pytest.mark.parametrize("failure", ["negative", "positive", "incomplete", "execution"])
def test_failed_controls_never_admit_environment(
    cohort_inputs, tmp_path, monkeypatch, failure
):
    inputs, client, _, _, _ = cohort_inputs

    def failed(**kwargs):
        return {
            "status": "control_execution_failed"
            if failure == "execution"
            else "control_graded",
            "negative_control_valid": failure
            not in ("negative", "incomplete", "execution"),
            "positive_control_valid": failure
            not in ("positive", "incomplete", "execution"),
        }

    monkeypatch.setattr(env, "control", failed)
    report = env.check_cohort(inputs, tmp_path / "output", client=client)
    assert report["environments_admitted"] == 0
    assert len(report["checks"]) == 150


def test_drain_failure_is_fatal_and_restores_grader_root(
    cohort_inputs, tmp_path, monkeypatch
):
    inputs, client, runner, calls, _ = cohort_inputs
    prior = runner.RUN_EVALUATION_LOG_DIR

    def undrained(**kwargs):
        raise env.GradingOwnershipError("owned control is still live")

    monkeypatch.setattr(env, "control", undrained)
    with pytest.raises(env.GradingOwnershipError):
        env.check_cohort(inputs, tmp_path / "output", client=client)
    assert len(calls) == 1
    assert (tmp_path / "output/enrollment.json").is_file()
    assert not (tmp_path / "output/summary.json").exists()
    assert runner.RUN_EVALUATION_LOG_DIR == prior and not env._GRADER_LOCK.locked()


def test_closed_outputs_are_not_overwritten(cohort_inputs, tmp_path):
    inputs, client, _, calls, _ = cohort_inputs
    out = tmp_path / "output"
    out.mkdir()
    (out / "summary.json").write_text("preserve")
    with pytest.raises(CohortError, match="fresh output"):
        env.check_cohort(inputs, out, client=client)
    assert not calls and (out / "summary.json").read_text() == "preserve"


@pytest.mark.parametrize("mutation", ["tree", "dirty", "command_failure"])
def test_container_must_start_clean_at_frozen_base(mutation):
    base = "a" * 40
    executions = []

    def execute(command, **kwargs):
        executions.append(command)
        if command == "git rev-parse HEAD^{tree}":
            output = ("b" * 40 if mutation == "tree" else "c" * 40).encode()
        elif "^{tree}" in command:
            output = ("c" * 40).encode()
        elif "rev-parse" in command:
            output = base.encode()
        else:
            output = b" M tracked.py\n" if mutation == "dirty" else b""
        return SimpleNamespace(
            exit_code=1 if mutation == "command_failure" else 0, output=output
        )

    proxy = env._CheckedContainer(
        SimpleNamespace(start=lambda: None, exec_run=execute), base, "/testbed"
    )
    with pytest.raises(CohortError, match="clean task base"):
        proxy.start()
    assert not proxy.base_verified and len(executions) == 4


def test_empty_setup_commit_can_match_the_exact_frozen_tree():
    base = "a" * 40
    setup = "b" * 40
    tree = "c" * 40

    def execute(command, **kwargs):
        value = (
            tree
            if "^{tree}" in command
            else setup
            if command == "git rev-parse HEAD"
            else ""
        )
        return SimpleNamespace(exit_code=0, output=(value + "\n").encode())

    proxy = env._CheckedContainer(
        SimpleNamespace(start=lambda: None, exec_run=execute), base, "/testbed"
    )
    proxy.start()
    assert proxy.base_verified
    assert proxy.base_identity == {
        "base_commit": base,
        "image_head": setup,
        "base_tree": tree,
        "image_tree": tree,
        "clean": True,
    }


def test_no_implicit_image_pull_or_deleting_preexisting_container():
    images = env._LocalImages(SimpleNamespace(get=lambda reference: reference))
    assert images.get("immutable") == "immutable"
    with pytest.raises(CohortError, match="cannot pull"):
        images.pull("image:latest")
    removed = []
    existing = SimpleNamespace(remove=lambda **kwargs: removed.append(True))
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda name: existing))
    containers = env._OwnedContainers(client, "a" * 40, "/testbed")
    with pytest.raises(CohortError, match="already exists"):
        containers.get("collision")
    assert not removed


@pytest.mark.parametrize("failure", ["inspect", "remove", "still_present"])
def test_drain_uncertainty_cannot_allow_the_next_control(docker_stub, failure):
    calls = []

    def remove(**kwargs):
        calls.append("remove")
        if failure == "remove":
            raise docker_stub.errors.DockerException("PRIVATE ERROR")

    def get(identity):
        calls.append(identity)
        if failure == "inspect":
            raise docker_stub.errors.DockerException("PRIVATE ERROR")
        return SimpleNamespace(remove=remove)

    client = SimpleNamespace(containers=SimpleNamespace(get=get))
    containers = env._OwnedContainers(client, "a" * 40, "/testbed")
    containers.created.append(SimpleNamespace(id="owned-id"))
    with pytest.raises(env.GradingOwnershipError):
        containers.drain()
    assert calls[0] == "owned-id"


def test_drain_checks_only_its_owned_containers(docker_stub):
    calls = []

    def get(identity):
        calls.append(identity)
        raise docker_stub.errors.NotFound("already terminated")

    client = SimpleNamespace(containers=SimpleNamespace(get=get))
    containers = env._OwnedContainers(client, "a" * 40, "/testbed")
    containers.created.append(SimpleNamespace(id="owned-id"))
    containers.drain()
    assert calls == ["owned-id"]


def test_cancellation_releases_grader_session_without_continuing(
    cohort_inputs, tmp_path, monkeypatch
):
    inputs, client, runner, calls, _ = cohort_inputs
    prior = runner.RUN_EVALUATION_LOG_DIR

    def cancelled(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(env, "control", cancelled)
    with pytest.raises(KeyboardInterrupt):
        env.check_cohort(inputs, tmp_path / "output", client=client)
    assert len(calls) == 1
    assert runner.RUN_EVALUATION_LOG_DIR == prior
    assert not env._GRADER_LOCK.locked()
    assert not (tmp_path / "output/summary.json").exists()


@pytest.fixture
def replayable_report(cohort_inputs, tmp_path, monkeypatch):
    inputs, client, _, _, _ = cohort_inputs
    task_id = inputs.cohort["tasks"][0]["task_id"]
    inputs.private_rows = {
        task_id: {
            "instance_id": task_id,
            "base_commit": "a" * 40,
            "patch": "PRIVATE GOLD PATCH",
        }
    }
    inputs.task_assets = tmp_path / "assets"
    spec = SimpleNamespace(
        instance_id=task_id,
        image="sha256:" + "0" * 64,
        eval_script="#!/bin/bash\necho test\n",
        FAIL_TO_PASS=["regression"],
        PASS_TO_PASS=["preservation"],
    )
    grade = grading()
    grade.get_logs_eval = lambda spec, path: (json.loads(open(path).read()), True)
    grade.parse_test_exit_code = lambda content: 1
    grade.get_eval_report = lambda spec, pred, path, include: {
        spec.instance_id: {
            "resolved": all(
                v == "PASSED" for v in json.loads(open(path).read()).values()
            )
        }
    }
    monkeypatch.setattr(env, "load_evaluator", lambda source: lambda row: spec)
    original = env.importlib.import_module
    monkeypatch.setattr(
        env.importlib,
        "import_module",
        lambda name: grade if name == "swebench.harness.grading" else original(name),
    )

    def receipt(**kwargs):
        name = kwargs["name"]
        output = kwargs["output"]
        directory = (
            output
            / "raw"
            / ("agentc-environment-" + ("0" if name == "unchanged" else "1") * 32)
            / name
            / task_id
        )
        directory.mkdir(parents=True)
        observed = {
            "regression": "FAILED" if name == "unchanged" else "PASSED",
            "preservation": "PASSED",
        }
        (directory / "test_output.txt").write_text(json.dumps(observed))
        (directory / "eval.sh").write_text(spec.eval_script)
        if name == "gold":
            (directory / "patch.diff").write_text(inputs.private_rows[task_id]["patch"])
        official = {"resolved": name == "gold"}
        return {
            "control": name,
            "task_id": task_id,
            "status": "control_graded",
            "base_verified": True,
            "base_identity": {
                "base_commit": "a" * 40,
                "image_head": "b" * 40,
                "base_tree": "c" * 40,
                "image_tree": "c" * 40,
                "clean": True,
            },
            "executed_image_id": kwargs["image_id"],
            "immutable_image": kwargs["pin"]["immutable_image"],
            "eval_script_sha256": env.sha256(spec.eval_script.encode()).hexdigest(),
            **env.reference_verdict(spec, observed, True, official, grade),
            "resolved": official["resolved"],
            "official": official,
            "observed_sha256": env.digest(observed),
            "test_exit_code": 1,
            "test_log_sha256": env.file_sha(directory / "test_output.txt"),
            "raw_artifacts": {
                p.relative_to(output).as_posix(): env.file_sha(p)
                for p in directory.iterdir()
            },
        }

    monkeypatch.setattr(env, "control", receipt)
    output = tmp_path / "output"
    report = env.check_cohort(inputs, output, client=client)
    return inputs, output, report


def test_saved_admission_replays_raw_scripts_gold_patch_and_official_results(
    replayable_report,
):
    inputs, output, report = replayable_report
    assert env.verify_environment_report(inputs, output) == report


@pytest.mark.parametrize(
    "mutation",
    [
        "task",
        "stage",
        "model_score",
        "image",
        "log",
        "script",
        "gold_patch",
        "flag",
        "base_tree",
        "count",
        "missing_task",
        "producer",
        "enrollment",
    ],
)
def test_saved_admission_rejects_changed_evidence(replayable_report, mutation):
    inputs, output, report = replayable_report
    check = report["checks"][0]
    if mutation == "task":
        check["task_id"] = "different"
    elif mutation == "stage":
        check["stage"] = "T"
    elif mutation == "model_score":
        check["official_score"] = 1
    elif mutation == "image":
        check["executed_image_id"] = "sha256:" + "f" * 64
    elif mutation == "flag":
        check["controls"][0]["negative_control_valid"] = False
    elif mutation == "base_tree":
        check["controls"][0]["base_identity"]["image_tree"] = "f" * 40
    elif mutation == "count":
        report["environments_admitted"] = 150
    elif mutation == "missing_task":
        report["checks"].pop()
    elif mutation == "producer":
        report["preflight_sha256"] = "changed"
    elif mutation == "enrollment":
        path = output / "enrollment.json"
        enrollment = json.loads(path.read_text())
        enrollment["assigned_task_count"] = 149
        path.write_text(json.dumps(enrollment))
    else:
        control = check["controls"][1 if mutation == "gold_patch" else 0]
        filename = {
            "log": "test_output.txt",
            "script": "eval.sh",
            "gold_patch": "patch.diff",
        }[mutation]
        path = next(
            output / p for p in control["raw_artifacts"] if p.endswith(filename)
        )
        path.write_text("changed bytes")
    (output / "summary.json").write_text(json.dumps(report))
    (output / (inputs.cohort["tasks"][0]["task_id"] + ".json")).write_text(
        json.dumps(check)
    )
    with pytest.raises(CohortError):
        env.verify_environment_report(inputs, output)
