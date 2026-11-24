"""Run tau2 banking_knowledge trajectories for the change-safety study.

Workload code, prompts, tools and control flow are upstream tau2 at the
pinned commit. This module only wraps the agent's and user simulator's
``generate`` aliases, to learn which actor is calling, and replaces tau2's
LiteLLM ``completion`` alias with the study dispatcher. Only the evaluated
agent's calls are eligible; the user simulator is never changed (protocol
Section 3).

Agentc is active during every run: reference calls pass through its LiteLLM
interception inside the evaluated-assistant scope with optimization off, so
it observes the call site (which the C5 and C6 rules need) without rewriting
anything. Forks bypass Agentc's interception so they never enter its history.
"""

from __future__ import annotations

import importlib
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from agentc.change_safety.records import ChangeSpec, TrajectoryRecord

from bench.change_safety.assignment import provider_seed
from bench.change_safety.dispatcher import Dispatch, StudyDispatcher, mark_call_site
from bench.change_safety.spend import SpendLedger

WORKLOAD = "tau2-banking-knowledge"
DOMAIN = "banking_knowledge"
TAU2_REVISION = "fc0055dc4e0a316c3f83133267fbd6faaa770992"
TASKS_SHA256 = "213c7f3e6dc0420b1184ee271e39e38c6ece3c43edfa362db49a560828ebd543"
AGENT_SCOPE = "tau2.evaluated_assistant"
USER_SCOPE = "tau2.user_simulator"

_actor: ContextVar[str | None] = ContextVar("change_safety_tau2_actor", default=None)


def current_actor() -> str:
    return _actor.get() or "unknown"


def litellm_dispatch() -> Dispatch:
    def call(request: Mapping[str, Any]) -> Any:
        import litellm

        # Same line on purpose: Agentc names this frame as the call site.
        return mark_call_site() and litellm.completion(**request)

    return call


def litellm_fork_dispatch() -> Dispatch:
    """LiteLLM ``completion`` without Agentc's interception wrapper."""

    def call(request: Mapping[str, Any]) -> Any:
        import litellm

        original = getattr(litellm.completion, "__wrapped__", litellm.completion)
        return original(**request)

    return call


@dataclass(frozen=True)
class Tau2StudyConfig:
    stage: str
    agent_model: str
    user_model: str
    max_steps: int = 200
    max_errors: int = 10
    agent_output_cap: int = 1024
    user_output_cap: int = 1024
    temperature: float = 0.0
    retrieval: str = "bm25"


class Tau2Study:
    """Install once, run many trajectories, then exit."""

    def __init__(
        self,
        *,
        config: Tau2StudyConfig,
        output: Path,
        ledger: SpendLedger,
        reference: Dispatch | None = None,
        fork: Dispatch | None = None,
        embed: bool = True,
    ):
        self.config = config
        self.dispatcher = StudyDispatcher(
            stage=config.stage,
            workload=WORKLOAD,
            model=config.agent_model,
            records=output / "records.jsonl",
            ledger=ledger,
            reference=reference or litellm_dispatch(),
            fork=fork or litellm_fork_dispatch(),
            agent_scope=AGENT_SCOPE,
            other_scope=USER_SCOPE,
            embed=embed,
        )
        self._patches: list[tuple[Any, str, Any]] = []

    def _patch(self, module: Any, name: str, value: Any) -> None:
        self._patches.append((module, name, getattr(module, name)))
        setattr(module, name, value)

    def __enter__(self) -> "Tau2Study":
        llm_utils = importlib.import_module("tau2.utils.llm_utils")
        agent_mod = importlib.import_module("tau2.agent.llm_agent")
        user_mod = importlib.import_module("tau2.user.user_simulator")
        self._patch(llm_utils, "completion", self._completion)
        for module, actor in ((agent_mod, "agent"), (user_mod, "user")):
            self._patch(module, "generate", self._actor_generate(module.generate, actor))
        return self

    def __exit__(self, *exc: Any) -> None:
        while self._patches:
            module, name, original = self._patches.pop()
            setattr(module, name, original)
        self.dispatcher.close()

    def _actor_generate(self, original: Callable[..., Any], actor: str) -> Callable[..., Any]:
        cap = self.config.agent_output_cap if actor == "agent" else self.config.user_output_cap

        def generate(*args: Any, **kwargs: Any) -> Any:
            from agentc._optimization_scope import optimization_scope

            kwargs["num_retries"] = 0  # retries would bill outside the ledger
            kwargs.setdefault("max_tokens", cap)
            token = _actor.set(actor)
            try:
                scope = AGENT_SCOPE if actor == "agent" else USER_SCOPE
                with optimization_scope(scope, optimize=False):
                    return original(*args, **kwargs)
            finally:
                _actor.reset(token)

        return generate

    def _completion(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            raise TypeError("tau2 completion is called with keyword arguments only")
        actor = current_actor()
        if actor == "agent":
            return self.dispatcher.agent_call(kwargs)
        if actor == "user":
            return self.dispatcher.passthrough("user", kwargs)
        raise RuntimeError(f"tau2 provider call from unknown actor {actor!r}")

    def run(
        self, *, task: Any, repetition: int, arm: str, catalog: Mapping[str, ChangeSpec]
    ) -> TrajectoryRecord:
        """Run one trajectory. ``arm`` is ``"baseline"`` or a change ID."""
        from tau2.data_model.simulation import TextRunConfig
        from tau2.runner.batch import run_single_task

        seed = provider_seed(WORKLOAD, task.id, arm, repetition)
        run_config = TextRunConfig(
            domain=DOMAIN,
            agent="llm_agent",
            llm_agent=self.config.agent_model,
            llm_args_agent={"temperature": self.config.temperature, "seed": seed},
            user="user_simulator",
            llm_user=self.config.user_model,
            llm_args_user={"temperature": self.config.temperature, "seed": seed},
            max_steps=self.config.max_steps,
            max_errors=self.config.max_errors,
            retrieval_config=self.config.retrieval,
        )
        with self.dispatcher.trajectory(
            task_id=task.id, repetition=repetition, arm=arm, catalog=catalog
        ) as state:
            simulation = run_single_task(run_config, task, seed=seed)
        reward = simulation.reward_info.reward if simulation.reward_info else 0.0
        termination = getattr(simulation.termination_reason, "value", simulation.termination_reason)
        return self.dispatcher.finish(state, reward=reward, termination=str(termination))


def load_tasks(task_ids: Sequence[str] | None = None) -> list[Any]:
    """Pinned banking_knowledge tasks, verified against the protocol digests."""
    import hashlib
    import subprocess

    from tau2.runner.helpers import load_tasks as tau2_load_tasks
    from tau2.utils.utils import DATA_DIR

    source = Path(DATA_DIR).resolve().parent
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    tasks_file = Path(DATA_DIR) / "tau2" / "domains" / DOMAIN / "tasks.json"
    if (
        revision != TAU2_REVISION
        or hashlib.sha256(tasks_file.read_bytes()).hexdigest() != TASKS_SHA256
    ):
        raise RuntimeError("tau2 checkout differs from the pinned revision or task digest")
    tasks = tau2_load_tasks(DOMAIN, task_split_name=None)
    if task_ids is None:
        return list(tasks)
    by_id = {task.id: task for task in tasks}
    return [by_id[task_id] for task_id in task_ids]


def register_mock_provider(provider: Callable[..., Any]) -> None:
    """Serve ``mock/<name>`` models from ``provider`` through LiteLLM itself."""
    import litellm
    from litellm import CustomLLM

    class _Mock(CustomLLM):  # type: ignore[misc]
        def completion(self, model: str, messages: list[Any], *args: Any, **kwargs: Any) -> Any:
            params = dict(kwargs.get("optional_params") or {})
            return provider(model=f"mock/{model}", messages=messages, **params)

    litellm.custom_provider_map = [
        entry for entry in litellm.custom_provider_map if entry.get("provider") != "mock"
    ] + [{"provider": "mock", "custom_handler": _Mock()}]


__all__ = [
    "AGENT_SCOPE",
    "DOMAIN",
    "TAU2_REVISION",
    "TASKS_SHA256",
    "Tau2Study",
    "Tau2StudyConfig",
    "USER_SCOPE",
    "WORKLOAD",
    "current_actor",
    "litellm_dispatch",
    "litellm_fork_dispatch",
    "load_tasks",
    "register_mock_provider",
]
