"""Own frozen SWE-agent function-calling queries without replacing its model.

The upstream query/history/response parser stays intact. Agentc wraps the
single owned LiteLLM backend, so sampled calls cannot execute agent tools.
"""

from __future__ import annotations

import importlib
import json
import sys
import threading
import uuid
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from functools import wraps
from pathlib import Path

from bench.campaign_accounting import digest, money
from bench.campaign_chat import PARAMETERS, prepare_request
from bench.campaign_transport import CampaignStopped
from bench.campaign_records import calibration_metadata_json
from bench.guard_evidence import file_sha
from bench.swebench_cohort import (
    AGENT_CLOSURE_SHA256,
    AGENT_CONFIG_SHA256,
    source_closure,
    require,
)


class CellBudgetExhausted(CampaignStopped):
    """The task cannot reserve another complete call within its USD 2 ceiling."""


def verify_agent(source: Path) -> dict:
    files = source_closure(source, ("sweagent", "config", "tools", "pyproject.toml"))
    require(digest(files) == AGENT_CLOSURE_SHA256, "frozen SWE-agent source differs")
    require(
        files["config/default.yaml"] == AGENT_CONFIG_SHA256,
        "default agent configuration differs",
    )
    return {"files": files, "closure_sha256": digest(files)}


@dataclass(frozen=True)
class _Frame:
    task_id: str
    arm: str
    cell_id: str
    requested_model: str
    optimize: bool
    turn_id: str = ""
    calibration_json: str | None = None


class SweAgentCampaignBinding:
    def __init__(
        self,
        *,
        transport,
        source: Path,
        models: frozenset[str],
        output_cap: int,
        cohort_sha256: str,
        execution_version: str | None = None,
        plan_observer=None,
        instrument: bool = True,
    ):
        if (
            type(instrument) is not bool
            or type(models) is not frozenset
            or not models
            or not models <= transport.contracts.keys()
            or type(output_cap) is not int
            or not 0 < output_cap <= 2048
            or any(
                transport.contracts[m].max_output_tokens < output_cap
                or not transport.contracts[m].cached_text_blocks
                for m in models
            )
            or not isinstance(cohort_sha256, str)
            or len(cohort_sha256) != 64
            or any(c not in "0123456789abcdef" for c in cohort_sha256)
            or execution_version is not None
            and (
                not isinstance(execution_version, str)
                or len(execution_version) != 64
                or any(c not in "0123456789abcdef" for c in execution_version)
            )
        ):
            raise CampaignStopped(
                "invalid frozen SWE-agent model/contract configuration"
            )
        self.transport, self.source = transport, source.resolve()
        self.models, self.output_cap = models, output_cap
        self.cohort_sha256, self.plan_observer = cohort_sha256, plan_observer
        self.execution_version = execution_version
        self.instrument = instrument
        self.upstream = verify_agent(self.source)
        self.settings_version = "frozen-swe-agent-v1:" + digest(
            {
                "source": self.upstream["closure_sha256"],
                "cohort": cohort_sha256,
                "output_cap": output_cap,
                "contracts": {
                    m: transport.contracts[m].record() for m in sorted(models)
                },
                "binding_sha256": file_sha(Path(__file__)),
                "chat_contract_sha256": file_sha(
                    Path(__file__).with_name("campaign_chat.py")
                ),
                "instrument": instrument,
                **(
                    {"execution_version": execution_version}
                    if execution_version is not None
                    else {}
                ),
            }
        )
        self._frame: ContextVar[_Frame | None] = ContextVar(
            "owned_swe_agent_query", default=None
        )
        self._lock = threading.Lock()
        self._spent, self._pending, self._exhausted = {}, {}, set()
        self._cells = {}
        self._execution_cells = set()
        self._turn = 0
        self._failed, self._installed = False, False
        self._patches = []
        self.plans = []

    def claim_execution(self, cell_id):
        with self._lock:
            if (
                not self._installed
                or self._failed
                or not isinstance(cell_id, str)
                or len(cell_id) != 64
                or any(c not in "0123456789abcdef" for c in cell_id)
                or cell_id in self._execution_cells
                or cell_id in self._cells
            ):
                raise CampaignStopped("SWE-agent execution cell cannot retry or resume")
            self._execution_cells.add(cell_id)

    @contextmanager
    def task(
        self,
        *,
        task_id,
        arm,
        cell_id,
        requested_model,
        optimize,
        calibration_metadata=None,
    ):
        if (
            not self._installed
            or self._failed
            or requested_model not in self.models
            or not isinstance(task_id, str)
            or not task_id
            or not isinstance(arm, str)
            or not arm
            or type(optimize) is not bool
            or not isinstance(cell_id, str)
            or len(cell_id) != 64
            or any(c not in "0123456789abcdef" for c in cell_id)
            or cell_id in self._exhausted
            or self._frame.get() is not None
        ):
            raise CampaignStopped("invalid frozen SWE-agent task scope")
        calibration_json = calibration_metadata_json(calibration_metadata)
        identity = (task_id, arm, requested_model, optimize, calibration_json)
        with self._lock:
            if self._cells.setdefault(cell_id, identity) != identity:
                self._failed = True
                raise CampaignStopped("SWE-agent cell identity changed")
        token = self._frame.set(
            _Frame(
                task_id,
                arm,
                cell_id,
                requested_model,
                optimize,
                calibration_json=calibration_json,
            )
        )
        try:
            yield
        finally:
            self._frame.reset(token)

    def cell_accounting(self, cell_id):
        with self._lock:
            return {
                "committed_usd": str(self._spent.get(cell_id, Decimal(0))),
                "pending_upper_usd": str(self._pending.get(cell_id, Decimal(0))),
                "budget_exhausted": cell_id in self._exhausted,
            }

    def _query(self, original):
        @wraps(original)
        def query(model, *args, **kwargs):
            frame = self._frame.get()
            if (
                frame is None
                or self._failed
                or model.config.name != frame.requested_model
                or model.config.per_instance_cost_limit != 2.0
                or not model.tools.use_function_calling
            ):
                self._failed = True
                raise CampaignStopped(
                    "upstream model query differs from its frozen task"
                )
            with self._lock:
                self._turn += 1
                turn = f"swe-agent-{self._turn}"
            token = self._frame.set(
                _Frame(
                    frame.task_id,
                    frame.arm,
                    frame.cell_id,
                    frame.requested_model,
                    frame.optimize,
                    turn,
                    frame.calibration_json,
                )
            )
            try:
                version_scope = role_scope = nullcontext()
                if self.instrument:
                    from agentc._optimization_scope import optimization_scope
                    from agentc import application_version

                    version_scope = application_version(self.settings_version)
                    role_scope = optimization_scope(
                        "swe_agent.evaluated_assistant", optimize=frame.optimize
                    )
                elif frame.optimize:
                    raise CampaignStopped("an uninstrumented baseline cannot optimize")
                with (
                    version_scope,
                    role_scope,
                ):
                    return original(model, *args, **kwargs)
            except CellBudgetExhausted:
                raise
            except BaseException:
                self._failed = True
                raise
            finally:
                self._frame.reset(token)

        return query

    def _wire_request(self, values):
        # Use the installed stock serializer for each actual target model.
        # It moves tool-envelope cache markers to their content blocks for
        # supported targets and applies that target's usual cache policy.
        from litellm.llms.openrouter.chat.transformation import OpenrouterConfig

        fields = {k: v for k, v in values.items() if k in PARAMETERS and v is not None}
        raw = OpenrouterConfig().transform_request(
            model=values["model"],
            messages=values["messages"],
            optional_params=fields,
            litellm_params={},
            headers={},
        )
        raw.pop("usage", None)  # The owned transport validates actual usage receipts.
        return raw

    def _completion(self, *args, **kwargs):
        from litellm import ModelResponse

        frame = self._frame.get()
        try:
            if (
                frame is None
                or not frame.turn_id
                or self._failed
                or not self._installed
                or len(args) > 2
            ):
                raise CampaignStopped("provider call lacks its frozen SWE-agent query")
            values = dict(kwargs)
            for name, value in zip(("model", "messages"), args):
                if name in values:
                    raise CampaignStopped("duplicate provider parameter")
                values[name] = value
            client_fields = {
                "api_key",
                "api_base",
                "api_version",
                "fallbacks",
                "n",
                "num_retries",
                "caching",
            }
            if (
                set(values) - {"model", "messages", *PARAMETERS, *client_fields}
                or values.get("api_version") is not None
                or values.get("fallbacks") not in (None, [])
                or values.get("n") not in (None, 1)
                or values.get("num_retries") not in (None, 0)
                or values.get("caching") not in (None, False)
                or values.get("model") not in self.models
                or values.get("api_base") not in (None, self.transport.http.base_url)
            ):
                raise CampaignStopped("unfrozen SWE-agent provider features")
            key = values.get("api_key")
            if key is not None:
                from hashlib import sha256

                if (
                    not isinstance(key, str)
                    or sha256(key.encode()).hexdigest() != self.transport.key_id
                ):
                    raise CampaignStopped(
                        "provider credential differs from owned transport"
                    )
            payload = self._wire_request(values)
            contract = self.transport.contracts[payload["model"]]
            if (
                type(payload.get("max_tokens")) is not int
                or not 0 < payload["max_tokens"] <= self.output_cap
                or not payload.get("tools")
            ):
                raise CampaignStopped(
                    "SWE-agent dispatch left its function/cap contract"
                )
            bounded = prepare_request(contract, payload)
            bound = contract.bound(bounded)
            call_id = uuid.uuid4().hex
            context = None
            if self.instrument:
                from agentc._dispatch_context import current_provider_dispatch

                context = current_provider_dispatch()
            metadata = {
                "actor": "agent",
                "kind": context.kind if context else "primary",
                "task_id": frame.task_id,
                "arm": frame.arm,
                "cell_id": frame.cell_id,
                "turn_id": frame.turn_id,
                "requested_model": frame.requested_model,
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
            with self._lock:
                committed = self._spent.get(frame.cell_id, Decimal(0))
                pending = self._pending.get(frame.cell_id, Decimal(0))
                if (
                    frame.cell_id in self._exhausted
                    or committed + pending + bound > Decimal("2.00")
                ):
                    self._exhausted.add(frame.cell_id)
                    raise CellBudgetExhausted(
                        "task cannot reserve the next complete provider call"
                    )
                self._pending[frame.cell_id] = pending + bound
            charge = bound
            try:
                body = self.transport.call(
                    call_id=call_id, request=payload, metadata=metadata
                )
                charge = money(body["usage"]["cost"])
            except BaseException:
                try:
                    receipt = self.transport.call_accounting(call_id)
                    charge = money(receipt["known_cost_usd"]) + money(
                        receipt["uncertain_budget_usd"]
                    )
                except BaseException:
                    pass  # Retain the complete bound if exact accounting is unavailable.
                raise
            finally:
                with self._lock:
                    self._pending[frame.cell_id] -= bound
                    self._spent[frame.cell_id] = (
                        self._spent.get(frame.cell_id, Decimal(0)) + charge
                    )
            response = ModelResponse(**body)
            response._hidden_params["response_cost"] = float(charge)
            return response
        except CellBudgetExhausted:
            raise
        except BaseException as error:
            self._failed = True
            if not isinstance(error, Exception):
                raise
            raise CampaignStopped(
                "SWE-agent owned dispatch failed; details suppressed"
            ) from None

    def _planner(self, original):
        @wraps(original)
        def plan(call):
            from time import perf_counter_ns

            started = perf_counter_ns()
            result = original(call)
            frame = self._frame.get()
            try:
                if frame is None or not frame.turn_id or self._failed:
                    raise CampaignStopped("SWE-agent plan lacks its owned query scope")
                event = {
                    "cell_id": frame.cell_id,
                    "task_id": frame.task_id,
                    "arm": frame.arm,
                    "turn_id": frame.turn_id,
                    "requested_model": frame.requested_model,
                    "call_site_id": call["call_site_id"],
                    "native_plan_json": result.raw_json,
                    "planning_overhead_us": (perf_counter_ns() - started) / 1000,
                    "planning_timing_scope": "optimizer.plan_call",
                    "selection_audit": json.loads(result.raw_json).get(
                        "agentc_selection_audit"
                    ),
                }
                with self._lock:
                    self.plans.append(event)
                if self.plan_observer is not None:
                    self.plan_observer(event)
            except BaseException:
                self._failed = True
                raise
            return result

        return plan

    def _patch(self, target, name, replacement):
        self._patches.append((target, name, getattr(target, name)))
        setattr(target, name, replacement)

    def __enter__(self):
        if self._installed or self._failed:
            raise CampaignStopped("SWE-agent binding must precede SDK initialization")
        if self.instrument:
            from agentc._lifecycle import is_initialized

            if is_initialized():
                raise CampaignStopped(
                    "SWE-agent binding must precede SDK initialization"
                )
        else:
            require(
                not any(n == "agentc" or n.startswith("agentc.") for n in sys.modules),
                "uninstrumented baseline already imported Agentc",
            )
        require(verify_agent(self.source) == self.upstream, "SWE-agent source changed")
        for name, module in tuple(sys.modules.items()):
            if name == "sweagent" or name.startswith("sweagent."):
                path = getattr(module, "__file__", None)
                require(
                    path is not None
                    and Path(path).resolve().is_relative_to(self.source / "sweagent"),
                    "foreign SWE-agent package is already loaded",
                )
        litellm = importlib.import_module("litellm")
        sys.path.insert(0, str(self.source))
        try:
            models = importlib.import_module("sweagent.agent.models")
        finally:
            sys.path.remove(str(self.source))
        package = importlib.import_module("sweagent")
        require(
            Path(models.__file__).resolve().is_relative_to(self.source / "sweagent")
            and Path(package.CONFIG_DIR).resolve() == self.source / "config"
            and Path(package.TOOLS_DIR).resolve() == self.source / "tools",
            "SWE-agent configuration or tools escaped the frozen source",
        )
        from importlib.metadata import version

        serializer = importlib.import_module(
            "litellm.llms.openrouter.chat.transformation"
        )
        self.settings_version = "frozen-swe-agent-v1:" + digest(
            {
                "binding": self.settings_version,
                "litellm": version("litellm"),
                "serializer_sha256": file_sha(Path(serializer.__file__)),
            }
        )

        def async_completion(*args, **kwargs):
            self._failed = True
            raise CampaignStopped("async calls are outside the frozen SWE-agent seam")

        def owned_cost(response=None, *args, **kwargs):
            if response is None:
                response = kwargs.get("completion_response")
            params = getattr(response, "_hidden_params", {})
            if "response_cost" not in params:
                self._failed = True
                raise CampaignStopped("upstream cost lacks an owned billing receipt")
            return float(money(params["response_cost"]))

        try:
            self._patch(litellm, "completion", self._completion)
            self._patch(litellm, "acompletion", async_completion)
            self._patch(litellm.cost_calculator, "completion_cost", owned_cost)
            self._patch(
                models.LiteLLMModel, "query", self._query(models.LiteLLMModel.query)
            )
            if self.instrument:
                from agentc import _optimizer

                self._patch(
                    _optimizer, "plan_call", self._planner(_optimizer.plan_call)
                )
            self._installed = True
            return self
        except BaseException:
            self._restore()
            raise

    def assert_complete(self):
        if self.instrument:
            from agentc._patches._optimizer_glue import drain_exploration

            drain_exploration(120000)
        if self._failed:
            raise CampaignStopped("SWE-agent binding recorded a failed query")
        return self.transport.assert_complete()

    def _restore(self):
        for target, name, original in reversed(self._patches):
            setattr(target, name, original)
        self._patches.clear()
        self._installed = False

    def __exit__(self, *exc):
        try:
            self.assert_complete()
        except BaseException:
            if exc[0] is None:
                raise
        finally:
            try:
                if self.instrument:
                    import agentc

                    agentc.shutdown()
            finally:
                self._restore()
