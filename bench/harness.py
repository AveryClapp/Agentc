"""Benchmark harness: mock multi-agent pipeline and SWE-bench task selection.

Provides a configurable mock agent pipeline that simulates multi-step LLM
workflows (planning, coding, review) with realistic token counts and latencies.
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass, field
from typing import Any


# SWE-bench Lite task IDs (300 total). We use deterministic selection.
# These are representative task identifiers for benchmarking.
SWEBENCH_LITE_TASK_IDS: list[str] = [f"swebench-lite-{i:04d}" for i in range(300)]


@dataclass
class TaskSplit:
    """Deterministic split of SWE-bench Lite tasks."""

    calibration: list[str]  # 20 tasks for threshold tuning
    validation: list[str]  # 20 tasks for evaluating tuned thresholds
    overhead: list[str]  # 10 tasks for overhead measurement

    @staticmethod
    def create(seed: int = 42) -> TaskSplit:
        """Create deterministic 20/20/10 split."""
        rng = random.Random(seed)
        shuffled = SWEBENCH_LITE_TASK_IDS.copy()
        rng.shuffle(shuffled)
        return TaskSplit(
            calibration=shuffled[:20],
            validation=shuffled[20:40],
            overhead=shuffled[40:50],
        )


@dataclass
class MockLLMCall:
    """A simulated LLM call with realistic parameters."""

    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    input_text: str = ""
    output_text: str = ""
    finish_reason: str = "end_turn"


@dataclass
class MockAgentStep:
    """A single step in a multi-agent pipeline."""

    agent_name: str
    step_name: str
    calls: list[MockLLMCall] = field(default_factory=list)


@dataclass
class MockPipelineTrace:
    """A complete trace from a mock multi-agent pipeline run."""

    task_id: str
    steps: list[MockAgentStep] = field(default_factory=list)
    wall_clock_ms: float = 0.0

    @property
    def total_calls(self) -> int:
        return sum(len(s.calls) for s in self.steps)

    @property
    def total_input_tokens(self) -> int:
        return sum(c.input_tokens for s in self.steps for c in s.calls)

    @property
    def total_output_tokens(self) -> int:
        return sum(c.output_tokens for s in self.steps for c in s.calls)


def _deterministic_text(task_id: str, role: str, step: int) -> str:
    """Generate deterministic pseudo-text for a given task/role/step."""
    h = hashlib.sha256(f"{task_id}:{role}:{step}".encode()).hexdigest()
    # Return a string of predictable length
    return f"[{role}] Task {task_id} step {step}: {h}"


def generate_pipeline_trace(
    task_id: str,
    *,
    seed: int | None = None,
    include_waste: bool = False,
) -> MockPipelineTrace:
    """Generate a mock multi-agent pipeline trace for one SWE-bench task.

    The pipeline simulates: planner -> coder -> reviewer -> coder (fix) -> verifier.
    Each agent makes 2-5 LLM calls. With include_waste=True, injects known waste
    patterns (redundant calls, retry storms, context bloat).

    Args:
        task_id: Task identifier.
        seed: RNG seed for reproducibility.
        include_waste: If True, inject known waste patterns.
    """
    rng = random.Random(seed if seed is not None else hash(task_id))
    steps: list[MockAgentStep] = []

    # Agent pipeline: planner -> coder -> reviewer -> coder_fix -> verifier
    agents = [
        ("planner", "plan", "claude-3-5-sonnet-20241022"),
        ("coder", "implement", "claude-3-5-sonnet-20241022"),
        ("reviewer", "review", "claude-3-5-sonnet-20241022"),
        ("coder", "fix", "claude-3-5-sonnet-20241022"),
        ("verifier", "verify", "claude-3-5-haiku-20241022"),
    ]

    for agent_name, step_name, model in agents:
        step = MockAgentStep(agent_name=agent_name, step_name=step_name)
        n_calls = rng.randint(2, 5)

        for i in range(n_calls):
            input_tokens = rng.randint(500, 8000)
            output_tokens = rng.randint(100, 2000)
            latency_ms = rng.uniform(200, 3000)

            call = MockLLMCall(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                input_text=_deterministic_text(task_id, f"{agent_name}-input", i),
                output_text=_deterministic_text(task_id, f"{agent_name}-output", i),
            )
            step.calls.append(call)

        steps.append(step)

    # Inject waste patterns if requested
    if include_waste:
        steps.extend(_inject_waste_patterns(task_id, rng))

    trace = MockPipelineTrace(task_id=task_id, steps=steps)
    trace.wall_clock_ms = sum(
        c.latency_ms for s in trace.steps for c in s.calls
    )
    return trace


def _inject_waste_patterns(
    task_id: str,
    rng: random.Random,
) -> list[MockAgentStep]:
    """Inject known waste patterns for calibration ground truth."""
    waste_steps: list[MockAgentStep] = []

    # Pattern 1: Redundant calls (same input, same output)
    redundant_step = MockAgentStep(agent_name="coder", step_name="redundant")
    shared_input = _deterministic_text(task_id, "redundant-input", 0)
    shared_output = _deterministic_text(task_id, "redundant-output", 0)
    for i in range(3):
        redundant_step.calls.append(MockLLMCall(
            model="claude-3-5-sonnet-20241022",
            input_tokens=2000,
            output_tokens=500,
            latency_ms=rng.uniform(500, 1500),
            input_text=shared_input,
            output_text=shared_output,
        ))
    waste_steps.append(redundant_step)

    # Pattern 2: Retry storm (rapid identical calls)
    retry_step = MockAgentStep(agent_name="coder", step_name="retry_storm")
    retry_input = _deterministic_text(task_id, "retry-input", 0)
    for i in range(5):
        retry_step.calls.append(MockLLMCall(
            model="claude-3-5-sonnet-20241022",
            input_tokens=1000,
            output_tokens=50,
            latency_ms=rng.uniform(100, 300),  # Rapid succession
            input_text=retry_input,
            output_text=_deterministic_text(task_id, "retry-output", i),
            finish_reason="error" if i < 4 else "end_turn",
        ))
    waste_steps.append(retry_step)

    # Pattern 3: Context bloat (huge input, tiny output)
    bloat_step = MockAgentStep(agent_name="planner", step_name="context_bloat")
    bloat_step.calls.append(MockLLMCall(
        model="claude-3-5-sonnet-20241022",
        input_tokens=180000,  # ~90% of 200K context window
        output_tokens=50,
        latency_ms=rng.uniform(2000, 5000),
        input_text=_deterministic_text(task_id, "bloat-input", 0),
        output_text=_deterministic_text(task_id, "bloat-output", 0),
    ))
    waste_steps.append(bloat_step)

    # Pattern 4: Model overkill (frontier model for trivial task)
    overkill_step = MockAgentStep(agent_name="verifier", step_name="model_overkill")
    overkill_step.calls.append(MockLLMCall(
        model="claude-3-5-opus-20250101",  # Frontier model
        input_tokens=200,
        output_tokens=30,
        latency_ms=rng.uniform(500, 1000),
        input_text=_deterministic_text(task_id, "overkill-input", 0),
        output_text=_deterministic_text(task_id, "overkill-output", 0),
    ))
    waste_steps.append(overkill_step)

    return waste_steps


def run_mock_pipeline(
    task_id: str,
    *,
    instrumented: bool = True,
    seed: int | None = None,
) -> dict[str, Any]:
    """Run mock pipeline, optionally with agentc instrumentation.

    Returns timing and span metadata for overhead measurement.
    """
    import agentc

    trace = generate_pipeline_trace(task_id, seed=seed)

    if instrumented and agentc.is_initialized():
        return _run_instrumented(trace)
    return _run_bare(trace)


def _run_bare(trace: MockPipelineTrace) -> dict[str, Any]:
    """Run pipeline without instrumentation (baseline)."""
    start = time.perf_counter_ns()

    for step in trace.steps:
        for call in step.calls:
            # Simulate LLM call latency (scaled down for benchmarking)
            time.sleep(call.latency_ms / 1_000_000)  # microsecond-scale sleep

    elapsed_ns = time.perf_counter_ns() - start

    return {
        "task_id": trace.task_id,
        "instrumented": False,
        "total_calls": trace.total_calls,
        "wall_clock_ns": elapsed_ns,
        "total_input_tokens": trace.total_input_tokens,
        "total_output_tokens": trace.total_output_tokens,
    }


def _run_instrumented(trace: MockPipelineTrace) -> dict[str, Any]:
    """Run pipeline with agentc instrumentation."""
    import agentc

    start = time.perf_counter_ns()
    spans_created: list[str] = []

    for step in trace.steps:

        @agentc.trace(name=f"{step.agent_name}-{step.step_name}")
        def agent_step(calls: list[MockLLMCall] = step.calls) -> None:
            for call in calls:
                with agentc.span(f"llm-{call.model}", kind="chat") as ctx:
                    spans_created.append(ctx.span_id)
                    # Simulate LLM call latency (scaled down)
                    time.sleep(call.latency_ms / 1_000_000)

        agent_step()

    elapsed_ns = time.perf_counter_ns() - start

    return {
        "task_id": trace.task_id,
        "instrumented": True,
        "total_calls": trace.total_calls,
        "wall_clock_ns": elapsed_ns,
        "spans_created": len(spans_created),
        "total_input_tokens": trace.total_input_tokens,
        "total_output_tokens": trace.total_output_tokens,
    }
