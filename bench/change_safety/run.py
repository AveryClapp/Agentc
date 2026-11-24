"""Command line for the change-safety study.

    python -m bench.change_safety.run e0 --out DIR [--tasks 3] [--repetitions 1]
    python -m bench.change_safety.run live --config STUDY.json --out DIR \\
        --cap-usd 5 --confirm-live
    python -m bench.change_safety.run analyze --records DIR/records.jsonl --out FILE

``e0`` runs the whole pipeline against the deterministic mock provider and
spends nothing. ``live`` dispatches real, billed provider calls; it refuses to
start without ``--confirm-live`` and a positive ``--cap-usd``, and stops when
the next call's worst-case cost would cross the cap.

A live study config is JSON:

    {
      "stage": "P1",                       # E1, P1, C or T
      "agent_model": "anthropic/...",
      "user_model": "openai/gpt-4.1-mini-2025-04-14",
      "split": "calibration",              # or "test"
      "tasks": 5,                          # first N of the split, or null for all
      "repetitions": 1,
      "pins": {...},                       # changes.catalog(...) keyword arguments
      "rollout_arms": ["C1", "X2"],        # changes applied at every call
      "fork_arms": ["C0"]                  # changes forked from baseline runs
    }

Agentc storage is fresh per (stage, workload, model, arm, repetition) and
persists across that arm's tasks in split order (predecessor protocol
Section 4).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from agentc.change_safety.records import ChangeSpec

#: Mock model pins for Stage E0. None of these are real models.
E0_AGENT_MODEL = "mock/agent-strong"
E0_USER_MODEL = "mock/user"
E0_PINS: dict[str, Any] = {
    "cheaper_tier_model": "mock/agent-cheap",
    "newer_version_model": "mock/agent-strong-v2",
    "smallest_model": "mock/agent-tiny",
    "clear_trigger_input_tokens": 2_000,
    "clear_keep_tool_uses": 2,
    "compaction_trigger_input_tokens": 50_000,
}


def _quiet_tau2() -> None:
    try:
        from loguru import logger

        logger.remove()
        logger.add(sys.stderr, level="WARNING")
    except ImportError:
        pass


def build_catalog(pins: Mapping[str, Any], arms: Sequence[str]) -> dict[str, ChangeSpec]:
    from agentc.change_safety.changes import catalog

    specs = {spec.change_id: spec for spec in catalog(**pins)}
    missing = set(arms) - set(specs)
    if missing:
        raise SystemExit(f"unknown change IDs: {sorted(missing)}")
    return {arm: specs[arm] for arm in arms}


def run_stage(
    *,
    stage: str,
    out: Path,
    agent_model: str,
    user_model: str,
    task_ids: Sequence[str],
    repetitions: int,
    pins: Mapping[str, Any],
    rollout_arms: Sequence[str],
    fork_arms: Sequence[str],
    cap_usd: float,
    embed: bool = True,
    max_steps: int = 200,
) -> Path:
    import agentc

    from bench.change_safety.assignment import arm_order
    from bench.change_safety.dispatcher import configure_study_environment
    from bench.change_safety.spend import SpendLedger
    from bench.change_safety.tau2_driver import Tau2Study, Tau2StudyConfig, WORKLOAD, load_tasks

    configure_study_environment()
    _quiet_tau2()
    catalog = build_catalog(pins, sorted(set(rollout_arms) | set(fork_arms) | {"C0"}))
    fork_catalog = {arm: catalog[arm] for arm in {"C0", *fork_arms}}
    tasks = load_tasks(task_ids)
    out.mkdir(parents=True, exist_ok=False)
    ledger = SpendLedger(out / "spend.jsonl", cap_usd=cap_usd)
    manifest = {
        "stage": stage,
        "workload": WORKLOAD,
        "agent_model": agent_model,
        "user_model": user_model,
        "task_ids": list(task_ids),
        "repetitions": repetitions,
        "pins": dict(pins),
        "rollout_arms": list(rollout_arms),
        "fork_arms": sorted(fork_catalog),
        "changes": {arm: {"identity": s.identity, "params": s.params} for arm, s in catalog.items()},
        "cap_usd": cap_usd,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    arms = ["baseline", *rollout_arms]
    config = Tau2StudyConfig(stage=stage, agent_model=agent_model, user_model=user_model, max_steps=max_steps)
    with Tau2Study(config=config, output=out, ledger=ledger, embed=embed) as study:
        for task in tasks:
            for repetition in range(repetitions):
                for arm in arm_order(WORKLOAD, task.id, repetition, arms):
                    storage = out / "agentc" / f"{arm}-r{repetition}"
                    agentc.init(storage_path=str(storage))
                    try:
                        record = study.run(
                            task=task,
                            repetition=repetition,
                            arm=arm,
                            catalog=fork_catalog if arm == "baseline" else catalog,
                        )
                    finally:
                        agentc.shutdown()
                    print(
                        f"{task.id} r{repetition} {arm:9s} reward={record.reward:.2f} "
                        f"calls={record.model_calls} changed={record.changed_calls} "
                        f"spent=${ledger.committed_usd:.4f}",
                        flush=True,
                    )
    return out / "records.jsonl"


def analyze(records: Path, out: Path, *, permutations: int = 1000) -> dict[str, Any]:
    from agentc.change_safety.records import read_records

    from bench.change_safety import analysis

    loaded = read_records(str(records))
    summary: dict[str, Any] = {"analysis": analysis.summarize(loaded)}
    from bench.change_safety import replay

    summary["replay"] = replay.replay(loaded, n_orders=permutations)
    out.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return summary


def _e0(args: argparse.Namespace) -> None:
    from bench.change_safety.assignment import splits
    from bench.change_safety.mock_provider import MockProvider
    from bench.change_safety.tau2_driver import WORKLOAD, current_actor, load_tasks, register_mock_provider

    register_mock_provider(MockProvider(actor=current_actor, noise=args.noise))
    ids = [task.id for task in load_tasks()]
    task_ids = splits(WORKLOAD, ids).calibration[: args.tasks]
    arms = ["C1", "C2", "C3", "C4", "C5", "C6", "X1", "X2", "X3", "B1", "B2"]
    records = run_stage(
        stage="E0",
        out=args.out,
        agent_model=E0_AGENT_MODEL,
        user_model=E0_USER_MODEL,
        task_ids=task_ids,
        repetitions=args.repetitions,
        pins=E0_PINS,
        rollout_arms=arms,
        fork_arms=arms,
        cap_usd=0.0,
        embed=not args.no_embed,
        max_steps=args.max_steps,
    )
    analyze(records, args.out / "summary.json", permutations=args.permutations)
    print(f"E0 complete: {records}")


def _live(args: argparse.Namespace) -> None:
    if not args.confirm_live or not args.cap_usd > 0:
        raise SystemExit("live stages bill real providers: pass --confirm-live and a positive --cap-usd")
    from bench.change_safety.assignment import splits
    from bench.change_safety.tau2_driver import WORKLOAD, load_tasks

    config = json.loads(args.config.read_text())
    if config["stage"] not in {"E1", "P1", "C", "T"}:
        raise SystemExit("live stage must be E1, P1, C or T")
    ids = [task.id for task in load_tasks()]
    task_ids = list(splits(WORKLOAD, ids).of(config["split"]))
    if config.get("tasks") is not None:
        task_ids = task_ids[: int(config["tasks"])]
    run_stage(
        stage=config["stage"],
        out=args.out,
        agent_model=config["agent_model"],
        user_model=config["user_model"],
        task_ids=task_ids,
        repetitions=int(config["repetitions"]),
        pins=config["pins"],
        rollout_arms=config.get("rollout_arms", []),
        fork_arms=config.get("fork_arms", []),
        cap_usd=args.cap_usd,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bench.change_safety.run")
    sub = parser.add_subparsers(dest="command", required=True)
    e0 = sub.add_parser("e0", help="mock-provider pipeline run ($0)")
    e0.add_argument("--out", type=Path, required=True)
    e0.add_argument("--tasks", type=int, default=3)
    e0.add_argument("--repetitions", type=int, default=1)
    e0.add_argument("--noise", type=float, default=0.1)
    e0.add_argument("--max-steps", type=int, default=60)
    e0.add_argument("--permutations", type=int, default=200)
    e0.add_argument("--no-embed", action="store_true")
    e0.set_defaults(func=_e0)
    live = sub.add_parser("live", help="billed provider run")
    live.add_argument("--config", type=Path, required=True)
    live.add_argument("--out", type=Path, required=True)
    live.add_argument("--cap-usd", type=float, required=True)
    live.add_argument("--confirm-live", action="store_true")
    live.set_defaults(func=_live)
    an = sub.add_parser("analyze", help="analysis and replay over recorded runs")
    an.add_argument("--records", type=Path, required=True)
    an.add_argument("--out", type=Path, required=True)
    an.add_argument("--permutations", type=int, default=10_000)
    an.set_defaults(func=lambda a: analyze(a.records, a.out, permutations=a.permutations))
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
