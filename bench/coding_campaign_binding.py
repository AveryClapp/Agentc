"""mini-SWE-agent model queries under one owned campaign dispatcher.

Install before Agentc initialization. The model returns only the foreground
content to the upstream agent; SDK probes/shadows call the provider backend
without parsing or executing shell commands. Every physical call is charged
and attributed through the same already-enrolled CampaignTransport.
"""

from __future__ import annotations

import importlib
import json
import threading
import time
import uuid
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from bench.campaign_accounting import digest, money
from bench.campaign_chat import PARAMETERS
from bench.campaign_transport import CampaignStopped, CampaignTransport
from bench.coding_submission import MINI_REVISION
from bench.coding_task_worker import source_manifest


@dataclass(frozen=True)
class ModelConfig:
    model_name: str


@dataclass(frozen=True)
class _QueryFrame:
    task_id: str
    arm: str
    cell_id: str
    turn_id: str
    requested_model: str
    calibration_json: str | None = None


class CodingCampaignBinding:
    def __init__(
        self,
        *,
        transport: CampaignTransport,
        source: Path,
        model_pair: frozenset[str],
        output_cap: int,
        max_steps: int,
        cohort_sha256: str,
        plan_observer: Callable[[dict[str, Any]], None] | None = None,
    ):
        from agentc import _native
        from agentc._text_actions import COMPARATOR_VERSION

        if (
            getattr(_native, "optimize_text_action_comparator_version", lambda: None)()
            != COMPARATOR_VERSION
        ):
            raise CampaignStopped(
                "native shell-action identity contract is unavailable"
            )
        if (
            type(model_pair) is not frozenset
            or not model_pair
            or not model_pair <= transport.contracts.keys()
            or type(output_cap) is not int
            or not 0 < output_cap <= 2048
            or type(max_steps) is not int
            or max_steps < 1
            or not isinstance(cohort_sha256, str)
            or len(cohort_sha256) != 64
            or any(c not in "0123456789abcdef" for c in cohort_sha256)
        ):
            raise CampaignStopped("invalid frozen coding model/cohort configuration")
        self.transport, self.source = transport, source.resolve()
        self.models, self.output_cap, self.max_steps = model_pair, output_cap, max_steps
        self.cohort_sha256, self.plan_observer = cohort_sha256, plan_observer
        self.upstream = source_manifest(self.source)
        self._frame: ContextVar[_QueryFrame | None] = ContextVar(
            "agentc_coding_query", default=None
        )
        self._lock = threading.Lock()
        self._charges: dict[str, Decimal] = {}
        self._turn, self._installed, self._failed = 0, False, False
        self._patches = []
        self.plans: list[dict[str, Any]] = []

    def model(
        self,
        *,
        task_id: str,
        arm: str,
        cell_id: str,
        requested_model: str,
        optimize: bool,
        calibration_metadata: dict | None = None,
    ):
        if (
            not self._installed
            or self._failed
            or requested_model not in self.models
            or not isinstance(task_id, str)
            or not task_id
            or not isinstance(arm, str)
            or not arm
            or not isinstance(cell_id, str)
            or len(cell_id) != 64
            or any(c not in "0123456789abcdef" for c in cell_id)
            or type(optimize) is not bool
        ):
            raise CampaignStopped("invalid coding task model scope")
        from bench.campaign_records import calibration_metadata_json

        return CodingBudgetModel(
            self,
            task_id,
            arm,
            cell_id,
            requested_model,
            optimize,
            calibration_metadata_json(calibration_metadata),
        )

    def _completion(self, *args, **kwargs):
        from agentc._dispatch_context import current_provider_dispatch
        from litellm import ModelResponse

        try:
            frame = self._frame.get()
            if frame is None or self._failed or not self._installed or len(args) > 2:
                raise CampaignStopped("provider call lacks its active coding scope")
            values = dict(kwargs)
            for name, value in zip(("model", "messages"), args):
                if name in values:
                    raise CampaignStopped("duplicate coding provider parameter")
                values[name] = value
            client = {"num_retries", "caching"}
            if set(values) - {"model", "messages", *PARAMETERS, *client}:
                raise CampaignStopped("coding provider has unfrozen client features")
            if values.get("num_retries") != 0 or values.get("caching") is not False:
                raise CampaignStopped("coding provider retries/cache are forbidden")
            payload = {
                k: v for k, v in values.items() if k not in client and v is not None
            }
            if (
                payload.get("model") not in self.models
                or type(payload.get("max_tokens")) is not int
                or not 0 < payload["max_tokens"] <= self.output_cap
                or payload.get("tools")
            ):
                raise CampaignStopped("coding dispatch left its frozen model/cap seam")
            context = current_provider_dispatch()
            from agentc._text_actions import current_text_action_contract

            contract = current_text_action_contract()
            if contract is None:
                raise CampaignStopped("coding dispatch lacks its action contract")
            metadata = {
                "actor": "agent",
                "kind": context.kind if context else "primary",
                "task_id": frame.task_id,
                "arm": frame.arm,
                "cell_id": frame.cell_id,
                "turn_id": frame.turn_id,
                "requested_model": frame.requested_model,
                "text_action_comparator_version": contract.comparator_version,
                "text_action_settings_version": contract.settings_version,
            }
            if frame.calibration_json is not None:
                metadata.update(json.loads(frame.calibration_json))
            if context is not None:
                for name in (
                    "native_plan_json",
                    "parent_native_plan_json",
                    "observation_token",
                    "parent_observation_token",
                    "exploration_lease_token",
                ):
                    value = getattr(context, name)
                    if value is not None:
                        metadata[name] = value
            body = self.transport.call(
                call_id=uuid.uuid4().hex, request=payload, metadata=metadata
            )
            charge = money(body["usage"]["cost"])
            with self._lock:
                self._charges[frame.cell_id] = (
                    self._charges.get(frame.cell_id, Decimal(0)) + charge
                )
            choice = body["choices"][0]
            message = choice["message"]
            if (
                len(body["choices"]) != 1
                or choice["finish_reason"] != "stop"
                or not isinstance(message.get("content"), str)
                or message.get("tool_calls")
                or message.get("refusal") is not None
            ):
                raise CampaignStopped(
                    "billed reply is outside the coding text-action seam"
                )
            response = ModelResponse(**body)
            response._hidden_params["response_cost"] = float(charge)
            return response
        except BaseException as error:
            self._failed = True
            if not isinstance(error, Exception):
                raise
            raise CampaignStopped(
                "coding provider binding failed; details suppressed"
            ) from None

    def _planner(self, original):
        @wraps(original)
        def plan_call(call):
            started = time.perf_counter_ns()
            plan = original(call)
            elapsed_us = (time.perf_counter_ns() - started) / 1000
            try:
                frame = self._frame.get()
                if frame is None or self._failed:
                    raise CampaignStopped("coding planning has no active task scope")
                audit = json.loads(plan.raw_json).get("agentc_selection_audit")
                if not isinstance(audit, dict) or audit.get("schema_version") != 1:
                    raise CampaignStopped(
                        "coding native selection audit is unavailable"
                    )
                event = {
                    "cell_id": frame.cell_id,
                    "task_id": frame.task_id,
                    "arm": frame.arm,
                    "actor": "agent",
                    "turn_id": frame.turn_id,
                    "call_site_id": call["call_site_id"],
                    "requested_model": call["model"],
                    "planning_overhead_us": elapsed_us,
                    "planning_timing_scope": "optimizer.plan_call",
                    "native_plan_json": plan.raw_json,
                    "selection_audit": audit,
                }
                with self._lock:
                    self.plans.append(event)
                if self.plan_observer is not None:
                    self.plan_observer(event)
            except BaseException:
                self._failed = True
                raise
            return plan

        return plan_call

    def _patch(self, module, name, replacement):
        original = getattr(module, name)
        setattr(module, name, replacement)
        self._patches.append((module, name, original))

    def __enter__(self):
        from agentc._lifecycle import is_initialized
        from agentc import _optimizer

        if self._installed or self._failed or is_initialized():
            raise CampaignStopped("coding binding must precede Agentc initialization")
        if source_manifest(self.source) != self.upstream:
            raise CampaignStopped("coding source changed before enrollment")
        litellm = importlib.import_module("litellm")

        def completion(*args, **kwargs):
            return self._completion(*args, **kwargs)

        async def acompletion(*args, **kwargs):
            self._failed = True
            raise CampaignStopped("async calls are outside the pinned coding seam")

        try:
            self._patch(litellm, "completion", completion)
            self._patch(litellm, "acompletion", acompletion)
            self._patch(_optimizer, "plan_call", self._planner(_optimizer.plan_call))
            self._installed = True
            return self
        except BaseException:
            self._restore()
            raise

    def assert_complete(self):
        from agentc._patches._optimizer_glue import drain_exploration

        drain_exploration(120000)
        if self._failed:
            raise CampaignStopped(
                "coding worker has a failed provider/planning outcome"
            )
        return self.transport.assert_complete()

    def _restore(self):
        for module, name, original in reversed(self._patches):
            setattr(module, name, original)
        self._patches.clear()
        self._installed = False

    def __exit__(self, *exc):
        import agentc

        try:
            self.assert_complete()
        except BaseException:
            if exc[0] is None:
                raise
        finally:
            try:
                agentc.shutdown()
            finally:
                self._restore()


class CodingBudgetModel:
    def __init__(
        self,
        binding,
        task_id,
        arm,
        cell_id,
        requested_model,
        optimize,
        calibration_json=None,
    ):
        self.binding, self.task_id, self.arm, self.cell_id = (
            binding,
            task_id,
            arm,
            cell_id,
        )
        self.config, self.optimize = ModelConfig(requested_model), optimize
        self.calibration_json = calibration_json
        self.n_calls, self.contract = 0, None

    @property
    def cost(self):
        with self.binding._lock:
            return float(self.binding._charges.get(self.cell_id, Decimal(0)))

    def prepare_execution(self, enrollment):
        from agentc._text_actions import TextActionContract, SETTINGS_PREFIX

        if (
            self.contract is not None
            or self.n_calls
            or enrollment["upstream_revision"] != MINI_REVISION
            or enrollment["upstream_source"] != self.binding.upstream
            or enrollment["agent_configuration"]["step_limit"] != self.binding.max_steps
            or enrollment["environment"].get("forward_env") != []
        ):
            raise CampaignStopped("coding execution differs from its action contract")
        environment = {
            k: v for k, v in enrollment["environment"].items() if k != "image"
        }
        settings = {
            "schema_version": 1,
            "upstream_source": self.binding.upstream,
            "cohort_sha256": self.binding.cohort_sha256,
            "agent_configuration": enrollment["agent_configuration"],
            "environment": environment,
            "model_response_fields": ["content"],
        }
        self.contract = TextActionContract(SETTINGS_PREFIX + digest(settings))

    def query(self, messages, **kwargs):
        from agentc._text_actions import text_action_scope
        from agentc._optimization_scope import optimization_scope
        import litellm

        binding = self.binding
        if self.contract is None or kwargs or binding._failed or not binding._installed:
            binding._failed = True
            raise CampaignStopped("coding query lacks its prepared frozen execution")
        with binding._lock:
            binding._turn += 1
            turn_id = f"coding-agent-{binding._turn}"
        token = binding._frame.set(
            _QueryFrame(
                self.task_id,
                self.arm,
                self.cell_id,
                turn_id,
                self.config.model_name,
                self.calibration_json,
            )
        )
        try:
            with (
                text_action_scope(self.contract),
                optimization_scope(
                    "mini_swe.evaluated_assistant", optimize=self.optimize
                ),
            ):
                response = litellm.completion(
                    model=self.config.model_name,
                    messages=deepcopy(messages),
                    temperature=0.0,
                    max_tokens=binding.output_cap,
                    num_retries=0,
                    caching=False,
                )
            self.n_calls += 1
            return {"content": response.choices[0].message.content}
        except BaseException:
            binding._failed = True
            raise
        finally:
            binding._frame.reset(token)
