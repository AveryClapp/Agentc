"""Offline controls for the pinned official tau retail environment.

This is a deterministic environment/evaluator smoke, not an agent policy or a
benchmark trial. It uses public simulated retail data; no reference task actions
are used to choose the scripted operations. Run with the isolated tau venv.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

REVISION = "672227c6b6676edc20d57ea53b7000262aae77b9"
SEED = "2026090903"


def encoded(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def source_manifest(source: Path) -> dict:
    """Bind exact tracked code/data bytes; reject dirty or mismatched sources."""
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    require(revision == REVISION, "Unexpected tau source revision")
    dirty = subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"]
    )
    require(not dirty, "Tau tracked source is modified")
    paths = (
        subprocess.check_output(
            [
                "git",
                "-C",
                str(source),
                "ls-files",
                "-z",
                "src/tau2",
                "data/tau2/domains/retail",
                "uv.lock",
                "pyproject.toml",
                "LICENSE",
                "docs/evaluation.md",
            ]
        )
        .decode()
        .strip("\0")
        .split("\0")
    )
    files = {}
    for relative in sorted(paths):
        data = (source / relative).read_bytes()
        files[relative] = {"bytes": len(data), "sha256": sha256(data)}
    return {
        "repository": "https://github.com/sierra-research/tau2-bench",
        "revision": revision,
        "files": files,
        "closure_sha256": sha256(encoded(files)),
    }


def check_tool_links(messages: list[dict]) -> int:
    """Check complete structured assistant-tool pairs without changing content."""
    pending: set[str] = set()
    seen: set[str] = set()
    for message in messages:
        role = message["role"]
        if role == "tool":
            tool_id = message.get("tool_call_id")
            require(tool_id in pending, "Orphan or repeated tool response")
            pending.remove(tool_id)
        else:
            require(not pending, "Unanswered tools before another participant message")
            for call in message.get("tool_calls") or []:
                require(role == "assistant", "Unsupported user-side tool projection")
                tool_id = call.get("id")
                require(
                    isinstance(tool_id, str) and bool(tool_id) and tool_id not in seen,
                    "Missing or reused tool call ID",
                )
                require(call.get("type") == "function", "Unsupported tool type")
                function = call.get("function", {})
                require(
                    isinstance(function.get("name"), str) and bool(function["name"]),
                    "Missing tool name",
                )
                require(
                    isinstance(json.loads(function["arguments"]), dict),
                    "Tool arguments must remain a JSON object",
                )
                seen.add(tool_id)
                pending.add(tool_id)
    require(not pending, "Unanswered tool calls")
    return len(seen)


def private_write(path: Path, data: bytes) -> None:
    path = path.resolve()
    require(
        not any((parent / ".git").exists() for parent in path.parents),
        "Public fixture bodies must be written outside Git",
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(
        path.parent.stat().st_mode & 0o077 == 0, "Private directory must have mode 0700"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def deny_network(event: str, args: tuple) -> None:
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise RuntimeError("Network is disabled during tau environment controls")


def run(source: Path, private_output: Path) -> dict:
    source = source.resolve()
    manifest = source_manifest(source)
    os.environ["TAU2_DATA_DIR"] = str(source / "data")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["LITELLM_TELEMETRY"] = "False"
    sys.addaudithook(deny_network)
    from loguru import logger

    logger.remove()  # Official tool debug logs contain fixture bodies.
    import tau2
    from tau2.data_model.message import (
        AssistantMessage,
        SystemMessage,
        ToolCall,
        UserMessage,
    )
    from tau2.data_model.tasks import (
        Action,
        EvaluationCriteria,
        RewardType,
        Task,
        UserScenario,
    )
    from tau2.domains.retail.environment import get_environment, get_tasks
    from tau2.evaluator.evaluator_env import EnvironmentEvaluator
    from tau2.utils.llm_utils import to_litellm_messages

    require(
        Path(tau2.__file__).resolve() == source / "src/tau2/__init__.py",
        "Imported tau package is not the pinned checkout",
    )
    require(
        importlib.metadata.version("tau2") == "1.0.1", "Unexpected tau package version"
    )
    environment = get_environment()
    untouched = get_environment()
    initial_hash = environment.get_db_hash()
    require(untouched.get_db_hash() == initial_hash, "Fresh environments differ")
    # ID ordering in the public DB, unrelated to task labels or optimizer coverage.
    order = next(
        order
        for _, order in sorted(environment.tools.db.orders.items())
        if order.status == "pending"
    )
    order_id, user_id = order.order_id, order.user_id
    history = [
        SystemMessage(role="system", content=environment.get_policy(), timestamp=None),
        UserMessage(
            role="user",
            content=f"My user ID is {user_id}. Please look up {order_id}.",
            timestamp=None,
        ),
    ]
    receipts = []

    def execute(name: str, arguments: dict, *, expect_error: bool = False) -> None:
        call = ToolCall(
            id=f"tau-preflight-{len(receipts):03d}", name=name, arguments=arguments
        )
        before = environment.get_db_hash()
        response = environment.get_response(call)
        response.timestamp = None
        require(
            response.id == call.id and response.requestor == call.requestor,
            "Official environment changed tool call identity",
        )
        require(response.error == expect_error, "Unexpected tool success/error")
        history.extend(
            [
                AssistantMessage(
                    role="assistant", content=None, tool_calls=[call], timestamp=None
                ),
                response,
            ]
        )
        receipts.append(
            {
                "id": call.id,
                "name": name,
                "error": response.error,
                "state_changed": environment.get_db_hash() != before,
            }
        )

    execute("get_user_details", {"user_id": user_id})
    execute("get_order_details", {"order_id": order_id})
    before_write = list(history)
    execute(
        "cancel_pending_order",
        {"order_id": order_id, "reason": "invalid-control-reason"},
        expect_error=True,
    )
    require(environment.get_db_hash() == initial_hash, "Rejected write changed state")
    history.append(
        AssistantMessage(
            role="assistant",
            content="Cancelling will refund the order. Do you confirm?",
            timestamp=None,
        )
    )
    history.append(
        UserMessage(role="user", content="Yes. It is no longer needed.", timestamp=None)
    )
    execute(
        "cancel_pending_order", {"order_id": order_id, "reason": "no longer needed"}
    )
    write_response_index = len(history) - 1
    execute("get_order_details", {"order_id": order_id})
    final_hash = environment.get_db_hash()
    require(initial_hash != final_hash, "Successful write did not change state")
    require(
        environment.tools.db.orders[order_id].status == "cancelled",
        "Write was not persisted",
    )
    require(
        json.loads(history[-1].content)["status"] == "cancelled",
        "Readback disagrees with state",
    )
    require(
        untouched.get_db_hash() == initial_hash, "Independent environment was mutated"
    )
    require(
        get_environment().get_db_hash() == initial_hash,
        "Reload did not reset the environment",
    )
    replay = get_environment()
    replay.set_state(None, None, list(history), strict=True)
    require(replay.get_db_hash() == final_hash, "Strict replay end state differs")

    # A hand-authored API control, deliberately NOT any benchmark task's gold policy.
    control_task = Task(
        id="agentc-retail-environment-control",
        user_scenario=UserScenario(
            instructions="Scripted environment control, not a benchmark task"
        ),
        evaluation_criteria=EvaluationCriteria(
            actions=[
                Action(
                    action_id="expected-state-write",
                    name="cancel_pending_order",
                    arguments={"order_id": order_id, "reason": "no longer needed"},
                )
            ],
            reward_basis=[RewardType.DB],
        ),
    )
    positive = EnvironmentEvaluator.calculate_reward(
        get_environment, control_task, history
    )
    negative = EnvironmentEvaluator.calculate_reward(
        get_environment, control_task, before_write
    )
    require(
        positive.reward == 1 and positive.db_check.db_match,
        "Positive DB control failed",
    )
    require(
        negative.reward == 0 and not negative.db_check.db_match,
        "Omitted-write control passed",
    )
    corrupt = [message.model_copy(deep=True) for message in history]
    corrupt[write_response_index].content = '{"corrupted_control": true}'
    try:
        get_environment().set_state(None, None, corrupt, strict=True)
    except ValueError:
        corruption_rejected = True
    else:
        raise ValueError("Strict replay accepted changed write output")

    projected = to_litellm_messages(history)
    count = check_tool_links(projected)
    for original, wire in zip(history, projected, strict=True):
        require(
            original.content == wire["content"], "Official projection altered content"
        )
        if isinstance(original, AssistantMessage) and original.tool_calls:
            for call, emitted in zip(
                original.tool_calls, wire["tool_calls"], strict=True
            ):
                require(
                    emitted["id"] == call.id
                    and emitted["function"]["name"] == call.name
                    and json.loads(emitted["function"]["arguments"]) == call.arguments,
                    "Official projection altered structured tool fields",
                )
    tool_schemas = [tool.openai_schema for tool in environment.get_tools()]
    private = {
        "messages": [message.model_dump(mode="json") for message in history],
        "official_litellm_messages": projected,
        "tools": tool_schemas,
    }
    private_bytes = encoded(private)
    private_write(private_output, private_bytes)

    # All base IDs and reward categories, no reference actions or success filtering.
    tasks = get_tasks("base")
    ids = [task.id for task in tasks]
    require(len(ids) == len(set(ids)), "Duplicate official task IDs")
    selected = sorted(
        ids, key=lambda task_id: hashlib.sha256(f"{SEED}:{task_id}".encode()).digest()
    )[:6]
    reward_basis = Counter(
        tuple(sorted(str(b.value) for b in task.evaluation_criteria.reward_basis))
        if task.evaluation_criteria
        else ()
        for task in tasks
    )
    distributions = sorted(
        {
            dist.metadata["Name"]: dist.version
            for dist in importlib.metadata.distributions()
        }.items()
    )
    report = {
        "schema": "agentc-tau-retail-preflight-v1",
        "harness_source_sha256": sha256(Path(__file__).read_bytes()),
        "source": manifest,
        "package_version": importlib.metadata.version("tau2"),
        "python": sys.version.split()[0],
        "installed_distributions": dict(distributions),
        "scope": "Scripted API/environment controls; no benchmark or agent quality score",
        "provider_calls": 0,
        "network_disabled_during_controls": True,
        "control": {
            "selection": "lexicographically first pending order in public DB",
            "tool_receipts": receipts,
            "structured_tool_pairs": count,
            "tool_schema_count": len(tool_schemas),
            "initial_db_hash": initial_hash,
            "final_db_hash": final_hash,
            "positive_db_reward": positive.reward,
            "omitted_write_db_reward": negative.reward,
            "strict_write_output_corruption_rejected": corruption_rejected,
            "independent_environment_unchanged": True,
            "fresh_reload_restored_initial_state": True,
            "replay_equals_executed_state": True,
        },
        "private_trace": {"bytes": len(private_bytes), "sha256": sha256(private_bytes)},
        "official_tasks": {
            "domain": "retail",
            "split": "base",
            "count": len(tasks),
            "ids_sha256": sha256(encoded(sorted(ids))),
            "reward_basis_counts": [
                {"basis": list(b), "count": n} for b, n in sorted(reward_basis.items())
            ],
            "prospective_development_seed": SEED,
            "prospective_development_ids": selected,
            "benchmark_trials_executed": 0,
        },
        "limitations": [
            "No provider wire capture, model/user simulation, optimization, cost or quality result",
            "Full task grading must honor each original reward_basis, not DB-only control grading",
            "Strict upstream replay checks mutating tool outputs; read outputs are skipped",
            "Transcript age does not establish StateDrop liveness; no dependency tags fabricated",
        ],
    }
    require(source_manifest(source) == manifest, "Source changed during execution")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--private-output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.source, args.private_output)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("xb") as stream:
        stream.write(encoded(report))
    print(
        json.dumps(
            {
                "control_passed": True,
                "tool_pairs": report["control"]["structured_tool_pairs"],
                "benchmark_trials": 0,
                "provider_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
