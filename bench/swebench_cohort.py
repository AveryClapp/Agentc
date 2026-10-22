"""Bind the frozen SWE-bench cohort and check its actual evaluator interface.

This preflight has no model or task-execution path. Its public task inventory
contains only agent inputs; evaluator probes run against the private dataset.
Every assigned task remains in the denominator when prerequisites fail.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import re
import sys
from collections import Counter
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from bench.campaign_accounting import digest
from bench.coding_submission import private_write
from bench.guard_evidence import file_sha
from bench.joint_campaign import ordered_id_digest

WORKLOAD = "swebench-verified"
PROTOCOL_SHA256 = "82943dbb351f9154f2e3a75f01d8659d590c9a7e45797725ba0cdfbced054791"
DATASET_REVISION = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"
DATASET_SHA256 = "a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd"
ORDERED_IDS_SHA256 = "5f649dc4518274c7a605cce1a5dcbf451eb1e14cba30a6dc2a032b84731ca9e4"
AGENT_REVISION = "0f3acafacabc0def8cc76b4e48acb4b6cf302cb9"
AGENT_CONFIG_SHA256 = "188b6c75b632c88ed53c78125642066f0c0836c57d00b5deadd9a6d01857447d"
AGENT_CLOSURE_SHA256 = (
    "f1106500e5005221184f290cf962e4b017fdb16547385aaa091cc6c1869376ff"
)
EVALUATOR_REVISION = "02e7a74ffd0b707aab73d203fe87bdc7c76afc8e"
EVALUATOR_CLOSURE_SHA256 = (
    "23a49aa495e75a9e69b48e44dcc3fd77978b6645d7eeef48b2a3212725b03bf3"
)
PUBLIC_FIELDS = frozenset(
    {"instance_id", "repo", "base_commit", "problem_statement", "version"}
)
EVALUATOR_FIELDS = frozenset(
    {
        "instance_id",
        "image",
        "repo",
        "version",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
        "log_parser",
        "eval_type",
        "eval_script",
    }
)
PARTITIONS = {"C": (0, 30), "P": (30, 60), "T": (60, 150), "not_selected": (150, 500)}


class CohortError(RuntimeError):
    """A frozen input or prerequisite differs from the enrolled cohort."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CohortError(message)


def source_closure(root: Path, parts: Sequence[str]) -> dict[str, str]:
    files = {}
    for name in parts:
        path = root / name
        require(path.exists(), f"missing frozen source input: {name}")
        for item in [path] if path.is_file() else sorted(path.rglob("*")):
            if item.is_file() and "__pycache__" not in item.parts:
                require(
                    item.resolve().is_relative_to(root.resolve()),
                    "source input escapes its frozen directory",
                )
                files[item.relative_to(root).as_posix()] = file_sha(item)
    return files


def verify_sources(agent: Path, evaluator: Path) -> dict[str, Any]:
    agent_files = source_closure(
        agent, ("sweagent", "config", "tools", "pyproject.toml")
    )
    evaluator_files = source_closure(evaluator, ("swebench", "pyproject.toml"))
    require(
        digest(agent_files) == AGENT_CLOSURE_SHA256, "frozen SWE-agent source differs"
    )
    require(
        agent_files.get("config/default.yaml") == AGENT_CONFIG_SHA256,
        "frozen SWE-agent default configuration differs",
    )
    require(
        digest(evaluator_files) == EVALUATOR_CLOSURE_SHA256,
        "frozen SWE-bench evaluator source differs",
    )
    return {
        "agent": {
            "revision": AGENT_REVISION,
            "files": agent_files,
            "closure_sha256": digest(agent_files),
        },
        "evaluator": {
            "revision": EVALUATOR_REVISION,
            "files": evaluator_files,
            "closure_sha256": digest(evaluator_files),
        },
    }


def split_key(task_id: str) -> bytes:
    return sha256(f"agentc-mlsys2027-v1\0{WORKLOAD}\0{task_id}".encode()).digest()


def build_cohort(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    require(len(rows) == 500, "frozen SWE-bench universe must contain 500 rows")
    by_id = {}
    for row in rows:
        require(isinstance(row, Mapping), "invalid dataset row")
        task = {field: row.get(field) for field in sorted(PUBLIC_FIELDS)}
        require(
            all(isinstance(value, str) and value for value in task.values())
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task["instance_id"])
            and re.fullmatch(r"[0-9a-f]{40}", task["base_commit"]),
            "dataset row lacks a canonical public task identity",
        )
        require(task["instance_id"] not in by_id, "duplicate canonical task ID")
        by_id[task["instance_id"]] = task
    ordered = sorted(by_id, key=lambda task_id: (split_key(task_id), task_id))
    require(
        ordered_id_digest(ordered) == ORDERED_IDS_SHA256,
        "frozen task ordering or universe differs",
    )
    partitions = {
        stage: ordered[start:end] for stage, (start, end) in PARTITIONS.items()
    }
    tasks = [
        {
            "task_id": task_id,
            "stage": stage,
            "public_task": by_id[task_id],
            "public_task_sha256": digest(by_id[task_id]),
        }
        for stage, ids in partitions.items()
        if stage != "not_selected"
        for task_id in ids
    ]
    return {
        "schema_version": 1,
        "record_type": "frozen_swebench_cohort",
        "workload_id": WORKLOAD,
        "protocol_sha256": PROTOCOL_SHA256,
        "dataset_revision": DATASET_REVISION,
        "dataset_sha256": DATASET_SHA256,
        "ordered_ids_sha256": ordered_id_digest(ordered),
        "partitions": partitions,
        "tasks": tasks,
        "repository_counts": {
            group: dict(sorted(Counter(by_id[t]["repo"] for t in ids).items()))
            for group, ids in {
                "selected": ordered[:150],
                "not_selected": ordered[150:],
            }.items()
        },
        "assigned_task_count": 150,
        "model_cost_ceiling_usd_per_instance": "2.00",
        "environment_admission_completed": False,
        "paper_evidence": False,
    }


def evaluator_inputs(
    rows: Sequence[Mapping[str, Any]], cohort: dict, make_spec
) -> list[dict]:
    """Probe the pinned function; a failed row is retained, never filtered."""
    by_id = {row["instance_id"]: row for row in rows}
    results = []
    for task in cohort["tasks"]:
        row = by_id[task["task_id"]]
        require(
            digest({field: row[field] for field in PUBLIC_FIELDS})
            == task["public_task_sha256"],
            "evaluator row differs from its public task",
        )
        try:
            spec = make_spec(deepcopy(row))
        except (KeyError, TypeError, ValueError) as error:
            result = {
                "status": "incompatible_dataset_row",
                "error_type": type(error).__name__,
                "missing_field": error.args[0] if isinstance(error, KeyError) else None,
            }
        else:
            require(
                spec.instance_id == task["task_id"], "evaluator changed task identity"
            )
            result = {
                "status": "test_spec_created",
                "eval_script_sha256": sha256(spec.eval_script.encode()).hexdigest(),
            }
            if hasattr(spec, "image"):
                require(
                    "image" in row and spec.image == row["image"],
                    "evaluator changed task image",
                )
                result["image"] = spec.image
        results.append(
            {
                "task_id": task["task_id"],
                "stage": task["stage"],
                **result,
                "missing_required_fields": sorted(EVALUATOR_FIELDS - row.keys()),
                "environment_admitted": False,
                "official_score": None,
            }
        )
    return results


def load_evaluator(source: Path):
    package = source.resolve() / "swebench"
    for name, module in tuple(sys.modules.items()):
        if name == "swebench" or name.startswith("swebench."):
            path = getattr(module, "__file__", None)
            require(
                path is not None and Path(path).resolve().is_relative_to(package),
                "a foreign SWE-bench evaluator is already loaded",
            )
    sys.path.insert(0, str(source.resolve()))
    try:
        module = importlib.import_module("swebench.harness.utils")
        require(
            Path(module.__file__).resolve().is_relative_to(package),
            "evaluator import escaped the frozen source",
        )
        return module.make_test_spec
    finally:
        sys.path.remove(str(source.resolve()))


def run(
    *,
    dataset: Path,
    agent: Path,
    evaluator: Path,
    output: Path,
    task_assets: Path | None = None,
    pin_images: bool = False,
    image_pins: Path | None = None,
) -> dict:
    require(not output.exists(), "cohort preflight requires a fresh output directory")
    require(
        not (pin_images or image_pins is not None) or task_assets is not None,
        "image pins require task assets",
    )
    protocol = Path(__file__).parent / "repro/mlsys-2027-evaluation-protocol.md"
    require(file_sha(protocol) == PROTOCOL_SHA256, "frozen protocol differs")
    require(file_sha(dataset) == DATASET_SHA256, "frozen dataset differs")
    sources = verify_sources(agent, evaluator)
    import pyarrow.parquet as parquet

    rows = parquet.read_table(dataset).to_pylist()
    cohort = build_cohort(rows)
    bridge = None
    evaluator_rows = rows
    if task_assets is not None:
        from bench.swebench_compatibility import apply_bridge

        evaluator_rows, bridge = apply_bridge(rows, cohort, task_assets, evaluator)
    images = None
    if image_pins is not None:
        from bench.swebench_images import verify_image_pins

        images = verify_image_pins(json.loads(image_pins.read_text()), bridge)
    if pin_images:
        from bench.swebench_images import capture_images

        images = capture_images(bridge, previous=images)
    if images is not None:
        by_id = {row["instance_id"]: row for row in evaluator_rows}
        for check in images["checks"]:
            if check["status"] == "immutable_image_verified":
                by_id[check["task_id"]]["image"] = check["immutable_image"]
    checks = evaluator_inputs(evaluator_rows, cohort, load_evaluator(evaluator))
    require(file_sha(dataset) == DATASET_SHA256, "dataset changed during enrollment")
    require(
        verify_sources(agent, evaluator) == sources, "source changed during enrollment"
    )
    if bridge is not None:
        from bench.swebench_compatibility import verify_task_assets

        require(
            verify_task_assets(task_assets, cohort) == bridge["sources"],
            "task assets changed during enrollment",
        )
    compatible = sum(row["status"] == "test_spec_created" for row in checks)
    images_incomplete = (
        images is not None and images["immutable_images_verified"] != 150
    )
    report = {
        "schema_version": 1,
        "record_type": "frozen_swebench_prerequisite_audit",
        "status": (
            "incompatible_evaluator_inputs"
            if compatible != len(checks)
            else "image_pins_incomplete"
            if images_incomplete
            else "evaluator_inputs_compatible"
        ),
        "cohort_sha256": digest(cohort),
        "source_sha256": digest(sources),
        "checks_sha256": digest(checks),
        "compatibility_bridge_sha256": digest(bridge) if bridge is not None else None,
        "image_pins_sha256": digest(images) if images is not None else None,
        "immutable_images_verified": (
            images["immutable_images_verified"] if images is not None else 0
        ),
        "image_pins_incomplete": images_incomplete,
        "assigned_task_count": len(checks),
        "compatible_evaluator_rows": compatible,
        "incompatible_evaluator_rows": len(checks) - compatible,
        "environment_admission_completed": False,
        "provider_acquisition_ready": False,
        "physical_provider_calls": 0,
        "task_environments_started": 0,
        "paper_evidence": False,
        "runtime": {
            "python_version": sys.version,
            "preflight_sha256": file_sha(Path(__file__)),
            "compatibility_bridge_sha256": (
                file_sha(Path(__file__).with_name("swebench_compatibility.py"))
                if bridge is not None
                else None
            ),
            "image_resolver_sha256": (
                file_sha(Path(__file__).with_name("swebench_images.py"))
                if images is not None
                else None
            ),
            "distributions": sorted(
                (d.metadata["Name"], d.version)
                for d in importlib.metadata.distributions()
            ),
        },
    }
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, artifact in (
        ("public-cohort.json", cohort),
        ("sources.json", sources),
        ("evaluator-checks.json", checks),
        ("summary.json", report),
    ):
        private_write(output / name, artifact)
    if bridge is not None:
        private_write(output / "compatibility-bridge.json", bridge)
    if images is not None:
        private_write(output / "image-pins.json", images)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dataset", "agent", "evaluator", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--task-assets", type=Path)
    parser.add_argument("--pin-images", action="store_true")
    parser.add_argument("--image-pins", type=Path)
    args = parser.parse_args(argv)
    report = run(
        **{
            name: getattr(args, name).resolve()
            for name in ("dataset", "agent", "evaluator", "output")
        },
        task_assets=args.task_assets.resolve()
        if args.task_assets is not None
        else None,
        pin_images=args.pin_images,
        image_pins=args.image_pins.resolve() if args.image_pins is not None else None,
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "assigned_task_count",
                    "compatible_evaluator_rows",
                    "immutable_images_verified",
                    "provider_acquisition_ready",
                    "physical_provider_calls",
                )
                if key in report
            }
        )
    )
    return (
        2
        if report["incompatible_evaluator_rows"] or report.get("image_pins_incomplete")
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
