"""Pinned Tau retail orchestration through local provider and synthetic budget.

Runs the real text agent, user simulator, tool environment and DB+NL evaluator
on a hand-authored control task. A scripted loopback provider supplies outputs
and artificial billing. This is E0 integration evidence, never workload quality,
provider performance or paid campaign evidence. No benchmark reference policy
is used and the original campaign journal is not opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bench.campaign_accounting import canonical, money, read_snapshot
from bench.campaign_chat import PARAMETERS, ChatContract
from bench.campaign_http import CampaignHTTP
from bench.campaign_transport import CampaignTransport
from bench.tau_campaign_binding import REVISION, TauCampaignBinding
from bench.tau_judge import JudgeCoverageError

MODELS = {
    "agent": "openai/gpt-4o",
    "user": "openai/gpt-4o-mini",
    "judge": "openai/gpt-4.1-2025-04-14",
}
FIXTURE_KEY = "literal-local-tau-fixture-key"


def _private_write(path: Path, value) -> None:
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(canonical(value) + b"\n")


class LocalProvider:
    def __init__(self, order_id: str, user_id: str, *, invalid_judge: bool):
        self.order_id, self.user_id = order_id, user_id
        self.invalid_judge = invalid_judge
        self.lock = threading.Lock()
        self.posts = []
        self.counts = Counter()
        self.tool_sequence = (
            "get_order_details",
            "confirm",
            "cancel_pending_order",
            "complete",
        )

    def message(self, body):
        model = body["model"]
        actor = next(actor for actor, value in MODELS.items() if value == model)
        with self.lock:
            ordinal = self.counts[actor]
            self.counts[actor] += 1
            self.posts.append(body)
            generation = f"fixture-{len(self.posts)}"
        message = {"role": "assistant", "content": None}
        finish = "stop"
        if actor == "agent":
            step = self.tool_sequence[ordinal]
            if step in {"get_order_details", "cancel_pending_order"}:
                arguments = {"order_id": self.order_id}
                if step == "cancel_pending_order":
                    arguments["reason"] = "no longer needed"
                message["tool_calls"] = [
                    {
                        "id": f"tool-{ordinal}",
                        "type": "function",
                        "function": {"name": step, "arguments": json.dumps(arguments)},
                    }
                ]
                finish = "tool_calls"
            else:
                message["content"] = (
                    "Please confirm cancellation."
                    if step == "confirm"
                    else "Your order has been cancelled."
                )
        elif actor == "user":
            message["content"] = (
                f"My user ID is {self.user_id}. Please cancel {self.order_id}."
                if ordinal == 0
                else "Yes, cancel it because it is no longer needed."
                if ordinal == 1
                else "###STOP###"
            )
        else:
            verdict = (
                {}
                if self.invalid_judge
                else {
                    "results": [
                        {
                            "expectedOutcome": "The agent asks for confirmation before cancellation.",
                            "metExpectation": True,
                            "reasoning": "Synthetic positive control.",
                        }
                    ]
                }
            )
            message["content"] = json.dumps(verdict)
        return {
            "id": generation,
            "model": model,
            "provider": "OpenAI",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 100,
                "cost": "0.0004",
                "is_byok": False,
            },
            "openrouter_metadata": {
                "requested": model,
                "strategy": "direct",
                "attempt": 1,
                "is_byok": False,
                "endpoints": {
                    "available": [
                        {"provider": "OpenAI", "model": model, "selected": True}
                    ]
                },
                "pipeline": [],
            },
        }

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body):
                raw = canonical(body)
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Content-Type", "application/json")
                self.send_header("X-OpenRouter-Cache-Status", "MISS")
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/api/v1/key":
                    self.reply(
                        {
                            "data": {
                                "usage": "7",
                                "byok_usage": "0",
                                "limit_remaining": "43",
                                "limit_reset": None,
                            }
                        }
                    )
                else:
                    model = self.path.removeprefix("/api/v1/models/").removesuffix(
                        "/endpoints"
                    )
                    self.reply(
                        {
                            "data": {
                                "endpoints": [
                                    {
                                        "tag": "openai",
                                        "provider_name": "OpenAI",
                                        "name": "OpenAI | " + model,
                                        "context_length": 16384,
                                        "pricing": {
                                            "prompt": "0.000001",
                                            "completion": "0.000002",
                                            "request": "0",
                                        },
                                        "supported_parameters": [
                                            *PARAMETERS,
                                            "structured_outputs",
                                        ],
                                    }
                                ]
                            }
                        }
                    )

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.headers.get("X-OpenRouter-Cache") == "false"
                assert self.headers.get("X-OpenRouter-Metadata") == "enabled"
                self.reply(owner.message(body))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.http = CampaignHTTP.local_test(
            f"http://127.0.0.1:{self.server.server_port}/api/v1"
        )
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def source_manifest(source: Path):
    paths = subprocess.check_output(
        [
            "git",
            "-C",
            str(source),
            "ls-files",
            "-z",
            "src/tau2",
            "data/tau2/domains/retail",
            "data/tau2/user_simulator",
            "uv.lock",
            "pyproject.toml",
            "LICENSE",
        ]
    )
    files = {
        name.decode(): hashlib.sha256((source / name.decode()).read_bytes()).hexdigest()
        for name in paths.split(b"\0")
        if name
    }
    return {
        "revision": subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip(),
        "files": files,
        "closure_sha256": hashlib.sha256(canonical(files)).hexdigest(),
    }


def run(
    source: Path,
    output: Path,
    *,
    invalid_judge: bool = False,
    optimize_agent: bool = False,
):
    import agentc
    from loguru import logger
    from tau2.agent.llm_agent import LLMAgent
    from tau2.data_model.tasks import (
        Action,
        EvaluationCriteria,
        RewardType,
        Task,
        UserScenario,
    )
    from tau2.domains.retail.environment import get_environment
    from tau2.orchestrator.orchestrator import Orchestrator
    from tau2.runner.simulation import run_simulation
    from tau2.user.user_simulator import UserSimulator

    logger.remove()
    manifest = source_manifest(source)
    assert manifest["revision"] == REVISION
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    environment = get_environment()
    order = next(
        order
        for _, order in sorted(environment.tools.db.orders.items())
        if order.status == "pending"
    )
    task = Task(
        id="agentc-retail-transport-control",
        user_scenario=UserScenario(
            instructions="Synthetic cancellation transport control."
        ),
        evaluation_criteria=EvaluationCriteria(
            actions=[
                Action(
                    action_id="control-write",
                    name="cancel_pending_order",
                    arguments={
                        "order_id": order.order_id,
                        "reason": "no longer needed",
                    },
                )
            ],
            nl_assertions=["The agent asks for confirmation before cancellation."],
            reward_basis=[RewardType.DB, RewardType.NL_ASSERTION],
        ),
    )
    ledger = output / "fixture-budget.jsonl"
    _private_write(
        ledger,
        {
            "event": "origin",
            "at": "fixture",
            "key_id": hashlib.sha256(FIXTURE_KEY.encode()).hexdigest(),
            "usage_usd": "7",
        },
    )
    provider = LocalProvider(
        order.order_id, order.user_id, invalid_judge=invalid_judge
    ).start()
    try:
        contracts = [
            ChatContract(model, "openai", "OpenAI", model, 16384, 1024)
            for model in MODELS.values()
        ]
        transport = CampaignTransport(
            ledger=ledger,
            expected_sha256=read_snapshot(ledger).sha256,
            key=FIXTURE_KEY,
            stage="local-tau-fixture",
            stage_cap_usd="2",
            contracts=contracts,
            http=provider.http,
        )
        aliases = {value: value for value in MODELS.values()}
        aliases["gpt-4.1-2025-04-14"] = MODELS["judge"]
        initial_hash = environment.get_db_hash()
        rejected = False
        simulation = None
        with TauCampaignBinding(
            transport=transport,
            source=source,
            model_aliases=aliases,
            actor_output_caps={actor: 1024 for actor in MODELS},
        ) as binding:
            agentc.init(capture_content=False, storage_path=str(output / "runtime"))
            agent = LLMAgent(
                tools=environment.get_tools(),
                domain_policy=environment.get_policy(),
                llm=MODELS["agent"],
                llm_args={"temperature": 0, "max_tokens": 1024},
            )
            user = UserSimulator(
                llm=MODELS["user"],
                instructions=task.user_scenario.instructions,
                llm_args={"temperature": 0, "max_tokens": 1024},
            )
            orchestrator = Orchestrator(
                domain="retail",
                agent=agent,
                user=user,
                environment=environment,
                task=task,
                seed=42,
                max_steps=20,
                max_errors=1,
            )
            try:
                with binding.task(
                    task_id=task.id, arm="local-control", optimize_agent=optimize_agent
                ):
                    simulation = run_simulation(orchestrator)
                binding.assert_complete()
            except JudgeCoverageError:
                rejected = True
                if not invalid_judge:
                    raise
            scope_report = agentc.optimization_scope_report()
            agentc.shutdown()
            if rejected:
                # Preserve the guard's fatal state through context exit, instead
                # of suppressing it and allowing a worker to publish a reward.
                raise JudgeCoverageError("expected malformed judge control")
    except JudgeCoverageError:
        if not invalid_judge:
            raise
    finally:
        provider.close()
    snapshot = read_snapshot(ledger)
    claims = [event for event in snapshot.events if event["event"] == "reserve"]
    results = [event for event in snapshot.events if event["event"] == "result"]
    counts = Counter(event["metadata"]["actor"] for event in claims)
    assert counts == {"agent": 4, "user": 3, "judge": 1}
    assert len(results) == len(claims) == len(provider.posts) == 8
    assert snapshot.state["pending_usd"] == snapshot.state["holds_usd"] == 0
    assert str(snapshot.state["known_usd"]) == "0.0032"
    assert initial_hash != environment.get_db_hash()
    if not invalid_judge:
        assert simulation.reward_info.reward == 1
        assert simulation.reward_info.db_check.db_match
        assert len(simulation.reward_info.nl_assertions) == 1
        assert sum(
            money(getattr(message, "cost", None) or 0)
            for message in simulation.messages
        ) == money("0.0028")
        _private_write(
            output / "synthetic-trajectory.json", simulation.model_dump(mode="json")
        )
    assert rejected == invalid_judge
    assert source_manifest(source) == manifest
    if optimize_agent:
        agent_claims = [
            claim for claim in claims if claim["metadata"]["actor"] == "agent"
        ]
        assert all(
            json.loads(claim["metadata"]["native_plan_json"])
            .get("agentc_observation_context", {})
            .get("key")
            for claim in agent_claims
        )
        assert not any(
            claim["metadata"].get("native_plan_json")
            for claim in claims
            if claim["metadata"]["actor"] != "agent"
        )
        assert (
            scope_report["eligible_calls"] == 4 and scope_report["excluded_calls"] == 4
        )
    else:
        assert (
            scope_report["eligible_calls"] == 0 and scope_report["excluded_calls"] == 8
        )
    report = {
        "stage": "E0",
        "upstream_revision": REVISION,
        "source": manifest,
        "optimize_agent": optimize_agent,
        "original_campaign_journal_accessed": False,
        "paid_provider_calls": 0,
        "local_http_post_count": len(provider.posts),
        "real_benchmark_trials": 0,
        "scripted_provider": True,
        "synthetic_billing": True,
        "paper_evidence": False,
        "actor_counts": dict(counts),
        "judge_invalid_rejected": rejected,
        "synthetic_control_reward": simulation.reward_info.reward
        if simulation is not None
        else None,
        "accounting": snapshot.summary(),
        "optimization_scopes": scope_report,
        "database_changed": initial_hash != environment.get_db_hash(),
    }
    _private_write(output / "summary.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--invalid-judge", action="store_true")
    parser.add_argument("--optimize-agent", action="store_true")
    args = parser.parse_args(argv)
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["TAU2_DATA_DIR"] = str(args.source.resolve() / "data")
    os.environ.update(
        AGENTC_OPTIMIZE="1" if args.optimize_agent else "0",
        AGENTC_ENABLED_RULES="OutputBudget",
        AGENTC_OPTIMIZE_EXPLORATION="0",
        AGENTC_OPTIMIZE_SHADOW="0",
    )

    def network_guard(event, arguments):
        if event == "socket.connect" and (
            not isinstance(arguments[1], tuple) or arguments[1][0] != "127.0.0.1"
        ):
            raise RuntimeError("E0 fixture forbids external network")

    sys.addaudithook(network_guard)
    report = run(
        args.source.resolve(),
        args.output.resolve(),
        invalid_judge=args.invalid_judge,
        optimize_agent=args.optimize_agent,
    )
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
