"""Exact upstream patch reads and terminal outcomes, including budget failure."""

from copy import deepcopy
from hashlib import sha256

import pytest

from bench.swe_agent_task_worker import capture_submission, load_submission
from bench.coding_submission import private_write
from bench.swebench_cohort import CohortError

TASK = {"instance_id": "fixture__task-1", "base_commit": "a" * 40}


def captured(patch="diff --git a/x b/x\n+literal\n", status="submitted"):
    events = [
        {"event": "step_start"},
        {"event": "file_read", "path": "/root/model.patch", "content": patch},
        {
            "event": "step_done",
            "step": {"done": True, "submission": patch, "exit_status": status},
            "info": {"submission": patch, "exit_status": status},
        },
    ]
    info = {"submission": patch, "exit_status": status}
    return events, info


def record(events, info, budget=False):
    return capture_submission(
        task=TASK,
        model="fixture/model",
        info=info,
        events=events,
        provenance={"literal": "fixture"},
        budget_exhausted=budget,
    )


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\n\n", "", " \n"])
def test_submission_preserves_the_exact_upstream_read_text(ending):
    patch = "diff --git a/x b/x\n+literal" + ending
    events, info = captured(patch)
    before = deepcopy(events)
    result = record(events, info)
    assert events == before
    assert result["model_patch"] == patch and result["upstream_submission"] == patch
    assert result["patch_sha256"] == sha256(patch.encode()).hexdigest()


@pytest.mark.parametrize(
    "change", ["missing", "changed", "stale", "unfinished", "status", "info"]
)
def test_patch_requires_a_matching_read_in_the_terminal_step(change):
    events, info = captured()
    if change == "missing":
        events.pop(1)
    elif change == "changed":
        events[1]["content"] = "different"
    elif change == "stale":
        events.insert(2, {"event": "step_start"})
    elif change == "unfinished":
        events[-1]["step"]["done"] = False
    elif change == "info":
        events[-1]["info"]["submission"] = "different"
    else:
        info["exit_status"] = "other"
    with pytest.raises(CohortError):
        record(events, info)


def test_budget_failure_is_retained_without_submitting_prior_worktree_content():
    events, info = captured(status="review")
    events[-1]["step"]["done"] = False
    events.append({"event": "budget_exhausted"})
    result = record(
        events, {"submission": None, "exit_status": "cell_budget_exhausted"}, True
    )
    assert result["task_status"] == "failed" and result["budget_exhausted"]
    assert (
        result["model_patch"] == ""
        and result["terminal_patch_read_event_index"] is None
    )


def test_non_submission_terminal_result_does_not_grade_an_earlier_patch_read():
    events, info = captured()
    info = {"submission": None, "exit_status": "exit_command"}
    events[-1]["step"].update(info)
    events[-1]["info"].update(info)
    result = record(events, info)
    assert result["task_status"] == "failed" and result["model_patch"] == ""


def test_saved_submission_replays_its_events_and_rejects_tampering(tmp_path):
    events, info = captured()
    result = record(events, info)
    private_write(tmp_path / "submission.json", result)
    private_write(
        tmp_path / "enrollment.json",
        {
            "public_task": TASK,
            "agent_configuration": {"model": {"name": "fixture/model"}},
            "provenance": {"literal": "fixture"},
        },
    )
    private_write(tmp_path / "cell-accounting.json", {"budget_exhausted": False})
    import json

    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events)
    )
    assert load_submission(tmp_path / "submission.json") == result
    result["model_patch"] += "invented\n"
    (tmp_path / "submission.json").write_text(json.dumps(result))
    with pytest.raises(CohortError):
        load_submission(tmp_path / "submission.json")
