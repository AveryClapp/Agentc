"""Offline request-shape preflight for optimizer experiments.

The preflight runs existing benchmark agents through the real Agentc SDK patch
and native optimizer, but replaces the OpenAI transport with a deterministic
local completion.  It answers one narrow question before paid evaluation:
which rules can the workload's actual request structure activate after the
optimizer warm-up gate?

The output is deliberately *not* paper evidence.  Provider latency, quality,
cost, and every output-dependent decision are synthetic.  No prompt or model
output content is written to the result; only shape and activation metadata is
retained.

Example:

    python -m bench.activation_preflight --tasks 8 --output /tmp/preflight.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Sequence


_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES_ROOT = _REPO_ROOT / "bench" / "fixtures"
_RESULT_SENTINEL = "AGENTC_ACTIVATION_PREFLIGHT_RESULT="
_CONTEXT_COMPRESS_MIN_PROMPT_BYTES = 8 * 1024
_CONTEXT_COMPRESS_MIN_DEAD_FRACTION = 0.30
_PREFLIGHT_MAX_OVERHEAD_MS = 1000


@dataclass(frozen=True)
class Workload:
    """One supported benchmark entry point and its interpretation class."""

    name: str
    module: str
    fixture_key: str
    workload_class: str
    interpretation: str


WORKLOADS: tuple[Workload, ...] = (
    Workload(
        name="hotpot_qa",
        module="bench.agents.hotpot_qa",
        fixture_key="hotpot_distractor",
        workload_class="natural_request",
        interpretation="Public multi-hop QA prompts; single-call retrieval context.",
    ),
    Workload(
        name="wikipedia_qa",
        module="bench.agents.support_qa",
        fixture_key="wikipedia_qa",
        workload_class="natural_request",
        interpretation="Cold-agent two-pass document QA, not designed for Agentc rules.",
    ),
    Workload(
        name="swebench_planner",
        module="bench.agents.swebench_planner",
        fixture_key="swebench_planner",
        workload_class="task_prompt_proxy",
        interpretation=(
            "SWE-bench issue text sent to a plan generator; this is not a code-solving "
            "SWE-bench agent and cannot establish resolve-rate external validity."
        ),
    ),
    Workload(
        name="rag_summarizer",
        module="bench.agents.rag_summarizer",
        fixture_key="rag_summarizer",
        workload_class="engineered_reference",
        interpretation="Repository map-reduce reference agent with explicit parallel_map.",
    ),
    Workload(
        name="long_context_qa",
        module="bench.agents.long_context_qa",
        fixture_key="long_context_qa",
        workload_class="purpose_built_control",
        interpretation="Positive control engineered to clear ContextCompress's size gate.",
    ),
    Workload(
        name="multirule_qa",
        module="bench.agents.multirule_qa",
        fixture_key="multirule_qa",
        workload_class="purpose_built_control",
        interpretation="Positive control engineered for long-context and state rewrites.",
    ),
)
_WORKLOAD_BY_NAME = {workload.name: workload for workload in WORKLOADS}


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _message_shape(messages: Iterable[Any]) -> tuple[int, int, str]:
    """Return message count, UTF-8 payload bytes, and a content digest."""
    normalized: list[dict[str, str]] = []
    for message in messages:
        if isinstance(message, dict):
            role = str(message.get("role", "user"))
            content = _content_text(message.get("content", ""))
        elif hasattr(message, "model_dump"):
            dumped = message.model_dump()
            role = str(dumped.get("role", "user"))
            content = _content_text(dumped.get("content", ""))
        else:
            role = str(getattr(message, "role", "user"))
            content = _content_text(getattr(message, "content", ""))
        normalized.append({"role": role, "content": content})
    payload = json.dumps(
        normalized,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    prompt_bytes = sum(len(item["content"].encode("utf-8")) for item in normalized)
    return len(normalized), prompt_bytes, digest


def _offline_completion(kwargs: dict[str, Any], sequence: int) -> SimpleNamespace:
    """Build a picklable OpenAI-shaped response without any network access."""
    messages = kwargs.get("messages") or []
    message_count, prompt_bytes, digest = _message_shape(messages)
    # This deterministic approximation only warms the native cost model.  It is
    # intentionally exposed in the output metadata and must not be interpreted
    # as provider-token accounting.
    prompt_tokens = max(1, math.ceil(prompt_bytes / 4) + 4 * message_count + 2)
    content = f"offline-response-{digest}"
    completion_tokens = max(1, math.ceil(len(content.encode("utf-8")) / 4))
    return SimpleNamespace(
        model=str(kwargs.get("model", "offline-model")),
        id=f"offline-{sequence:06d}",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(role="assistant", content=content),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


def _install_offline_openai(dispatch_events: list[dict[str, Any]]) -> None:
    """Replace sync and async OpenAI chat dispatch before Agentc patches it."""
    import openai.resources.chat.completions as completions

    def fake_create(_instance: Any, *args: Any, **kwargs: Any) -> SimpleNamespace:
        sequence = len(dispatch_events) + 1
        response = _offline_completion(kwargs, sequence)
        count, prompt_bytes, digest = _message_shape(kwargs.get("messages") or [])
        dispatch_events.append(
            {
                "sequence": sequence,
                "model": str(kwargs.get("model", "")),
                "message_count": count,
                "prompt_bytes": prompt_bytes,
                "prompt_digest": digest,
                "max_output_tokens": kwargs.get(
                    "max_tokens", kwargs.get("max_completion_tokens")
                ),
            }
        )
        return response

    async def fake_create_async(
        instance: Any, *args: Any, **kwargs: Any
    ) -> SimpleNamespace:
        return fake_create(instance, *args, **kwargs)

    completions.Completions.create = fake_create
    completions.AsyncCompletions.create = fake_create_async


def _plan_rules(plan: Any) -> list[str]:
    if getattr(plan, "kind", None) == "composed":
        return [str(rule) for rule in (getattr(plan, "rules", None) or []) if rule]
    rule = getattr(plan, "rule", None)
    return [str(rule)] if rule else []


def _call_shape(call: dict[str, Any]) -> dict[str, Any]:
    messages = call.get("messages") or []
    message_count, prompt_bytes, _ = _message_shape(messages)
    parameters = call.get("parameters") or {}
    extra = parameters.get("extra") if isinstance(parameters, dict) else {}
    extra = extra if isinstance(extra, dict) else {}
    attention_scores = [
        float(score)
        for score in (extra.get("attention_scores") or [])
        if isinstance(score, (int, float))
    ]
    dead_epsilon = float(extra.get("dead_attention_epsilon", 1e-4))
    dead_count = sum(score <= dead_epsilon for score in attention_scores)
    dead_fraction = dead_count / len(attention_scores) if attention_scores else None
    cc_size_gate = prompt_bytes > _CONTEXT_COMPRESS_MIN_PROMPT_BYTES
    cc_attention_gate = len(attention_scores) == message_count and message_count > 0
    return {
        "model": str(call.get("model", "")),
        "message_count": message_count,
        "prompt_bytes": prompt_bytes,
        "max_output_tokens": parameters.get("max_output_tokens"),
        "attention_score_count": len(attention_scores),
        "attention_dead_count": dead_count,
        "attention_dead_fraction": (
            round(dead_fraction, 6) if dead_fraction is not None else None
        ),
        "dead_attention_epsilon": dead_epsilon,
        "context_compress_size_gate": cc_size_gate,
        "context_compress_attention_gate": cc_attention_gate,
        "context_compress_dead_fraction_gate": bool(
            dead_fraction is not None
            and dead_fraction >= _CONTEXT_COMPRESS_MIN_DEAD_FRACTION
        ),
        "message_dependency_count": len(extra.get("message_deps") or []),
        "window_state_read_count": len(extra.get("window_state_reads") or []),
        "has_parallel_peer": bool(extra.get("parallel_peer")),
    }


def _install_plan_capture(plan_events: list[dict[str, Any]]) -> None:
    """Capture sanitized inputs and full composed-rule sets around native planning."""
    from agentc import _optimizer

    original = _optimizer.plan_call
    seen_by_site: Counter[str] = Counter()

    def capture(call: dict[str, Any]) -> Any:
        site = str(call.get("call_site_id", ""))
        seen_by_site[site] += 1
        plan = original(call)
        rewritten = getattr(plan, "call", None)
        plan_events.append(
            {
                "sequence": len(plan_events) + 1,
                "call_site_id": site,
                "call_site_ordinal": seen_by_site[site],
                "input": _call_shape(call),
                "plan_kind": str(getattr(plan, "kind", "pass_through")),
                "rules": _plan_rules(plan),
                "rewritten": _call_shape(rewritten)
                if isinstance(rewritten, dict)
                else None,
            }
        )
        return plan

    _optimizer.plan_call = capture


def _shape_stats(values: Sequence[int]) -> dict[str, int | None]:
    if not values:
        return {"min": None, "median": None, "p95": None, "max": None}
    ordered = sorted(values)
    median_ix = (len(ordered) - 1) // 2
    p95_ix = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "min": ordered[0],
        "median": ordered[median_ix],
        "p95": ordered[p95_ix],
        "max": ordered[-1],
    }


def _summarize_plan_events(
    events: Sequence[dict[str, Any]], hot_threshold: int
) -> dict[str, Any]:
    kind_counts = Counter(str(event["plan_kind"]) for event in events)
    rule_counts: Counter[str] = Counter()
    site_counts: Counter[str] = Counter()
    post_warmup: list[dict[str, Any]] = []
    for event in events:
        site_counts[str(event["call_site_id"])] += 1
        rule_counts.update(str(rule) for rule in event.get("rules") or [])
        if int(event["call_site_ordinal"]) > hot_threshold:
            post_warmup.append(event)

    post_kind_counts = Counter(str(event["plan_kind"]) for event in post_warmup)
    post_activations = sum(
        count for kind, count in post_kind_counts.items() if kind != "pass_through"
    )
    post_total = len(post_warmup)
    prompt_bytes = [int(event["input"]["prompt_bytes"]) for event in events]
    message_counts = [int(event["input"]["message_count"]) for event in events]
    cc_size_gate = sum(
        bool(event["input"].get("context_compress_size_gate")) for event in events
    )
    cc_attention_gate = sum(
        bool(event["input"].get("context_compress_attention_gate")) for event in events
    )
    cc_dead_fraction_gate = sum(
        bool(event["input"].get("context_compress_dead_fraction_gate"))
        for event in events
    )
    return {
        "optimizer_decisions": len(events),
        "call_site_count": len(site_counts),
        "call_site_decisions": dict(sorted(site_counts.items())),
        "plan_kind_counts": dict(sorted(kind_counts.items())),
        "rule_activation_counts": dict(sorted(rule_counts.items())),
        "warmup_decisions": len(events) - post_total,
        "post_warmup_decisions": post_total,
        "post_warmup_activations": post_activations,
        "post_warmup_pass_through": post_kind_counts.get("pass_through", 0),
        "post_warmup_activation_rate": (
            round(post_activations / post_total, 6) if post_total else None
        ),
        "prompt_bytes": _shape_stats(prompt_bytes),
        "message_count": _shape_stats(message_counts),
        "context_compress_screen": {
            "size_gate_decisions": cc_size_gate,
            "attention_gate_decisions": cc_attention_gate,
            "dead_fraction_gate_decisions": cc_dead_fraction_gate,
        },
    }


def _fixture_metadata(fixture_key: str) -> dict[str, Any]:
    path = _FIXTURES_ROOT / f"{fixture_key}.json"
    metadata: dict[str, Any] = {
        "key": fixture_key,
        "path": str(path.relative_to(_REPO_ROOT)),
        "exists": path.is_file(),
        "sha256": None,
        "records": None,
    }
    if not path.is_file():
        return metadata
    payload = path.read_bytes()
    metadata["sha256"] = hashlib.sha256(payload).hexdigest()
    try:
        rows = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return metadata
    metadata["records"] = len(rows) if isinstance(rows, list) else None
    return metadata


def _audit_summary(storage_dir: Path) -> dict[str, Any]:
    path = storage_dir / "optimizer_audit.db"
    if not path.is_file():
        return {"row_count": 0, "plan_kind_counts": {}, "first_rule_counts": {}}
    with sqlite3.connect(str(path)) as connection:
        row_count = int(
            connection.execute("SELECT COUNT(*) FROM plan_audit").fetchone()[0]
        )
        kinds = {
            str(kind): int(count)
            for kind, count in connection.execute(
                "SELECT plan_kind, COUNT(*) FROM plan_audit GROUP BY plan_kind"
            )
        }
        rules = {
            str(rule): int(count)
            for rule, count in connection.execute(
                "SELECT rule, COUNT(*) FROM plan_audit "
                "WHERE rule IS NOT NULL GROUP BY rule"
            )
        }
    return {
        "row_count": row_count,
        "plan_kind_counts": dict(sorted(kinds.items())),
        "first_rule_counts": dict(sorted(rules.items())),
    }


def _offline_env(
    base: dict[str, str],
    *,
    storage_dir: Path,
    tasks: int,
    model: str,
    hot_threshold: int,
) -> dict[str, str]:
    """Return a provider-sanitized environment for a worker subprocess."""
    env = dict(base)
    env.update(
        {
            "AGENTC_STORAGE_PATH": str(storage_dir),
            "AGENTC_CAPTURE_CONTENT": "0",
            "AGENTC_CAPTURE_EMBEDDINGS": "0",
            "AGENTC_OPTIMIZE": "1",
            "AGENTC_OPTIMIZE_HOT_THRESHOLD": str(hot_threshold),
            "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": str(_PREFLIGHT_MAX_OVERHEAD_MS),
            "AGENTC_OPTIMIZE_SHADOW": "0",
            "AGENTC_COMPOSE": "1",
            "BENCH_MAX_TASKS": str(tasks),
            "BENCH_TASK_OFFSET": "0",
            "BENCH_BASELINE_MODEL": model,
            "BENCH_OPENAI_BASE_URL": "",
            "BENCH_FIXTURE_OVERRIDE": "",
            "OPENAI_API_KEY": "offline-preflight-no-network",
            "OPENAI_MAX_RETRIES": "0",
            "ANTHROPIC_API_KEY": "",
            "TOGETHER_API_KEY": "",
            "HF_TOKEN": "",
            "HF_KEY": "",
            "GROQ_API_KEY": "",
            "PYTHONPATH": os.pathsep.join(
                [str(_REPO_ROOT / "python"), str(_REPO_ROOT)]
            ),
        }
    )
    return env


def _worker_result(
    workload: Workload,
    *,
    storage_dir: Path,
    tasks: int,
    model: str,
    hot_threshold: int,
) -> dict[str, Any]:
    base_env = os.environ.copy()
    os.environ.clear()
    os.environ.update(
        _offline_env(
            base_env,
            storage_dir=storage_dir,
            tasks=tasks,
            model=model,
            hot_threshold=hot_threshold,
        )
    )
    dispatch_events: list[dict[str, Any]] = []
    plan_events: list[dict[str, Any]] = []
    _install_offline_openai(dispatch_events)

    import agentc

    agentc.init(
        capture_content=False,
        capture_embeddings=False,
        storage_path=str(storage_dir),
    )
    _install_plan_capture(plan_events)
    run_error: str | None = None
    results: list[Any] = []
    try:
        module = importlib.import_module(workload.module)
        results = list(module.run())
    except BaseException as exc:
        run_error = f"{type(exc).__name__}: {exc}"
    finally:
        agentc.shutdown()

    summary = _summarize_plan_events(plan_events, hot_threshold)
    audit = _audit_summary(storage_dir)
    return {
        **asdict(workload),
        "status": "error" if run_error else ("ok" if plan_events else "no_calls"),
        "error": run_error,
        "tasks_requested": tasks,
        "tasks_returned": len(results),
        "fixture": _fixture_metadata(workload.fixture_key),
        "network_calls": 0,
        "offline_provider_dispatches": len(dispatch_events),
        **summary,
        "audit": audit,
        "audit_matches_capture": audit["row_count"] == len(plan_events),
        "plan_events": plan_events,
        "dispatch_events": dispatch_events,
    }


def _run_worker(args: argparse.Namespace) -> int:
    workload = _WORKLOAD_BY_NAME[args.worker]
    result = _worker_result(
        workload,
        storage_dir=Path(args.storage_dir),
        tasks=args.tasks,
        model=args.model,
        hot_threshold=args.hot_threshold,
    )
    print(_RESULT_SENTINEL + json.dumps(result, sort_keys=True))
    return 1 if result["status"] == "error" else 0


def _extract_worker_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith(_RESULT_SENTINEL):
            return json.loads(line.removeprefix(_RESULT_SENTINEL))
    raise RuntimeError("preflight worker did not emit a result")


def _git_commit() -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def _run_parent(args: argparse.Namespace) -> int:
    names = args.workload or [workload.name for workload in WORKLOADS]
    unknown = sorted(set(names) - set(_WORKLOAD_BY_NAME))
    if unknown:
        raise SystemExit(f"unknown workloads: {', '.join(unknown)}")

    if args.storage_root:
        storage_root = Path(args.storage_root).resolve()
        storage_root.mkdir(parents=True, exist_ok=True)
    else:
        storage_root = Path(tempfile.mkdtemp(prefix="agentc-activation-preflight-"))

    results: list[dict[str, Any]] = []
    for name in names:
        workload = _WORKLOAD_BY_NAME[name]
        storage_dir = storage_root / name
        storage_dir.mkdir(parents=True, exist_ok=False)
        command = [
            sys.executable,
            "-m",
            "bench.activation_preflight",
            "--worker",
            name,
            "--storage-dir",
            str(storage_dir),
            "--tasks",
            str(args.tasks),
            "--model",
            args.model,
            "--hot-threshold",
            str(args.hot_threshold),
        ]
        env = _offline_env(
            os.environ.copy(),
            storage_dir=storage_dir,
            tasks=args.tasks,
            model=args.model,
            hot_threshold=args.hot_threshold,
        )
        proc = subprocess.run(
            command,
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            result = _extract_worker_result(proc.stdout)
        except (RuntimeError, json.JSONDecodeError) as exc:
            result = {
                **asdict(workload),
                "status": "worker_failure",
                "error": f"{exc}; exit={proc.returncode}; stderr={proc.stderr[-2000:]}",
            }
        results.append(result)
        status = result.get("status", "unknown")
        decisions = result.get("optimizer_decisions", 0)
        activations = result.get("post_warmup_activations", 0)
        print(
            f"{name}: {status}; {decisions} decisions; "
            f"{activations} post-warmup activations",
            file=sys.stderr,
        )

    document = {
        "schema_version": 1,
        "experiment_kind": "offline_optimizer_activation_preflight",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "paper_evidence": False,
        "mocked_provider": True,
        "network_calls": 0,
        "interpretation_limits": [
            "Valid only for optimizer wiring and request-shape screening.",
            "Quality, latency, billed cost, token counts, and model outputs are synthetic.",
            "The planning-overhead kill switch is raised; this run cannot measure optimizer overhead.",
            "OutputBudget, ModelDowngrade, cache behavior, and downstream prompts may depend on mocked outputs.",
            "A rule firing here is a candidate for live evaluation, not evidence of safe savings.",
        ],
        "settings": {
            "tasks_per_workload": args.tasks,
            "model": args.model,
            "hot_threshold": args.hot_threshold,
            "max_planning_overhead_ms": _PREFLIGHT_MAX_OVERHEAD_MS,
            "composition": True,
            "shadow_rate": 0.0,
            "capture_content": False,
            "token_estimator": "ceil(utf8_prompt_bytes/4) + 4*message_count + 2",
            "context_compress_screen": {
                "min_prompt_bytes_exclusive": _CONTEXT_COMPRESS_MIN_PROMPT_BYTES,
                "min_dead_attention_fraction": _CONTEXT_COMPRESS_MIN_DEAD_FRACTION,
                "dead_attention_epsilon": "read from each intercepted call",
            },
            "storage_root": str(storage_root),
        },
        "workloads": results,
    }
    encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded)
    else:
        sys.stdout.write(encoded)
    return 1 if any(result.get("status") not in {"ok"} for result in results) else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workload",
        action="append",
        choices=sorted(_WORKLOAD_BY_NAME),
        help="Workload to scan; repeat to select multiple (default: all).",
    )
    parser.add_argument("--tasks", type=int, default=8)
    parser.add_argument("--model", default="gpt-4o-mini-2024-07-18")
    parser.add_argument("--hot-threshold", type=int, default=3)
    parser.add_argument("--storage-root")
    parser.add_argument("--output")
    parser.add_argument(
        "--worker", choices=sorted(_WORKLOAD_BY_NAME), help=argparse.SUPPRESS
    )
    parser.add_argument("--storage-dir", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.tasks < 1:
        raise SystemExit("--tasks must be at least 1")
    if args.hot_threshold < 0:
        raise SystemExit("--hot-threshold must be non-negative")
    if args.worker:
        if not args.storage_dir:
            raise SystemExit("--storage-dir is required in worker mode")
        return _run_worker(args)
    return _run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
