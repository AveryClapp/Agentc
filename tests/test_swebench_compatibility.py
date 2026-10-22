"""Keep frozen rows, denominators and private oracles intact through bridging."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from bench import swebench_cohort as cohort
from bench import swebench_compatibility as compatibility


@pytest.fixture
def inputs(monkeypatch, tmp_path):
    rows = [
        {
            "instance_id": f"repo__task-{i}",
            "repo": "org/repo",
            "base_commit": f"{i:040x}",
            "version": "1.0",
            "created_at": "2020-01-01T00:00:00Z",
            "environment_setup_commit": "f" * 40,
            "difficulty": "easy",
            "problem_statement": f"Fix {i}",
            "hints_text": "",
            "patch": "PRIVATE GOLD PATCH",
            "test_patch": "PRIVATE TEST PATCH",
            "FAIL_TO_PASS": '["PRIVATE regression"]',
            "PASS_TO_PASS": '["PRIVATE preservation"]',
        }
        for i in range(500)
    ]
    ordered = sorted((r["instance_id"] for r in rows), key=cohort.split_key)
    monkeypatch.setattr(cohort, "ORDERED_IDS_SHA256", cohort.ordered_id_digest(ordered))
    selected = cohort.build_cohort(rows)
    assets = {}
    for row in rows:
        asset = deepcopy(row)
        for name in compatibility.TEST_FIELDS:
            asset[name] = json.loads(asset[name])
        asset.update(
            image="swebench/sweb.eval.x86_64.repo_1776_task-1:latest",
            log_parser="parse_log_pytest",
            eval_type="pass_and_fail",
            eval_script="echo 'PRIVATE TEST PATCH'\n",
            unrelated_private_metadata="must never be copied",
        )
        assets[row["instance_id"]] = asset

    def bridge(data=rows, selection=selected):
        return compatibility.bridge_rows(
            data,
            selection,
            tmp_path,
            load_task=lambda path: assets[path.name],
            parser_names=frozenset({"parse_log_pytest"}),
            eval_types=frozenset({"pass_and_fail"}),
        )

    return rows, selected, assets, bridge


def test_bridge_only_adds_metadata_and_keeps_every_original_byte(inputs):
    rows, selected, _, bridge = inputs
    snapshot = deepcopy(rows)
    changed, checks = bridge()
    assert rows == snapshot
    assert len(changed) == 500 and len(checks) == 150
    assert cohort.build_cohort(changed) == selected
    assert [r["instance_id"] for r in changed] == [r["instance_id"] for r in rows]
    selected_ids = {t["task_id"] for t in selected["tasks"]}
    for original, row in zip(rows, changed, strict=True):
        assert {key: row[key] for key in original} == original
        assert set(row) - set(original) == (
            compatibility.ADDED_FIELDS
            if original["instance_id"] in selected_ids
            else set()
        )
    assert all(c["status"] == "compatibility_metadata_added" for c in checks)
    assert all(c["image_pinned"] is False for c in checks)
    assert all(c["environment_admitted"] is False for c in checks)
    assert all(c["official_score"] is None for c in checks)
    assert "PRIVATE" not in json.dumps(checks)
    assert (
        checks[0]["source_eval_script_sha256"]
        == hashlib.sha256(b"echo 'PRIVATE TEST PATCH'\n").hexdigest()
    )


@pytest.mark.parametrize("field", sorted(compatibility.ORIGINAL_FIELDS))
def test_any_original_field_mismatch_retains_task_without_adding_metadata(
    inputs, field
):
    rows, selected, assets, bridge = inputs
    task_id = selected["tasks"][0]["task_id"]
    asset = assets[task_id]
    asset[field] = (
        ["different test"] if field in compatibility.TEST_FIELDS else "changed"
    )
    changed, checks = bridge()
    assert len(checks) == 150
    assert checks[0]["mismatched_original_fields"] == [field]
    assert checks[0]["status"] == "incompatible_task_assets"
    assert next(r for r in changed if r["instance_id"] == task_id) == next(
        r for r in rows if r["instance_id"] == task_id
    )
    assert sum(c["status"] == "compatibility_metadata_added" for c in checks) == 149


@pytest.mark.parametrize(
    "field,value",
    [
        ("image", "other/repo:latest"),
        ("image", "swebench/sweb.eval.x86_64.repo_1776_task-1:changed"),
        ("log_parser", "new_unpinned_parser"),
        ("eval_type", "always_pass"),
        ("eval_script", ""),
        ("image_assets", {"test_patch": [{"path": "baseline.png"}]}),
    ],
)
def test_invalid_added_metadata_cannot_admit_a_task(inputs, field, value):
    rows, selected, assets, bridge = inputs
    task_id = selected["tasks"][0]["task_id"]
    assets[task_id][field] = value
    changed, checks = bridge()
    assert checks[0]["status"] == "incompatible_task_assets"
    assert next(r for r in changed if r["instance_id"] == task_id) == next(
        r for r in rows if r["instance_id"] == task_id
    )


def test_unselected_asset_drift_does_not_change_membership_or_rows(inputs):
    rows, selected, assets, bridge = inputs
    task_id = selected["partitions"]["not_selected"][0]
    assets[task_id]["PASS_TO_PASS"] = ["changed outside the cohort"]
    changed, checks = bridge()
    assert all(c["status"] == "compatibility_metadata_added" for c in checks)
    assert cohort.build_cohort(changed) == selected
    assert next(r for r in changed if r["instance_id"] == task_id) == next(
        r for r in rows if r["instance_id"] == task_id
    )


def test_injected_schema_or_repartitioning_is_rejected(inputs):
    rows, selected, _, bridge = inputs
    altered = deepcopy(selected)
    altered["tasks"].pop()
    with pytest.raises(cohort.CohortError, match="cohort differs"):
        bridge(selection=altered)
    injected = deepcopy(rows)
    task_id = selected["tasks"][0]["task_id"]
    next(r for r in injected if r["instance_id"] == task_id)["eval_script"] = "injected"
    changed, checks = bridge(data=injected)
    assert checks[0]["status"] == "incompatible_task_assets"
    assert (
        next(r for r in changed if r["instance_id"] == task_id)["eval_script"]
        == "injected"
    )


def test_selected_file_drift_and_escaping_asset_paths_fail_before_use(
    inputs, tmp_path, monkeypatch
):
    _, selected, _, _ = inputs
    (tmp_path / "sweb.yaml").write_text("datasets: {}\n")
    parts = ["sweb.yaml"]
    for task in selected["tasks"]:
        directory = tmp_path / "tasks" / task["task_id"]
        directory.mkdir(parents=True)
        (directory / "eval.sh").write_text("# frozen\n")
        parts.append("tasks/" + task["task_id"])
    files = cohort.source_closure(tmp_path, parts)
    monkeypatch.setattr(
        compatibility, "TASK_ASSET_CLOSURE_SHA256", cohort.digest(files)
    )
    compatibility.verify_task_assets(tmp_path, selected)
    script = tmp_path / "tasks" / selected["tasks"][0]["task_id"] / "eval.sh"
    script.write_text("# changed\n")
    with pytest.raises(cohort.CohortError, match="assets differ"):
        compatibility.verify_task_assets(tmp_path, selected)
    outside = tmp_path.parent / "outside-task-asset"
    outside.write_text("# outside\n")
    script.unlink()
    script.symlink_to(outside)
    with pytest.raises(cohort.CohortError, match="escapes"):
        compatibility.verify_task_assets(tmp_path, selected)
