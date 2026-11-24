"""Run SWE-agent on SWE-bench Verified for the change-safety study.

The frozen SWE-agent execution and grading pipeline (environment admission,
owned containers, the pinned evaluator) is reused unchanged from
``bench.swe_agent_task_worker.run_task`` and ``bench.swe_agent_grading``.
:class:`SweStudyBinding` is a drop-in for the campaign binding those
functions accept: instead of the retired OpenRouter transport, SWE-agent's
LiteLLM calls go to the study dispatcher, which applies rollouts, sends
forks, and bills every call through the study's spend ledger.

SWE-agent has a single model actor, so every call is an eligible
evaluated-agent call. The protocol's USD 2.00 per-instance ceiling stays in
force through SWE-agent's own ``per_instance_cost_limit`` (set by
``run_task``).

Running a trajectory needs Docker and the admitted task images (see
``bench/repro/docker-image-provisioning.md``).
"""

from __future__ import annotations

import hashlib
import importlib
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Mapping

from agentc.change_safety.records import ChangeSpec, TrajectoryRecord
from agentc.change_safety.signals import TerminationMarkers

from bench.change_safety.assignment import PROTOCOL, provider_seed
from bench.change_safety.dispatcher import (
    StudyDispatcher,
    TrajectoryState,
    mark_call_site,
)
from bench.change_safety.spend import SpendLedger

WORKLOAD = "swebench-verified"
AGENT_SCOPE = "swe_agent.evaluated_assistant"
OTHER_SCOPE = "swe_agent.other"
PER_INSTANCE_CEILING_USD = Decimal("2.00")
#: SWE-agent ends a run by submitting; there is no transfer action.
SWE_MARKERS = TerminationMarkers(end_tools=frozenset({"submit"}), end_tokens=())


class SweStudyBinding:
    """Duck-typed replacement for ``SweAgentCampaignBinding`` in ``run_task``.

    Enter it after ``agentc.init`` so reference calls pass through Agentc's
    LiteLLM interception (they are observed, never rewritten, under the study
    environment); forks bypass that interception.
    """

    def __init__(
        self,
        *,
        stage: str,
        model: str,
        source: Path,
        output: Path,
        ledger: SpendLedger,
        output_cap: int,
        cohort_sha256: str,
        execution_version: str,
        context_tokens: Mapping[str, int],
        verify: Callable[[Path], dict[str, Any]] | None = None,
        embed: bool = True,
    ):
        if verify is None:
            from bench.swe_agent_binding import verify_agent

            verify = verify_agent
        self.source = source.resolve()
        self.upstream = verify(self.source)
        self.output_cap = output_cap
        self.cohort_sha256 = cohort_sha256
        self.execution_version = execution_version
        self.models = frozenset(context_tokens)
        self.transport = SimpleNamespace(
            contracts={m: SimpleNamespace(context_tokens=n) for m, n in context_tokens.items()},
            session_id=uuid.uuid4().hex,
        )
        self._original: Any = None
        self.dispatcher = StudyDispatcher(
            stage=stage,
            workload=WORKLOAD,
            model=model,
            records=output / "records.jsonl",
            ledger=ledger,
            reference=self._reference,
            fork=self._fork,
            agent_scope=AGENT_SCOPE,
            other_scope=OTHER_SCOPE,
            markers=SWE_MARKERS,
            embed=embed,
        )
        self._claimed: set[str] = set()
        self._plan: tuple[int, Mapping[str, ChangeSpec]] | None = None
        self.state: TrajectoryState | None = None
        self._patches: list[tuple[Any, str, Any]] = []

    # -- provider entry points -------------------------------------------

    def _reference(self, request: Mapping[str, Any]) -> Any:
        original = self._original
        # Same line on purpose: Agentc names this frame as the call site.
        return mark_call_site() and original(**request)

    def _fork(self, request: Mapping[str, Any]) -> Any:
        original = getattr(self._original, "__wrapped__", self._original)
        return original(**request)

    def _completion(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            raise TypeError("SWE-agent calls LiteLLM with keyword arguments only")
        return self.dispatcher.agent_call(kwargs)

    # -- campaign-binding interface used by run_task -------------------------

    def prepare(self, *, repetition: int, catalog: Mapping[str, ChangeSpec]) -> None:
        """Set the next trajectory's repetition and change catalog."""
        self._plan = (repetition, catalog)

    def claim_execution(self, cell_id: str) -> None:
        if cell_id in self._claimed:
            raise RuntimeError("SWE-agent study cells cannot retry or resume")
        self._claimed.add(cell_id)

    @contextmanager
    def task(
        self,
        *,
        task_id: str,
        arm: str,
        cell_id: str,
        requested_model: str,
        optimize: bool,
        calibration_metadata: Any = None,
    ) -> Iterator[None]:
        if optimize or calibration_metadata is not None:
            raise RuntimeError("the change-safety study never optimizes or calibrates")
        if requested_model not in self.models or self._plan is None:
            raise RuntimeError("SWE-agent study task lacks a prepared model and plan")
        repetition, catalog = self._plan
        with self.dispatcher.trajectory(
            task_id=task_id, repetition=repetition, arm=arm, catalog=catalog
        ) as state:
            self.state = state
            yield

    def cell_accounting(self, cell_id: str) -> dict[str, Any]:
        state = self.state
        spent = Decimal(str(state.cost)) if state is not None else Decimal(0)
        return {
            "committed_usd": str(spent),
            "pending_upper_usd": "0",
            "budget_exhausted": spent >= PER_INSTANCE_CEILING_USD,
        }

    def assert_complete(self) -> None:
        """Every call is synchronous and billed before it returns."""

    def __enter__(self) -> "SweStudyBinding":
        from agentc._lifecycle import is_initialized

        if not is_initialized():
            raise RuntimeError("enter the SWE study binding after agentc.init")
        litellm = importlib.import_module("litellm")
        self._original = litellm.completion
        self._patches.append((litellm, "completion", litellm.completion))
        litellm.completion = self._completion
        return self

    def __exit__(self, *exc: Any) -> None:
        while self._patches:
            module, name, original = self._patches.pop()
            setattr(module, name, original)
        self.dispatcher.close()

    # -- one trajectory -------------------------------------------------------

    def run(
        self,
        *,
        inputs: Any,
        environment_report: Path,
        bundle: Any,
        task_id: str,
        arm: str,
        repetition: int,
        catalog: Mapping[str, ChangeSpec],
        model: str,
        output: Path,
        run_task: Callable[..., Any] | None = None,
        grade_task: Callable[..., Any] | None = None,
    ) -> TrajectoryRecord:
        """Run, grade and record one trajectory with the frozen pipeline."""
        if run_task is None:
            from bench.swe_agent_task_worker import run_task
        if grade_task is None:
            from bench.swe_agent_grading import grade_task
        self.prepare(repetition=repetition, catalog=catalog)
        cell_id = hashlib.sha256(
            "\x00".join((PROTOCOL, self.dispatcher.stage, task_id, arm, str(repetition))).encode()
        ).hexdigest()
        submission = run_task(
            inputs=inputs,
            environment_report=environment_report,
            task_id=task_id,
            binding=self,
            bundle=bundle,
            output=output / "run",
            requested_model=model,
            arm=arm,
            cell_id=cell_id,
            optimize=False,
            run_seed=provider_seed(WORKLOAD, task_id, arm, repetition),
        )
        state = self.state
        if state is None:
            raise RuntimeError("SWE-agent trajectory did not open a study scope")

        def accounting(_submission: Any, _provenance: Any) -> tuple[int, Decimal, str]:
            ledger = self.dispatcher.ledger.path
            return (
                state.calls + state.failed_calls,
                Decimal(str(state.cost)),
                hashlib.sha256(ledger.read_bytes()).hexdigest(),
            )

        grade = grade_task(
            inputs=inputs,
            environment_report=environment_report,
            bundle=bundle,
            ledger=None,
            submission_path=output / "run" / "submission.json",
            output=output / "grade",
            accounting=accounting,
        )
        score = grade.get("protocol_score")
        return self.dispatcher.finish(
            state,
            reward=float(score) if score is not None else 0.0,
            termination=str(submission.get("upstream_exit_status") or "unknown"),
        )


def run_swe_stage(
    *,
    stage: str,
    model: str,
    task_ids: list[str],
    repetitions: int,
    rollout_arms: list[str],
    fork_arms: list[str],
    catalog: Mapping[str, ChangeSpec],
    inputs: Any,
    environment_report: Path,
    bundle: Any,
    output: Path,
    cap_usd: float,
    output_cap: int = 1024,
    context_tokens: int = 200_000,
) -> list[TrajectoryRecord]:
    """Run one SWE stage in predecessor arm order.

    ``inputs``, ``environment_report`` and ``bundle`` come from the existing
    frozen preparation (``bench.swebench_environment.load_inputs``, the
    environment admission report, and ``bench.swe_agent_runtime.RuntimeBundle``);
    the study does not re-admit environments. Agentc storage is fresh per
    (stage, arm, repetition), as for tau2.
    """
    import agentc

    from bench.campaign_accounting import digest
    from bench.change_safety.assignment import arm_order
    from bench.change_safety.dispatcher import configure_study_environment
    from bench.swe_agent_task_worker import execution_version

    configure_study_environment()
    ledger = SpendLedger(output / "spend.jsonl", cap_usd=cap_usd)
    fork_catalog = {arm: catalog[arm] for arm in {"C0", *fork_arms}}
    records: list[TrajectoryRecord] = []
    arms = ["baseline", *rollout_arms]
    for task_id in task_ids:
        for repetition in range(repetitions):
            for arm in arm_order(WORKLOAD, task_id, repetition, arms):
                agentc.init(storage_path=str(output / "agentc" / f"{arm}-r{repetition}"))
                try:
                    with SweStudyBinding(
                        stage=stage,
                        model=model,
                        source=inputs.agent,
                        output=output,
                        ledger=ledger,
                        output_cap=output_cap,
                        cohort_sha256=digest(inputs.cohort),
                        execution_version=execution_version(bundle),
                        context_tokens={model: context_tokens},
                    ) as binding:
                        records.append(
                            binding.run(
                                inputs=inputs,
                                environment_report=environment_report,
                                bundle=bundle,
                                task_id=task_id,
                                arm=arm,
                                repetition=repetition,
                                catalog=fork_catalog if arm == "baseline" else catalog,
                                model=model,
                                output=output / "tasks" / f"{task_id}-{arm}-r{repetition}",
                            )
                        )
                finally:
                    agentc.shutdown()
    return records


__all__ = ["AGENT_SCOPE", "SWE_MARKERS", "SweStudyBinding", "WORKLOAD", "run_swe_stage"]
