"""Sealed denominators, billing, seed joins and unresolved native evidence."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from bench.campaign_accounting import digest


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))


@pytest.fixture
def campaign(tmp_path):
    root = tmp_path / "campaign"
    acq = root / "acquisition"
    store = root / "store"
    store.mkdir(parents=True)
    cells = [
        dict(
            workload_id="frozen",
            task_id="unavailable",
            arm="joint_guarded",
            repetition=0,
            run_seed=1,
            ordinal=0,
        ),
        dict(
            workload_id="frozen",
            task_id="available",
            arm="joint_guarded",
            repetition=0,
            run_seed=2,
            ordinal=1,
        ),
    ]
    enrollment = {
        "schedule": cells,
        "runtime": {"closure": "fixture"},
        "storage": {"1": str(store)},
    }
    write(acq / "enrollment.json", enrollment)
    results = []
    for cell in cells:
        path = acq / digest(cell)
        calls = []
        if cell["ordinal"]:
            for index, kind, cost in [(0, "primary", "0.001"), (1, "shadow", "0.0002")]:
                request = {"seed": cell["run_seed"], "model": "fixture"}
                plan = {
                    "agentc_observation_context": {
                        "key": {"call_site_version": "version"}
                    },
                    "agentc_selection_audit": {
                        "effective_selected_plan_id": "reference",
                        "reference": {"id": "reference"},
                        "search_status": "cold_call_site",
                        "abstention_reason": "no_admissible_alternative",
                        "selection": {"selected_reference": True},
                    },
                }
                calls.append(
                    {
                        "call_id": str(index),
                        "call_index": index,
                        "attempt_id": str(index),
                        "request": request,
                        "request_digest": digest(request),
                        "metadata": {
                            "cell_id": digest(cell),
                            "task_id": cell["task_id"],
                            "arm": cell["arm"],
                            "kind": kind,
                        },
                        "billed_result": {"cost_usd": cost},
                        "planning": {"native_plan_json": json.dumps(plan)},
                    }
                )
            write(path / "process-result.json", {})
            write(
                path / "runtime-storage.json",
                dict(
                    python_store=str(store),
                    native_store=str(store),
                    environment_store=str(store),
                ),
            )
        result = dict(
            cell_id=digest(cell),
            calls_sha256=digest(calls),
            provider_call_count=len(calls),
            all_call_cost_usd="0.0012" if calls else "0",
        )
        task = (
            result
            | {k: cell[k] for k in ("task_id", "arm", "run_seed", "repetition")}
            | dict(
                schedule_ordinal=cell["ordinal"],
                acquisition_enrollment_sha256=digest(enrollment),
                official_score=0 if calls else None,
            )
        )
        normalized = [
            dict(
                attempt_id=c["attempt_id"],
                request_digest=c["request_digest"],
                cost_usd_exact=c["billed_result"]["cost_usd"],
                eligible=True,
            )
            for c in calls
        ]
        write(path / "calls.json", calls)
        write(path / "result.json", result)
        write(path / "normalized.json", [task, *normalized])
        results.append(result)
    write(
        acq / "completion.json",
        dict(
            enrollment_sha256=digest(enrollment),
            results_sha256=digest(results),
            visited_cells=2,
        ),
    )
    with sqlite3.connect(store / "cost_model.db") as sql:
        sql.execute(
            "CREATE TABLE execution_plan_profile (call_site_version TEXT, execution_plan_id TEXT, n_observations INTEGER, paired_observations INTEGER)"
        )
        sql.execute("INSERT INTO execution_plan_profile VALUES ('version','plan',1,0)")
        sql.execute("CREATE TABLE execution_plan_exploration (status TEXT)")
        sql.execute("INSERT INTO execution_plan_exploration VALUES ('completed')")
    return root


def run_audit(root):
    output = root / "audit.json"
    code = "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from bench.swe_agent_profile_audit import main; raise SystemExit(main(sys.argv[2:]))"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            code,
            str(Path.cwd()),
            str(root),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, output


def test_audit_retains_unknown_denominator_and_all_shadow_spend(campaign):
    result, output = run_audit(campaign)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    counts = report["arm_counts"]["joint_guarded"]
    assert counts["assigned_cells"] == 2 and counts["unknown_score_cells"] == 1
    assert counts["executed_cells"] == 1 and counts["eligible_primary_calls"] == 1
    assert report["physical_spend_usd_by_arm_and_kind"]["joint_guarded"] == {
        "primary": "0.001",
        "shadow": "0.0002",
    }
    assert report["cells"][1]["native_call_site_versions"] == ["version"]
    assert report["cells"][1]["first_selected_alternative_query"] is None
    assert report["paper_evidence"] is False


@pytest.mark.parametrize("reference", [{"id": "reference"}, None])
@pytest.mark.parametrize("after_shadow", [False, True])
def test_selection_uses_effective_identity_and_retains_missing_reference(
    campaign, reference, after_shadow
):
    from copy import deepcopy

    acq = campaign / "acquisition"
    enrollment = json.loads((acq / "enrollment.json").read_text())
    path = acq / digest(enrollment["schedule"][1])
    calls = json.loads((path / "calls.json").read_text())
    selected_call = deepcopy(calls[0]) if after_shadow else calls[0]
    plan = json.loads(selected_call["planning"]["native_plan_json"])
    selection = plan["agentc_selection_audit"]
    selection["selection"] = {"id": "candidate"}
    selection["effective_selected_plan_id"] = "candidate"
    selection["reference"] = reference
    selection["search_status"] = "frozen_policy_not_enumerated"
    selected_call["planning"]["native_plan_json"] = json.dumps(plan)
    if after_shadow:
        selected_call.update(call_id="2", call_index=2, attempt_id="2")
        calls.append(selected_call)
    write(path / "calls.json", calls)
    result = json.loads((path / "result.json").read_text())
    records = json.loads((path / "normalized.json").read_text())
    if after_shadow:
        records.append(records[1] | {"attempt_id": "2"})
        result["provider_call_count"] = records[0]["provider_call_count"] = 3
        result["all_call_cost_usd"] = records[0]["all_call_cost_usd"] = "0.0022"
    result["calls_sha256"] = records[0]["calls_sha256"] = digest(calls)
    write(path / "result.json", result)
    write(path / "normalized.json", records)
    results = [
        json.loads((acq / digest(c) / "result.json").read_text())
        for c in enrollment["schedule"]
    ]
    completion = json.loads((acq / "completion.json").read_text())
    completion["results_sha256"] = digest(results)
    write(acq / "completion.json", completion)
    completed, output = run_audit(campaign)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text())
    assert report["cells"][1]["first_selected_alternative_query"] == (
        int(after_shadow) if reference else None
    )
    counts = report["arm_counts"]["joint_guarded"]
    assert counts.get("selected_alternative_primary_calls", 0) == int(
        reference is not None
    )
    assert counts.get("selection_reference_unavailable_primary_calls", 0) == int(
        reference is None
    )


@pytest.mark.parametrize(
    "change", ["completion", "seed", "billing", "duplicate", "unresolved"]
)
def test_audit_rejects_unsealed_or_mismatched_evidence(campaign, change):
    acq = campaign / "acquisition"
    enrollment = json.loads((acq / "enrollment.json").read_text())
    path = acq / digest(enrollment["schedule"][1])
    if change == "completion":
        completion = json.loads((acq / "completion.json").read_text())
        completion["results_sha256"] = "foreign"
        write(acq / "completion.json", completion)
    elif change == "unresolved":
        with sqlite3.connect(campaign / "store/cost_model.db") as sql:
            sql.execute("INSERT INTO execution_plan_exploration VALUES ('started')")
    elif change == "billing":
        records = json.loads((path / "normalized.json").read_text())
        records[1]["cost_usd_exact"] = "0"
        write(path / "normalized.json", records)
    else:
        calls = json.loads((path / "calls.json").read_text())
        if change == "seed":
            calls[0]["request"]["seed"] = 3
        else:
            calls[1]["call_id"] = calls[0]["call_id"]
        write(path / "calls.json", calls)
        result = json.loads((path / "result.json").read_text())
        records = json.loads((path / "normalized.json").read_text())
        result["calls_sha256"] = records[0]["calls_sha256"] = digest(calls)
        write(path / "result.json", result)
        write(path / "normalized.json", records)
    result, output = run_audit(campaign)
    assert result.returncode != 0 and not output.exists()
