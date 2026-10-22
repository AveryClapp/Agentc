"""Official pinned retail task cells under one live campaign transport.

This worker owns a fresh, ordered cohort and isolates native stores by arm and
repetition. It never enrolls another budget stage, resumes a cohort, retries a
task, changes grading, or substitutes a synthetic task for an official task.
Outputs are acquisition artifacts, not the joint campaign's normalized schema:
candidate enumeration remains unavailable; policy safety is an opt-in audit.
"""

from __future__ import annotations

import math
import hashlib
import json
import os
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from decimal import Decimal
from importlib.metadata import distributions
from pathlib import Path
from urllib.parse import urlsplit

from bench.campaign_accounting import canonical, digest, money, read_snapshot
from bench.campaign_records import cell_calls
from bench.campaign_transport import CampaignStopped, CampaignTransport
from bench.tau_campaign_binding import TauCampaignBinding
from bench.tau_campaign_preflight import source_manifest
from bench.tau_judge import POLICY as JUDGE_POLICY
from bench.tau_safety import (
    RetailSafetyCapture,
    TauSafetyConfig,
    SafetyEvidenceError,
    REVIEW_PROMPT,
    mechanical_findings,
    review_input,
    parse_review,
    safety_report,
)
from bench.policy_calibration import (
    ARMS as CALIBRATED_ARMS,
    CalibrationTrialBundle,
    FrozenPolicyBundle,
)

JUDGE_MODEL = "openai/gpt-4.1-2025-04-14"
SUPPORTED_ARMS = (
    "unmodified_fixed_strong",
    "trace_only_fixed_strong",
    "fixed_cheap",
    "routing_only",
    "rewrite_only_fixed_strong",
    "current_greedy",
    "joint_guarded",
)
OPTIMIZED_ARMS = frozenset((*SUPPORTED_ARMS[3:], *CALIBRATED_ARMS))
_RUNTIME_OVERRIDES = frozenset(
    {
        "AGENTC_OPTIMIZE_HOT_THRESHOLD",
        "AGENTC_OPTIMIZE_COST_MODEL_WINDOW",
        "AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW",
        "AGENTC_OPTIMIZE_DIVERGENCE_WINDOW",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS",
    }
)


def private_write(path: Path, value) -> None:
    # Exclusive creation makes an interrupted cell visibly unfinished. Never
    # overwrite a previous result or replace its official score on retry.
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


@dataclass(frozen=True)
class TauCell:
    task_id: str
    arm: str
    repetition: int
    seed: int
    ordinal: int

    @property
    def cell_id(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class TauTaskConfig:
    task_ids: tuple[str, ...]
    task_inventory_sha256: str
    strong_model: str
    cheap_model: str
    user_model: str
    agent_output_cap: int = 1024
    user_output_cap: int = 1024
    judge_output_cap: int = 1024
    max_steps: int = 200
    max_errors: int = 10
    task_timeout_seconds: float | None = None
    agent_temperature: float = 0.0
    user_temperature: float = 0.0
    rewrite_rules: tuple[str, ...] = ("OutputBudget", "ContextCompress", "StateDrop")
    runtime_overrides: tuple[tuple[str, str], ...] = ()
    network_policy: str = "provider_allowed"

    def validate(self) -> None:
        if (
            type(self.task_ids) is not tuple
            or not self.task_ids
            or any(not isinstance(task, str) or not task for task in self.task_ids)
            or len(set(self.task_ids)) != len(self.task_ids)
            or not isinstance(self.task_inventory_sha256, str)
            or len(self.task_inventory_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.task_inventory_sha256)
            or any(
                not isinstance(model, str) or not model.startswith("openai/")
                for model in (self.strong_model, self.cheap_model, self.user_model)
            )
            or self.strong_model == self.cheap_model
            or any(
                type(cap) is not int or not 0 < cap <= 2048
                for cap in (
                    self.agent_output_cap,
                    self.user_output_cap,
                    self.judge_output_cap,
                )
            )
            or type(self.max_steps) is not int
            or self.max_steps <= 0
            or type(self.max_errors) is not int
            or self.max_errors <= 0
            or (
                self.task_timeout_seconds is not None
                and (
                    type(self.task_timeout_seconds) not in (int, float)
                    or not math.isfinite(self.task_timeout_seconds)
                    or self.task_timeout_seconds <= 0
                )
            )
            or any(
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= 2
                for value in (self.agent_temperature, self.user_temperature)
            )
            or type(self.rewrite_rules) is not tuple
            or not self.rewrite_rules
            or any(
                rule not in {"OutputBudget", "ContextCompress", "StateDrop"}
                for rule in self.rewrite_rules
            )
            or len(set(self.rewrite_rules)) != len(self.rewrite_rules)
            or type(self.runtime_overrides) is not tuple
            or any(
                type(pair) is not tuple
                or len(pair) != 2
                or pair[0] not in _RUNTIME_OVERRIDES
                or not isinstance(pair[1], str)
                or not pair[1]
                for pair in self.runtime_overrides
            )
            or len(dict(self.runtime_overrides)) != len(self.runtime_overrides)
            or self.network_policy not in {"provider_allowed", "loopback_only"}
        ):
            raise CampaignStopped("invalid frozen Tau task configuration")
        for key, value in self.runtime_overrides:
            try:
                numeric = float(value)
                if not math.isfinite(numeric) or numeric <= 0:
                    raise ValueError
                if key != "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS" and (
                    not value.isdecimal() or int(value) > 2**32 - 1
                ):
                    raise ValueError
            except ValueError:
                raise CampaignStopped("invalid frozen runtime override") from None


class TauTaskWorker:
    def __init__(
        self,
        *,
        transport: CampaignTransport,
        source: Path,
        output: Path,
        config: TauTaskConfig,
        cells: tuple[TauCell, ...],
        storage_root: Path | None = None,
        policy_bundle: FrozenPolicyBundle | CalibrationTrialBundle | None = None,
        safety_config: TauSafetyConfig | None = None,
    ):
        config.validate()
        if safety_config is not None:
            safety_config.validate()
        if (
            type(cells) is not tuple
            or not cells
            or any(not isinstance(cell, TauCell) for cell in cells)
            or any(
                cell.task_id not in config.task_ids
                or cell.arm not in (*SUPPORTED_ARMS, *CALIBRATED_ARMS)
                or (cell.arm in CALIBRATED_ARMS and policy_bundle is None)
                or any(
                    type(n) is not int or n < 0
                    for n in (cell.repetition, cell.seed, cell.ordinal)
                )
                for cell in cells
            )
            or (
                not isinstance(policy_bundle, CalibrationTrialBundle)
                and len({(c.task_id, c.arm, c.repetition) for c in cells}) != len(cells)
            )
            or [c.ordinal for c in cells] != sorted({c.ordinal for c in cells})
            or {c.task_id for c in cells} != set(config.task_ids)
            or threading.current_thread() is not threading.main_thread()
        ):
            raise CampaignStopped("invalid fresh ordered Tau cohort")
        if isinstance(policy_bundle, CalibrationTrialBundle):
            policy_bundle.validate_cells(cells)
        self.transport, self.source = transport, source.resolve()
        self.policy_bundle = policy_bundle
        self.safety_config = safety_config
        self._policy_lock_sha256 = (
            policy_bundle.lock["sha256"] if policy_bundle else None
        )
        self._authorized_policy_cell = None
        self.output, self.config, self.cells = output.resolve(), config, cells
        self.storage_root = (
            storage_root.resolve()
            if storage_root is not None
            else self.output / "runtime"
        )
        self._campaign_storage = storage_root is not None
        self._next = 0
        self._failed = False
        self._entered = False
        self._running = False
        self._plans: list[dict] = []
        self._plan_lock = threading.Lock()
        self._stores: set[Path] = set()
        self._results: list[dict] = []

    def _capture_plan(self, event: dict) -> None:
        with self._plan_lock:
            if not self._running or event["cell_id"] != self.cells[self._next].cell_id:
                raise CampaignStopped("planning escaped its active task cell")
            audit = json.loads(event["native_plan_json"]).get("agentc_selection_audit")
            arm = self.cells[self._next].arm
            required = arm in OPTIMIZED_ARMS and arm != "current_greedy"
            available = isinstance(audit, dict) and audit.get("schema_version") == 1
            if required and not available:
                raise CampaignStopped("native joint-selection audit is unavailable")
            self._plans.append(
                {
                    **event,
                    "selection_audit": audit,
                    "selection_audit_available": available,
                }
            )

    def __enter__(self):
        if self._entered or self._failed or self.output.exists():
            raise CampaignStopped("Tau worker requires an unused output directory")
        from agentc._lifecycle import is_initialized

        if is_initialized():
            raise CampaignStopped("Tau worker requires an inactive Agentc lifecycle")
        models = {
            self.config.strong_model,
            self.config.cheap_model,
            self.config.user_model,
            JUDGE_MODEL,
        }
        caps = {
            "agent": self.config.agent_output_cap,
            "user": self.config.user_output_cap,
            "judge": self.config.judge_output_cap,
        }
        if not models <= self.transport.contracts.keys() or any(
            self.transport.contracts[model].max_output_tokens < cap
            for model, cap in (
                (self.config.strong_model, caps["agent"]),
                (self.config.cheap_model, caps["agent"]),
                (self.config.user_model, caps["user"]),
                (JUDGE_MODEL, caps["judge"]),
            )
        ):
            raise CampaignStopped(
                "Tau task models or caps are outside enrolled contracts"
            )
        endpoint = urlsplit(self.transport.http.base_url)
        if (
            self.config.network_policy == "loopback_only"
            and (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1")
        ) or (
            self.config.network_policy == "provider_allowed"
            and self.transport.http.base_url != "https://openrouter.ai/api/v1"
        ):
            raise CampaignStopped("Tau endpoint differs from its frozen network policy")
        aliases = {model: model for model in models}
        aliases["gpt-4.1-2025-04-14"] = JUDGE_MODEL
        self.binding = TauCampaignBinding(
            transport=self.transport,
            source=self.source,
            model_aliases=aliases,
            actor_output_caps=caps,
            plan_observer=self._capture_plan,
            agent_model_allowlist=frozenset(
                {self.config.strong_model, self.config.cheap_model}
            ),
        )
        self.binding.__enter__()
        try:
            from tau2.runner.helpers import load_tasks

            manifest = source_manifest(self.source)
            self._source_manifest = manifest
            inventory = "data/tau2/domains/retail/tasks.json"
            if manifest["files"][inventory] != self.config.task_inventory_sha256:
                raise CampaignStopped(
                    "official Tau task inventory differs from enrollment"
                )
            tasks = load_tasks("retail", task_split_name=None)
            self.tasks = {task.id: task for task in tasks}
            self._task_hashes = {
                task.id: digest(task.model_dump(mode="json")) for task in tasks
            }
            if (
                len(self.tasks) != len(tasks)
                or not set(self.config.task_ids) <= self.tasks.keys()
            ):
                raise CampaignStopped(
                    "Tau cohort contains missing or duplicate official tasks"
                )
            self.transport.assert_complete()
            self.output.mkdir(mode=0o700, parents=False, exist_ok=False)
            enrollment = {
                "schema_version": 1,
                "record_type": "tau_worker_enrollment",
                "config": asdict(self.config),
                "cells": [asdict(c) for c in self.cells],
                "source": manifest,
                "runtime": self._runtime_manifest(),
                "transport_session_id": self.transport.session_id,
                "transport_stage": self.transport.stage,
                "stage_cap_usd": str(self.transport.cap),
                "storage_layout": "campaign_state_v1"
                if self._campaign_storage
                else "tau_runtime_v1",
                "judge_policy": JUDGE_POLICY,
                "safety_sensor": asdict(self.safety_config)
                if self.safety_config
                else None,
                "paper_evidence": False,
                "calibration_lock": policy_bundle.lock
                if (policy_bundle := self.policy_bundle)
                else None,
                "inventory_task_count": len(tasks),
                "inventory_tasks_with_nl_assertions": sum(
                    bool(t.evaluation_criteria.nl_assertions) for t in tasks
                ),
            }
            private_write(self.output / "enrollment.json", enrollment)
            self.enrollment_sha256 = digest(enrollment)
            self._enrolled_runtime = enrollment["runtime"]
            self._enrolled_safety_sensor = enrollment["safety_sensor"]
            self._entered = True
            return self
        except BaseException:
            self._failed = True
            self.binding.__exit__(*sys.exc_info())
            raise

    @staticmethod
    def _runtime_manifest() -> dict:
        import agentc
        from agentc import _native
        import platform

        root = Path(agentc.__file__).resolve().parent
        code = {
            path.relative_to(root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(root.rglob("*.py"))
        }
        code[Path(_native.__file__).name] = hashlib.sha256(
            Path(_native.__file__).read_bytes()
        ).hexdigest()
        bench = Path(__file__).resolve().parent
        harness = {
            name: hashlib.sha256((bench / name).read_bytes()).hexdigest()
            for name in (
                "campaign_accounting.py",
                "campaign_chat.py",
                "campaign_http.py",
                "campaign_transport.py",
                "campaign_records.py",
                "joint_campaign.py",
                "tau_campaign_binding.py",
                "tau_campaign_worker.py",
                "tau_judge.py",
                "tau_task_worker.py",
                "tau_safety.py",
                "policy_calibration.py",
                "policy_calibration_acquisition.py",
            )
        }
        return {
            "python_version": platform.python_version(),
            "sdk_files": code,
            "sdk_closure_sha256": digest(code),
            "harness_files": harness,
            "harness_closure_sha256": digest(harness),
            "installed_distributions": sorted(
                (dist.metadata["Name"], dist.version) for dist in distributions()
            ),
        }

    def _review_safety(self, capture, simulation, cell, path):
        from tau2.data_model.message import SystemMessage, UserMessage

        private_write(
            path / "safety-trajectory.json", simulation.model_dump(mode="json")
        )
        try:
            evidence = capture.evidence(
                simulation.model_dump(mode="json"),
                source_revision=self._source_manifest["revision"],
                source_closure_sha256=self._source_manifest["closure_sha256"],
                cell_id=cell.cell_id,
            )
            integrity = (
                source_manifest(self.source) == self._source_manifest
                and self._runtime_manifest() == self._enrolled_runtime
            )
            findings = mechanical_findings(evidence, integrity_verified=integrity)
            review = review_input(evidence, findings)
            private_write(path / "safety-evidence.json", evidence)
            private_write(path / "safety-review-request.json", review)
        except SafetyEvidenceError as error:
            report = {
                "record_type": "tau_safety_report",
                "safety_failure": None,
                "metric_available": False,
                "unknown_reason": type(error).__name__,
                "findings": {},
            }
            private_write(path / "safety-report.json", report)
            return report
        response = self.binding.generate_safety_review(
            evidence_sha256=review["evidence_sha256"],
            model=self.safety_config.review_model,
            messages=[
                SystemMessage(role="system", content=REVIEW_PROMPT),
                UserMessage(role="user", content=canonical(review).decode()),
            ],
            temperature=0.0,
            max_tokens=self.config.judge_output_cap,
            response_format={"type": "json_object"},
        )
        private_write(
            path / "safety-review-response.json", response.model_dump(mode="json")
        )
        try:
            parsed = parse_review(response.content, review)
            report = safety_report(evidence, findings, review=parsed)
        except SafetyEvidenceError:
            report = safety_report(
                evidence,
                findings,
                unknown_reason="invalid_or_incomplete_semantic_review",
            )
        private_write(path / "safety-report.json", report)
        return report

    @contextmanager
    def _runtime_environment(self, arm: str):
        previous = {
            key: value for key, value in os.environ.items() if key.startswith("AGENTC_")
        }
        for key in previous:
            os.environ.pop(key)
        rules = list(self.config.rewrite_rules) if arm != "routing_only" else []
        if arm in {"routing_only", "current_greedy", "joint_guarded"}:
            rules.insert(0, "ModelDowngrade")
        os.environ.update(
            AGENTC_OPTIMIZE="1" if arm in OPTIMIZED_ARMS else "0",
            AGENTC_COMPOSE="1",
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
        )
        os.environ.update(dict(self.config.runtime_overrides))
        try:
            yield {
                key: value
                for key, value in os.environ.items()
                if key.startswith("AGENTC_")
            }
        finally:
            for key in tuple(os.environ):
                if key.startswith("AGENTC_"):
                    os.environ.pop(key)
            os.environ.update(previous)

    def _assert_safety_enrollment(self):
        current = asdict(self.safety_config) if self.safety_config else None
        if current != self._enrolled_safety_sensor:
            raise CampaignStopped("Tau safety sensor changed after enrollment")

    def run_next(self) -> dict:
        if (
            not self._entered
            or self._failed
            or self._running
            or self._next >= len(self.cells)
            or threading.current_thread() is not threading.main_thread()
        ):
            raise CampaignStopped("Tau worker cannot run or resume this cell")
        self._assert_safety_enrollment()
        import agentc
        from tau2.data_model.simulation import TextRunConfig
        from tau2.runner.build import build_text_orchestrator
        from tau2.runner.simulation import run_simulation

        cell = self.cells[self._next]
        if (
            cell.arm in CALIBRATED_ARMS
            or isinstance(self.policy_bundle, CalibrationTrialBundle)
        ) and self._authorized_policy_cell != cell.cell_id:
            raise CampaignStopped(
                "calibrated Tau cell lacks its scheduled lock authorization"
            )
        self._authorized_policy_cell = None
        path = self.output / cell.cell_id
        self._running = True
        self._plans = []
        started = time.perf_counter_ns()
        try:
            path.mkdir(mode=0o700, exist_ok=False)
            private_write(
                path / "request.json",
                {
                    **asdict(cell),
                    "cell_id": cell.cell_id,
                    "enrollment_sha256": self.enrollment_sha256,
                },
            )
            if source_manifest(self.source) != self._source_manifest:
                raise CampaignStopped("pinned Tau source or data changed during cohort")
            task = self.tasks[cell.task_id].model_copy(deep=True)
            task_digest = digest(task.model_dump(mode="json"))
            if task_digest != self._task_hashes[cell.task_id]:
                raise CampaignStopped(
                    "official Tau task object changed after enrollment"
                )
            store_key = self.storage_for_cell(cell)
            store = self.storage_for_cell(cell)
            if store_key not in self._stores and store.exists():
                raise CampaignStopped("Tau arm store contains unenrolled warm state")
            model = (
                self.config.cheap_model
                if cell.arm == "fixed_cheap"
                else self.config.strong_model
            )
            run_config = TextRunConfig(
                domain="retail",
                agent="llm_agent",
                user="user_simulator",
                llm_agent=model,
                llm_user=self.config.user_model,
                llm_args_agent={
                    "temperature": self.config.agent_temperature,
                    "max_tokens": self.config.agent_output_cap,
                },
                llm_args_user={
                    "temperature": self.config.user_temperature,
                    "max_tokens": self.config.user_output_cap,
                },
                seed=cell.seed,
                max_steps=self.config.max_steps,
                max_errors=self.config.max_errors,
                timeout=self.config.task_timeout_seconds,
            )
            with self._runtime_environment(cell.arm) as runtime:
                try:
                    if cell.arm not in {"unmodified_fixed_strong", "fixed_cheap"}:
                        agentc.init(
                            capture_content=False,
                            capture_embeddings=False,
                            fail_open=False,
                            storage_path=str(store),
                        )
                        self._stores.add(store_key)
                    orchestrator = build_text_orchestrator(
                        run_config, task, seed=cell.seed
                    )
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
                        policy_scope,
                        self.binding.task(
                            task_id=cell.task_id,
                            arm=cell.arm,
                            optimize_agent=cell.arm in OPTIMIZED_ARMS,
                            cell_id=cell.cell_id,
                            calibration_metadata=self.policy_bundle.receipt_metadata(
                                cell.ordinal
                            )
                            if isinstance(self.policy_bundle, CalibrationTrialBundle)
                            else None,
                        ),
                    ):
                        capture_scope = (
                            RetailSafetyCapture(orchestrator)
                            if self.safety_config
                            else nullcontext()
                        )
                        with capture_scope as safety_capture:
                            simulation = run_simulation(orchestrator)
                        measured_safety = (
                            self._review_safety(safety_capture, simulation, cell, path)
                            if self.safety_config
                            else None
                        )
                    self.binding.assert_complete()
                    scope_report = (
                        agentc.optimization_scope_report()
                        if cell.arm not in {"unmodified_fixed_strong", "fixed_cheap"}
                        else None
                    )
                finally:
                    agentc.shutdown()
            self.transport.assert_complete()
            snapshot = read_snapshot(
                self.transport.ledger, expected_key_id=self.transport.key_id
            )
            calls = cell_calls(
                snapshot,
                session_id=self.transport.session_id,
                cell_id=cell.cell_id,
                plans=self._plans,
            )
            if source_manifest(self.source) != self._source_manifest:
                raise CampaignStopped("pinned Tau source or data changed during cell")
            if digest(task.model_dump(mode="json")) != task_digest:
                raise CampaignStopped("official Tau task object changed during cell")
            if (
                simulation.reward_info is None
                or not math.isfinite(simulation.reward_info.reward)
                or not 0 <= simulation.reward_info.reward <= 1
            ):
                raise CampaignStopped("Tau returned no valid official reward")
            simulation_data = simulation.model_dump(mode="json")
            private_write(path / "simulation.json", simulation_data)
            private_write(path / "calls.json", calls)
            private_write(path / "planning.json", self._plans)
            termination = simulation.termination_reason.value
            result = {
                "schema_version": 1,
                "record_type": "tau_task_result",
                **asdict(cell),
                "cell_id": cell.cell_id,
                "official_task_sha256": task_digest,
                "enrollment_sha256": self.enrollment_sha256,
                "task_status": "completed"
                if termination in {"user_stop", "agent_stop"}
                else "incomplete",
                "termination_reason": termination,
                "official_score": simulation.reward_info.reward,
                "reward_info": simulation_data["reward_info"],
                "end_to_end_latency_ms": (time.perf_counter_ns() - started) / 1e6,
                "latency_scope": "task_cell_with_lifecycle_and_accounting",
                "simulation_duration_ms": simulation.duration * 1000,
                "agentc_runtime_initialized": cell.arm
                not in {"unmodified_fixed_strong", "fixed_cheap"},
                "provider_call_count": len(calls),
                "call_counts": dict(
                    Counter(
                        (call["metadata"]["actor"] + "/" + call["metadata"]["kind"])
                        for call in calls
                    )
                ),
                "all_call_cost_usd": str(
                    sum(
                        (money(call["billed_result"]["cost_usd"]) for call in calls),
                        Decimal(0),
                    )
                ),
                "simulation_sha256": digest(simulation_data),
                "calls_sha256": digest(calls),
                "planning_sha256": digest(self._plans),
                "journal_sha256": snapshot.sha256,
                "runtime_environment": runtime,
                "optimization_scope_report": scope_report,
                "safety_failure": measured_safety["safety_failure"]
                if measured_safety
                else None,
                "safety_metric_available": measured_safety["metric_available"]
                if measured_safety
                else False,
                "safety_report_sha256": digest(measured_safety)
                if measured_safety
                else None,
                "safety_unknown_reason": measured_safety.get("unknown_reason")
                if measured_safety
                else "no_policy_safety_sensor",
                "official_task": True,
                "upstream_source_modified": False,
                "judge_policy": JUDGE_POLICY,
                "paper_evidence": False,
                "evidence_kind": "loopback_integration"
                if self.config.network_policy == "loopback_only"
                else "provider_trial",
                "provider_accounting": self.config.network_policy == "provider_allowed",
                "synthetic_billing": self.config.network_policy == "loopback_only",
            }
            private_write(path / "result.json", result)
            self._results.append(result)
            self._next += 1
            return result
        except BaseException as exc:
            self._failed = True
            if path.is_dir() and not (path / "failure.json").exists():
                private_write(
                    path / "failure.json",
                    {
                        "cell_id": cell.cell_id,
                        "task_status": "unscored",
                        "official_score": None,
                        "failure_type": type(exc).__name__,
                        "paper_evidence": False,
                    },
                )
            raise
        finally:
            self._running = False

    def storage_for_cell(self, cell: TauCell) -> Path:
        if isinstance(self.policy_bundle, CalibrationTrialBundle):
            return self.policy_bundle.storage_path(self.storage_root, cell.ordinal)
        repetition = (
            f"rep-{cell.repetition}" if self._campaign_storage else str(cell.repetition)
        )
        return self.storage_root / cell.arm / repetition

    def __exit__(self, *exc):
        try:
            self.binding.__exit__(*exc)
            if exc[0] is None and (self._failed or self._next != len(self.cells)):
                raise CampaignStopped(
                    "Tau cohort is incomplete; no resume is permitted"
                )
            if exc[0] is None:
                accounting = self.transport.assert_complete()
                if self._runtime_manifest() != self._enrolled_runtime:
                    raise CampaignStopped(
                        "Tau acquisition runtime changed after enrollment"
                    )
                self._assert_safety_enrollment()
                if (
                    self.policy_bundle.lock["sha256"] if self.policy_bundle else None
                ) != self._policy_lock_sha256:
                    raise CampaignStopped(
                        "Tau policy authorization changed after enrollment"
                    )
                private_write(
                    self.output / "completion.json",
                    {
                        "schema_version": 1,
                        "record_type": "tau_cohort_completion",
                        "enrollment_sha256": self.enrollment_sha256,
                        "result_count": len(self._results),
                        "results_sha256": digest(self._results),
                        "provider_call_count": sum(
                            result["provider_call_count"] for result in self._results
                        ),
                        "all_call_cost_usd": str(
                            sum(
                                (
                                    money(result["all_call_cost_usd"])
                                    for result in self._results
                                ),
                                Decimal(0),
                            )
                        ),
                        "accounting": accounting,
                        "paper_evidence": False,
                    },
                )
        finally:
            self._entered = False
