"""Actual mini-SWE-agent submission round trips into pinned Requests grading.

Scripted gold, inapplicable and step-limit controls are engineering diagnostics.
They make no provider calls and cannot establish agent quality or savings.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from bench.coding_submission import MARKER, private_write
from bench.coding_task_worker import PUBLIC_FIELDS, run_task, source_manifest
from bench.requests_fixture_preflight import (
    BASE_COMMIT,
    DATASET_SHA256,
    TASK,
    TASK_IMAGE,
    TASK_IMAGE_ID,
    run_preflight,
)


@dataclass
class ScriptedConfig:
    model_name: str


class ScriptedModel:
    def __init__(self, name, actions):
        self.config = ScriptedConfig(name)
        self.actions, self.n_calls, self.cost = actions, 0, 0.0

    def query(self, messages, **kwargs):
        action = self.actions[self.n_calls]
        self.n_calls += 1
        return {
            "content": "THOUGHT: scripted E0 control.\n```bash\n" + action + "\n```"
        }


def run(*, source: Path, dataset: Path, wheels: Path, output: Path) -> dict:
    import pyarrow.parquet as parquet

    if hashlib.sha256(dataset.read_bytes()).hexdigest() != DATASET_SHA256:
        raise RuntimeError("task dataset differs from the original pin")
    upstream = source_manifest(source)
    rows = [
        row
        for row in parquet.read_table(dataset).to_pylist()
        if row["instance_id"] == TASK
    ]
    if len(rows) != 1 or rows[0]["base_commit"] != BASE_COMMIT:
        raise RuntimeError("original Requests task is missing or changed")
    row = rows[0]
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    public = {field: row[field] for field in PUBLIC_FIELDS}
    gold = base64.b64encode(row["patch"].encode()).decode()
    apply_gold = (
        "printf '%s' " + shlex.quote(gold) + " | base64 -d | git apply -"
        # Exercise a diff whose context overlaps fixture trust configuration.
        " && printf '\\n# E0 submitted trust-data comment\\n' >> requests/cacert.pem"
    )
    final = (
        "printf '%s\\n' "
        + shlex.quote(MARKER)
        + " && git --no-pager diff --binary HEAD"
    )
    bad_patch = (
        "diff --git a/requests/auth.py b/requests/auth.py\n"
        "--- a/requests/auth.py\n+++ b/requests/auth.py\n@@ -1 +1 @@\n"
        "-not-present-in-the-frozen-base\n+replacement\n"
    )
    bad_output = base64.b64encode((MARKER + "\n" + bad_patch).encode()).decode()
    controls = {
        "gold_roundtrip": (
            ScriptedModel("scripted-gold-roundtrip", [apply_gold, final]),
            3,
        ),
        "inapplicable_submission": (
            ScriptedModel(
                "scripted-inapplicable-submission",
                ["printf '%s' " + shlex.quote(bad_output) + " | base64 -d"],
            ),
            2,
        ),
        "step_limit_with_unsubmitted_edits": (
            ScriptedModel("scripted-step-limit", [apply_gold]),
            1,
        ),
    }
    submissions = []
    acquisition = {}
    for name, (model, step_limit) in controls.items():
        path = output / name
        record = run_task(
            source=source,
            public_task=public,
            model=model,
            output=path,
            image=TASK_IMAGE,
            image_id=TASK_IMAGE_ID,
            dataset_sha256=DATASET_SHA256,
            step_limit=step_limit,
            acquisition_kind="scripted_e0_submission_control",
        )
        acquisition[name] = {
            "upstream_status": record["upstream_status"],
            "serialization_changed": record["serialization_changed"],
            "patch_sha256": record["patch_sha256"],
            "model_queries": model.n_calls,
        }
        submissions.append(path / "submission.json")
    assert acquisition["gold_roundtrip"]["serialization_changed"] is True
    assert (
        acquisition["step_limit_with_unsubmitted_edits"]["upstream_status"]
        == "LimitsExceeded"
    )
    grading = run_preflight(
        dataset, output / "grading", wheels, submissions=submissions
    )
    assert (
        grading["fixture_admitted"]
        and not grading["candidate_grades_blocked_by_environment"]
    )
    gold_grade, bad_grade, limit_grade = grading["candidate_grades"]
    assert gold_grade["resolved"] and gold_grade["complete"]
    assert bad_grade["status"] == "patch_apply_failed" and not bad_grade["resolved"]
    assert limit_grade["complete"] and not limit_grade["resolved"]
    assert limit_grade["submission"]["upstream_status"] == "LimitsExceeded"
    report = {
        "schema_version": 1,
        "stage": "E0",
        "paper_evidence": False,
        "provider_calls": 0,
        "original_campaign_journal_accessed": False,
        "purpose": "scripted gold/invalid/unsubmitted engineering controls; not agent efficacy",
        "upstream": upstream,
        "acquisition": acquisition,
        "fixture_admitted": grading["fixture_admitted"],
        "candidate_grades": [
            {k: grade[k] for k in ("status", "complete", "resolved", "submission")}
            for grade in grading["candidate_grades"]
        ],
    }
    private_write(output / "summary.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--pytest-wheels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    report = run(
        source=args.source.resolve(),
        dataset=args.dataset.resolve(),
        wheels=args.pytest_wheels.resolve(),
        output=args.output.resolve(),
    )
    print(
        json.dumps(
            {"fixture_admitted": report["fixture_admitted"], "provider_calls": 0}
        )
    )


if __name__ == "__main__":
    main()
