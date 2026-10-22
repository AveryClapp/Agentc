"""Bind pinned Tau text actors and judge to one original-journal dispatcher.

Install before agentc.init(), then shut Agentc down before leaving the binding.
Framework generate/serialization/response parsing and evaluation remain upstream;
the benchmark supplies a single-attempt LiteLLM-compatible provider backend and
an explicit complete-coverage judge guard. Model aliases and output caps are
prospective frozen configuration, not guesses based on prompt or call names.
Top-level null omission is shared by the upstream wire seam and backend, so
planning sees the same message fields that the owned transport dispatches.
"""

from __future__ import annotations

import json
import hashlib
import importlib
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable

from bench.campaign_accounting import AccountingError, money
from bench.campaign_chat import PARAMETERS
from bench.campaign_transport import CampaignStopped, CampaignTransport
from bench.tau_judge import TauJudgeGuard


def _wire_messages(messages):
    """Apply the same top-level null omission before planning and dispatch.

    Preserve nulls inside argument strings and all non-null upstream values.
    """
    return [{k: v for k, v in message.items() if v is not None} for message in messages]


REVISION = "672227c6b6676edc20d57ea53b7000262aae77b9"
_TARGETS = (
    ("tau2.agent.llm_agent", "agent"),
    ("tau2.user.user_simulator", "user"),
    ("tau2.evaluator.evaluator_nl_assertions", "judge"),
)


@dataclass(frozen=True)
class _TaskFrame:
    task_id: str
    arm: str
    optimize_agent: bool
    cell_id: str | None = None
    calibration_json: str | None = None


@dataclass(frozen=True)
class _ActorFrame:
    task: _TaskFrame
    actor: str
    turn_id: str
    requested_model: str
    review_json: str | None = None


class TauCampaignBinding:
    def __init__(
        self,
        *,
        transport: CampaignTransport,
        source: Path,
        model_aliases: dict[str, str],
        actor_output_caps: dict[str, int],
        plan_observer: Callable[[dict[str, Any]], None] | None = None,
        agent_model_allowlist: frozenset[str] | None = None,
    ):
        self.transport = transport
        self.source = source.resolve()
        self.aliases = MappingProxyType(dict(model_aliases))
        self.caps = MappingProxyType(dict(actor_output_caps))
        self.plan_observer = plan_observer
        self.agent_models = agent_model_allowlist
        if (
            set(self.caps) != {"agent", "user", "judge"}
            or (
                agent_model_allowlist is not None
                and (
                    type(agent_model_allowlist) is not frozenset
                    or not agent_model_allowlist
                    or not agent_model_allowlist <= transport.contracts.keys()
                )
            )
            or not self.aliases
            or any(model not in transport.contracts for model in self.aliases.values())
            or any(
                alias != target and ("/" in alias or target != "openai/" + alias)
                for alias, target in self.aliases.items()
            )
            or any(
                type(cap) is not int or not 0 < cap <= 2048
                for cap in self.caps.values()
            )
        ):
            raise CampaignStopped("invalid frozen Tau model or actor configuration")
        self._task: ContextVar[_TaskFrame | None] = ContextVar(
            "agentc_tau_task", default=None
        )
        self._actor: ContextVar[_ActorFrame | None] = ContextVar(
            "agentc_tau_actor", default=None
        )
        self._lock = threading.Lock()
        self._turn = 0
        self._failed = False
        self._patches: list[tuple[Any, str, Any, Any]] = []
        self._installed = False

    @contextmanager
    def task(
        self,
        *,
        task_id: str,
        arm: str,
        optimize_agent: bool,
        cell_id: str | None = None,
        calibration_metadata: dict | None = None,
    ):
        if (
            not self._installed
            or not isinstance(task_id, str)
            or not task_id
            or not isinstance(arm, str)
            or not arm
            or type(optimize_agent) is not bool
            or (
                cell_id is not None
                and (
                    not isinstance(cell_id, str)
                    or len(cell_id) != 64
                    or any(char not in "0123456789abcdef" for char in cell_id)
                )
            )
        ):
            raise CampaignStopped("invalid active Tau task scope")
        from bench.campaign_records import calibration_metadata_json

        token = self._task.set(
            _TaskFrame(
                task_id,
                arm,
                optimize_agent,
                cell_id,
                calibration_metadata_json(calibration_metadata),
            )
        )
        try:
            yield
        finally:
            self._task.reset(token)

    def _patch(self, module, name, replacement):
        original = getattr(module, name)
        setattr(module, name, replacement)
        self._patches.append((module, name, original, replacement))

    def _actor_generate(self, original, actor: str, review_metadata=None):
        review_json = None
        if review_metadata is not None:
            if (
                actor != "judge"
                or set(review_metadata) != {"judge_purpose", "judge_evidence_sha256"}
                or not isinstance(review_metadata["judge_evidence_sha256"], str)
                or len(review_metadata["judge_evidence_sha256"]) != 64
                or any(
                    c not in "0123456789abcdef"
                    for c in review_metadata["judge_evidence_sha256"]
                )
            ):
                raise CampaignStopped("invalid policy review attribution")
            review_json = json.dumps(review_metadata, sort_keys=True)

        @wraps(original)
        def generate(*args, **kwargs):
            from agentc._optimization_scope import optimization_scope

            frame = self._task.get()
            if frame is None or self._failed:
                self._failed = True
                raise CampaignStopped("Tau call has no active valid task scope")
            self.guard.assert_valid()
            requested_model = kwargs.get("model", args[0] if args else None)
            if (
                not isinstance(requested_model, str)
                or requested_model not in self.aliases
            ):
                self._failed = True
                raise CampaignStopped("Tau actor requests an unenrolled model")
            with self._lock:
                self._turn += 1
                turn_id = f"{actor}-{self._turn}"
            token = self._actor.set(
                _ActorFrame(
                    frame, actor, turn_id, self.aliases[requested_model], review_json
                )
            )
            kwargs = dict(kwargs)
            try:
                if kwargs.get("num_retries") not in (None, 0):
                    raise CampaignStopped("Tau retries are not permitted")
                kwargs["num_retries"] = 0
                if kwargs.get("max_tokens") is None:
                    kwargs["max_tokens"] = self.caps[actor]
                if (
                    type(kwargs["max_tokens"]) is not int
                    or not 0 < kwargs["max_tokens"] <= self.caps[actor]
                ):
                    raise CampaignStopped(
                        "Tau output cap exceeds its frozen actor configuration"
                    )
                scope = {
                    "agent": "tau2.evaluated_assistant",
                    "user": "tau2.user_simulator",
                    "judge": "tau2.nl_judge",
                }[actor]
                with optimization_scope(
                    scope, optimize=frame.optimize_agent if actor == "agent" else False
                ):
                    return original(*args, **kwargs)
            except BaseException:
                self._failed = True
                raise
            finally:
                self._actor.reset(token)

        return generate

    def generate_safety_review(self, *, evidence_sha256, **kwargs):
        from tau2.utils.llm_utils import generate
        from bench.tau_safety import VERSION

        return self._actor_generate(
            generate,
            "judge",
            {"judge_purpose": VERSION, "judge_evidence_sha256": evidence_sha256},
        )(**kwargs)

    def _completion(self, *args, **kwargs):
        from agentc._dispatch_context import current_provider_dispatch
        from litellm import ModelResponse

        try:
            frame = self._actor.get()
            if frame is None or self._failed or not self._installed:
                raise CampaignStopped("provider call lacks a valid Tau actor scope")
            self.guard.assert_valid()
            if len(args) > 2:
                raise AccountingError("unsupported positional provider parameters")
            values = dict(kwargs)
            for name, value in zip(("model", "messages"), args):
                if name in values:
                    raise AccountingError("duplicate provider request parameter")
                values[name] = value
            client_fields = {"num_retries", "api_key", "api_base", "timeout", "caching"}
            if set(values) - {
                "model",
                "messages",
                "stream",
                *PARAMETERS,
                *client_fields,
            }:
                raise AccountingError("provider request has unfrozen client features")
            if values.get("num_retries") != 0 or values.get("caching") not in (
                None,
                False,
            ):
                raise AccountingError("provider cache/retry configuration differs")
            api_key = values.get("api_key")
            if (
                api_key is not None
                and hashlib.sha256(api_key.encode()).hexdigest()
                != self.transport.key_id
            ):
                raise AccountingError(
                    "provider credentials differ from the campaign binding"
                )
            if values.get("api_base") not in (None, self.transport.http.base_url):
                raise AccountingError("provider URL differs from the campaign binding")
            if values.get("timeout") not in (
                None,
                self.transport.http.deadline_seconds,
            ):
                raise AccountingError(
                    "provider timeout differs from the frozen deadline"
                )
            payload = {
                name: value
                for name, value in values.items()
                if name not in client_fields and value is not None
            }
            # Pinned LiteLLM omits null message-envelope fields on the wire.
            # Keep all actual text, argument strings, IDs and redundant tool
            # names unchanged; this is serialization, not history rewriting.
            payload["messages"] = _wire_messages(payload["messages"])
            payload["model"] = self.aliases[payload["model"]]
            if (
                frame.actor == "agent"
                and self.agent_models is not None
                and payload["model"] not in self.agent_models
            ):
                raise AccountingError("agent dispatch left its frozen model pair")
            context = current_provider_dispatch()
            metadata = {
                "actor": frame.actor,
                "kind": context.kind if context is not None else "primary",
                "task_id": frame.task.task_id,
                "arm": frame.task.arm,
                "turn_id": frame.turn_id,
            }
            if frame.task.cell_id is not None:
                metadata.update(
                    cell_id=frame.task.cell_id, requested_model=frame.requested_model
                )
            if frame.task.calibration_json is not None:
                metadata.update(json.loads(frame.task.calibration_json))
            if frame.review_json is not None:
                metadata.update(json.loads(frame.review_json))
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
            response = ModelResponse(**body)
            # The unchanged Tau helper asks get_response_cost(). Supply the real
            # validated charge instead of LiteLLM's catalog-price estimate.
            response._hidden_params["response_cost"] = float(
                money(body["usage"]["cost"])
            )
            return response
        except BaseException as exc:
            self._failed = True
            if not isinstance(exc, Exception):
                raise
            raise CampaignStopped(
                "Tau provider binding failed; details suppressed"
            ) from None

    def _observed_planner(self, original):
        @wraps(original)
        def plan_call(call):
            started = time.perf_counter_ns()
            plan = original(call)
            elapsed_us = (time.perf_counter_ns() - started) / 1000
            frame = self._actor.get()
            try:
                if frame is None or self._failed:
                    raise CampaignStopped("planning has no active Tau actor scope")
                self.plan_observer(
                    {
                        "cell_id": frame.task.cell_id,
                        "actor": frame.actor,
                        "task_id": frame.task.task_id,
                        "arm": frame.task.arm,
                        "turn_id": frame.turn_id,
                        "call_site_id": call["call_site_id"],
                        "requested_model": call["model"],
                        "planning_overhead_us": elapsed_us,
                        "planning_timing_scope": "optimizer.plan_call",
                        "native_plan_json": plan.raw_json,
                    }
                )
            except BaseException:
                # The SDK deliberately fails open on planning errors. Mark the
                # binding first so an audit failure cannot become a billed call
                # lacking its measured planning record.
                self._failed = True
                raise
            return plan

        return plan_call

    def __enter__(self):
        import subprocess
        from agentc._lifecycle import is_initialized

        if self._installed or is_initialized():
            raise CampaignStopped("Tau campaign binding is already installed")
        revision = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        dirty = subprocess.check_output(
            [
                "git",
                "-C",
                str(self.source),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ]
        )
        if revision != REVISION or dirty:
            raise CampaignStopped("Tau source differs from the pinned revision")
        litellm = importlib.import_module("litellm")
        llm_utils = importlib.import_module("tau2.utils.llm_utils")
        utils = importlib.import_module("tau2.utils.utils")
        if Path(utils.DATA_DIR).resolve() != self.source / "data":
            raise CampaignStopped("Tau data is outside the pinned checkout")
        modules = [importlib.import_module(name) for name, _ in _TARGETS]
        for module in [llm_utils, *modules]:
            if (
                not Path(module.__file__)
                .resolve()
                .is_relative_to(self.source / "src/tau2")
            ):
                raise CampaignStopped("Tau import is outside the pinned checkout")
        self.guard = TauJudgeGuard(modules[-1])

        def completion(*args, **kwargs):
            return self._completion(*args, **kwargs)

        async def acompletion(*args, **kwargs):
            # The pinned text workload only uses the synchronous generate seam.
            # An unexpected async bypass cannot reach an unaccounted provider.
            self._failed = True
            raise CampaignStopped("async calls are outside the pinned Tau text seam")

        try:
            original_serializer = llm_utils.to_litellm_messages

            @wraps(original_serializer)
            def wire_serializer(*args, **kwargs):
                return _wire_messages(original_serializer(*args, **kwargs))

            self._patch(llm_utils, "to_litellm_messages", wire_serializer)
            if self.plan_observer is not None:
                from agentc import _optimizer

                self._patch(
                    _optimizer,
                    "plan_call",
                    self._observed_planner(_optimizer.plan_call),
                )
            self._patch(litellm, "completion", completion)
            self._patch(litellm, "acompletion", acompletion)
            self._patch(llm_utils, "completion", completion)
            self._patch(
                llm_utils,
                "get_response_cost",
                lambda response: response._hidden_params["response_cost"],
            )
            for module, (_, actor) in zip(modules, _TARGETS):
                self._patch(
                    module, "generate", self._actor_generate(module.generate, actor)
                )
            self.guard.__enter__()
            self._installed = True
            return self
        except BaseException:
            self._restore()
            raise

    def assert_complete(self, *, drain_timeout_ms: int = 120000):
        from agentc._patches._optimizer_glue import drain_exploration

        drain_exploration(drain_timeout_ms)
        if self._failed:
            raise CampaignStopped("Tau worker has a failed actor/provider outcome")
        self.guard.assert_valid()
        return self.transport.assert_complete()

    def _restore(self):
        if hasattr(self, "guard"):
            self.guard.__exit__(None, None, None)
        for module, name, original, replacement in reversed(self._patches):
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
