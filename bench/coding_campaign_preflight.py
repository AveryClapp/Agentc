"""Owned coding calls and exact shell-action learning on a loopback provider.

Real mini-SWE-agent tasks surround a repeated-query mechanism control. The
scripted replies and bills are E0 engineering evidence, not task efficacy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from pathlib import Path

from bench.campaign_accounting import digest, money, read_snapshot
from bench.campaign_chat import ChatContract
from bench.campaign_transport import CampaignStopped, CampaignTransport
from bench.coding_campaign_binding import CodingCampaignBinding
from bench.coding_submission import MARKER, private_write
from bench.coding_task_worker import PUBLIC_FIELDS, run_task, source_manifest
from bench.requests_fixture_preflight import (
    DATASET_SHA256,
    TASK,
    TASK_IMAGE,
    TASK_IMAGE_ID,
)
from bench.tau_campaign_preflight import FIXTURE_KEY, LocalProvider

STRONG = "openai/gpt-4o-2024-11-20"
CHEAP = "openai/gpt-4o-mini-2024-07-18"


class Provider(LocalProvider):
    def __init__(self, mode):
        super().__init__("unused", "unused", invalid_judge=False)
        self.mode, self.framework = mode, True

    def message(self, body):
        with self.lock:
            self.posts.append(body)
            generation = "coding-fixture-" + str(len(self.posts))
        candidate = body["max_tokens"] < 1024
        command = "printf '%s\\n' reference-visible"
        if self.framework and len(body["messages"]) > 2:
            command = "printf '%s\\n' " + MARKER + " && git --no-pager diff HEAD"
        if candidate and self.mode == "changed":
            command = "touch /testbed/probe-must-not-run"
        content = (
            ("THOUGHT: candidate prose" if candidate else "THOUGHT: reference prose")
            + "\n```bash\n"
            + command
            + "\n```"
        )
        if candidate and self.mode == "malformed":
            content = "No supported bash fence."
        return {
            "id": generation,
            "model": body["model"],
            "provider": "OpenAI",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "length"
                    if candidate and self.mode == "failed-response"
                    else "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cost": "0.0001" if candidate else "0.001",
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


def run(*, source: Path, public_task: Path, output: Path, mode: str):
    import agentc
    from agentc._patches._optimizer_glue import drain_exploration

    task = json.loads(public_task.read_bytes())
    assert set(task) == PUBLIC_FIELDS and task["instance_id"] == TASK
    upstream = source_manifest(source)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
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
    provider = Provider(mode).start()
    plans, model_costs, models = [], [], []
    for key in tuple(os.environ):
        if key.startswith("AGENTC_"):
            os.environ.pop(key)
    os.environ.update(
        AGENTC_OPTIMIZE="1",
        AGENTC_COMPOSE="1",
        AGENTC_ENABLED_RULES="OutputBudget",
        AGENTC_OPTIMIZE_EXPLORATION="1",
        AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION="completion_first",
        AGENTC_OPTIMIZE_SHADOW="0.02",
        AGENTC_OPTIMIZE_SELECTION_AUDIT="1",
        AGENTC_OPTIMIZE_MAX_OVERHEAD_MS="50",
    )
    try:
        transport = CampaignTransport(
            ledger=ledger,
            expected_sha256=read_snapshot(ledger).sha256,
            key=FIXTURE_KEY,
            stage="local-owned-coding",
            stage_cap_usd="2",
            contracts=[
                ChatContract(m, "openai", "OpenAI", m, 16384, 1024)
                for m in (STRONG, CHEAP)
            ],
            http=provider.http,
        )
        binding = CodingCampaignBinding(
            transport=transport,
            source=source,
            model_pair=frozenset({STRONG, CHEAP}),
            output_cap=1024,
            max_steps=3,
            cohort_sha256=digest(
                {"task": task, "models": [STRONG, CHEAP], "purpose": "E0 mechanism"}
            ),
            plan_observer=plans.append,
        )
        stopped = False
        try:
            with binding:
                agentc.init(storage_path=str(output / "runtime"))
                first = binding.model(
                    task_id=TASK,
                    arm="joint_guarded",
                    cell_id=digest("framework-first"),
                    requested_model=STRONG,
                    optimize=True,
                )
                record = run_task(
                    source=source,
                    public_task=task,
                    model=first,
                    output=output / "framework-first",
                    image=TASK_IMAGE,
                    image_id=TASK_IMAGE_ID,
                    dataset_sha256=DATASET_SHA256,
                    step_limit=3,
                    acquisition_kind="loopback_e0_owned_coding",
                )
                assert (
                    record["upstream_status"] == "Submitted"
                    and record["model_patch"] == ""
                )
                binding.assert_complete()
                models.append(first)
                enrollment = json.loads(
                    (output / "framework-first/enrollment.json").read_bytes()
                )
                events = [
                    json.loads(line)
                    for line in (output / "framework-first/events.jsonl")
                    .read_text()
                    .splitlines()
                ]
                history = next(
                    e["messages"] for e in events if e["event"] == "model_request"
                )
                provider.framework = False
                for ordinal in range(26):
                    if ordinal == 22:
                        random.seed(31)
                    model = binding.model(
                        task_id=TASK,
                        arm="joint_guarded",
                        cell_id=digest({"mechanism": ordinal}),
                        requested_model=STRONG,
                        optimize=True,
                    )
                    model.prepare_execution(enrollment)
                    models.append(model)
                    answer = model.query(history)
                    assert "reference-visible" in answer["content"]
                    drain_exploration(120000)
                    binding.assert_complete()
                provider.framework = True
                last = binding.model(
                    task_id=TASK,
                    arm="joint_guarded",
                    cell_id=digest("framework-last"),
                    requested_model=STRONG,
                    optimize=True,
                )
                models.append(last)
                record = run_task(
                    source=source,
                    public_task=task,
                    model=last,
                    output=output / "framework-last",
                    image=TASK_IMAGE,
                    image_id=TASK_IMAGE_ID,
                    dataset_sha256=DATASET_SHA256,
                    step_limit=3,
                    acquisition_kind="loopback_e0_owned_coding",
                )
                assert (
                    record["upstream_status"] == "Submitted"
                    and record["model_patch"] == ""
                )
                binding.assert_complete()
                for name in ("framework-first", "framework-last"):
                    record = json.loads(
                        (output / name / "submission.json").read_bytes()
                    )
                    assert not any(
                        "probe-must-not-run" in e.get("command", "")
                        for e in record["tool_events"]
                    )
        except CampaignStopped:
            stopped = True
            if mode != "failed-response":
                raise
        snapshot = read_snapshot(ledger)
        claims = [e for e in snapshot.events if e["event"] == "reserve"]
        results = [e for e in snapshot.events if e["event"] == "result"]
        assert len(claims) == len(results) == len(provider.posts)
        assert (
            len([e for e in snapshot.events if e["event"] == "campaign_session"]) == 1
        )
        assert snapshot.state["pending_usd"] == 0
        assert snapshot.state["known_usd"] == sum(
            (money(e["cost_usd"]) for e in results), money("0")
        )
        model_costs = [
            {"cell_id": m.cell_id, "logical_queries": m.n_calls, "cost_usd": m.cost}
            for m in models
        ]
        assert (
            abs(
                sum(m["cost_usd"] for m in model_costs)
                - float(snapshot.state["known_usd"])
            )
            < 1e-12
        )
        assert stopped == (mode == "failed-response")
        counts = {
            kind: sum(e["metadata"]["kind"] == kind for e in claims)
            for kind in ("primary", "exploration", "shadow", "fallback")
        }
        assert counts["exploration"] > 0
        if mode in {"changed", "malformed"}:
            assert all(
                e["request"]["max_tokens"] == 1024
                for e in claims
                if e["metadata"]["kind"] == "primary"
            )
        if mode == "matching":
            assert any(
                e["request"]["max_tokens"] < 1024
                for e in claims
                if e["metadata"]["kind"] == "primary"
            )
        private_write(output / "planning.json", plans)
        private_write(output / "model-costs.json", model_costs)
        report = {
            "stage": "E0",
            "paper_evidence": False,
            "paid_provider_calls": 0,
            "synthetic_billing": True,
            "original_campaign_journal_accessed": False,
            "mode": mode,
            "stopped": stopped,
            "local_http_post_count": len(provider.posts),
            "dispatch_counts": counts,
            "accounting": snapshot.summary(),
            "upstream_closure_sha256": upstream["closure_sha256"],
            "counterfactual_tool_executions": 0,
            "purpose": "real framework integration plus repeated-query mechanism control; no efficacy claim",
        }
        private_write(output / "summary.json", report)
        if not stopped:
            private_write(
                output / "completion.json",
                {
                    "record_type": "owned_coding_e0_completion",
                    "accounting": snapshot.summary(),
                },
            )
        return report
    finally:
        provider.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--public-task", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=("matching", "changed", "malformed", "failed-response"),
        default="matching",
    )
    args = parser.parse_args(argv)
    os.environ.update(PYTHON_DOTENV_DISABLED="1", LITELLM_LOCAL_MODEL_COST_MAP="True")

    def network_guard(event, arguments):
        if event == "socket.connect" and (
            not isinstance(arguments[1], tuple) or arguments[1][0] != "127.0.0.1"
        ):
            raise RuntimeError("coding E0 preflight forbids external network")

    sys.addaudithook(network_guard)
    report = run(
        source=args.source.resolve(),
        public_task=args.public_task.resolve(),
        output=args.output.resolve(),
        mode=args.mode,
    )
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("mode", "local_http_post_count", "dispatch_counts", "stopped")
            }
        )
    )


if __name__ == "__main__":
    main()
