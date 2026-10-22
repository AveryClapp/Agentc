"""Ordered frozen SWE-agent cells, official grades, and every owned invocation.

Unavailable environments retain null scores. E0/E1 mechanics cannot supply
paper-stage admission or policy-safety measurements. Each task executes the
unchanged DefaultAgent, tools, history processor and submission path.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import platform
import random
import re
import time
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from bench.campaign_accounting import digest, money, read_snapshot
from bench.campaign_records import cell_calls, normalize_owned_calls
from bench.coding_submission import private_write
from bench.guard_evidence import file_sha
from bench.joint_campaign import (
    PRIMARY_ARMS,
    ScheduledRun,
    _cell_storage_path,
    derive_run_seed,
    validate_worker_records,
)
from bench.policy_calibration import ARMS as CALIBRATED_ARMS
from bench.policy_calibration import CalibrationTrialBundle, FrozenPolicyBundle
from bench.swe_agent_binding import SweAgentCampaignBinding
from bench.swe_agent_grading import grade_task
from bench.swe_agent_task_worker import execution_version, load_submission, run_task
from bench.swe_agent_process import launch_task, runtime_environment
from bench.swebench_cohort import (
    AGENT_REVISION,
    DATASET_SHA256,
    PROTOCOL_SHA256,
    require,
)
from bench.swebench_environment import verify_environment_report
from bench.swebench_image_cache import ImageOwnershipError, ProvisioningError

OPTIMIZED_ARMS = frozenset(PRIMARY_ARMS) - {
    "unmodified_fixed_strong",
    "trace_only_fixed_strong",
    "fixed_cheap",
}


@dataclass(frozen=True)
class SweAgentTaskConfig:
    strong_model: str
    cheap_model: str
    output_cap: int = 1024
    rewrite_rules: tuple[str, ...] = ("OutputBudget", "ContextCompress", "StateDrop")
    network_policy: str = "provider_allowed"
    max_overhead_ms: float = 2.0

    def validate(self):
        require(
            all(
                isinstance(m, str) and re.fullmatch(r"[A-Za-z0-9_./:-]+", m)
                for m in (self.strong_model, self.cheap_model)
            )
            and self.strong_model != self.cheap_model
            and type(self.output_cap) is int
            and 0 < self.output_cap <= 2048
            and type(self.rewrite_rules) is tuple
            and len(set(self.rewrite_rules)) == len(self.rewrite_rules)
            and set(self.rewrite_rules)
            <= {"OutputBudget", "ContextCompress", "StateDrop"}
            and self.network_policy in {"provider_allowed", "loopback_only"}
            and type(self.max_overhead_ms) in (int, float)
            and 0 < self.max_overhead_ms < float("inf"),
            "invalid frozen SWE-agent task configuration",
        )


def cell_identity(cell: ScheduledRun) -> str:
    return digest(asdict(cell))


def runtime_manifest() -> dict:
    module = importlib.util.find_spec("agentc")
    require(
        module is not None and module.origin is not None,
        "Agentc package is unavailable",
    )
    sdk = Path(module.origin).resolve().parent
    sdk_files = {
        p.relative_to(sdk).as_posix(): file_sha(p)
        for p in sorted(sdk.rglob("*"))
        if p.is_file() and p.suffix in {".py", ".pyi", ".so"}
    }
    harness = {
        name: file_sha(Path(__file__).with_name(name))
        for name in (
            "swe_agent_campaign_worker.py",
            "swe_agent_binding.py",
            "swe_agent_task_worker.py",
            "swe_agent_runtime.py",
            "swe_agent_grading.py",
            "swebench_environment.py",
            "swebench_image_cache.py",
            "swebench_cohort.py",
            "swebench_compatibility.py",
            "swebench_images.py",
            "campaign_accounting.py",
            "campaign_chat.py",
            "campaign_http.py",
            "campaign_transport.py",
            "campaign_records.py",
            "joint_campaign.py",
            "policy_calibration.py",
            "swe_agent_process.py",
            "campaign_rpc.py",
            "process_resources.py",
            "swe_requests_fixture.py",
        )
    }
    return {
        "python_version": platform.python_version(),
        "sdk_files": sdk_files,
        "sdk_closure_sha256": digest(sdk_files),
        "harness_files": harness,
        "harness_closure_sha256": digest(harness),
        "distributions": sorted(
            (d.metadata["Name"], d.version) for d in importlib.metadata.distributions()
        ),
    }


def validate_grade(grade: dict, submission: dict, path: Path, calls: list[dict]):
    require(
        grade.get("task_id") == submission["task_id"]
        and grade.get("submission_sha256") == file_sha(path)
        and grade.get("patch_sha256") == submission["patch_sha256"]
        and grade.get("all_provider_calls") == len(calls)
        and money(grade["all_provider_cost_usd"])
        == sum((money(c["billed_result"]["cost_usd"]) for c in calls), Decimal(0))
        and type(grade.get("reference_tests_complete")) is bool,
        "official grading differs from its captured submission or owned calls",
    )
    if grade["reference_tests_complete"]:
        expected = float(grade["official"]["resolved"])
        require(
            type(grade["official"]["resolved"]) is bool
            and grade["official_score"] == expected,
            "official score differs from its raw report",
        )
    else:
        require(
            grade["official_score"] is None,
            "incomplete reference tests cannot supply an official score",
        )
    require(
        grade["protocol_score"]
        == (0.0 if submission["budget_exhausted"] else grade["official_score"]),
        "protocol outcome differs from official grading or the task budget",
    )


class SweAgentCampaignWorkerSession:
    def __init__(
        self,
        *,
        transport,
        inputs,
        environment_report: Path,
        bundle,
        output: Path,
        campaign_output: Path,
        workload_id: str,
        cells: tuple[ScheduledRun, ...],
        config: SweAgentTaskConfig,
        image_cache=None,
        policy_bundle=None,
        process_isolation=True,
    ):
        config.validate()
        require(type(process_isolation) is bool, "invalid task process mode")
        tasks = {t["task_id"]: t for t in inputs.cohort["tasks"]}
        require(
            type(cells) is tuple
            and bool(cells)
            and re.fullmatch(r"[A-Za-z0-9_.-]+", workload_id) is not None
            and all(
                c.workload_id == workload_id
                and c.task_id in tasks
                and c.arm in PRIMARY_ARMS
                and type(c.repetition) is int
                and c.repetition >= 0
                and type(c.ordinal) is int
                and c.ordinal >= 0
                and type(c.run_seed) is int
                and c.run_seed
                == derive_run_seed(workload_id, c.task_id, c.arm, c.repetition)
                and (c.arm not in CALIBRATED_ARMS or policy_bundle is not None)
                for c in cells
            )
            and len({c.ordinal for c in cells}) == len(cells)
            and (
                isinstance(policy_bundle, CalibrationTrialBundle)
                or len({c.key for c in cells}) == len(cells)
            )
            and tuple(sorted(cells, key=lambda c: c.ordinal)) == cells,
            "SWE-agent session lacks a frozen ordered schedule",
        )
        if isinstance(policy_bundle, CalibrationTrialBundle):
            policy_bundle.validate_cells(cells)
        require(
            policy_bundle is None
            or isinstance(policy_bundle, (CalibrationTrialBundle, FrozenPolicyBundle)),
            "unsupported calibration authorization",
        )
        self.transport, self.inputs, self.bundle = transport, inputs, bundle
        self.environment_report = environment_report.resolve()
        self.output, self.campaign_output = output.resolve(), campaign_output.resolve()
        self.cells, self.config, self.workload_id = cells, config, workload_id
        self.image_cache, self.policy_bundle = image_cache, policy_bundle
        self.process_isolation = process_isolation
        self.tasks = tasks
        self._policy_lock_sha256 = (
            policy_bundle.lock["sha256"] if policy_bundle else None
        )
        self._entered = self._failed = False
        self._next = 0
        self.results = []

    def storage_for_cell(self, cell):
        # The runner-owned arm/rep prefix is validated separately. Public cohort
        # partitions and model pools must also have independent native stores.
        prefix = _cell_storage_path(self.campaign_output, cell)
        stage = self.tasks[cell.task_id]["stage"]
        model = digest(
            {"strong": self.config.strong_model, "cheap": self.config.cheap_model}
        )
        if isinstance(self.policy_bundle, CalibrationTrialBundle):
            return self.policy_bundle.storage_path(prefix / stage / model, cell.ordinal)
        return prefix / stage / model

    def __enter__(self):
        require(
            not self._entered and not self._failed and not self.output.exists(),
            "SWE-agent session requires fresh output; no resume",
        )
        self.inputs.verify()
        self.bundle.verify()
        self.admission = verify_environment_report(self.inputs, self.environment_report)
        self._report_sha256 = file_sha(self.environment_report / "summary.json")
        models = frozenset({self.config.strong_model, self.config.cheap_model})
        require(
            models <= self.transport.contracts.keys()
            and all(
                self.transport.contracts[m].max_output_tokens >= self.config.output_cap
                and self.transport.contracts[m].cached_text_blocks
                for m in models
            ),
            "SWE-agent model/cap exceeds enrolled provider contracts",
        )
        endpoint = urlsplit(self.transport.http.base_url)
        require(
            (
                self.config.network_policy == "loopback_only"
                and endpoint.scheme == "http"
                and endpoint.hostname == "127.0.0.1"
            )
            or (
                self.config.network_policy == "provider_allowed"
                and self.transport.http.base_url == "https://openrouter.ai/api/v1"
            ),
            "SWE-agent endpoint differs from enrolled network policy",
        )
        require(
            not any(self.storage_for_cell(c).exists() for c in self.cells),
            "SWE-agent arm stores must be fresh at enrollment",
        )
        self.output.mkdir(mode=0o700)
        self._stack = ExitStack()
        try:
            self._ledger_event_start = len(read_snapshot(self.transport.ledger).events)
            self.enrollment = {
                "schema_version": 1,
                "record_type": "frozen_swe_agent_worker_enrollment",
                "cohort": self.inputs.cohort,
                "cohort_sha256": digest(self.inputs.cohort),
                "image_pins_sha256": self.inputs.image_pins_sha256,
                "sources_sha256": digest(self.inputs.sources),
                "environment_report_sha256": self._report_sha256,
                "execution_version": execution_version(self.bundle),
                "configuration": asdict(self.config),
                "schedule": [asdict(c) for c in self.cells],
                "storage": {
                    str(c.ordinal): str(self.storage_for_cell(c)) for c in self.cells
                },
                "calibration_lock": self.policy_bundle.lock
                if self.policy_bundle
                else None,
                "transport_session_id": self.transport.session_id,
                "transport_stage": self.transport.stage,
                "stage_cap_usd": str(self.transport.cap),
                "runtime": runtime_manifest(),
                "provider_response_cache": False,
                "original_paid_pilot_reused": False,
                "process_isolation": self.process_isolation,
            }
            self.enrollment_sha256 = digest(self.enrollment)
            private_write(self.output / "enrollment.json", self.enrollment)
            self.binding = (
                None
                if self.process_isolation
                else self._stack.enter_context(
                    SweAgentCampaignBinding(
                        transport=self.transport,
                        source=self.inputs.agent,
                        models=models,
                        output_cap=self.config.output_cap,
                        cohort_sha256=digest(self.inputs.cohort),
                        execution_version=execution_version(self.bundle),
                    )
                )
            )
            self._entered = True
            return self
        except BaseException:
            self._failed = True
            self._stack.close()
            raise

    def _validate_request(self, request):
        require(
            self._entered and not self._failed and self._next < len(self.cells),
            "SWE-agent session cannot retry or resume a cell",
        )
        cell = self.cells[self._next]
        expected = {
            "workload_id": self.workload_id,
            "task_id": cell.task_id,
            "arm": cell.arm,
            "repetition": cell.repetition,
            "run_seed": cell.run_seed,
            "schedule_ordinal": cell.ordinal,
            "model_pair": {
                "strong": self.config.strong_model,
                "cheap": self.config.cheap_model,
            },
            "network_policy": self.config.network_policy,
            "protocol_sha256": PROTOCOL_SHA256,
        }
        require(
            all(request.get(k) == v for k, v in expected.items())
            and Path(request["storage_path"]).resolve()
            == _cell_storage_path(self.campaign_output, cell)
            and request.get("workload_configuration")
            == json.loads(json.dumps(asdict(self.config)))
            and not request.get("arm_configuration")
            and request["workload_provenance"].get("upstream_commit") == AGENT_REVISION
            and request["workload_provenance"].get("task_universe_sha256")
            == DATASET_SHA256,
            "SWE-agent request differs from frozen enrollment",
        )
        require(
            request["stage"] == "E0" and request["paper_evidence"] is False,
            "SWE-agent campaign lacks qualified live-launcher and policy-safety admission",
        )
        require(
            self.config.network_policy != "loopback_only" or request["stage"] == "E0",
            "local fixture replies are E0 diagnostics only",
        )
        require(
            file_sha(self.environment_report / "summary.json") == self._report_sha256
            and digest(self.inputs.cohort) == self.enrollment["cohort_sha256"],
            "SWE-agent cohort or admission changed after enrollment",
        )
        if self.policy_bundle is not None:
            require(
                request.get("calibration_lock") == self.policy_bundle.lock
                and self.policy_bundle.lock["sha256"] == self._policy_lock_sha256,
                "SWE-agent policy lock changed",
            )
            self.policy_bundle.authorize(
                workload_id=self.workload_id,
                models=request["model_pair"],
                task_ids=(cell.task_id,),
                stage=request["stage"],
                paper_evidence=False,
                network_policy=request["network_policy"],
                output_cap=self.config.output_cap,
                task_universe_sha256=DATASET_SHA256,
                protocol_sha256=PROTOCOL_SHA256,
                quality_margin=request["quality_margin"],
                cell={
                    "task_id": cell.task_id,
                    "arm": cell.arm,
                    "repetition": cell.repetition,
                    "seed": cell.run_seed,
                    "ordinal": cell.ordinal,
                }
                | (
                    {"trial_id": request.get("calibration_trial_id")}
                    if isinstance(self.policy_bundle, CalibrationTrialBundle)
                    else {}
                ),
            )
        return cell

    @contextmanager
    def _runtime_environment(self, arm, storage):
        previous = {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
        for key in previous:
            os.environ.pop(key)
        os.environ.update(runtime_environment(self.config, arm, storage))
        try:
            yield
        finally:
            for key in tuple(os.environ):
                if key.startswith("AGENTC_"):
                    os.environ.pop(key)
            os.environ.update(previous)

    def _result(
        self,
        cell,
        *,
        score,
        status,
        reason,
        elapsed_ms,
        calls,
        submission=None,
        grade=None,
    ):
        unknown = score is None
        total = sum((money(c["billed_result"]["cost_usd"]) for c in calls), Decimal(0))
        return {
            "schema_version": 1,
            "record_type": "task",
            "task_status": status,
            "official_score": score,
            "raw_official_score": grade["official_score"] if grade else None,
            "end_to_end_latency_ms": elapsed_ms,
            "latency_scope": "image_acquisition_sdk_initialization_agent_tools_provider_drain_task_container_cleanup; grading_image_eviction_excluded",
            "termination_reason": reason,
            "safety_failure": None,
            "metric_availability": {
                "safety_failure": False,
                "official_score": not unknown,
            },
            "unavailable_metric_reasons": {"safety_failure": "no_policy_safety_sensor"}
            | ({"official_score": reason} if unknown else {}),
            "cell_id": cell_identity(cell),
            "cohort_stage": self.tasks[cell.task_id]["stage"],
            "acquisition_enrollment_sha256": self.enrollment_sha256,
            "official_task_sha256": self.tasks[cell.task_id]["public_task_sha256"],
            "submission_sha256": digest(submission) if submission else None,
            "official_grade_sha256": digest(grade) if grade else None,
            "calls_sha256": digest(calls),
            "provider_call_count": len(calls),
            "all_call_cost_usd": str(total),
            "network_calls": 0,
            "synthetic_billing": self.config.network_policy == "loopback_only",
            "conformance": {
                "worker_kind": "frozen_swe_agent_owned_acquisition_v1",
                "upstream_source_modified": False,
                "official_task": True,
                "official_score": not unknown,
                "provider_accounting": self.config.network_policy == "provider_allowed",
            },
        }

    def run(self, request):
        import docker

        leased = initialized = False
        try:
            cell = self._validate_request(request)
            identity = cell_identity(cell)
            output = self.output / identity
            output.mkdir(mode=0o700)
            started = time.perf_counter_ns()
            admitted = next(
                c for c in self.admission["checks"] if c["task_id"] == cell.task_id
            )
            reason = (
                None if admitted["environment_admitted"] else "environment_not_admitted"
            )
            if reason is None and self.image_cache is not None:
                pin = next(
                    p
                    for p in self.inputs.pins["checks"]
                    if p["task_id"] == cell.task_id
                )
                try:
                    receipt = self.image_cache.acquire(pin)
                    leased = True
                    private_write(output / "image-provisioning.json", receipt)
                except (ProvisioningError, docker.errors.ImageNotFound):
                    reason = "image_provisioning_unavailable"
            calls, submission, grade = [], None, None
            binding_version = (
                self.binding.settings_version
                if self.binding is not None
                else "unexecuted:" + self.enrollment["runtime"]["sdk_closure_sha256"]
            )
            if reason is None:
                if self.process_isolation:
                    receipt = launch_task(
                        transport=self.transport,
                        inputs=self.inputs,
                        environment_report=self.environment_report,
                        bundle=self.bundle,
                        output=output,
                        cell=cell,
                        storage=self.storage_for_cell(cell),
                        config=self.config,
                        policy=asdict(
                            self.policy_bundle.policy(
                                cell.arm, schedule_ordinal=cell.ordinal
                            )
                        )
                        if cell.arm in CALIBRATED_ARMS
                        else None,
                        calibration_metadata=self.policy_bundle.receipt_metadata(
                            cell.ordinal
                        )
                        if isinstance(self.policy_bundle, CalibrationTrialBundle)
                        else None,
                    )
                    require(
                        digest(receipt["runtime"])
                        == digest(self.enrollment["runtime"]),
                        "task process runtime differs from worker enrollment",
                    )
                    submission = receipt["submission"]
                    plans = receipt["plans"]
                    accounting = receipt["cell_accounting"]
                    binding_version = receipt["binding_settings_version"]
                    if receipt["instrumented"]:
                        private_write(
                            output / "runtime-storage.json",
                            {
                                "python_store": receipt["python_store"],
                                "native_store": receipt["native_store"],
                                "environment_store": receipt["pre_import_storage"],
                            },
                        )
                else:
                    import agentc
                    from agentc._evaluation_policy import evaluation_policy_scope

                    policy_scope = (
                        evaluation_policy_scope(
                            self.policy_bundle.policy(
                                cell.arm, schedule_ordinal=cell.ordinal
                            )
                        )
                        if cell.arm in CALIBRATED_ARMS
                        else nullcontext()
                    )
                    with (
                        self._runtime_environment(
                            cell.arm, self.storage_for_cell(cell)
                        ),
                        policy_scope,
                    ):
                        random.seed(cell.run_seed)
                        try:
                            if cell.arm not in {
                                "unmodified_fixed_strong",
                                "fixed_cheap",
                            }:
                                agentc.init(
                                    storage_path=str(self.storage_for_cell(cell)),
                                    capture_content=False,
                                )
                                initialized = True
                                from agentc import _native
                                from agentc._lifecycle import get_config

                                python_store = Path(get_config().storage_path).resolve()
                                native_store = Path(
                                    _native.optimize_storage_path()
                                ).resolve()
                                require(
                                    python_store
                                    == native_store
                                    == self.storage_for_cell(cell)
                                    and os.environ["AGENTC_STORAGE_PATH"]
                                    == str(python_store),
                                    "SWE-agent Python/native storage ownership differs",
                                )
                                private_write(
                                    output / "runtime-storage.json",
                                    {
                                        "python_store": str(python_store),
                                        "native_store": str(native_store),
                                        "environment_store": os.environ[
                                            "AGENTC_STORAGE_PATH"
                                        ],
                                    },
                                )
                            submission = run_task(
                                inputs=self.inputs,
                                environment_report=self.environment_report,
                                task_id=cell.task_id,
                                binding=self.binding,
                                bundle=self.bundle,
                                output=output / "task",
                                requested_model=self.config.cheap_model
                                if cell.arm == "fixed_cheap"
                                else self.config.strong_model,
                                arm=cell.arm,
                                cell_id=identity,
                                optimize=cell.arm in OPTIMIZED_ARMS,
                                run_seed=cell.run_seed,
                                calibration_metadata=self.policy_bundle.receipt_metadata(
                                    cell.ordinal
                                )
                                if isinstance(
                                    self.policy_bundle, CalibrationTrialBundle
                                )
                                else None,
                            )
                            self.binding.assert_complete()
                        finally:
                            if initialized:
                                agentc.shutdown()
                                initialized = False
                    plans = [p for p in self.binding.plans if p["cell_id"] == identity]
                    accounting = self.binding.cell_accounting(identity)
                    binding_version = self.binding.settings_version
                elapsed = (time.perf_counter_ns() - started) / 1_000_000
                snapshot = read_snapshot(self.transport.ledger)
                claims = [
                    e
                    for e in snapshot.events
                    if e["event"] == "reserve"
                    and e.get("session_id") == self.transport.session_id
                    and e.get("metadata", {}).get("cell_id") == identity
                ]
                if claims:
                    calls = cell_calls(
                        snapshot,
                        session_id=self.transport.session_id,
                        cell_id=identity,
                        plans=plans,
                    )
                else:
                    require(
                        submission["budget_exhausted"],
                        "executed task lacks owned calls",
                    )
                require(
                    money(accounting["committed_usd"])
                    == sum(
                        (money(c["billed_result"]["cost_usd"]) for c in calls),
                        Decimal(0),
                    ),
                    "SWE-agent cell omits billed invocation charges",
                )
                grading_started = time.perf_counter_ns()
                grade = grade_task(
                    inputs=self.inputs,
                    environment_report=self.environment_report,
                    bundle=self.bundle,
                    ledger=self.transport.ledger,
                    submission_path=output / "task/submission.json",
                    output=output / "grading",
                    measure_resources=self.process_isolation,
                )
                require(
                    load_submission(output / "task/submission.json") == submission,
                    "persisted submission differs from upstream result",
                )
                validate_grade(
                    grade, submission, output / "task/submission.json", calls
                )
                score = grade["protocol_score"]
                status = (
                    submission["task_status"] if score is not None else "incomplete"
                )
                reason = (
                    "incomplete_reference_tests"
                    if score is None
                    else submission["upstream_exit_status"]
                )
                result = self._result(
                    cell,
                    score=score,
                    status=status,
                    reason=reason,
                    elapsed_ms=elapsed,
                    calls=calls,
                    submission=submission,
                    grade=grade,
                )
                if self.process_isolation:
                    result["process_resources"] = json.loads(
                        (output / "process-resources.json").read_bytes()
                    )
                    result["container_resources"] = json.loads(
                        (output / "task/container-resources.json").read_bytes()
                    )
                    result["optimization_scope"] = receipt["optimization_scope"]
                result["grading_latency_ms"] = (
                    time.perf_counter_ns() - grading_started
                ) / 1_000_000
            else:
                result = self._result(
                    cell,
                    score=None,
                    status="incomplete",
                    reason=reason,
                    elapsed_ms=(time.perf_counter_ns() - started) / 1_000_000,
                    calls=[],
                )
            if leased:
                leased = False
                self.image_cache.release()
            normalized = validate_worker_records(
                [
                    result,
                    *normalize_owned_calls(
                        calls,
                        request,
                        cell_id=identity,
                        optimized_arms=OPTIMIZED_ARMS,
                        fixed_schema="frozen-swe-agent-fixed-dispatch-policy-v1:"
                        + binding_version,
                        call_site_prefix="sweagent.agent.models.LiteLLMModel.query/",
                    ),
                ],
                request,
            )
            for name, record in (
                ("calls.json", calls),
                ("result.json", result),
                ("normalized.json", normalized),
            ):
                private_write(output / name, record)
            self.results.append(result)
            self._next += 1
            return normalized
        except BaseException:
            self._failed = True
            raise
        finally:
            try:
                if initialized:
                    agentc.shutdown()
            finally:
                if leased:
                    leased = False
                    try:
                        self.image_cache.release()
                    except ImageOwnershipError:
                        self._failed = True
                        raise

    def __exit__(self, *exc):
        error = None
        total = Decimal(0)
        try:
            require(
                not (self._failed and exc[0] is None),
                "SWE-agent session has failed cells",
            )
            if not self._failed and exc[0] is None:
                if self.process_isolation:
                    self.transport.assert_complete()
                else:
                    self.binding.assert_complete()
                require(
                    self._next == len(self.cells),
                    "SWE-agent schedule has incomplete cells",
                )
                self.inputs.verify()
                self.bundle.verify()
                require(
                    runtime_manifest() == self.enrollment["runtime"]
                    and file_sha(self.environment_report / "summary.json")
                    == self._report_sha256
                    and (
                        self.policy_bundle.lock["sha256"]
                        if self.policy_bundle
                        else None
                    )
                    == self._policy_lock_sha256,
                    "SWE-agent acquisition inputs changed",
                )
                snapshot = read_snapshot(self.transport.ledger)
                bills = [
                    e
                    for e in snapshot.events[self._ledger_event_start :]
                    if e["event"] == "result"
                    and e.get("session_id") == self.transport.session_id
                ]
                total = sum(
                    (money(r["all_call_cost_usd"]) for r in self.results), Decimal(0)
                )
                require(
                    len(bills) == sum(r["provider_call_count"] for r in self.results)
                    and all(
                        e["metadata"].get("cell_id")
                        in {r["cell_id"] for r in self.results}
                        for e in bills
                    )
                    and total == sum((money(e["cost_usd"]) for e in bills), Decimal(0)),
                    "SWE-agent schedule omits owned billed invocations",
                )
        except BaseException as caught:
            self._failed, error = True, caught
        finally:
            self._entered = False
            self._stack.__exit__(*exc)
        if error is not None:
            raise error
        if exc[0] is None and not self._failed:
            private_write(
                self.output / "completion.json",
                {
                    "schema_version": 1,
                    "record_type": "frozen_swe_agent_worker_completion",
                    "enrollment_sha256": self.enrollment_sha256,
                    "visited_cells": self._next,
                    "results_sha256": digest(self.results),
                    "official_score_unknown_cells": sum(
                        r["official_score"] is None for r in self.results
                    ),
                    "all_call_cost_usd": str(total),
                    "transport_session_id": self.transport.session_id,
                },
            )
