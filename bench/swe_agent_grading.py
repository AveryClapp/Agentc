"""Grade captured frozen SWE-agent submissions with the actual pinned evaluator."""

from __future__ import annotations

import importlib
import resource
import time
import uuid
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from bench.campaign_accounting import digest, json_object, money, read_snapshot
from bench.coding_submission import private_write
from bench.guard_evidence import file_sha
from bench.swe_agent_task_worker import execution_version, load_submission
from bench.swebench_cohort import load_evaluator, require
from bench.swebench_environment import (
    _GRADER_LOCK,
    _LocalImages,
    _OwnedContainers,
    reference_verdict,
    verify_environment_report,
)
from bench.swe_requests_fixture import RequestsHTTPFixture, TASK as REQUESTS_TASK


class _MeasuredContainer:
    def __init__(self, container):
        self.container, self.measurement = container, None

    def __getattr__(self, name):
        return getattr(self.container, name)

    def _capture(self):
        if self.measurement is not None:
            return
        from bench.process_resources import container_usage

        try:
            self.measurement = container_usage(self.container)
        except Exception:
            self.measurement = {
                "container_id": self.container.id,
                "cpu_seconds": None,
                "peak_memory_bytes": None,
                "peak_memory_available": False,
                "unavailable_reason": "container_counters_unavailable_before_cleanup",
            }

    def stop(self, *args, **kwargs):
        self._capture()
        return self.container.stop(*args, **kwargs)

    def remove(self, *args, **kwargs):
        self._capture()
        return self.container.remove(*args, **kwargs)


def grade_task(
    *,
    inputs,
    environment_report: Path,
    bundle,
    ledger: Path,
    submission_path: Path,
    output: Path,
    timeout: int = 600,
    client=None,
    measure_resources=False,
):
    import docker

    require(type(timeout) is int and 0 < timeout <= 1800, "invalid grading timeout")
    require(not output.exists(), "grading output already exists; no resume")
    inputs.verify()
    report = verify_environment_report(inputs, environment_report)
    bundle.verify()
    submission = load_submission(submission_path)
    task_id = submission["task_id"]
    task = next((t for t in inputs.cohort["tasks"] if t["task_id"] == task_id), None)
    require(
        task is not None
        and any(
            c["task_id"] == task_id and c["environment_admitted"]
            for c in report["checks"]
        ),
        "submitted task lacks admitted frozen environment",
    )
    pin = next(p for p in inputs.pins["checks"] if p["task_id"] == task_id)
    provenance = submission["provenance"]
    require(
        provenance["public_task_sha256"] == task["public_task_sha256"]
        and submission["base_commit"] == task["public_task"]["base_commit"]
        and provenance["cohort_sha256"] == digest(inputs.cohort)
        and provenance["image_id"] == pin["config_digest"]
        and provenance["immutable_image"] == pin["immutable_image"]
        and provenance["execution_version"] == execution_version(bundle)
        and provenance["environment_report_sha256"]
        == file_sha(environment_report / "summary.json"),
        "submission differs from frozen execution inputs",
    )
    snapshot = read_snapshot(ledger)
    require(
        any(
            e["event"] == "campaign_session"
            and e["session_id"] == provenance["transport_session_id"]
            for e in snapshot.events
        ),
        "submission lacks an owned accounting session",
    )
    claims = [
        e
        for e in snapshot.events
        if e["event"] == "reserve"
        and e.get("session_id") == provenance["transport_session_id"]
        and e.get("metadata", {}).get("cell_id") == provenance["cell_id"]
    ]
    require(
        all(e["id"] in snapshot.state["results"] for e in claims),
        "task contains incomplete provider calls",
    )
    require(
        (claims or submission["budget_exhausted"])
        and all(
            e["metadata"]["task_id"] == task_id
            and e["metadata"]["arm"] == provenance["arm"]
            and e["metadata"]["requested_model"] == submission["model_name_or_path"]
            and e["metadata"]["actor"] == "agent"
            for e in claims
        ),
        "provider calls differ from task identity",
    )
    total = sum(
        (money(snapshot.state["results"][e["id"]]["cost_usd"]) for e in claims),
        Decimal(0),
    )
    state = json_object((submission_path.parent / "cell-accounting.json").read_bytes())
    require(
        total == money(state["committed_usd"])
        and total <= 2
        and money(state["pending_upper_usd"]) == 0,
        "task accounting differs from original receipts",
    )
    require(
        _GRADER_LOCK.acquire(blocking=False), "another frozen grader session is active"
    )
    own_client = client is None
    containers, runner, previous_root, previous_cleanup = None, None, None, None
    fixture = None
    measured = []
    resource_started = time.monotonic()
    usage_started = resource.getrusage(resource.RUSAGE_SELF)
    children_started = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        if own_client:
            client = docker.from_env(timeout=60)
        image = client.images.get(pin["config_digest"])
        require(
            image.id == pin["config_digest"]
            and image.attrs["Architecture"] == "amd64"
            and image.attrs["Os"] == "linux",
            "grading image differs from immutable pin",
        )
        make_spec = load_evaluator(inputs.evaluator)
        runner = importlib.import_module("swebench.harness.run_evaluation")
        grading = importlib.import_module("swebench.harness.grading")
        previous_root = runner.RUN_EVALUATION_LOG_DIR
        output.mkdir(mode=0o700)
        fixture_recipe = report.get("http_fixtures", {}).get(task_id)
        if fixture_recipe is not None:
            require(task_id == REQUESTS_TASK, "HTTP fixture belongs to another task")
            fixture = RequestsHTTPFixture(client, output / "http-fixture")
            fixture.__enter__()
        runner.RUN_EVALUATION_LOG_DIR = output / "raw"
        if measure_resources:
            previous_cleanup = runner.cleanup_container

            def measured_cleanup(client, container, logger):
                # The frozen evaluator uses Docker CLI cleanup. Observe the
                # counters first, then invoke the original cleanup intact.
                if container is not None:
                    owned = next((c for c in measured if c.id == container.id), None)
                    require(
                        owned is not None, "grader cleanup escaped its owned container"
                    )
                    owned._capture()
                return previous_cleanup(client, container, logger)

            runner.cleanup_container = measured_cleanup
        row = deepcopy(inputs.private_rows[task_id])
        row["image"] = pin["config_digest"]
        spec = make_spec(row)
        prediction = {
            "instance_id": task_id,
            "model_name_or_path": submission["model_name_or_path"],
            "model_patch": submission["model_patch"],
        }
        require(
            spec.instance_id == task_id and spec.image == pin["config_digest"],
            "grader changed task/image identity",
        )
        private_write(
            output / "enrollment.json",
            {
                "submission_sha256": file_sha(submission_path),
                "prediction": prediction,
                "eval_script_sha256": sha256(spec.eval_script.encode()).hexdigest(),
                "grading_harness_sha256": file_sha(Path(__file__)),
                "frozen_sources_sha256": digest(inputs.sources),
                "ledger_sha256": snapshot.sha256,
                "environment_report_sha256": provenance["environment_report_sha256"],
            },
        )
        run_id = "agentc-swe-grade-" + uuid.uuid4().hex
        log_dir = (
            output
            / "raw"
            / run_id
            / prediction["model_name_or_path"].replace("/", "__")
            / task_id
        )
        container_client = client
        if measure_resources:

            def create(**kwargs):
                container = _MeasuredContainer(client.containers.create(**kwargs))
                measured.append(container)
                return container

            container_client = SimpleNamespace(
                containers=SimpleNamespace(
                    create=create,
                    get=client.containers.get,
                )
            )
        containers = _OwnedContainers(
            container_client, row["base_commit"], runner.CONTAINER_WORKDIR, fixture
        )
        local_client = SimpleNamespace(
            images=_LocalImages(client.images), containers=containers
        )
        result = runner.run_instance(
            spec,
            prediction,
            local_client,
            run_id,
            timeout=timeout,
            skip_patch=prediction["model_patch"] == "",
            task_repo=str(inputs.task_assets),
        )
        containers.drain()
        require(
            len(containers.created) == 1 and containers.created[0].base_verified,
            "grader did not execute a verified fresh task base",
        )
        log = log_dir / runner.LOG_TEST_OUTPUT
        require(
            result is not None and log.is_file(),
            "grader did not produce actual test output",
        )
        observed, found = grading.get_logs_eval(spec, str(log))
        official = grading.get_eval_report(spec, prediction, str(log), True)[task_id]
        require(
            result == (task_id, {task_id: official}),
            "grader result differs from raw test logs",
        )
        verdict = reference_verdict(spec, observed, found, official, grading)
        if fixture is not None:
            fixture.close()
        grade = {
            "schema_version": 1,
            "task_id": task_id,
            "submission_sha256": file_sha(submission_path),
            "patch_sha256": submission["patch_sha256"],
            "official": official,
            "official_score": float(official["resolved"])
            if verdict["complete"]
            else None,
            "protocol_score": 0.0
            if submission["budget_exhausted"]
            else float(official["resolved"])
            if verdict["complete"]
            else None,
            "reference_tests_complete": verdict["complete"],
            "preservation_passed": verdict["preservation_passed"],
            "actual_test_exit_code": grading.parse_test_exit_code(log.read_text()),
            "all_provider_calls": len(claims),
            "all_provider_cost_usd": str(total),
            "raw_artifacts": {
                p.relative_to(output).as_posix(): file_sha(p)
                for p in sorted(log_dir.rglob("*"))
                if p.is_file()
            },
            "http_fixture": fixture_recipe,
            "http_fixture_resources": fixture.resources
            if fixture is not None
            else None,
            "http_fixture_artifacts": {
                p.relative_to(output).as_posix(): file_sha(p)
                for p in fixture.output.iterdir()
                if p.is_file() and p.name != "key.pem"
            }
            if fixture is not None
            else {},
        }
        if measure_resources:
            current = resource.getrusage(resource.RUSAGE_SELF)
            children = resource.getrusage(resource.RUSAGE_CHILDREN)
            grade["resource_measurements"] = {
                "wall_seconds": time.monotonic() - resource_started,
                "host_user_cpu_seconds": current.ru_utime - usage_started.ru_utime,
                "host_system_cpu_seconds": current.ru_stime - usage_started.ru_stime,
                "containers": [c.measurement for c in measured],
                "all_container_counters_available": bool(measured)
                and all(
                    c.measurement is not None
                    and c.measurement.get("cpu_seconds") is not None
                    and c.measurement.get("peak_memory_available") is True
                    for c in measured
                ),
                "reaped_child_user_cpu_seconds": children.ru_utime
                - children_started.ru_utime,
                "reaped_child_system_cpu_seconds": children.ru_stime
                - children_started.ru_stime,
                "scope": "official grading only; provider and task-process work separate",
            }
        inputs.verify()
        require(
            file_sha(submission_path) == grade["submission_sha256"],
            "submission changed during grading",
        )
        private_write(output / "grade.json", grade)
        return grade
    finally:
        try:
            if containers is not None:
                containers.drain()
        finally:
            try:
                if fixture is not None:
                    fixture.close()
            finally:
                if runner is not None and previous_root is not None:
                    runner.RUN_EVALUATION_LOG_DIR = previous_root
                if runner is not None and previous_cleanup is not None:
                    runner.cleanup_container = previous_cleanup
                try:
                    if own_client and client is not None:
                        client.close()
                finally:
                    _GRADER_LOCK.release()
