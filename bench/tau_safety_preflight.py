"""Pinned real Tau tool/state and owned safety-review controls; E0 only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from bench.campaign_accounting import digest, money, read_snapshot
from bench.campaign_chat import ChatContract
from bench.campaign_transport import CampaignTransport
from bench.tau_campaign_preflight import FIXTURE_KEY, source_manifest
from bench.tau_safety import CATEGORIES, REVIEW_PROMPT, TauSafetyConfig
from bench.tau_task_preflight import TaskProvider
from bench.tau_task_worker import (
    JUDGE_MODEL,
    TauCell,
    TauTaskConfig,
    TauTaskWorker,
    private_write,
)


class SafetyProvider(TaskProvider):
    def __init__(self, control, order_id):
        super().__init__()
        self.control, self.order_id = control, order_id

    def message(self, body):
        safety = body["messages"][0].get("content") == REVIEW_PROMPT
        agent = body["model"] != JUDGE_MODEL and not body["messages"][0][
            "content"
        ].startswith("# User Simulation Guidelines")
        tool = (
            agent
            and self.control in {"unsafe-read", "invalid-tool-schema"}
            and not any(m["role"] == "tool" for m in body["messages"])
        )
        if not safety and not tool:
            return super().message(body)
        with self.lock:
            self.posts.append(body)
            self.counts["judge" if safety else "agent"] += 1
            generation = f"safety-fixture-{len(self.posts)}"
        if safety:
            request = json.loads(body["messages"][-1]["content"])
            ids = [m["message_id"] for m in request["evidence"]["messages"]]
            events = [e["event_id"] for e in request["evidence"]["events"]]
            content = json.dumps(
                {
                    "evidence_sha256": request["evidence_sha256"],
                    "examined_message_ids": ids,
                    "examined_event_ids": events,
                    "findings": {
                        c: {
                            "failure": False,
                            "evidence_ids": ids[:1],
                            "reason": "Synthetic safety-review control.",
                        }
                        for c in CATEGORIES
                    },
                }
            )
            if self.control == "malformed-review":
                content = "{}"
            message = {"role": "assistant", "content": content}
            finish = "stop"
        else:
            args = {
                "order_id": []
                if self.control == "invalid-tool-schema"
                else self.order_id
            }
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": generation,
                        "type": "function",
                        "function": {
                            "name": "get_order_details",
                            "arguments": json.dumps(args),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        return {
            "id": generation,
            "model": body["model"],
            "provider": "OpenAI",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 10,
                "total_tokens": 130,
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


def run(source, output, *, control="safe"):
    from loguru import logger

    logger.remove()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    manifest = source_manifest(source)
    db = json.loads((source / "data/tau2/domains/retail/db.json").read_bytes())
    order_id = next(iter(db["orders"]))
    config = TauTaskConfig(
        task_ids=("0", "2"),
        task_inventory_sha256=manifest["files"]["data/tau2/domains/retail/tasks.json"],
        strong_model="openai/gpt-4o-2024-11-20",
        cheap_model="openai/gpt-4o-mini-2024-07-18",
        user_model="openai/gpt-4o-mini-2024-07-18",
        max_steps=10,
        task_timeout_seconds=120,
        network_policy="loopback_only",
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
    provider = SafetyProvider(control, order_id).start()
    try:
        transport = CampaignTransport(
            ledger=ledger,
            expected_sha256=read_snapshot(ledger).sha256,
            key=FIXTURE_KEY,
            stage="local-safety-screen",
            stage_cap_usd="2",
            contracts=[
                ChatContract(m, "openai", "OpenAI", m, 16384, 1024)
                for m in {config.strong_model, config.cheap_model, JUDGE_MODEL}
            ],
            http=provider.http,
        )
        cells = tuple(
            TauCell(task, "unmodified_fixed_strong", 0, 42 + i, i)
            for i, task in enumerate(config.task_ids)
        )
        with TauTaskWorker(
            transport=transport,
            source=source,
            output=output / "acquisition",
            config=config,
            cells=cells,
            safety_config=TauSafetyConfig(),
        ) as worker:
            results = [worker.run_next() for _ in cells]
        snapshot = read_snapshot(ledger)
        expected = (
            None
            if control == "malformed-review"
            else control in {"unsafe-read", "invalid-tool-schema"}
        )
        assert all(
            r["safety_failure"] is expected
            and r["safety_metric_available"] is (expected is not None)
            for r in results
        )
        assert all(r["official_score"] == 0 for r in results)
        bills = [e for e in snapshot.events if e["event"] == "result"]
        reviews = [
            b
            for b in bills
            if b["metadata"].get("judge_purpose") == "tau-retail-safety-v1"
        ]
        assert len(reviews) == len(cells)
        assert len(bills) == len(provider.posts)
        assert snapshot.state["pending_usd"] == 0
        assert snapshot.state["known_usd"] == len(provider.posts) * money("0.0004")
        assert (
            sum(money(r["all_call_cost_usd"]) for r in results)
            == snapshot.state["known_usd"]
        )
        for result in results:
            path = worker.output / result["cell_id"]
            report = json.loads((path / "safety-report.json").read_bytes())
            assert digest(report) == result["safety_report_sha256"]
            assert any(
                b["metadata"]["judge_evidence_sha256"] == report["evidence_sha256"]
                and b["metadata"]["cell_id"] == result["cell_id"]
                for b in reviews
            )
        report = {
            "schema_version": 1,
            "purpose": "task safety observer and review accounting engineering control",
            "control": control,
            "paper_evidence": False,
            "synthetic_billing": True,
            "paid_provider_calls": 0,
            "task_cells": len(cells),
            "local_http_post_count": len(provider.posts),
            "safety_review_calls": len(reviews),
            "expected_safety_failure": expected,
            "known_fixture_charge_usd": str(snapshot.state["known_usd"]),
            "pending_usd": "0",
            "journal_sha256": snapshot.sha256,
            "sensor_accuracy_validated": False,
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
        choices=("safe", "unsafe-read", "invalid-tool-schema", "malformed-review"),
        default="safe",
    )
    args = parser.parse_args(argv)
    os.environ.update(PYTHON_DOTENV_DISABLED="1", LITELLM_LOCAL_MODEL_COST_MAP="True")

    def network_guard(event, arguments):
        if event == "socket.connect" and (
            not isinstance(arguments[1], tuple) or arguments[1][0] != "127.0.0.1"
        ):
            raise RuntimeError("safety E0 forbids external network")

    sys.addaudithook(network_guard)
    print(
        json.dumps(
            run(args.source.resolve(), args.output.resolve(), control=args.control)
        )
    )


if __name__ == "__main__":
    main()
