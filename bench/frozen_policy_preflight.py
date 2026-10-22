"""Execute frozen baseline mechanisms via pinned Tau and one loopback budget.

Configurations here are declared engineering fixtures, not trained policies
or calibration evidence. These calls are real upstream generate invocations
with synthetic histories; they are not official task trajectories.
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
from bench.campaign_records import cell_calls
from bench.campaign_transport import CampaignTransport
from bench.tau_campaign_binding import TauCampaignBinding
from bench.tau_campaign_preflight import FIXTURE_KEY, LocalProvider, source_manifest
from bench.tau_task_worker import private_write

STRONG = "openai/gpt-4o-2024-11-20"
CHEAP = "openai/gpt-4o-mini-2024-07-18"
MODES = ("best_static_joint", "route_then_rewrite", "rewrite_then_route")


class FrozenProvider(LocalProvider):
    def __init__(self):
        super().__init__("unused", "unused", invalid_judge=False)

    def message(self, body):
        with self.lock:
            self.posts.append(body)
            generation = f"frozen-policy-fixture-{len(self.posts)}"
        return {
            "id": generation,
            "model": body["model"],
            "provider": "OpenAI",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "stable answer"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cost": "0.0004",
                "is_byok": False,
            },
            "openrouter_metadata": {
                "requested": body["model"],
                "strategy": "direct",
                "attempt": 1,
                "is_byok": False,
                "pipeline": [],
                "endpoints": {
                    "available": [
                        {
                            "provider": "OpenAI",
                            "model": body["model"],
                            "selected": True,
                        }
                    ]
                },
            },
        }


def configuration(mode):
    value = {
        "strong_model": STRONG,
        "cheap_model": CHEAP,
        "divergence_threshold": 0.05,
        "static_plan": None,
        "router": None,
        "rewrite_by_target": {},
    }
    if mode == "best_static_joint":
        value["static_plan"] = {
            "target_model": CHEAP,
            "rewrites": [{"rule": "OutputBudget", "max_output_tokens": 128}],
        }
    else:
        value["router"] = {
            "feature": "prompt_utf8_bytes_v1",
            "cheap_max_prompt_bytes": 400,
        }
        value["rewrite_by_target"] = {
            STRONG: [
                {
                    "rule": "ContextCompress",
                    "min_prompt_bytes": 0,
                    "min_dead_fraction": 0.1,
                    "dead_attention_epsilon": 0.1,
                },
                {"rule": "OutputBudget", "max_output_tokens": 384},
            ],
            CHEAP: [{"rule": "OutputBudget", "max_output_tokens": 128}],
        }
    return value


def run(source: Path, output: Path):
    import agentc
    from agentc import _native
    from agentc._evaluation_policy import (
        FrozenEvaluationPolicy,
        evaluation_policy_scope,
        VERSION,
    )
    from agentc._patches._optimizer_glue import drain_exploration

    assert _native.optimize_evaluation_policy_version() == VERSION
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
    lock = {
        "schema_version": 1,
        "evidence_kind": "engineering_fixture",
        "source_stage": "E0",
        "configurations": {mode: configuration(mode) for mode in MODES},
    }
    lock_sha = digest(lock)
    private_write(
        output / "fixture-lock.json", {"selections": lock, "sha256": lock_sha}
    )
    provider = FrozenProvider().start()
    plans, executions = [], []
    try:
        transport = CampaignTransport(
            ledger=ledger,
            expected_sha256=read_snapshot(ledger).sha256,
            key=FIXTURE_KEY,
            stage="local-frozen-policy-mechanisms",
            stage_cap_usd="2",
            contracts=[
                ChatContract(m, "openai", "OpenAI", m, 16384, 1024)
                for m in (STRONG, CHEAP)
            ],
            http=provider.http,
        )
        with TauCampaignBinding(
            transport=transport,
            source=source,
            model_aliases={m: m for m in (STRONG, CHEAP)},
            actor_output_caps={actor: 1024 for actor in ("agent", "user", "judge")},
            agent_model_allowlist=frozenset({STRONG, CHEAP}),
            plan_observer=plans.append,
        ) as binding:
            from tau2.agent import llm_agent
            from tau2.data_model.message import (
                AssistantMessage,
                SystemMessage,
                UserMessage,
            )

            messages = [
                SystemMessage(role="system", content="Preserve current instructions."),
                UserMessage(role="user", content="Answer the current task."),
                AssistantMessage(
                    role="assistant", content="obsolete unrelated archive " * 100
                ),
                UserMessage(role="user", content="obsolete unrelated archive " * 100),
                AssistantMessage(role="assistant", content="Current answer context."),
                UserMessage(role="user", content="Give the current answer."),
            ]
            for mode in MODES:
                for key in tuple(os.environ):
                    if key.startswith("AGENTC_"):
                        os.environ.pop(key)
                os.environ.update(
                    AGENTC_OPTIMIZE="1",
                    AGENTC_COMPOSE="1",
                    AGENTC_EVAL_PLANNER_MODE=mode,
                    AGENTC_MODEL_CATALOG_OFFLINE="1",
                    AGENTC_OPTIMIZE_SELECTION_AUDIT="1",
                    AGENTC_OPTIMIZE_EXPLORATION="1",
                    AGENTC_OPTIMIZE_SHADOW="0.02",
                    AGENTC_OPTIMIZE_MAX_OVERHEAD_MS="50",
                )
                frozen = FrozenEvaluationPolicy.from_configuration(
                    mode=mode,
                    calibration_lock_sha256=lock_sha,
                    configuration=configuration(mode),
                )
                cell = digest({"mechanism": mode})
                agentc.init(storage_path=str(output / mode))
                try:
                    random.seed(31)
                    with (
                        evaluation_policy_scope(frozen),
                        binding.task(
                            task_id="synthetic-frozen-policy-history",
                            arm=mode,
                            optimize_agent=True,
                            cell_id=cell,
                        ),
                    ):
                        answer = llm_agent.generate(
                            model=STRONG,
                            messages=messages,
                            tools=None,
                            temperature=0,
                            max_tokens=1024,
                        )
                    assert answer.content == "stable answer"
                    drain_exploration(120000)
                    binding.assert_complete()
                    selected = [p for p in plans if p["cell_id"] == cell]
                    assert len(selected) == 1
                    plan = json.loads(selected[0]["native_plan_json"])
                    assert "agentc_exploration_context" not in plan
                    audit = plan["agentc_selection_audit"]
                    assert (
                        audit["planner_mode"] == mode
                        and audit["candidate_count"] is None
                    )
                    assert audit["policy"]["calibration_lock_sha256"] == lock_sha
                    assert audit["policy"]["interaction_profiles_consulted"] is False
                    expected_model = STRONG if mode == "route_then_rewrite" else CHEAP
                    assert plan["call"]["model"] == expected_model
                    if mode != "best_static_joint":
                        assert (audit["policy"]["router_prompt_bytes"] <= 400) == (
                            mode == "rewrite_then_route"
                        )
                    calls = cell_calls(
                        read_snapshot(ledger),
                        session_id=transport.session_id,
                        cell_id=cell,
                        plans=selected,
                    )
                    primary = [c for c in calls if c["metadata"]["kind"] == "primary"]
                    assert (
                        len(primary) == 1
                        and primary[0]["request"]["model"] == expected_model
                    )
                    assert primary[0]["request"]["max_tokens"] == (
                        128 if mode == "best_static_joint" else 384
                    )
                    assert all(
                        c["request"]["model"] == STRONG
                        for c in calls
                        if c["metadata"]["kind"] == "shadow"
                    )
                    executions.append(
                        {"mode": mode, "cell_id": cell, "plan": plan, "calls": calls}
                    )
                finally:
                    agentc.shutdown()
        snapshot = read_snapshot(ledger)
        assert sum(e["event"] == "campaign_session" for e in snapshot.events) == 1
        assert snapshot.state["pending_usd"] == 0 and snapshot.state["holds_usd"] == 0
        assert snapshot.state["known_usd"] == sum(
            (
                money(c["billed_result"]["cost_usd"])
                for e in executions
                for c in e["calls"]
            ),
            money("0"),
        )
        assert len(provider.posts) == sum(len(e["calls"]) for e in executions)
        assert source_manifest(source) == upstream
        private_write(output / "executions.json", executions)
        summary = {
            "schema_version": 1,
            "purpose": "frozen baseline execution mechanisms",
            "evidence_kind": "engineering_fixture",
            "source_stage": "E0",
            "paper_evidence": False,
            "trained_policies": False,
            "paid_provider_calls": 0,
            "local_http_post_count": len(provider.posts),
            "fixture_cost_usd": str(snapshot.state["known_usd"]),
            "source_manifest": upstream,
            "lock_sha256": lock_sha,
            "executions_sha256": digest(executions),
            "journal_sha256": snapshot.sha256,
        }
        private_write(output / "summary.json", summary)
        return summary
    finally:
        provider.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    os.environ.update(PYTHON_DOTENV_DISABLED="1", LITELLM_LOCAL_MODEL_COST_MAP="True")

    def network_guard(event, arguments):
        if event == "socket.connect" and (
            not isinstance(arguments[1], tuple) or arguments[1][0] != "127.0.0.1"
        ):
            raise RuntimeError("frozen E0 mechanisms forbid external network")

    sys.addaudithook(network_guard)
    result = run(args.source.resolve(), args.output.resolve())
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "local_http_post_count",
                    "fixture_cost_usd",
                    "trained_policies",
                    "paid_provider_calls",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
