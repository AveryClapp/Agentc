from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from bench.coding_submission import (
    MARKER,
    MINI_REVISION,
    SubmissionError,
    capture_submission,
    load_submission,
    private_write,
)
from bench.coding_task_worker import CodingTaskError, initialize_task, run_task


def submission(
    body="diff --git a/x b/x\n context\n", *, status="Submitted", events=None
):
    output = MARKER + "\n" + body
    return capture_submission(
        task_id="psf__requests-1766",
        base_commit="a" * 40,
        model_name="fixture-model",
        status=status,
        returned_text="\n".join(output.lstrip().splitlines()[1:])
        if status == "Submitted"
        else "",
        events=events
        if events is not None
        else [
            {
                "event": "tool",
                "command": "actual submission command",
                "result": {"returncode": 0, "output": output},
            },
        ],
        provenance={"upstream_revision": MINI_REVISION},
    )


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\n\n", ""])
def test_only_actual_tool_line_endings_are_preserved(ending):
    body = "diff --git a/x b/x\n context" + ending
    record = submission(body)
    assert record["model_patch"] == body
    assert record["patch_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert record["serialization_changed"] == bool(ending)


def test_capture_does_not_mutate_or_retain_mutable_tool_state():
    events = [
        {"event": "tool", "result": {"returncode": 0, "output": MARKER + "\nx\n"}}
    ]
    original = deepcopy(events)
    record = submission("x\n", events=events)
    assert events == original
    events[0]["result"]["output"] = "changed later"
    assert record["tool_events"] == original


@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"event": "tool_failure"}],
        [{"event": "tool", "result": {"returncode": 1, "output": MARKER + "\nx\n"}}],
        [
            {
                "event": "tool",
                "result": {"returncode": False, "output": MARKER + "\nx\n"},
            }
        ],
        [{"event": "tool", "result": {"returncode": 0, "output": "different\nx\n"}}],
        [
            {
                "event": "tool",
                "result": {"returncode": 0, "output": MARKER + "\nother\n"},
            }
        ],
        [
            {"event": "tool", "result": {"returncode": 0, "output": MARKER + "\nx\n"}},
            {
                "event": "tool",
                "result": {"returncode": 0, "output": "later non-submission"},
            },
        ],
    ],
)
def test_missing_failed_stale_or_changed_submission_receipts_are_rejected(events):
    with pytest.raises(SubmissionError):
        submission("x\n", events=events)


def test_limits_do_not_submit_worktree_edits_or_prior_marker_outputs():
    record = submission(status="LimitsExceeded")
    assert record["model_patch"] == ""
    assert record["terminal_tool_receipt_index"] is None
    assert not record["serialization_changed"]


@pytest.mark.parametrize(
    "field",
    ["model_patch", "patch_sha256", "serialization_changed", "tool_events_sha256"],
)
def test_modified_submission_artifacts_cannot_be_graded(tmp_path, field):
    record = submission()
    record[field] = "tampered"
    path = tmp_path / "submission.json"
    private_write(path, record)
    with pytest.raises(SubmissionError):
        load_submission(path)


def test_private_exclusive_roundtrip_and_duplicate_keys(tmp_path):
    record = submission()
    path = tmp_path / "submission.json"
    private_write(path, record)
    assert load_submission(path) == record
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        private_write(path, record)
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"task_id":"changed",' + json.dumps(record)[1:])
    with pytest.raises(SubmissionError, match="duplicate"):
        load_submission(duplicate)


def test_oracle_fields_are_rejected_before_any_container_or_model_access(tmp_path):
    from types import SimpleNamespace

    with pytest.raises(CodingTaskError, match="public fields"):
        run_task(
            source=tmp_path,
            public_task={"patch": "oracle"},
            model=SimpleNamespace(n_calls=0, cost=0),
            output=tmp_path / "output",
            image="unused",
            image_id="unused",
            dataset_sha256="unused",
            step_limit=1,
            acquisition_kind="test",
        )
    assert not (tmp_path / "output").exists()


def test_task_initialization_rejects_inherited_changes_before_checkout():
    from types import SimpleNamespace

    commands = []

    def execute(command):
        commands.append(command)
        return {"returncode": 0, "output": " M inherited.py"}

    with pytest.raises(CodingTaskError, match="inherited"):
        initialize_task(SimpleNamespace(execute=execute), "a" * 40)
    assert commands == ["git status --porcelain --untracked-files=all"]


@pytest.mark.parametrize("field", ["task", "base", "image", "image_id", "dataset"])
def test_grader_rejects_foreign_submissions_before_environment_access(
    tmp_path, monkeypatch, field
):
    from bench import requests_fixture_preflight as fixture

    record = submission()
    record["base_commit"] = fixture.BASE_COMMIT
    record["provenance"].update(
        task_image=fixture.TASK_IMAGE,
        task_image_id=fixture.TASK_IMAGE_ID,
        dataset_sha256=fixture.DATASET_SHA256,
    )
    if field == "task":
        record["task_id"] = "other-task"
    elif field == "base":
        record["base_commit"] = "b" * 40
    else:
        key = {
            "image": "task_image",
            "image_id": "task_image_id",
            "dataset": "dataset_sha256",
        }[field]
        record["provenance"][key] = "changed"
    path = tmp_path / "submission.json"
    private_write(path, record)
    dataset = tmp_path / "dataset.parquet"
    dataset.write_bytes(b"dataset placeholder")
    original_sha = fixture._sha
    monkeypatch.setattr(
        fixture,
        "_sha",
        lambda p: fixture.DATASET_SHA256 if p == dataset else original_sha(p),
    )
    monkeypatch.setattr(
        fixture, "_verified_wheels", lambda _: pytest.fail("environment touched")
    )
    with pytest.raises(fixture.FixtureError, match="pinned task environment"):
        fixture.run_preflight(dataset, tmp_path / "out", tmp_path, submissions=[path])
    assert not (tmp_path / "out").exists()


def test_duplicate_submissions_cannot_expand_the_grading_denominator(
    tmp_path, monkeypatch
):
    from bench import requests_fixture_preflight as fixture

    record = submission()
    record["base_commit"] = fixture.BASE_COMMIT
    record["provenance"].update(
        task_image=fixture.TASK_IMAGE,
        task_image_id=fixture.TASK_IMAGE_ID,
        dataset_sha256=fixture.DATASET_SHA256,
    )
    path = tmp_path / "submission.json"
    private_write(path, record)
    dataset = tmp_path / "dataset.parquet"
    dataset.write_bytes(b"dataset placeholder")
    original_sha = fixture._sha
    monkeypatch.setattr(
        fixture,
        "_sha",
        lambda p: fixture.DATASET_SHA256 if p == dataset else original_sha(p),
    )
    monkeypatch.setattr(
        fixture, "_verified_wheels", lambda _: pytest.fail("environment touched")
    )
    with pytest.raises(fixture.FixtureError, match="duplicate"):
        fixture.run_preflight(
            dataset, tmp_path / "out", tmp_path, submissions=[path, path]
        )


@pytest.mark.parametrize("phase", ["initialization", "model"])
def test_acquisition_failures_remain_unscored_and_cannot_resume(
    tmp_path, monkeypatch, phase
):
    from types import SimpleNamespace
    from dataclasses import dataclass
    from bench import coding_task_worker as worker
    from bench.campaign_transport import CampaignStopped

    @dataclass
    class Config:
        model_name: str = "fixture"

    class Model:
        config, n_calls, cost = Config(), 0, 0.0

        def query(self, *_args, **_kwargs):
            raise CampaignStopped("retained financial failure")

    class Environment:
        def __init__(self, **kwargs):
            self.container_id = "a" * 64

        def execute(self, command, cwd=""):
            return {
                "returncode": 0,
                "output": "b" * 40 if command == "git rev-parse HEAD" else "",
            }

    class Agent:
        def __init__(self, model, environment, **kwargs):
            self.model = model

        def run(self, task):
            self.model.query([{"role": "user", "content": task}])

    commands = []

    def command(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [{"Id": "sha256:" + "a" * 64, "Architecture": "amd64"}]
            ).encode(),
        )

    def load(*args):
        if phase == "initialization":
            raise CodingTaskError("missing pinned dependency")
        return Agent, Environment, {"agent": {}, "environment": {}}, {}

    monkeypatch.setattr(worker.subprocess, "run", command)
    monkeypatch.setattr(worker, "_load_upstream", load)
    arguments = dict(
        source=tmp_path,
        public_task={
            "instance_id": "fixture-task",
            "repo": "fixture/repo",
            "base_commit": "b" * 40,
            "problem_statement": "public task",
            "version": "1",
        },
        model=Model(),
        output=tmp_path / "acquisition",
        image="fixture@sha256:" + "a" * 64,
        image_id="sha256:" + "a" * 64,
        dataset_sha256="c" * 64,
        step_limit=1,
        acquisition_kind="test",
    )
    with pytest.raises(
        CodingTaskError if phase == "initialization" else CampaignStopped
    ):
        run_task(**arguments)
    failure = json.loads((arguments["output"] / "failure.json").read_bytes())
    assert failure["official_score"] is None
    assert not (arguments["output"] / "submission.json").exists()
    if phase == "model":
        assert ["docker", "rm", "-f", "a" * 64] in commands
        events = [
            json.loads(line)
            for line in (arguments["output"] / "events.jsonl").read_text().splitlines()
        ]
        assert events[-1]["failure_type"] == "CampaignStopped"
    with pytest.raises(CodingTaskError, match="no retry or resume"):
        run_task(**arguments)
