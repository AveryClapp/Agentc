"""Exercise frozen SWE-agent typed calls with real native planning and local HTTP.

Predetermined replies and simulated bills establish mechanics only. The
unchanged upstream default tools, history processor, model and parser run;
no tool commands execute and no external provider is contacted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path

from bench.campaign_accounting import digest, read_snapshot
from bench.campaign_chat import ChatContract
from bench.campaign_transport import CampaignTransport
from bench.coding_submission import private_write
from bench.swe_agent_binding import SweAgentCampaignBinding
from bench.tau_campaign_preflight import FIXTURE_KEY, LocalProvider

STRONG = "openai/gpt-4o-2024-11-20"
CHEAP = "openai/gpt-4o-mini-2024-07-18"


class Provider(LocalProvider):
    def __init__(self, mode):
        super().__init__("unused", "unused", invalid_judge=False)
        self.mode = mode

    def message(self, body):
        with self.lock:
            self.posts.append(body)
            index = len(self.posts)
        candidate = body["max_tokens"] < 1024 or body["model"] == CHEAP
        arguments = '{"command":"pwd"}'
        if candidate and self.mode == "changed":
            arguments = '{"command":"touch /tmp/probe-must-not-execute"}'
        if candidate and self.mode == "malformed":
            arguments = "INVALID JSON"
        return {
            "id": f"swe-fixture-{index}",
            "model": body["model"],
            "provider": "OpenAI",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "Inspect the repository.",
                        "tool_calls": [
                            {
                                "id": f"tool-{index}",
                                "type": "function",
                                "function": {"name": "bash", "arguments": arguments},
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": min(20, body["max_tokens"]),
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


def run(*, source: Path, output: Path, mode: str):
    import agentc
    from agentc._patches._optimizer_glue import drain_exploration
    import yaml

    if output.exists():
        raise ValueError("preflight output must be fresh")
    output.mkdir(mode=0o700)
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
    previous_env = {
        key: value for key, value in os.environ.items() if key.startswith("AGENTC_")
    }
    for key in previous_env:
        os.environ.pop(key)
    os.environ.update(
        AGENTC_OPTIMIZE="1",
        AGENTC_ENABLED_RULES="OutputBudget",
        AGENTC_OPTIMIZE_EXPLORATION="1",
        AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION="completion_first",
        AGENTC_OPTIMIZE_SHADOW="0.02",
        AGENTC_OPTIMIZE_SELECTION_AUDIT="1",
        AGENTC_OPTIMIZE_MAX_OVERHEAD_MS="50",
    )
    plans, actions = [], []
    transport = None
    try:
        transport = CampaignTransport(
            ledger=ledger,
            expected_sha256=read_snapshot(ledger).sha256,
            key=FIXTURE_KEY,
            stage="local-frozen-swe-agent-" + mode,
            stage_cap_usd="2",
            contracts=[
                ChatContract(
                    m, "openai", "OpenAI", m, 16384, 1024, cached_text_blocks=True
                )
                for m in (STRONG, CHEAP)
            ],
            http=provider.http,
        )
        with SweAgentCampaignBinding(
            transport=transport,
            source=source,
            models=frozenset({STRONG, CHEAP}),
            output_cap=1024,
            cohort_sha256=digest({"purpose": "E0 mechanics", "source": str(source)}),
            plan_observer=plans.append,
        ) as binding:
            from sweagent.agent.models import GenericAPIModelConfig, LiteLLMModel
            from sweagent.tools.tools import ToolConfig
            from sweagent.agent.history_processors import CacheControlHistoryProcessor

            raw = yaml.safe_load((source / "config/default.yaml").read_text())
            tools = ToolConfig.model_validate(raw["agent"]["tools"])
            model = LiteLLMModel(
                GenericAPIModelConfig(
                    name=STRONG,
                    api_key=FIXTURE_KEY,
                    per_instance_cost_limit=2.0,
                    max_input_tokens=16384,
                    max_output_tokens=1024,
                    completion_kwargs={"max_tokens": 1024},
                ),
                tools,
            )
            history = CacheControlHistoryProcessor.model_validate(
                raw["agent"]["history_processors"][0]
            )(
                [
                    {
                        "role": "system",
                        "content": raw["agent"]["templates"]["system_template"],
                    },
                    {
                        "role": "user",
                        "content": "Inspect the public synthetic fixture repository.",
                    },
                ]
            )
            agentc.init(storage_path=str(output / "runtime"), capture_content=False)
            for index in range(28):
                random.seed(31 + index)
                with binding.task(
                    task_id="synthetic-frozen-swe-task",
                    arm="joint_guarded",
                    cell_id=digest({"mechanics": mode}),
                    requested_model=STRONG,
                    optimize=True,
                ):
                    answer = model.query(history)
                    action = tools.parse_function(answer, tools.commands)[1]
                    assert action == "pwd"
                    actions.append(action)
                    drain_exploration(120000)
                    binding.assert_complete()
            state = binding.cell_accounting(digest({"mechanics": mode}))
            private_write(output / "plans.json", plans)
            private_write(output / "requests.json", provider.posts)
            private_write(output / "actions.json", actions)
            private_write(output / "upstream.json", binding.upstream)
            report = {
                "schema_version": 1,
                "mode": mode,
                "logical_queries": len(actions),
                "physical_fixture_calls": len(provider.posts),
                "owned_cell_accounting": state,
                "upstream_foreground_cost": model.stats.instance_cost,
                "settings_version": binding.settings_version,
                "function_calling": tools.use_function_calling,
                "default_configuration_sha256": hashlib.sha256(
                    (source / "config/default.yaml").read_bytes()
                ).hexdigest(),
                "native_plans": len(plans),
                "tool_commands_executed": 0,
                "external_provider_calls": 0,
                "paper_evidence": False,
            }
        # Inspect persisted profiles in a separate process: the native extension
        # and Python's sqlite3 can load different SQLite implementations.
        profiles = json.loads(
            subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import json,sqlite3,sys; "
                    "c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); "
                    "c.row_factory=sqlite3.Row; "
                    "print(json.dumps({t:[dict(r) for r in c.execute('select * from '+t)] "
                    "for t in ('execution_plan_profile','execution_plan_disabled',"
                    "'execution_plan_exploration')}))",
                    str(output / "runtime/cost_model.db"),
                ],
                text=True,
                timeout=30,
            )
        )
        private_write(output / "profiles.json", profiles)
        paired = sum(
            row["n_paired_observations"] for row in profiles["execution_plan_profile"]
        )
        disabled = len(profiles["execution_plan_disabled"])
        visible = sum(post["max_tokens"] < 1024 for post in provider.posts)
        probes = len(profiles["execution_plan_exploration"])
        # Probe calls are extra physical calls; successful foreground rewrites
        # must remain visible after those calls are excluded.
        foreground_rewrites = visible - probes
        if mode == "identity":
            assert paired >= 20 and disabled == 0 and foreground_rewrites > 0
        else:
            assert paired > 0 and disabled > 0 and foreground_rewrites == 0
        report.update(
            paired_native_observations=paired,
            disabled_native_plans=disabled,
            foreground_rewrites=foreground_rewrites,
        )
        private_write(output / "summary.json", report)
        return report
    finally:
        try:
            agentc.shutdown()
        finally:
            try:
                provider.server.shutdown()
                provider.server.server_close()
                provider.thread.join(timeout=10)
                assert not provider.thread.is_alive()
            finally:
                for key in tuple(os.environ):
                    if key.startswith("AGENTC_"):
                        os.environ.pop(key)
                os.environ.update(previous_env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("identity", "changed", "malformed"), default="identity"
    )
    args = parser.parse_args(argv)
    print(
        json.dumps(
            run(
                source=args.source.resolve(),
                output=args.output.resolve(),
                mode=args.mode,
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
