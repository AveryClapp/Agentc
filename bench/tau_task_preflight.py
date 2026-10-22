"""Run real pinned retail task cells with a scripted loopback provider.

The official tasks and scores are preserved, including negative and step-limit
outcomes. Provider text and billing are synthetic. This exercises acquisition
and lifecycle wiring; it supplies no benchmark efficacy or paid trial evidence.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from bench.campaign_accounting import money, read_snapshot
from bench.campaign_chat import ChatContract
from bench.campaign_transport import CampaignStopped, CampaignTransport
from bench.tau_campaign_preflight import FIXTURE_KEY, LocalProvider, source_manifest
from bench.tau_judge import JudgeCoverageError
from bench.tau_task_worker import (
    JUDGE_MODEL,
    SUPPORTED_ARMS,
    TauCell,
    TauTaskConfig,
    TauTaskWorker,
    private_write,
)


class TaskProvider(LocalProvider):
    def __init__(self, *, invalid_judge=False):
        super().__init__("unused", "unused", invalid_judge=invalid_judge)

    def message(self, body):
        if body["model"] == JUDGE_MODEL:
            actor = "judge"
        elif body["messages"][0]["content"].startswith("# User Simulation Guidelines"):
            actor = "user"
        else:
            actor = "agent"
        with self.lock:
            self.posts.append(body)
            self.counts[actor] += 1
            generation = f"task-fixture-{len(self.posts)}"
        if actor == "user":
            content = (
                "###STOP###"
                if any(
                    message.get("content")
                    == "Local fixture answer; no requested action was performed."
                    for message in body["messages"]
                )
                else "Local transport control."
            )
        elif actor == "agent":
            content = "Local fixture answer; no requested action was performed."
        else:
            expected = ast.literal_eval(
                body["messages"][-1]["content"]
                .rsplit("expectedOutcomes:", 1)[1]
                .strip()
            )
            content = json.dumps(
                {}
                if self.invalid_judge
                else {
                    "results": [
                        {
                            "expectedOutcome": assertion,
                            "metExpectation": False,
                            "reasoning": "Synthetic negative control.",
                        }
                        for assertion in expected
                    ]
                }
            )
        return {
            "id": generation,
            "model": body["model"],
            "provider": "OpenAI",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 100,
                "cost": "0.0004",
                "is_byok": False,
            },
            "openrouter_metadata": {
                "requested": body["model"],
                "strategy": "direct",
                "attempt": 1,
                "is_byok": False,
                "endpoints": {
                    "available": [
                        {"provider": "OpenAI", "model": body["model"], "selected": True}
                    ]
                },
                "pipeline": [],
            },
        }


def run(source: Path, output: Path, *, control: str = "arms") -> dict:
    from loguru import logger

    logger.remove()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    manifest = source_manifest(source)
    inventory = manifest["files"]["data/tau2/domains/retail/tasks.json"]
    config = TauTaskConfig(
        task_ids=("0", "2"),
        task_inventory_sha256=inventory,
        strong_model="openai/gpt-4o-2024-11-20",
        cheap_model="openai/gpt-4o-mini-2024-07-18",
        user_model="openai/gpt-4o-mini-2024-07-18",
        max_steps=10,
        network_policy="loopback_only",
        runtime_overrides=(("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS", "50"),),
    )
    arms = SUPPORTED_ARMS if control == "arms" else ("joint_guarded",)
    if control == "warm":
        config = replace(config, task_ids=tuple(str(i) for i in range(26)))
    if control == "step-limit":
        config = replace(config, max_steps=1)
    cells = tuple(
        TauCell(task, arm, 0, 20261011, ordinal)
        for ordinal, (arm, task) in enumerate(
            (arm, task) for arm in arms for task in config.task_ids
        )
    )
    ledger = output / "fixture-budget.jsonl"
    private_write(
        ledger,
        {
            "event": "origin",
            "at": "fixture",
            "key_id": hashlib.sha256(FIXTURE_KEY.encode()).hexdigest(),
            "usage_usd": "7",
        },
    )
    provider = TaskProvider(invalid_judge=control == "invalid-judge").start()
    results = []
    stopped = False
    try:
        transport = CampaignTransport(
            ledger=ledger,
            expected_sha256=read_snapshot(ledger).sha256,
            key=FIXTURE_KEY,
            stage="local-real-tau-task-cohort",
            stage_cap_usd="0.1305" if control == "stage-cap" else "2",
            contracts=[
                ChatContract(model, "openai", "OpenAI", model, 16384, 1024)
                for model in {
                    config.strong_model,
                    config.cheap_model,
                    config.user_model,
                    JUDGE_MODEL,
                }
            ],
            http=provider.http,
        )
        try:
            with TauTaskWorker(
                transport=transport,
                source=source,
                output=output / "cohort",
                config=config,
                cells=cells,
            ) as worker:
                for _ in cells:
                    results.append(worker.run_next())
        except (CampaignStopped, JudgeCoverageError):
            stopped = True
            if control not in {"invalid-judge", "stage-cap"}:
                raise
        snapshot = read_snapshot(ledger)
        assert stopped == (control in {"invalid-judge", "stage-cap"})
        assert source_manifest(source) == manifest
        assert (
            len([e for e in snapshot.events if e["event"] == "campaign_session"]) == 1
        )
        claims = [e for e in snapshot.events if e["event"] == "reserve"]
        assert len(claims) == len(provider.posts)
        assert {e["session_id"] for e in claims} == {transport.session_id}
        assert all(
            e["metadata"]["cell_id"] in {c.cell_id for c in cells} for e in claims
        )
        assert snapshot.state["pending_usd"] == 0
        assert snapshot.state["known_usd"] == len(provider.posts) * money("0.0004")
        if not stopped:
            assert sum(result["provider_call_count"] for result in results) == len(
                provider.posts
            )
            assert (
                sum(money(result["all_call_cost_usd"]) for result in results)
                == snapshot.state["known_usd"]
            )
        if control == "warm":
            assert len(results) == 26
            probes = [
                event for event in claims if event["metadata"]["kind"] == "exploration"
            ]
            assert len(probes) >= 20
            assert all(
                event["request"]["model"] in {config.strong_model, config.cheap_model}
                for event in probes
            )
        if control == "arms":
            assert len(results) == 14 and len(provider.posts) == 49
            assert all(
                result["official_score"] == 0 and result["task_status"] == "completed"
                for result in results
            )
            cheap = [
                e
                for e in claims
                if e["metadata"]["arm"] == "fixed_cheap"
                and e["metadata"]["actor"] == "agent"
            ]
            assert len(cheap) == 2 and all(
                e["request"]["model"] == config.cheap_model for e in cheap
            )
            assert not any(
                e["metadata"].get("native_plan_json")
                for e in claims
                if e["metadata"]["actor"] != "agent"
            )
        if control == "step-limit":
            assert len(results) == 2 and all(
                r["termination_reason"] == "max_steps"
                and r["task_status"] == "incomplete"
                for r in results
            )
        if stopped:
            assert (
                len(results) == 1
                and not (output / "cohort" / cells[1].cell_id / "result.json").exists()
            )
        assert (output / "cohort" / "completion.json").exists() == (not stopped)
        report = {
            "control": control,
            "stage": "E0",
            "paper_evidence": False,
            "original_campaign_journal_accessed": False,
            "paid_provider_calls": 0,
            "scripted_provider": True,
            "synthetic_billing": True,
            "official_task_cell_count": len(results),
            "local_http_post_count": len(provider.posts),
            "all_call_cost_usd": str(snapshot.state["known_usd"]),
            "actor_counts": dict(provider.counts),
            "stopped": stopped,
            "results": results,
            "source_closure_sha256": manifest["closure_sha256"],
            "accounting": snapshot.summary(),
        }
        private_write(output / "summary.json", report)
        return report
    finally:
        provider.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--control",
        choices=("arms", "step-limit", "invalid-judge", "stage-cap", "warm"),
        default="arms",
    )
    args = parser.parse_args(argv)
    os.environ.update(
        PYTHON_DOTENV_DISABLED="1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        TAU2_DATA_DIR=str(args.source.resolve() / "data"),
    )

    def network_guard(event, arguments):
        if event == "socket.connect" and (
            not isinstance(arguments[1], tuple) or arguments[1][0] != "127.0.0.1"
        ):
            raise RuntimeError("task fixture forbids external network")

    sys.addaudithook(network_guard)
    report = run(args.source.resolve(), args.output.resolve(), control=args.control)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "control",
                    "official_task_cell_count",
                    "local_http_post_count",
                    "stopped",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
