"""Ordered mini-SWE acquisition and official grading under an owned session.

The current grader admits the original Requests task only. Its engineering
cohort exercises real upstream execution, official scores and owned scheduling;
it supplies neither a held-out task inventory nor a policy-safety sensor.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import random
import time
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import asdict, dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from bench.campaign_accounting import digest, money, read_snapshot
from bench.campaign_records import cell_calls, normalize_owned_calls
from bench.campaign_transport import CampaignStopped, CampaignTransport
from bench.coding_campaign_binding import CodingCampaignBinding
from bench.coding_submission import MINI_REVISION, private_write
from bench.coding_task_worker import PUBLIC_FIELDS, run_task, source_manifest
from bench.joint_campaign import (
    CampaignError,
    ScheduledRun,
    _cell_storage_path,
    validate_worker_records,
)
from bench.requests_fixture_preflight import (
    BASE_COMMIT,
    DATASET_SHA256,
    EVAL_SCRIPT_SHA256,
    TASK,
    TASK_IMAGE,
    TASK_IMAGE_ID,
    RequestsGradingSession,
)
from bench.policy_calibration import (
    ARMS as CALIBRATED_ARMS,
    CalibrationTrialBundle,
    FrozenPolicyBundle,
)

SUPPORTED_ARMS = frozenset(
    {
        "unmodified_fixed_strong",
        "trace_only_fixed_strong",
        "fixed_cheap",
        "routing_only",
        "rewrite_only_fixed_strong",
        "current_greedy",
        "joint_guarded",
    }
)
OPTIMIZED_ARMS = (SUPPORTED_ARMS | set(CALIBRATED_ARMS)) - {
    "unmodified_fixed_strong",
    "trace_only_fixed_strong",
    "fixed_cheap",
}


@dataclass(frozen=True)
class CodingTaskConfig:
    strong_model: str
    cheap_model: str
    output_cap: int = 1024
    step_limit: int = 200
    rewrite_rules: tuple[str, ...] = ("OutputBudget", "ContextCompress", "StateDrop")
    network_policy: str = "provider_allowed"
    max_overhead_ms: float = 2.0

    def validate(self):
        if (
            any(
                not isinstance(m, str) or not m.startswith("openai/")
                for m in (self.strong_model, self.cheap_model)
            )
            or self.strong_model == self.cheap_model
            or type(self.output_cap) is not int
            or not 0 < self.output_cap <= 2048
            or type(self.step_limit) is not int
            or self.step_limit < 1
            or type(self.rewrite_rules) is not tuple
            or len(set(self.rewrite_rules)) != len(self.rewrite_rules)
            or not set(self.rewrite_rules)
            <= {"OutputBudget", "ContextCompress", "StateDrop"}
            or self.network_policy not in {"provider_allowed", "loopback_only"}
            or type(self.max_overhead_ms) not in (float, int)
            or not 0 < self.max_overhead_ms < float("inf")
        ):
            raise CampaignStopped("invalid frozen coding task configuration")


def cell_identity(run: ScheduledRun) -> str:
    return digest(
        {
            "task_id": run.task_id,
            "arm": run.arm,
            "repetition": run.repetition,
            "seed": run.run_seed,
            "ordinal": run.ordinal,
        }
    )


def validate_grade(grade: dict, submission: dict, path: Path) -> None:
    """Require the actual submitted bytes and a complete official outcome."""
    receipt = grade.get("submission")
    if (
        grade.get("task") != TASK
        or grade.get("eval_script_sha256") != EVAL_SCRIPT_SHA256
        or grade.get("patch_sha256") != submission["patch_sha256"]
        or not isinstance(receipt, dict)
        or receipt.get("artifact_sha256") != sha256(path.read_bytes()).hexdigest()
        or receipt.get("patch_sha256") != submission["patch_sha256"]
        or receipt.get("upstream_status") != submission["upstream_status"]
        or type(grade.get("resolved")) is not bool
    ):
        raise CampaignStopped("coding grade does not match its captured submission")
    if grade.get("status") == "graded":
        official = grade.get("official")
        if (
            grade.get("complete") is not True
            or not isinstance(official, dict)
            or official.get("resolved") is not grade["resolved"]
        ):
            raise CampaignStopped("coding grade lacks a complete official outcome")
    elif grade.get("status") == "patch_apply_failed":
        if (
            grade["resolved"] is not False
            or grade.get("complete") is not False
            or grade.get("official") is not None
            or type(grade.get("patch_apply_exit_code")) is not int
            or grade["patch_apply_exit_code"] == 0
        ):
            raise CampaignStopped("coding patch application outcome is inconsistent")
    else:
        raise CampaignStopped("coding grade has incomplete official reference tests")


class CodingCampaignWorkerSession:
    def __init__(
        self,
        *,
        transport: CampaignTransport,
        source: Path,
        dataset: Path,
        pytest_wheels: Path,
        output: Path,
        campaign_output: Path,
        workload_id: str,
        public_task: dict,
        cells: tuple[ScheduledRun, ...],
        config: CodingTaskConfig,
        policy_bundle: FrozenPolicyBundle | CalibrationTrialBundle | None = None,
    ):
        config.validate()
        if (
            not cells
            or type(cells) is not tuple
            or any(
                c.workload_id != workload_id
                or c.task_id != TASK
                or c.arm not in (*SUPPORTED_ARMS, *CALIBRATED_ARMS)
                or (c.arm in CALIBRATED_ARMS and policy_bundle is None)
                for c in cells
            )
            or len({cell_identity(c) for c in cells}) != len(cells)
            or (
                not isinstance(policy_bundle, CalibrationTrialBundle)
                and len({c.key for c in cells}) != len(cells)
            )
            or len({c.ordinal for c in cells}) != len(cells)
            or tuple(sorted(cells, key=lambda c: c.ordinal)) != cells
            or set(public_task) != PUBLIC_FIELDS
            or any(not isinstance(v, str) for v in public_task.values())
            or public_task.get("instance_id") != TASK
            or public_task.get("base_commit") != BASE_COMMIT
        ):
            raise CampaignStopped("coding session lacks a supported frozen schedule")
        if isinstance(policy_bundle, CalibrationTrialBundle):
            policy_bundle.validate_cells(cells)
        self.transport, self.source, self.dataset = transport, source.resolve(), dataset
        self.pytest_wheels, self.output = pytest_wheels, output.resolve()
        self.campaign_output, self.workload_id = campaign_output.resolve(), workload_id
        self.public_task = json.loads(json.dumps(public_task))
        self.cells, self.config = cells, config
        self.policy_bundle = policy_bundle
        self._policy_lock_sha256 = (
            policy_bundle.lock["sha256"] if policy_bundle else None
        )
        self._next, self._entered, self._failed = 0, False, False
        self.results = []

    def __enter__(self):
        if self._entered or self._failed or self.output.exists():
            raise CampaignStopped("coding session requires a fresh output directory")
        models = frozenset({self.config.strong_model, self.config.cheap_model})
        if not models <= self.transport.contracts.keys() or any(
            self.transport.contracts[m].max_output_tokens < self.config.output_cap
            for m in models
        ):
            raise CampaignStopped("coding models/caps exceed the enrolled contracts")
        endpoint = urlsplit(self.transport.http.base_url)
        if (
            self.config.network_policy == "loopback_only"
            and (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1")
        ) or (
            self.config.network_policy == "provider_allowed"
            and self.transport.http.base_url != "https://openrouter.ai/api/v1"
        ):
            raise CampaignStopped("coding endpoint differs from its network policy")
        self.output.mkdir(mode=0o700, parents=False, exist_ok=False)
        self._stack = ExitStack()
        try:
            self._ledger_event_start = len(read_snapshot(self.transport.ledger).events)
            if any(self.storage_for_cell(c).exists() for c in self.cells):
                raise CampaignStopped("coding arm stores must be fresh at enrollment")
            self.upstream = source_manifest(self.source)
            self.grader = self._stack.enter_context(
                RequestsGradingSession(
                    self.dataset,
                    self.output / "grading",
                    self.pytest_wheels,
                )
            )
            if not self.grader.admitted:
                raise CampaignStopped(
                    "coding environment failed admission before acquisition"
                )
            self.enrollment = {
                "schema_version": 1,
                "record_type": "coding_worker_enrollment",
                "upstream_source": self.upstream,
                "public_task_sha256": digest(self.public_task),
                "grading_provenance": self.grader.provenance,
                "configuration": asdict(self.config),
                "calibration_lock": self.policy_bundle.lock
                if self.policy_bundle
                else None,
                "schedule": [asdict(c) for c in self.cells],
                "network_policy": self.config.network_policy,
                "transport_session_id": self.transport.session_id,
                "transport_stage": self.transport.stage,
                "stage_cap_usd": str(self.transport.cap),
                "provider_response_cache": False,
                "original_paid_pilot_reused": False,
                "runtime": self._runtime_manifest(),
            }
            self.enrollment_sha256 = digest(self.enrollment)
            self._public_task_sha256 = self.enrollment["public_task_sha256"]
            private_write(self.output / "enrollment.json", self.enrollment)
            self.binding = self._stack.enter_context(
                CodingCampaignBinding(
                    transport=self.transport,
                    source=self.source,
                    model_pair=models,
                    output_cap=self.config.output_cap,
                    max_steps=self.config.step_limit,
                    cohort_sha256=self.enrollment_sha256,
                )
            )
            self._entered = True
            return self
        except BaseException:
            self._failed = True
            self._stack.close()
            raise

    @staticmethod
    def _runtime_manifest():
        import agentc

        sdk = Path(agentc.__file__).resolve().parent
        sdk_files = {
            p.relative_to(sdk).as_posix(): sha256(p.read_bytes()).hexdigest()
            for p in sorted(sdk.rglob("*"))
            if p.is_file() and p.suffix in {".py", ".pyi", ".so"}
        }
        bench = Path(__file__).parent
        harness = {
            name: sha256((bench / name).read_bytes()).hexdigest()
            for name in (
                "campaign_accounting.py",
                "campaign_chat.py",
                "campaign_http.py",
                "campaign_transport.py",
                "campaign_records.py",
                "joint_campaign.py",
                "coding_campaign_binding.py",
                "coding_campaign_worker.py",
                "coding_task_worker.py",
                "coding_submission.py",
                "requests_fixture_preflight.py",
                "_legacy_pytest_reporter.py",
                "policy_calibration.py",
                "policy_calibration_acquisition.py",
            )
        }
        return {
            "python_version": platform.python_version(),
            "sdk_files": sdk_files,
            "sdk_closure_sha256": digest(sdk_files),
            "harness_files": harness,
            "harness_closure_sha256": digest(harness),
            "distributions": sorted(
                (d.metadata["Name"], d.version)
                for d in importlib.metadata.distributions()
            ),
        }

    @contextmanager
    def _runtime_environment(self, arm):
        previous = {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
        for key in previous:
            os.environ.pop(key)
        rules = list(self.config.rewrite_rules) if arm != "routing_only" else []
        if arm in {"routing_only", "current_greedy", "joint_guarded"}:
            rules.insert(0, "ModelDowngrade")
        os.environ.update(
            AGENTC_OPTIMIZE="1" if arm in OPTIMIZED_ARMS else "0",
            AGENTC_COMPOSE="1",
            AGENTC_MODEL_CATALOG_OFFLINE="1",
            AGENTC_EVAL_PLANNER_MODE=arm
            if arm in CALIBRATED_ARMS
            else "current_greedy"
            if arm == "current_greedy"
            else "joint_guarded",
            AGENTC_ENABLED_RULES=",".join(rules),
            AGENTC_OPTIMIZE_EXPLORATION="1",
            AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION="completion_first",
            AGENTC_OPTIMIZE_SHADOW="0.02",
            AGENTC_OPTIMIZE_SELECTION_AUDIT="1",
            AGENTC_OPTIMIZE_MAX_OVERHEAD_MS=str(self.config.max_overhead_ms),
        )
        try:
            yield
        finally:
            for key in tuple(os.environ):
                if key.startswith("AGENTC_"):
                    os.environ.pop(key)
            os.environ.update(previous)

    def _validate_request(self, request):
        if not self._entered or self._failed or self._next >= len(self.cells):
            raise CampaignStopped("coding session cannot run or resume this cell")
        if (
            hasattr(self, "_public_task_sha256")
            and digest(self.public_task) != self._public_task_sha256
        ):
            raise CampaignStopped("coding public task changed after enrollment")
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
        }
        if (
            any(request.get(k) != v for k, v in expected.items())
            or Path(request["storage_path"]).resolve() != self.storage_for_cell(cell)
            or request.get("workload_configuration")
            != json.loads(json.dumps(asdict(self.config)))
            or request.get("arm_configuration")
            or request["workload_provenance"].get("upstream_commit") != MINI_REVISION
            or request["workload_provenance"].get("task_universe_sha256")
            != DATASET_SHA256
        ):
            raise CampaignError("coding request differs from the frozen enrollment")
        if request["stage"] in {"C", "P", "T"} or request["paper_evidence"]:
            raise CampaignError("coding session has no measured policy-safety sensor")
        if self.config.network_policy == "loopback_only" and request["stage"] != "E0":
            raise CampaignError("loopback coding cells are Stage E0 diagnostics only")
        if (
            cell.arm in CALIBRATED_ARMS
            or getattr(self, "policy_bundle", None) is not None
        ):
            if (
                self.policy_bundle is None
                or request.get("calibration_lock") != self.policy_bundle.lock
                or self.policy_bundle.lock["sha256"] != self._policy_lock_sha256
            ):
                raise CampaignError(
                    "coding scheduled policy lock differs from enrollment"
                )
            self.policy_bundle.authorize(
                workload_id=self.workload_id,
                models=request["model_pair"],
                task_ids=(cell.task_id,),
                stage=request["stage"],
                paper_evidence=request["paper_evidence"],
                network_policy=request["network_policy"],
                output_cap=self.config.output_cap,
                task_universe_sha256=DATASET_SHA256,
                protocol_sha256=request["protocol_sha256"],
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

    def storage_for_cell(self, cell):
        if isinstance(getattr(self, "policy_bundle", None), CalibrationTrialBundle):
            return self.policy_bundle.storage_path(
                self.campaign_output / "state" / self.workload_id, cell.ordinal
            )
        return _cell_storage_path(self.campaign_output, cell)

    def call_receipts(self, cell_id):
        if (
            not isinstance(cell_id, str)
            or len(cell_id) != 64
            or any(c not in "0123456789abcdef" for c in cell_id)
        ):
            raise CampaignError("invalid calibration receipt cell")
        return json.loads((self.output / cell_id / "calls.json").read_bytes())

    def run(self, request):
        import agentc

        try:
            cell = self._validate_request(request)
            identity = cell_identity(cell)
            path = self.output / identity
            started = time.perf_counter_ns()
            from agentc._evaluation_policy import evaluation_policy_scope

            policy_scope = (
                evaluation_policy_scope(
                    self.policy_bundle.policy(cell.arm, schedule_ordinal=cell.ordinal)
                )
                if cell.arm in CALIBRATED_ARMS
                else nullcontext()
            )
            with self._runtime_environment(cell.arm), policy_scope:
                random.seed(cell.run_seed)
                initialized = False
                try:
                    if cell.arm not in {"unmodified_fixed_strong", "fixed_cheap"}:
                        agentc.init(storage_path=request["storage_path"])
                        initialized = True
                    model = self.binding.model(
                        task_id=cell.task_id,
                        arm=cell.arm,
                        cell_id=identity,
                        requested_model=self.config.cheap_model
                        if cell.arm == "fixed_cheap"
                        else self.config.strong_model,
                        optimize=cell.arm in OPTIMIZED_ARMS,
                        calibration_metadata=self.policy_bundle.receipt_metadata(
                            cell.ordinal
                        )
                        if isinstance(self.policy_bundle, CalibrationTrialBundle)
                        else None,
                    )
                    submission = run_task(
                        source=self.source,
                        public_task=self.public_task,
                        model=model,
                        output=path,
                        image=TASK_IMAGE,
                        image_id=TASK_IMAGE_ID,
                        dataset_sha256=DATASET_SHA256,
                        step_limit=self.config.step_limit,
                        acquisition_kind="owned_coding_campaign",
                        acquisition_context={
                            "cell_id": identity,
                            "transport_session_id": self.transport.session_id,
                        },
                    )
                    self.binding.assert_complete()
                    snapshot = read_snapshot(self.transport.ledger)
                finally:
                    if initialized:
                        agentc.shutdown()
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            calls = cell_calls(
                snapshot,
                session_id=self.transport.session_id,
                cell_id=identity,
                plans=[p for p in self.binding.plans if p["cell_id"] == identity],
            )
            total = sum(
                (money(c["billed_result"]["cost_usd"]) for c in calls), Decimal(0)
            )
            if total != self.binding._charges.get(identity, Decimal(0)):
                raise CampaignStopped(
                    "coding model cost omits owned physical invocations"
                )
            private_write(path / "calls.json", calls)
            grading_started = time.perf_counter_ns()
            grade = self.grader.grade(path / "submission.json")
            validate_grade(grade, submission, path / "submission.json")
            grading_ms = (time.perf_counter_ns() - grading_started) / 1_000_000
            result = {
                "schema_version": 1,
                "record_type": "task",
                "task_status": "completed"
                if submission["upstream_status"] == "Submitted"
                else "failed",
                "official_score": float(grade["resolved"]),
                "end_to_end_latency_ms": elapsed_ms,
                "latency_scope": "agentc_initialization_upstream_agent_tools_provider_drain_cleanup; grading_excluded",
                "grading_latency_ms": grading_ms,
                "termination_reason": submission["upstream_status"],
                "safety_failure": None,
                "metric_availability": {"safety_failure": False},
                "unavailable_metric_reasons": {
                    "safety_failure": "no_policy_safety_sensor"
                },
                "cell_id": identity,
                "acquisition_enrollment_sha256": self.enrollment_sha256,
                "official_task_sha256": digest(self.public_task),
                "submission_sha256": digest(submission),
                "official_grade_sha256": digest(grade),
                "calls_sha256": digest(calls),
                "provider_call_count": len(calls),
                "all_call_cost_usd": str(total),
                "network_calls": 0,
                "synthetic_billing": self.config.network_policy == "loopback_only",
                "conformance": {
                    "worker_kind": "pinned_mini_swe_owned_requests_acquisition_v1",
                    "upstream_source_modified": False,
                    "official_task": True,
                    "official_score": True,
                    "provider_accounting": self.config.network_policy
                    == "provider_allowed",
                },
            }
            normalized = validate_worker_records(
                [
                    result,
                    *normalize_owned_calls(
                        calls,
                        request,
                        cell_id=identity,
                        optimized_arms=OPTIMIZED_ARMS,
                        fixed_schema="mini-swe-fixed-dispatch-policy-v1",
                        call_site_prefix="microsweagent.models.owned.query/",
                    ),
                ],
                request,
            )
            private_write(path / "normalized.json", normalized)
            self.results.append(result)
            self._next += 1
            return normalized
        except BaseException:
            self._failed = True
            raise

    def __exit__(self, *exc):
        error = None
        try:
            if self._failed and exc[0] is None:
                raise CampaignStopped("coding session has a failed cell outcome")
            if not self._failed:
                self.binding.assert_complete()
                if self._next != len(self.cells):
                    raise CampaignStopped("coding schedule has incomplete cells")
                if source_manifest(self.source) != self.upstream:
                    raise CampaignStopped("coding upstream changed during acquisition")
                if self._runtime_manifest() != self.enrollment["runtime"]:
                    raise CampaignStopped(
                        "coding acquisition runtime changed after enrollment"
                    )
                if (
                    self.policy_bundle.lock["sha256"] if self.policy_bundle else None
                ) != self._policy_lock_sha256:
                    raise CampaignStopped(
                        "coding policy authorization changed after enrollment"
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
                if (
                    len(bills) != sum(r["provider_call_count"] for r in self.results)
                    or any(
                        b["metadata"].get("cell_id")
                        not in {r["cell_id"] for r in self.results}
                        for b in bills
                    )
                    or sum((money(b["cost_usd"]) for b in bills), Decimal(0)) != total
                ):
                    raise CampaignStopped(
                        "coding schedule omits owned billed invocations"
                    )
        except BaseException as caught:
            self._failed, error = True, caught
        finally:
            self._entered = False
            # All financial and owned-container gates run before any completion seal.
            self._stack.__exit__(*exc)
        if error is not None:
            raise error
        if exc[0] is None and not self._failed:
            private_write(
                self.output / "completion.json",
                {
                    "schema_version": 1,
                    "record_type": "coding_worker_completion",
                    "enrollment_sha256": self.enrollment_sha256,
                    "completed_cells": self._next,
                    "results_sha256": digest(self.results),
                    "transport_session_id": self.transport.session_id,
                    "all_call_cost_usd": str(total),
                },
            )
