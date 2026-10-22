"""Add pinned evaluator metadata without replacing frozen SWE-bench rows.

Task assets stay in the private grading process. Only source hashes and
diagnostics leave it; the public cohort and agent inputs are unchanged.
This bridge checks schema compatibility, not environment admission.
"""

from __future__ import annotations

import importlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from bench.campaign_accounting import digest
from bench.swebench_cohort import (
    CohortError,
    build_cohort,
    load_evaluator,
    require,
    source_closure,
)

TASK_ASSET_REVISION = "3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d"
TASK_ASSET_ARCHIVE_SHA256 = (
    "ff31aa1f8d2bb78f16559524264a99b868b7218ca82b390dc750bbc59538aa20"
)
TASK_ASSET_CLOSURE_SHA256 = (
    "9106a4693998f10ab0736b79fd39e0de0b9c77b30c952a788e4e82263295f4c5"
)
ORIGINAL_FIELDS = frozenset(
    {
        "instance_id",
        "repo",
        "base_commit",
        "version",
        "created_at",
        "environment_setup_commit",
        "difficulty",
        "patch",
        "test_patch",
        "problem_statement",
        "hints_text",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
    }
)
ADDED_FIELDS = frozenset({"image", "log_parser", "eval_type", "eval_script"})
TEST_FIELDS = frozenset({"FAIL_TO_PASS", "PASS_TO_PASS"})


def verify_task_assets(root: Path, cohort: dict) -> dict[str, Any]:
    """Bind all selected task files, including Dockerfiles and test assets."""
    parts = ["sweb.yaml", *(f"tasks/{t['task_id']}" for t in cohort["tasks"])]
    files = source_closure(root, parts)
    require(
        digest(files) == TASK_ASSET_CLOSURE_SHA256,
        "frozen SWE-bench compatibility assets differ",
    )
    return {
        "repository": "SWE-bench/swe-bench-tasks",
        "revision": TASK_ASSET_REVISION,
        "archive_sha256": TASK_ASSET_ARCHIVE_SHA256,
        "closure_sha256": digest(files),
        "files": files,
    }


def mismatched_fields(original: Mapping, asset: Mapping) -> list[str]:
    """Compare all original fields; preserve the dataset's JSON strings."""
    mismatches = []
    for field in sorted(ORIGINAL_FIELDS):
        if field not in original or field not in asset:
            mismatches.append(field)
            continue
        expected, actual = original[field], asset[field]
        if field in TEST_FIELDS:
            if isinstance(expected, str):
                expected = json.loads(expected)
            require(
                isinstance(expected, list)
                and all(isinstance(test, str) for test in expected),
                "invalid original reference-test list",
            )
        # bools and numbers must not compare equal to another field's type.
        if type(expected) is not type(actual) or expected != actual:
            mismatches.append(field)
    return mismatches


def bridge_rows(
    rows: Sequence[Mapping],
    cohort: dict,
    assets: Path,
    *,
    load_task: Callable,
    parser_names: frozenset[str],
    eval_types: frozenset[str],
) -> tuple[list[dict], list[dict]]:
    """Keep every input row and record every selected task's bridge outcome."""
    require(build_cohort(rows) == cohort, "compatibility cohort differs")
    bridged = deepcopy(list(rows))
    by_id = {row["instance_id"]: row for row in bridged}
    checks = []
    for task in cohort["tasks"]:
        task_id = task["task_id"]
        original = by_id[task_id]
        result = {
            "task_id": task_id,
            "stage": task["stage"],
            "original_row_sha256": digest(original),
            "status": "incompatible_task_assets",
            "environment_admitted": False,
            "official_score": None,
        }
        try:
            require(
                set(original) == ORIGINAL_FIELDS,
                "original dataset schema differs",
            )
            asset = load_task(assets / "tasks" / task_id)
            require(isinstance(asset, Mapping), "invalid task asset row")
            mismatches = mismatched_fields(original, asset)
            result["mismatched_original_fields"] = mismatches
            require(not mismatches, "task assets differ from original dataset row")
            require(
                all(isinstance(asset.get(f), str) and asset[f] for f in ADDED_FIELDS),
                "missing evaluator compatibility metadata",
            )
            require(
                re.fullmatch(
                    r"swebench/sweb\.eval\.x86_64\.[a-z0-9_.-]+:latest",
                    asset["image"],
                )
                is not None,
                "unsupported task image reference",
            )
            require(asset["log_parser"] in parser_names, "unknown frozen log parser")
            require(asset["eval_type"] in eval_types, "unknown frozen evaluation type")
            require(
                not asset.get("image_assets"), "unsupported binary evaluator assets"
            )
            # No official asset field can overwrite a frozen dataset field.
            additions = {field: asset[field] for field in sorted(ADDED_FIELDS)}
            original.update(additions)
            result.update(
                status="compatibility_metadata_added",
                bridged_row_sha256=digest(original),
                image=asset["image"],
                image_pinned=False,
                log_parser=asset["log_parser"],
                eval_type=asset["eval_type"],
                source_eval_script_sha256=digest_script(asset["eval_script"]),
                added_fields=sorted(ADDED_FIELDS),
            )
        except (CohortError, OSError, KeyError, TypeError, ValueError) as error:
            # Content-free diagnostics: exceptions can contain private patches.
            result["error_type"] = type(error).__name__
        checks.append(result)
    return bridged, checks


def digest_script(script: str) -> str:
    from hashlib import sha256

    return sha256(script.encode()).hexdigest()


def apply_bridge(
    rows: Sequence[Mapping], cohort: dict, assets: Path, evaluator: Path
) -> tuple[list[dict], dict]:
    sources = verify_task_assets(assets, cohort)
    load_evaluator(evaluator)  # Reject already imported foreign evaluators.
    repo = importlib.import_module("swebench.task.repo")
    parsers = importlib.import_module("swebench.harness.log_parsers")
    constants = importlib.import_module("swebench.harness.constants")
    bridged, checks = bridge_rows(
        rows,
        cohort,
        assets,
        load_task=repo.load_task,
        parser_names=frozenset(parsers.PARSER_REGISTRY),
        eval_types=frozenset(item.value for item in constants.EvalType),
    )
    require(
        verify_task_assets(assets, cohort) == sources,
        "task assets changed during compatibility verification",
    )
    return bridged, {
        "schema_version": 1,
        "record_type": "frozen_swebench_compatibility_bridge",
        "cohort_sha256": digest(cohort),
        "sources": sources,
        "checks": checks,
        "assigned_task_count": len(checks),
        "compatible_task_assets": sum(
            c["status"] == "compatibility_metadata_added" for c in checks
        ),
        "immutable_images_verified": False,
        "environment_admission_completed": False,
        "provider_acquisition_ready": False,
        "paper_evidence": False,
    }
