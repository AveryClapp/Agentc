"""No-network admission preflight for frozen LiteLLM workloads.

Run this file with the Python environment belonging to the frozen upstream
checkout.  It invokes the upstream call site with LiteLLM's deterministic
``mock_response`` transport while Agentc's real native planner is active.

This is wiring evidence, not paper evidence: provider quality, latency, and
billed cost are not measured.  Each upstream workload requires its own
environment because tau2 and SWE-agent pin different dependency surfaces.

Examples::

    /path/to/tau2/.venv/bin/python bench/litellm_admission_preflight.py \
        --workload tau2 --upstream-root /path/to/tau2 --turns 8

    /path/to/swe-agent/.venv/bin/python bench/litellm_admission_preflight.py \
        --workload sweagent --upstream-root /path/to/swe-agent
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import subprocess
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Iterator


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODEL = "gpt-4o"
_MAX_OUTPUT_TOKENS = 256
_HOT_THRESHOLD = 3


def _run_git(root: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _package_version(distribution: str) -> str | None:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


def _digest(value: Any) -> str:
    payload = json.dumps(
        value,
        default=str,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _plan_rules(plan: Any) -> list[str]:
    if getattr(plan, "kind", None) == "composed":
        return [str(rule) for rule in (getattr(plan, "rules", None) or [])]
    rule = getattr(plan, "rule", None)
    return [str(rule)] if rule else []


def _plan_event(call: dict[str, Any], plan: Any, sequence: int) -> dict[str, Any]:
    parameters = call.get("parameters") or {}
    rewritten = getattr(plan, "call", None) or {}
    rewritten_parameters = rewritten.get("parameters") or {}
    return {
        "sequence": sequence,
        "call_site_id": str(call.get("call_site_id", "")),
        "input": {
            "model": str(call.get("model", "")),
            "message_count": len(call.get("messages") or []),
            "max_output_tokens": parameters.get("max_output_tokens"),
        },
        "plan_kind": str(getattr(plan, "kind", "")),
        "rules": _plan_rules(plan),
        "rewritten": (
            {
                "model": str(rewritten.get("model", "")),
                "message_count": len(rewritten.get("messages") or []),
                "max_output_tokens": rewritten_parameters.get("max_output_tokens"),
            }
            if rewritten
            else None
        ),
    }


def _outcome_event(outcome: dict[str, Any], sequence: int) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "input_tokens": int(outcome.get("input_tokens", 0)),
        "output_tokens": int(outcome.get("output_tokens", 0)),
        "latency_ms": float(outcome.get("latency_ms", 0.0)),
        "cost_usd": float(outcome.get("cost_usd", 0.0)),
        "call_site_id": str(outcome.get("call_site_id", "")),
    }


def _span_summary(spans: list[dict[str, Any]]) -> dict[str, Any]:
    scopes: Counter[str] = Counter()
    eligible = 0
    excluded = 0
    statuses: Counter[str] = Counter()
    for span in spans:
        attrs = json.loads(span.get("attributes") or "{}")
        scopes[str(attrs.get("agentc.optimization.scope", "unscoped"))] += 1
        if attrs.get("agentc.optimization.eligible") is True:
            eligible += 1
        elif attrs.get("agentc.optimization.eligible") is False:
            excluded += 1
        statuses[str(span.get("status", ""))] += 1
    return {
        "count": len(spans),
        "scope_counts": dict(sorted(scopes.items())),
        "eligible": eligible,
        "excluded": excluded,
        "status_counts": dict(sorted(statuses.items())),
    }


def _configure_optimizer_environment(storage_path: Path) -> None:
    os.environ.update(
        {
            "AGENTC_STORAGE_PATH": str(storage_path),
            "AGENTC_OPTIMIZE": "1",
            "AGENTC_OPTIMIZE_HOT_THRESHOLD": str(_HOT_THRESHOLD),
            "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
            "AGENTC_OPTIMIZE_SHADOW": "0",
            "AGENTC_ENABLED_RULES": "OutputBudget",
            "AGENTC_COMPOSE": "1",
            "OPENAI_API_KEY": "offline-preflight-no-network",
            "ANTHROPIC_API_KEY": "",
            "TOGETHER_API_KEY": "",
        }
    )


@contextmanager
def _blocked_network() -> Iterator[list[str]]:
    """Fail the run if a mocked workload tries to open a network socket."""
    attempts: list[str] = []
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection

    def blocked_connect(sock: socket.socket, address: Any) -> None:
        attempts.append(repr(address))
        raise RuntimeError("network disabled by LiteLLM admission preflight")

    def blocked_create_connection(*args: Any, **kwargs: Any) -> None:
        address = args[0] if args else kwargs.get("address")
        attempts.append(repr(address))
        raise RuntimeError("network disabled by LiteLLM admission preflight")

    socket.socket.connect = blocked_connect
    socket.create_connection = blocked_create_connection
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect
        socket.create_connection = original_create_connection


def _install_capture() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    tuple[Any, Any, Any],
]:
    from agentc import _optimizer
    from agentc._patches import _litellm

    plans: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    original_plan = _optimizer.plan_call
    original_observe = _optimizer.observe_outcome
    original_write_root = _litellm._write_root_span

    def capture_plan(call: dict[str, Any]) -> Any:
        plan = original_plan(call)
        plans.append(_plan_event(call, plan, len(plans) + 1))
        return plan

    def capture_observe(plan: Any, outcome: dict[str, Any]) -> None:
        outcomes.append(_outcome_event(outcome, len(outcomes) + 1))
        original_observe(plan, outcome)

    _optimizer.plan_call = capture_plan
    _optimizer.observe_outcome = capture_observe
    _litellm._write_root_span = spans.append
    return (
        plans,
        outcomes,
        spans,
        (original_plan, original_observe, original_write_root),
    )


def _restore_capture(originals: tuple[Any, Any, Any]) -> None:
    from agentc import _optimizer
    from agentc._patches import _litellm

    original_plan, original_observe, original_write_root = originals
    _optimizer.plan_call = original_plan
    _optimizer.observe_outcome = original_observe
    _litellm._write_root_span = original_write_root


def _run_tau2(turns: int, storage_path: Path) -> dict[str, Any]:
    # Import aliases first: initialization must repair tau2's by-value imports.
    from tau2.agent import llm_agent
    from tau2.data_model.message import UserMessage
    from tau2.user import user_simulator
    from tau2.utils import llm_utils

    completion_before = llm_utils.completion
    assistant_generate_before = llm_agent.generate
    user_generate_before = user_simulator.generate

    import agentc

    agentc.init(capture_content=False, storage_path=str(storage_path))
    plans, outcomes, spans, originals = _install_capture()
    response_digests: list[str] = []
    try:
        alias_repaired = (
            llm_utils.completion is not completion_before
            and getattr(llm_utils.completion, "__wrapped__", None) is completion_before
        )
        scope_wrapped = (
            llm_agent.generate is not assistant_generate_before
            and user_simulator.generate is not user_generate_before
        )
        with _blocked_network() as network_attempts:
            for turn in range(turns):
                messages = [
                    UserMessage(role="user", content=f"offline admission turn {turn}")
                ]
                assistant_response = llm_agent.generate(
                    model=_MODEL,
                    messages=messages,
                    temperature=0,
                    max_tokens=_MAX_OUTPUT_TOKENS,
                    mock_response=f"assistant-{turn}",
                    call_name="agent_response",
                )
                user_response = user_simulator.generate(
                    model=_MODEL,
                    messages=messages,
                    temperature=0,
                    max_tokens=_MAX_OUTPUT_TOKENS,
                    mock_response=f"user-{turn}",
                    call_name="user_simulator_response",
                )
                response_digests.extend(
                    [
                        _digest(assistant_response.raw_data),
                        _digest(user_response.raw_data),
                    ]
                )
        if network_attempts:
            raise RuntimeError(f"unexpected network attempts: {network_attempts}")
        scope_report = agentc.optimization_scope_report()
    finally:
        _restore_capture(originals)
        agentc.shutdown()

    alias_restored = llm_utils.completion is completion_before
    scope_restored = (
        llm_agent.generate is assistant_generate_before
        and user_simulator.generate is user_generate_before
    )
    summary = _span_summary(spans)
    activation_count = sum(bool(event["rules"]) for event in plans)
    plan_kind_counts = dict(
        sorted(Counter(event["plan_kind"] for event in plans).items())
    )
    rule_activation_counts = dict(
        sorted(Counter(rule for event in plans for rule in event["rules"]).items())
    )
    assert alias_repaired and alias_restored
    assert scope_wrapped and scope_restored
    assert len(plans) == turns
    assert len(outcomes) == turns
    assert len({event["call_site_id"] for event in plans}) == 1
    assert summary["count"] == turns * 2
    assert summary["scope_counts"] == {
        "tau2.evaluated_assistant": turns,
        "tau2.user_simulator": turns,
    }
    assert summary["eligible"] == turns and summary["excluded"] == turns
    assert all(
        event["rules"] in ([], ["OutputBudget"])
        and (
            event["rewritten"] is None
            or event["rewritten"]["max_output_tokens"]
            <= event["input"]["max_output_tokens"]
        )
        for event in plans
    )
    return {
        "logical_calls": turns * 2,
        "assistant_calls": turns,
        "user_simulator_calls": turns,
        "planner_calls": len(plans),
        "observation_calls": len(outcomes),
        "activations": activation_count,
        "plan_kind_counts": plan_kind_counts,
        "rule_activation_counts": rule_activation_counts,
        "plan_events": plans,
        "outcome_events": outcomes,
        "spans": summary,
        "scope_report": scope_report,
        "alias_repaired": alias_repaired,
        "alias_restored": alias_restored,
        "scope_wrapped": scope_wrapped,
        "scope_restored": scope_restored,
        "response_count": len(response_digests),
        "response_digest": _digest(response_digests),
        "network_attempts": 0,
    }


def _run_sweagent(storage_path: Path) -> dict[str, Any]:
    # Import the actual model module before Agentc to cover lifecycle patch order.
    import litellm
    from sweagent.agent.models import GenericAPIModelConfig, LiteLLMModel
    from sweagent.tools.parsing import ThoughtActionParser
    from sweagent.tools.tools import ToolConfig

    completion_before = litellm.completion
    import agentc

    agentc.init(capture_content=False, storage_path=str(storage_path))
    plans, outcomes, spans, originals = _install_capture()
    try:
        completion_patched = (
            litellm.completion is not completion_before
            and getattr(litellm.completion, "__wrapped__", None) is completion_before
        )
        config = GenericAPIModelConfig(
            name=_MODEL,
            max_input_tokens=8192,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
            per_instance_cost_limit=0,
            completion_kwargs={"mock_response": "sweagent-offline-response"},
        )
        tools = ToolConfig(parse_function=ThoughtActionParser())
        model = LiteLLMModel(config, tools)
        with _blocked_network() as network_attempts:
            outputs = model._single_query(
                [{"role": "user", "content": "offline SWE-agent admission prompt"}]
            )
        if network_attempts:
            raise RuntimeError(f"unexpected network attempts: {network_attempts}")
        scope_report = agentc.optimization_scope_report()
    finally:
        _restore_capture(originals)
        agentc.shutdown()

    completion_restored = litellm.completion is completion_before
    summary = _span_summary(spans)
    assert completion_patched and completion_restored
    assert len(plans) == 1 and len(outcomes) == 1
    assert summary["count"] == 1
    assert summary["eligible"] == 1 and summary["excluded"] == 0
    assert summary["scope_counts"] == {"unscoped": 1}
    assert outputs == [{"message": "sweagent-offline-response"}]
    return {
        "logical_calls": 1,
        "planner_calls": len(plans),
        "observation_calls": len(outcomes),
        "plan_events": plans,
        "outcome_events": outcomes,
        "spans": summary,
        "scope_report": scope_report,
        "completion_patched": completion_patched,
        "completion_restored": completion_restored,
        "response_count": len(outputs),
        "response_digest": _digest(outputs),
        "network_attempts": 0,
    }


def _source_metadata(root: Path, workload: str) -> dict[str, Any]:
    distribution = "tau2" if workload == "tau2" else "sweagent"
    status = _run_git(root, "status", "--short")
    return {
        "distribution": distribution,
        "version": _package_version(distribution),
        "git_commit": _run_git(root, "rev-parse", "HEAD"),
        "git_exact_tag": _run_git(root, "describe", "--tags", "--exact-match"),
        "dirty_paths": status.splitlines() if status else [],
    }


def run(workload: str, upstream_root: Path, turns: int) -> dict[str, Any]:
    """Run one frozen workload admission check in the current interpreter."""
    with tempfile.TemporaryDirectory(prefix=f"agentc-{workload}-admission-") as temp:
        storage_path = Path(temp) / "agentc"
        # The native optimizer reads its store during module initialization.
        # Set the environment path before importing Agentc in either workload.
        _configure_optimizer_environment(storage_path)
        result = (
            _run_tau2(turns, storage_path)
            if workload == "tau2"
            else _run_sweagent(storage_path)
        )

    return {
        "schema_version": 1,
        "experiment_kind": "litellm_workload_admission_preflight",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "paper_evidence": False,
        "mocked_provider": True,
        "network_calls": 0,
        "workload": workload,
        "agentc": {
            "git_commit": _run_git(_REPO_ROOT, "rev-parse", "HEAD"),
            "package_version": _package_version("agentc"),
        },
        "upstream": _source_metadata(upstream_root, workload),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "litellm_version": _package_version("litellm"),
        },
        "settings": {
            "model": _MODEL,
            "max_output_tokens": _MAX_OUTPUT_TOKENS,
            "hot_threshold": _HOT_THRESHOLD,
            "enabled_rules": ["OutputBudget"],
            "shadow_rate": 0,
            "max_planning_overhead_ms": 1000,
            "turns": turns if workload == "tau2" else 1,
        },
        "interpretation_limits": [
            "Valid only for workload call-shape, actor-scope, lifecycle, and native-planner admission.",
            "Quality, provider latency, and billed cost are not measured.",
            "LiteLLM mock_response supplies deterministic outputs and the socket guard forbids network access.",
            "The model and raised planning-overhead threshold are activation controls, not frozen confirmatory settings.",
            "The frozen MLSys workloads are non-streaming; LiteLLM streaming remains outside this preflight envelope.",
        ],
        "result": result,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", choices=("tau2", "sweagent"), required=True)
    parser.add_argument("--upstream-root", type=Path, required=True)
    parser.add_argument("--turns", type=int, default=8)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.turns < 1:
        parser.error("--turns must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = run(args.workload, args.upstream_root.resolve(), args.turns)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
