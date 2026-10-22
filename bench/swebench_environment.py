"""Run fresh unchanged/gold controls with the actual frozen SWE-bench grader.

The entire selected cohort stays in the report, including unavailable images
and incomplete controls. Only already-local, verified images are executable.
An optional explicitly bounded cache can provision exact pinned images before
controls; the default still uses local images. Private grading rows never
become agent inputs. No model calls are made.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from contextlib import nullcontext

from bench.campaign_accounting import digest
from bench.coding_submission import private_write
from bench.guard_evidence import file_sha
from bench.swebench_cohort import (
    DATASET_SHA256,
    PROTOCOL_SHA256,
    CohortError,
    build_cohort,
    load_evaluator,
    require,
    verify_sources,
)
from bench.swebench_compatibility import apply_bridge, verify_task_assets
from bench.swebench_images import verify_image_pins
from bench.swebench_image_cache import ImageOwnershipError, ProvisioningError
from bench.swe_requests_fixture import (
    FixtureOwnershipError,
    RequestsHTTPFixture,
    TASK as REQUESTS_TASK,
    definition as http_fixture_definition,
)

_GRADER_LOCK = threading.Lock()


class GradingOwnershipError(CohortError):
    """The check cannot proceed while an owned container may still be running."""


@dataclass
class FrozenInputs:
    cohort: dict
    private_rows: dict[str, dict]
    bridge: dict
    pins: dict
    sources: dict
    dataset: Path
    agent: Path
    evaluator: Path
    task_assets: Path
    image_pins: Path
    image_pins_sha256: str

    def verify(self) -> None:
        protocol = Path(__file__).parent / "repro/mlsys-2027-evaluation-protocol.md"
        require(file_sha(protocol) == PROTOCOL_SHA256, "frozen protocol differs")
        require(file_sha(self.dataset) == DATASET_SHA256, "frozen dataset differs")
        require(
            file_sha(self.image_pins) == self.image_pins_sha256,
            "image pin artifact changed",
        )
        require(
            verify_sources(self.agent, self.evaluator) == self.sources,
            "frozen agent/evaluator source changed",
        )
        require(
            verify_task_assets(self.task_assets, self.cohort) == self.bridge["sources"],
            "frozen task assets changed",
        )
        require(
            build_cohort(list(self.private_rows.values())) == self.cohort,
            "in-memory grading cohort changed",
        )
        for check in self.bridge["checks"]:
            require(
                digest(self.private_rows[check["task_id"]])
                == check["bridged_row_sha256"],
                "in-memory private grading row changed",
            )
        verify_image_pins(self.pins, self.bridge)


def load_inputs(
    *, dataset: Path, agent: Path, evaluator: Path, task_assets: Path, image_pins: Path
) -> FrozenInputs:
    require(file_sha(dataset) == DATASET_SHA256, "frozen dataset differs")
    sources = verify_sources(agent, evaluator)
    pin_sha = file_sha(image_pins)
    import pyarrow.parquet as parquet

    rows = parquet.read_table(dataset).to_pylist()
    cohort = build_cohort(rows)
    bridged, bridge = apply_bridge(rows, cohort, task_assets, evaluator)
    require(bridge["compatible_task_assets"] == 150, "task assets are incompatible")
    pins = verify_image_pins(json.loads(image_pins.read_text()), bridge)
    inputs = FrozenInputs(
        cohort=cohort,
        private_rows={r["instance_id"]: r for r in bridged},
        bridge=bridge,
        pins=pins,
        sources=sources,
        dataset=dataset,
        agent=agent,
        evaluator=evaluator,
        task_assets=task_assets,
        image_pins=image_pins,
        image_pins_sha256=pin_sha,
    )
    inputs.verify()
    return inputs


def reference_verdict(
    spec: Any, observed: dict, found: bool, official: dict, grading
) -> dict:
    """Require observed reference tests using the pinned grader's ID semantics."""
    f2p, p2p = spec.FAIL_TO_PASS, spec.PASS_TO_PASS
    keys = {test: grading._resolve_case(test, observed) for test in f2p + p2p}
    complete = bool(
        found
        and f2p
        and p2p
        and all(
            key is not None and observed[key] in {"PASSED", "XFAIL", "FAILED", "ERROR"}
            for key in keys.values()
        )
    )
    preserved = bool(
        complete and all(grading.test_passed(test, observed) for test in p2p)
    )
    return {
        "complete": complete,
        "required_test_count": len(set(f2p + p2p)),
        "missing_reference_tests": sum(key is None for key in keys.values()),
        "preservation_passed": preserved,
        "negative_control_valid": bool(
            preserved
            and official.get("resolved") is False
            and any(grading.test_failed(test, observed) for test in f2p)
        ),
        "positive_control_valid": bool(
            preserved
            and official.get("resolved") is True
            and all(grading.test_passed(test, observed) for test in f2p)
        ),
    }


class _CheckedContainer:
    def __init__(self, container, base_commit: str, workdir: str, http_fixture=None):
        self.container, self.base_commit, self.workdir = container, base_commit, workdir
        self.base_verified = False
        self.base_identity = None
        self.http_fixture, self.fixture_attachment = http_fixture, None

    def __getattr__(self, name):
        return getattr(self.container, name)

    def start(self):
        self.container.start()
        head = self.container.exec_run("git rev-parse HEAD", workdir=self.workdir)
        image_tree = self.container.exec_run(
            "git rev-parse HEAD^{tree}", workdir=self.workdir
        )
        base_tree = self.container.exec_run(
            f"git rev-parse {self.base_commit}^{{tree}}", workdir=self.workdir
        )
        status = self.container.exec_run(
            "git status --porcelain --untracked-files=all", workdir=self.workdir
        )
        require(
            head.exit_code
            == status.exit_code
            == image_tree.exit_code
            == base_tree.exit_code
            == 0
            and re.fullmatch(r"[0-9a-f]{40}", head.output.decode().strip()) is not None
            and re.fullmatch(r"[0-9a-f]{40}", base_tree.output.decode().strip())
            is not None
            and image_tree.output.strip() == base_tree.output.strip()
            and not status.output.strip(),
            "fresh grading container differs from the clean task base",
        )
        self.base_verified = True
        self.base_identity = {
            "base_commit": self.base_commit,
            "image_head": head.output.decode().strip(),
            "base_tree": base_tree.output.decode().strip(),
            "image_tree": image_tree.output.decode().strip(),
            "clean": True,
        }
        if self.http_fixture is not None:
            self.fixture_attachment = self.http_fixture.configure(self.container)


class _LocalImages:
    def __init__(self, images):
        self.images = images

    def get(self, reference):
        return self.images.get(reference)

    def pull(self, *args, **kwargs):
        raise CohortError("grading cannot pull an image implicitly")


class _OwnedContainers:
    def __init__(self, client, base_commit: str, workdir: str, http_fixture=None):
        self.client, self.base_commit, self.workdir = client, base_commit, workdir
        self.created: list[_CheckedContainer] = []
        self.http_fixture = http_fixture

    def get(self, name):
        # The upstream grader removes collisions. Keep pre-existing containers.
        self.client.containers.get(name)
        raise CohortError("fresh grading container name already exists")

    def create(self, **kwargs):
        if self.http_fixture is not None:
            options = self.http_fixture.container_options()
            prior = kwargs.get("environment") or {}
            require(
                isinstance(prior, dict)
                and not set(prior) & options["environment"].keys(),
                "grader environment overlaps HTTP fixture configuration",
            )
            kwargs["environment"] = {**prior, **options["environment"]}
            require(
                not kwargs.get("network_mode"),
                "grader already enrolled a different network",
            )
            kwargs["network_mode"] = options["network_mode"]
        container = _CheckedContainer(
            self.client.containers.create(**kwargs),
            self.base_commit,
            self.workdir,
            self.http_fixture,
        )
        self.created.append(container)
        return container

    def drain(self):
        import docker

        for owned in self.created:
            try:
                remaining = self.client.containers.get(owned.id)
            except docker.errors.NotFound:
                continue
            except docker.errors.DockerException:
                raise GradingOwnershipError(
                    "owned grading container state is unknown"
                ) from None
            try:
                remaining.remove(force=True)
            except docker.errors.DockerException:
                raise GradingOwnershipError(
                    "owned grading container could not terminate"
                ) from None
            try:
                self.client.containers.get(owned.id)
            except docker.errors.NotFound:
                continue
            except docker.errors.DockerException:
                raise GradingOwnershipError(
                    "owned grading container termination is unverified"
                ) from None
            raise GradingOwnershipError("owned grading container did not terminate")


def control(
    *,
    inputs: FrozenInputs,
    task: dict,
    pin: dict,
    image_id: str,
    client,
    output: Path,
    name: str,
    timeout: int,
    runner,
    grading,
    make_spec,
    http_fixture=None,
) -> dict:
    """One control owns a fresh upstream container and preserves raw grader output."""
    task_id = task["task_id"]
    require(
        runner.RUN_EVALUATION_LOG_DIR == output / "raw",
        "grader output root differs from its owned directory",
    )
    row = deepcopy(inputs.private_rows[task_id])
    row["image"] = image_id
    spec = make_spec(row)
    require(spec.instance_id == task_id, "grader changed control task identity")
    require(spec.image == image_id, "grader changed control image identity")
    patch = row["patch"] if name == "gold" else ""
    prediction = {
        "instance_id": task_id,
        "model_name_or_path": name,
        "model_patch": patch,
    }
    run_id = "agentc-environment-" + uuid.uuid4().hex
    log_dir = output / "raw" / run_id / name / task_id
    require(not log_dir.exists(), "control log directory must be fresh")
    containers = _OwnedContainers(
        client, row["base_commit"], runner.CONTAINER_WORKDIR, http_fixture
    )
    local_client = SimpleNamespace(
        images=_LocalImages(client.images), containers=containers
    )
    try:
        result = runner.run_instance(
            spec,
            prediction,
            local_client,
            run_id,
            timeout=timeout,
            skip_patch=name == "unchanged",
            task_repo=str(inputs.task_assets),
        )
    finally:
        containers.drain()
    base_verified = len(containers.created) == 1 and containers.created[0].base_verified
    log = log_dir / runner.LOG_TEST_OUTPUT
    record = {
        "control": name,
        "task_id": task_id,
        "status": "control_execution_failed",
        "base_verified": base_verified,
        "base_identity": containers.created[0].base_identity if base_verified else None,
        "executed_image_id": image_id,
        "immutable_image": pin["immutable_image"],
        "eval_script_sha256": sha256(spec.eval_script.encode()).hexdigest(),
        "complete": False,
        "negative_control_valid": False,
        "positive_control_valid": False,
        "resolved": None,
        "http_fixture": http_fixture_definition() if http_fixture else None,
        "fixture_attachment": containers.created[0].fixture_attachment
        if base_verified
        else None,
    }
    if result is not None and log.is_file() and base_verified:
        require(
            isinstance(result, tuple) and len(result) == 2 and result[0] == task_id,
            "grader returned a different control task",
        )
        observed, found = grading.get_logs_eval(spec, str(log))
        official = grading.get_eval_report(spec, prediction, str(log), True)[task_id]
        require(
            result[1] == {task_id: official},
            "grader report differs from its raw test log",
        )
        record.update(
            status="control_graded",
            **reference_verdict(spec, observed, found, official, grading),
            resolved=official["resolved"],
            official=official,
            test_exit_code=grading.parse_test_exit_code(log.read_text()),
            test_log_sha256=file_sha(log),
            observed_sha256=digest(observed),
        )
    files = {}
    if log_dir.exists():
        for path in sorted(log_dir.rglob("*")):
            if path.is_file():
                require(
                    path.resolve().is_relative_to(output.resolve()),
                    "grader artifact escapes output",
                )
                path.chmod(0o600)
                files[path.relative_to(output).as_posix()] = file_sha(path)
    record["raw_artifacts"] = files
    return record


def check_cohort(
    inputs: FrozenInputs,
    output: Path,
    *,
    timeout: int = 600,
    client=None,
    image_cache=None,
    isolated_requests_http=False,
) -> dict:
    """Sequential controls, retaining tasks even when provisioning fails."""
    require(type(timeout) is int and 0 < timeout <= 1800, "invalid control timeout")
    require(type(isolated_requests_http) is bool, "invalid HTTP fixture mode")
    require(not output.exists(), "environment checks require a fresh output directory")
    require(
        _GRADER_LOCK.acquire(blocking=False), "another frozen grader session is active"
    )
    previous_root = None
    own_client = client is None
    runner = None
    preflight_sha = file_sha(Path(__file__))
    try:
        inputs.verify()
        make_spec = load_evaluator(inputs.evaluator)
        runner = importlib.import_module("swebench.harness.run_evaluation")
        grading = importlib.import_module("swebench.harness.grading")
        import docker

        if client is None:
            client = docker.from_env(timeout=60)
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        previous_root = runner.RUN_EVALUATION_LOG_DIR
        runner.RUN_EVALUATION_LOG_DIR = output / "raw"
        private_write(
            output / "enrollment.json",
            {
                "schema_version": 1,
                "record_type": "frozen_swebench_environment_enrollment",
                "cohort": inputs.cohort,
                "bridge_sha256": digest(inputs.bridge),
                "image_pins_sha256": digest(inputs.pins),
                "sources_sha256": digest(inputs.sources),
                "assigned_task_count": 150,
                "timeout_seconds": timeout,
                "physical_provider_calls": 0,
                "preflight_sha256": preflight_sha,
                "image_cache_enrollment_sha256": digest(image_cache.enrollment)
                if image_cache is not None
                else None,
                "http_fixtures": {REQUESTS_TASK: http_fixture_definition()}
                if isolated_requests_http
                else {},
            },
        )
        pins = {pin["task_id"]: pin for pin in inputs.pins["checks"]}
        checks = []
        for task in inputs.cohort["tasks"]:
            pin = pins[task["task_id"]]
            check = {
                "task_id": task["task_id"],
                "stage": task["stage"],
                "public_task_sha256": task["public_task_sha256"],
                "status": "immutable_image_unavailable",
                "environment_admitted": False,
                "official_score": None,
                "controls": [],
            }
            if pin["status"] == "immutable_image_verified":
                leased = False
                try:
                    if image_cache is not None:
                        check["image_provisioning"] = image_cache.acquire(pin)
                        leased = True
                    image = client.images.get(pin["config_digest"])
                    require(
                        image.id == pin["config_digest"]
                        and image.attrs["Os"] == "linux"
                        and image.attrs["Architecture"] == "amd64",
                        "local image differs from the frozen manifest",
                    )
                    check["executed_image_id"] = image.id
                    check["immutable_image"] = pin["immutable_image"]
                    check["status"] = "controls_failed"
                    fixture_context = nullcontext()
                    if isolated_requests_http and task["task_id"] == REQUESTS_TASK:
                        (output / "http-fixtures").mkdir(mode=0o700, exist_ok=True)
                        fixture_context = RequestsHTTPFixture(
                            client, output / "http-fixtures" / REQUESTS_TASK
                        )
                    with fixture_context as fixture:
                        for name in ("unchanged", "gold"):
                            result = control(
                                inputs=inputs,
                                task=task,
                                pin=pin,
                                image_id=image.id,
                                client=client,
                                output=output,
                                name=name,
                                timeout=timeout,
                                runner=runner,
                                grading=grading,
                                make_spec=make_spec,
                                http_fixture=fixture,
                            )
                            check["controls"].append(result)
                            if result["status"] == "control_execution_failed":
                                break
                    if fixture is not None:
                        check["http_fixture"] = http_fixture_definition()
                        check["http_fixture_artifacts"] = {
                            p.relative_to(output).as_posix(): file_sha(p)
                            for p in fixture.output.iterdir()
                            if p.is_file() and p.name != "key.pem"
                        }
                    if (
                        len(check["controls"]) == 2
                        and check["controls"][0]["negative_control_valid"]
                        and check["controls"][1]["positive_control_valid"]
                    ):
                        check.update(
                            status="environment_admitted", environment_admitted=True
                        )
                except docker.errors.ImageNotFound:
                    check["status"] = "local_image_unavailable"
                except (
                    GradingOwnershipError,
                    ImageOwnershipError,
                    FixtureOwnershipError,
                ):
                    raise
                except ProvisioningError as error:
                    check.update(
                        status="image_provisioning_failed",
                        error_type=type(error).__name__,
                    )
                except (
                    CohortError,
                    docker.errors.DockerException,
                    OSError,
                    KeyError,
                    TypeError,
                    ValueError,
                ) as error:
                    check.update(
                        status="environment_check_failed",
                        error_type=type(error).__name__,
                    )
                finally:
                    if leased:
                        image_cache.release()
            checks.append(check)
            private_write(output / (task["task_id"] + ".json"), check)
        inputs.verify()
        require(
            file_sha(Path(__file__)) == preflight_sha,
            "environment checker changed during execution",
        )
        admitted = sum(c["environment_admitted"] for c in checks)
        report = {
            "schema_version": 1,
            "record_type": "frozen_swebench_environment_checks",
            "cohort_sha256": digest(inputs.cohort),
            "bridge_sha256": digest(inputs.bridge),
            "image_pins_sha256": digest(inputs.pins),
            "sources_sha256": digest(inputs.sources),
            "assigned_task_count": len(checks),
            "environments_admitted": admitted,
            "environment_admission_completed": admitted == len(checks) == 150,
            "provider_acquisition_ready": False,
            "physical_provider_calls": 0,
            "paper_evidence": False,
            "checks": checks,
            "preflight_sha256": preflight_sha,
            "timeout_seconds": timeout,
            "http_fixtures": {REQUESTS_TASK: http_fixture_definition()}
            if isolated_requests_http
            else {},
        }
        private_write(output / "summary.json", report)
        return report
    finally:
        if runner is not None and previous_root is not None:
            runner.RUN_EVALUATION_LOG_DIR = previous_root
        try:
            if own_client and client is not None:
                client.close()
        finally:
            _GRADER_LOCK.release()


def _verify_http_fixture(output, check, recipe):
    require(
        check.get("http_fixture") == recipe,
        "HTTP fixture was not bound to task controls",
    )
    directory = output / "http-fixtures" / REQUESTS_TASK
    files = check.get("http_fixture_artifacts", {})
    required = {
        "cert.pem",
        "definition.json",
        "network-intent.json",
        "network-created.json",
        "container-intent.json",
        "container-created.json",
        "enrollment.json",
        "cleanup.json",
    }
    require(
        set(files)
        == {(Path("http-fixtures") / REQUESTS_TASK / n).as_posix() for n in required},
        "HTTP fixture artifact inventory differs",
    )
    for name, h in files.items():
        path = output / name
        require(
            not path.is_symlink()
            and path.resolve().is_relative_to(directory.resolve())
            and file_sha(path) == h,
            "HTTP fixture artifact changed or escaped its directory",
        )
    enrollment = json.loads((directory / "enrollment.json").read_text())
    cleanup = json.loads((directory / "cleanup.json").read_text())
    require(
        enrollment["definition"] == recipe
        and json.loads((directory / "definition.json").read_text()) == recipe
        and enrollment["http_https_hostname_verified"] is True
        and enrollment["certificate_sha256"] == file_sha(directory / "cert.pem")
        and re.fullmatch(r"agentc-http-[0-9a-f]{32}", enrollment["owner"]) is not None
        and cleanup["verified"] is True
        and cleanup["creation_uncertain"] is False,
        "HTTP fixture trust or cleanup is unqualified",
    )
    for kind in ("network", "container"):
        identity = enrollment[kind + "_id"]
        require(
            re.fullmatch(r"[0-9a-f]{64}", identity) is not None
            and json.loads((directory / (kind + "-created.json")).read_text())["id"]
            == identity,
            "HTTP fixture ownership receipt differs",
        )
    attachments = cleanup["attachments"]
    require(
        len(attachments) == 2
        and len({a["container_id"] for a in attachments}) == 2
        and all(
            a["checkout_unchanged"] is True and a["trust_verified"] is True
            for a in attachments
        )
        and [c.get("fixture_attachment") for c in check["controls"]] == attachments
        and all(c.get("http_fixture") == recipe for c in check["controls"]),
        "HTTP fixture is not bound to both unchanged/gold checkouts",
    )


def verify_environment_report(inputs: FrozenInputs, output: Path) -> dict:
    """Replay admitted controls before a worker can trust their saved flags."""
    inputs.verify()
    report = json.loads((output / "summary.json").read_text())
    require(
        report.get("schema_version") == 1
        and report.get("record_type") == "frozen_swebench_environment_checks"
        and report.get("cohort_sha256") == digest(inputs.cohort)
        and report.get("bridge_sha256") == digest(inputs.bridge)
        and report.get("image_pins_sha256") == digest(inputs.pins)
        and report.get("sources_sha256") == digest(inputs.sources)
        and report.get("assigned_task_count") == 150
        and isinstance(report.get("checks"), list)
        and len(report["checks"]) == 150
        and report.get("provider_acquisition_ready") is False
        and report.get("physical_provider_calls") == 0
        and report.get("paper_evidence") is False,
        "environment report differs from the frozen cohort",
    )
    require(
        report.get("preflight_sha256") == file_sha(Path(__file__)),
        "saved admission came from a different environment checker",
    )
    enrollment = json.loads((output / "enrollment.json").read_text())
    require(
        enrollment.get("cohort") == inputs.cohort
        and enrollment.get("preflight_sha256") == report["preflight_sha256"]
        and enrollment.get("bridge_sha256") == report["bridge_sha256"]
        and enrollment.get("image_pins_sha256") == report["image_pins_sha256"]
        and enrollment.get("sources_sha256") == report["sources_sha256"]
        and enrollment.get("assigned_task_count") == 150
        and enrollment.get("physical_provider_calls") == 0,
        "environment enrollment differs from its completed report",
    )
    fixtures = report.get("http_fixtures", {})
    require(
        fixtures in ({}, {REQUESTS_TASK: http_fixture_definition()})
        and enrollment.get("http_fixtures", {}) == fixtures,
        "HTTP fixture recipe differs from enrollment",
    )
    make_spec = load_evaluator(inputs.evaluator)
    grading = importlib.import_module("swebench.harness.grading")
    pins = {p["task_id"]: p for p in inputs.pins["checks"]}
    admitted = 0
    for task, check in zip(inputs.cohort["tasks"], report["checks"], strict=True):
        task_id = task["task_id"]
        require(
            check["task_id"] == task_id
            and check["stage"] == task["stage"]
            and check["public_task_sha256"] == task["public_task_sha256"]
            and check.get("official_score") is None
            and type(check.get("environment_admitted")) is bool,
            "environment report changed a task or claimed a model score",
        )
        require(
            json.loads((output / (task_id + ".json")).read_text()) == check,
            "saved task check differs from the cohort report",
        )
        require(
            check.get("http_fixture") is None or task_id in fixtures,
            "unregistered HTTP fixture in task controls",
        )
        if not check["environment_admitted"]:
            require(
                check["status"] != "environment_admitted", "invalid admission status"
            )
            continue
        pin = pins[task_id]
        require(
            pin["status"] == "immutable_image_verified"
            and check["executed_image_id"] == pin["config_digest"]
            and check["immutable_image"] == pin["immutable_image"]
            and check["status"] == "environment_admitted"
            and len(check["controls"]) == 2,
            "admission is not bound to its verified image and controls",
        )
        row = deepcopy(inputs.private_rows[task_id])
        row["image"] = pin["config_digest"]
        spec = make_spec(row)
        if task_id in fixtures:
            _verify_http_fixture(output, check, fixtures[task_id])
        for name, record in zip(("unchanged", "gold"), check["controls"], strict=True):
            require(
                record["control"] == name
                and record["task_id"] == task_id
                and record["status"] == "control_graded"
                and record["base_verified"] is True
                and record["executed_image_id"] == pin["config_digest"]
                and record["immutable_image"] == pin["immutable_image"]
                and record["eval_script_sha256"]
                == sha256(spec.eval_script.encode()).hexdigest(),
                "control identity differs from its frozen task",
            )
            base = record["base_identity"]
            require(
                base["base_commit"] == row["base_commit"]
                and base["clean"] is True
                and re.fullmatch(r"[0-9a-f]{40}", base["image_head"]) is not None
                and re.fullmatch(r"[0-9a-f]{40}", base["base_tree"]) is not None
                and base["base_tree"] == base["image_tree"],
                "control did not bind a clean frozen repository tree",
            )
            files = record["raw_artifacts"]
            logs = [Path(p) for p in files if Path(p).name == "test_output.txt"]
            require(len(logs) == 1, "control has no unique raw test log")
            relative = logs[0]
            require(
                len(relative.parts) == 5
                and relative.parts[0] == "raw"
                and re.fullmatch(r"agentc-environment-[0-9a-f]{32}", relative.parts[1])
                is not None
                and relative.parts[2:4] == (name, task_id),
                "control raw log belongs to another task",
            )
            for path, expected in files.items():
                artifact = output / path
                require(
                    Path(path).parts[:4] == relative.parts[:4]
                    and artifact.resolve().is_relative_to(output.resolve())
                    and file_sha(artifact) == expected,
                    "raw control artifact changed or escaped its directory",
                )
            log = output / relative
            directory = log.parent
            require(
                file_sha(directory / "eval.sh") == record["eval_script_sha256"]
                and file_sha(log) == record["test_log_sha256"],
                "control script/log differs from the frozen grader bytes",
            )
            patch = row["patch"] if name == "gold" else ""
            if name == "gold":
                require(
                    file_sha(directory / "patch.diff")
                    == sha256(patch.encode()).hexdigest(),
                    "positive control did not execute the original gold patch",
                )
            prediction = {
                "instance_id": task_id,
                "model_name_or_path": name,
                "model_patch": patch,
            }
            observed, found = grading.get_logs_eval(spec, str(log))
            official = grading.get_eval_report(spec, prediction, str(log), True)[
                task_id
            ]
            verdict = reference_verdict(spec, observed, found, official, grading)
            require(
                all(record.get(k) == v for k, v in verdict.items())
                and record["official"] == official
                and record["resolved"] is official["resolved"]
                and record["observed_sha256"] == digest(observed)
                and record["test_exit_code"]
                == grading.parse_test_exit_code(log.read_text())
                and verdict[
                    "negative_control_valid"
                    if name == "unchanged"
                    else "positive_control_valid"
                ],
                "saved admission differs from replayed official controls",
            )
        admitted += 1
    require(
        report.get("environments_admitted") == admitted
        and report.get("environment_admission_completed") is (admitted == 150),
        "environment admission count differs from verified controls",
    )
    inputs.verify()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "dataset",
        "agent",
        "evaluator",
        "task-assets",
        "image-pins",
        "output",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--isolated-requests-http", action="store_true")
    args = parser.parse_args(argv)
    inputs = load_inputs(
        **{
            name: getattr(args, name).resolve()
            for name in ("dataset", "agent", "evaluator", "task_assets", "image_pins")
        }
    )
    report = check_cohort(
        inputs,
        args.output.resolve(),
        timeout=args.timeout,
        isolated_requests_http=args.isolated_requests_http,
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "assigned_task_count",
                    "environments_admitted",
                    "environment_admission_completed",
                    "provider_acquisition_ready",
                    "physical_provider_calls",
                )
            }
        )
    )
    return 0 if report["environment_admission_completed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
