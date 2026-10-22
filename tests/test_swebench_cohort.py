"""Exercise frozen membership, oracle isolation and failed prerequisites."""

from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from bench import swebench_cohort as cohort


@pytest.fixture
def rows(monkeypatch):
    data = [
        {
            "instance_id": f"repo__task-{i}",
            "repo": f"org/repo-{i % 7}",
            "base_commit": f"{i:040x}",
            "version": "1.0",
            "problem_statement": f"Fix {i}",
            "patch": "PRIVATE GOLD PATCH",
            "test_patch": "PRIVATE TEST PATCH",
            "FAIL_TO_PASS": '["private-regression"]',
            "PASS_TO_PASS": '["private-preservation"]',
        }
        for i in range(500)
    ]
    ordered = sorted(
        (row["instance_id"] for row in data),
        key=lambda task_id: hashlib.sha256(
            ("agentc-mlsys2027-v1\0swebench-verified\0" + task_id).encode()
        ).digest(),
    )
    monkeypatch.setattr(
        cohort,
        "ORDERED_IDS_SHA256",
        hashlib.sha256(
            "".join(task_id + "\n" for task_id in ordered).encode()
        ).hexdigest(),
    )
    return data


def test_frozen_partitions_ignore_row_order_outcomes_and_private_fields(rows):
    original = cohort.build_cohort(rows)
    changed = deepcopy(list(reversed(rows)))
    for row in changed:
        row["patch"] = "a completely different gold patch"
        row["baseline_resolved"] = True
        row["difficulty"] = "easy"
    assert cohort.build_cohort(changed) == original
    assert {key: len(value) for key, value in original["partitions"].items()} == {
        "C": 30,
        "P": 30,
        "T": 90,
        "not_selected": 350,
    }
    partitions = list(original["partitions"].values())
    assert len(set(sum(partitions, []))) == 500
    assert len(original["tasks"]) == original["assigned_task_count"] == 150
    assert sum(original["repository_counts"]["selected"].values()) == 150
    assert sum(original["repository_counts"]["not_selected"].values()) == 350
    encoded = json.dumps(original)
    assert "PRIVATE" not in encoded and "private-regression" not in encoded
    for task in original["tasks"]:
        assert set(task["public_task"]) == cohort.PUBLIC_FIELDS
        assert task["task_id"] in original["partitions"][task["stage"]]


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "different_id"])
def test_changed_universe_is_rejected_before_task_selection(rows, mutation):
    if mutation == "missing":
        rows.pop()
    elif mutation == "extra":
        rows.append(deepcopy(rows[-1]))
    elif mutation == "duplicate":
        rows[-1] = deepcopy(rows[0])
    else:
        rows[-1]["instance_id"] = "replacement-task"
    with pytest.raises(cohort.CohortError, match="500|duplicate|ordering"):
        cohort.build_cohort(rows)


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_commit", "main"),
        ("instance_id", "../../task"),
        ("problem_statement", None),
        ("repo", True),
        ("version", ""),
    ],
)
def test_malformed_public_identity_is_rejected(rows, field, value):
    rows[0][field] = value
    with pytest.raises(cohort.CohortError, match="public task identity"):
        cohort.build_cohort(rows)


def test_failed_evaluator_inputs_retain_every_task_and_cannot_claim_a_score(rows):
    selected = cohort.build_cohort(rows)
    calls = []

    def evaluator(row):
        calls.append(row["instance_id"])
        return row["image"]  # Same missing prerequisite as the frozen evaluator.

    results = cohort.evaluator_inputs(rows, selected, evaluator)
    assert calls == [task["task_id"] for task in selected["tasks"]]
    assert len(results) == 150
    assert all(row["missing_field"] == "image" for row in results)
    assert all(
        row["missing_required_fields"]
        == ["eval_script", "eval_type", "image", "log_parser"]
        for row in results
    )
    assert all(row["environment_admitted"] is False for row in results)
    assert all(row["official_score"] is None for row in results)


def test_spec_creation_does_not_admit_an_environment_and_binds_script_bytes(rows):
    selected = cohort.build_cohort(rows)
    script = "#!/bin/bash\necho 'actual bytes'\n"

    def evaluator(row):
        row["problem_statement"] = "attempted input mutation"
        return SimpleNamespace(instance_id=row["instance_id"], eval_script=script)

    results = cohort.evaluator_inputs(rows, selected, evaluator)
    assert all(row["status"] == "test_spec_created" for row in results)
    assert all(
        row["eval_script_sha256"] == hashlib.sha256(script.encode()).hexdigest()
        for row in results
    )
    assert all(row["environment_admitted"] is False for row in results)
    assert rows[0]["problem_statement"] == "Fix 0"


def test_evaluator_row_cannot_change_after_public_enrollment(rows):
    selected = cohort.build_cohort(rows)
    by_id = {row["instance_id"]: row for row in rows}
    by_id[selected["tasks"][0]["task_id"]]["base_commit"] = "f" * 40
    with pytest.raises(cohort.CohortError, match="differs from its public task"):
        cohort.evaluator_inputs(rows, selected, lambda row: None)


def test_evaluator_cannot_grade_a_different_task(rows):
    selected = cohort.build_cohort(rows)
    with pytest.raises(cohort.CohortError, match="changed task identity"):
        cohort.evaluator_inputs(
            rows, selected, lambda row: SimpleNamespace(instance_id="other")
        )


def test_evaluator_cannot_switch_the_task_image(rows):
    selected = cohort.build_cohort(rows)
    with pytest.raises(cohort.CohortError, match="changed task image"):
        cohort.evaluator_inputs(
            rows,
            selected,
            lambda row: SimpleNamespace(
                instance_id=row["instance_id"],
                eval_script="# frozen\n",
                image="changed",
            ),
        )


def test_source_drift_and_files_escaping_the_pin_are_rejected(tmp_path, monkeypatch):
    agent, evaluator = tmp_path / "agent", tmp_path / "evaluator"
    for root, dirs in (
        (agent, ("sweagent", "config", "tools")),
        (evaluator, ("swebench",)),
    ):
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\n")
        for name in dirs:
            (root / name).mkdir()
            (root / name / "source.py").write_text("# frozen\n")
    (agent / "config/default.yaml").write_text("agent: {}\n")
    agent_files = cohort.source_closure(
        agent, ("sweagent", "config", "tools", "pyproject.toml")
    )
    eval_files = cohort.source_closure(evaluator, ("swebench", "pyproject.toml"))
    monkeypatch.setattr(cohort, "AGENT_CLOSURE_SHA256", cohort.digest(agent_files))
    monkeypatch.setattr(
        cohort, "AGENT_CONFIG_SHA256", agent_files["config/default.yaml"]
    )
    monkeypatch.setattr(cohort, "EVALUATOR_CLOSURE_SHA256", cohort.digest(eval_files))
    cohort.verify_sources(agent, evaluator)
    (evaluator / "swebench/source.py").write_text("# changed\n")
    with pytest.raises(cohort.CohortError, match="evaluator source differs"):
        cohort.verify_sources(agent, evaluator)
    (agent / "tools/outside").symlink_to(evaluator / "pyproject.toml")
    with pytest.raises(cohort.CohortError, match="escapes"):
        cohort.verify_sources(agent, evaluator)


def test_foreign_evaluator_import_is_rejected_before_use(tmp_path, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "swebench",
        SimpleNamespace(__file__="/foreign/swebench/__init__.py"),
    )
    with pytest.raises(cohort.CohortError, match="foreign SWE-bench"):
        cohort.load_evaluator(tmp_path)


def test_preflight_never_overwrites_closed_artifacts(tmp_path):
    output = tmp_path / "closed"
    output.mkdir()
    marker = output / "summary.json"
    marker.write_text("preserve me")
    with pytest.raises(cohort.CohortError, match="fresh output"):
        cohort.run(
            dataset=tmp_path / "missing",
            agent=tmp_path,
            evaluator=tmp_path,
            output=output,
        )
    assert marker.read_text() == "preserve me"


def test_incompatible_prerequisites_return_a_failure_exit_code(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        cohort,
        "run",
        lambda **kwargs: {
            "assigned_task_count": 150,
            "compatible_evaluator_rows": 0,
            "incompatible_evaluator_rows": 150,
            "provider_acquisition_ready": False,
            "physical_provider_calls": 0,
        },
    )
    args = [
        item
        for name in ("dataset", "agent", "evaluator", "output")
        for item in (f"--{name}", str(tmp_path / name))
    ]
    assert cohort.main(args) == 2
    assert json.loads(capsys.readouterr().out)["assigned_task_count"] == 150
