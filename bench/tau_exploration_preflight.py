"""E0: real Tau serialization, native leases, concurrent HTTP and restart.

Uses only synthetic prompts, scripted loopback responses and a new fixture
journal. It is a transport/mechanism control, never a benchmark trial or a
savings result. The negative control bills an invalid background response and
requires the worker completion gate to stop even after the reference returned.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import threading
from collections import Counter
from pathlib import Path

from bench.campaign_accounting import money, read_snapshot
from bench.campaign_chat import ChatContract
from bench.campaign_transport import CampaignStopped, CampaignTransport
from bench.tau_campaign_binding import TauCampaignBinding
from bench.tau_campaign_preflight import (
    FIXTURE_KEY,
    MODELS,
    LocalProvider,
    _private_write,
    source_manifest,
)

REFERENCE_CAP = 1024
PAIRS = 20


class CalibrationProvider(LocalProvider):
    def __init__(self, *, fail_probe: bool, tool_mode: str | None = None):
        super().__init__("unused", "unused", invalid_judge=False)
        self.fail_probe = fail_probe
        self.probe_entered = threading.Event()
        self.release_probe = threading.Event()
        self.candidate_count = 0
        self.tool_mode = tool_mode

    def message(self, body):
        assert body["model"] == MODELS["agent"]
        candidate = body["max_tokens"] < REFERENCE_CAP
        with self.lock:
            self.posts.append(body)
            generation = f"calibration-fixture-{len(self.posts)}"
            first_candidate = candidate and self.candidate_count == 0
            self.candidate_count += int(candidate)
        if first_candidate:
            self.probe_entered.set()
            assert self.release_probe.wait(8)
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 40 if candidate else 100,
            "cost": "0.0002" if candidate else "0.0004",
            "is_byok": False,
        }
        response = {
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
            "usage": usage,
            "openrouter_metadata": {
                "requested": body["model"],
                "strategy": "direct",
                "attempt": 1,
                "is_byok": False,
                "endpoints": {
                    "available": [
                        {
                            "provider": "OpenAI",
                            "model": body["model"],
                            "selected": True,
                        }
                    ]
                },
                "pipeline": [],
            },
        }
        if self.tool_mode is not None:
            arguments = json.dumps({"value": "stable"})
            if candidate and self.tool_mode == "different":
                arguments = json.dumps({"value": "different"})
            elif candidate and self.tool_mode == "malformed-json":
                arguments = '{"value":'
            elif candidate and self.tool_mode == "invalid-envelope":
                arguments = None
            response["choices"][0].update(
                finish_reason="tool_calls",
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "action-" + generation,
                            "type": "function",
                            "function": {
                                "name": "fixture_action",
                                "arguments": arguments,
                            },
                        }
                    ],
                },
            )
        if self.fail_probe and candidate:
            # Preserve a real synthetic charge before rejecting missing usage;
            # the transport retains the remainder of the full allowance.
            del usage["completion_tokens"]
        return response


def invoke(task_id, binding, tools=None):
    from tau2.agent import llm_agent
    from tau2.data_model.message import (
        UserMessage,
        AssistantMessage,
        ToolCall,
        ToolMessage,
    )

    messages = [
        UserMessage(role="user", content="Synthetic transport mechanism control.")
    ]
    if tools is not None:
        # Synthetic but structurally real completed tool exchange. The probe
        # must preserve its nullable assistant envelope and result identity.
        messages.extend(
            [
                AssistantMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="prior-action",
                            name="fixture_action",
                            arguments={"value": "prior"},
                        )
                    ],
                ),
                ToolMessage(
                    role="tool", id="prior-action", content="Synthetic prior result."
                ),
            ]
        )

    with binding.task(task_id=task_id, arm="synthetic-control", optimize_agent=True):
        return llm_agent.generate(
            model=MODELS["agent"],
            messages=messages,
            tools=tools,
            temperature=0,
            max_tokens=REFERENCE_CAP,
        )


def run(
    source: Path,
    output: Path,
    *,
    fail_probe: bool = False,
    tool_mode: str | None = None,
):
    import agentc
    from loguru import logger
    from agentc._patches._optimizer_glue import drain_exploration

    logger.remove()
    manifest = source_manifest(source)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
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
    provider = CalibrationProvider(fail_probe=fail_probe, tool_mode=tool_mode).start()
    calibration = []
    overlap_verified = False
    stopped = False
    admitted_cap = None
    tool_executions = []
    tools = None
    if tool_mode is not None:
        from tau2.environment.tool import Tool

        def fixture_action(value: str) -> str:
            """Record a synthetic tool execution for the isolation control."""
            tool_executions.append(value)
            return value

        tools = [Tool(fixture_action)]
    expect_stop = fail_probe or tool_mode == "invalid-envelope"
    unsafe_action = tool_mode in {"different", "malformed-json"}
    target_pairs = 2 if unsafe_action else PAIRS

    def assert_visible(result):
        if tools is None:
            assert result.content == "stable answer"
        else:
            assert result.content is None and len(result.tool_calls) == 1
            assert result.tool_calls[0].name == "fixture_action"
            assert result.tool_calls[0].arguments == {"value": "stable"}

    try:
        transport = CampaignTransport(
            ledger=ledger,
            expected_sha256=read_snapshot(ledger).sha256,
            key=FIXTURE_KEY,
            stage="local-tau-calibration-fixture",
            stage_cap_usd="2",
            contracts=[
                ChatContract(model, "openai", "OpenAI", model, 16384, REFERENCE_CAP)
                for model in MODELS.values()
            ],
            http=provider.http,
        )
        try:
            with TauCampaignBinding(
                transport=transport,
                source=source,
                model_aliases={model: model for model in MODELS.values()},
                actor_output_caps={actor: REFERENCE_CAP for actor in MODELS},
            ) as binding:
                agentc.init(capture_content=False, storage_path=str(output / "runtime"))
                for _ in range(3 + PAIRS * 4):
                    result = invoke("synthetic-calibration", binding, tools)
                    calibration.append(result)
                    assert_visible(result)
                    assert money(result.cost) == money("0.0004")
                    if not overlap_verified and provider.probe_entered.wait(1):
                        try:
                            # The task ContextVar has already reset. A different
                            # foreground task must complete while this owned
                            # background HTTP request remains blocked.
                            other = invoke(
                                "synthetic-concurrent-foreground", binding, tools
                            )
                            assert_visible(other)
                            assert not provider.release_probe.is_set()
                            overlap_verified = True
                        finally:
                            provider.release_probe.set()
                    drain_exploration(10000)
                    binding.assert_complete()
                    with provider.lock:
                        if provider.candidate_count >= target_pairs:
                            break
                assert provider.candidate_count == target_pairs
                assert overlap_verified
                agentc.shutdown()
                # Restart the real native optimizer from this fixture's own
                # persisted evidence. No tokens or feedback are manufactured.
                agentc.init(capture_content=False, storage_path=str(output / "runtime"))
                # One deterministic sampling draw exercises the real 2% gate.
                # A 100% shadow configuration correctly makes this synthetic
                # candidate uneconomic and cannot certify restart admission.
                random.seed(31)
                admitted = invoke("synthetic-after-restart", binding, tools)
                assert_visible(admitted)
                assert money(admitted.cost) == money(
                    "0.0004" if unsafe_action else "0.0002"
                )
                with provider.lock:
                    admitted_cap = provider.posts[-1 if unsafe_action else -2][
                        "max_tokens"
                    ]
                assert (
                    admitted_cap == REFERENCE_CAP
                    if unsafe_action
                    else admitted_cap < REFERENCE_CAP
                )
                binding.assert_complete()
        except CampaignStopped:
            if not expect_stop:
                raise
            stopped = True
        assert stopped == expect_stop
    finally:
        provider.release_probe.set()
        provider.close()

    snapshot = read_snapshot(ledger)
    claims = [event for event in snapshot.events if event["event"] == "reserve"]
    results = [event for event in snapshot.events if event["event"] == "result"]
    failures = [
        event for event in snapshot.events if event["event"] == "attempt_failure"
    ]
    kinds = Counter(claim["metadata"]["kind"] for claim in claims)
    assert len(claims) == len(provider.posts)
    assert snapshot.state["pending_usd"] == 0
    assert all(claim["metadata"]["actor"] == "agent" for claim in claims)
    assert all(
        claim["metadata"].get("native_plan_json")
        for claim in claims
        if claim["metadata"]["kind"] in {"primary", "exploration"}
    )
    probes = [claim for claim in claims if claim["metadata"]["kind"] == "exploration"]
    assert len(probes) == (1 if expect_stop else target_pairs)
    for probe in probes:
        meta = probe["metadata"]
        assert meta["task_id"] == "synthetic-calibration"
        assert meta["parent_observation_token"] and meta["exploration_lease_token"]
        primary = next(
            claim
            for claim in claims
            if claim["metadata"]["turn_id"] == meta["turn_id"]
            and claim["metadata"]["kind"] == "primary"
        )
        assert (
            meta["parent_native_plan_json"] == primary["metadata"]["native_plan_json"]
        )
    assert overlap_verified
    if expect_stop:
        assert len(failures) == 1 and len(results) + 1 == len(claims)
        assert snapshot.state["holds_usd"] > 0
        assert admitted_cap is None
    else:
        assert len(results) == len(claims) and not failures
        assert snapshot.state["holds_usd"] == 0
        assert kinds["shadow"] == (0 if unsafe_action else 1)
        if not unsafe_action:
            shadow = next(
                claim for claim in claims if claim["metadata"]["kind"] == "shadow"
            )
            assert shadow["metadata"]["parent_observation_token"]
            assert shadow["metadata"]["parent_native_plan_json"]
            assert not shadow["metadata"].get("native_plan_json")
            assert not shadow["metadata"].get("observation_token")
    assert not tool_executions, "provider-only probes must never execute a tool"
    if tools is not None:
        expected_tools = [tool.openai_schema for tool in tools]
        assert all(post["tools"] == expected_tools for post in provider.posts)
        assert all(
            post["messages"] == provider.posts[0]["messages"] for post in provider.posts
        )
    assert source_manifest(source) == manifest
    report = {
        "stage": "E0",
        "paper_evidence": False,
        "source": manifest,
        "paid_provider_calls": 0,
        "real_benchmark_trials": 0,
        "original_campaign_journal_accessed": False,
        "synthetic_billing": True,
        "scripted_provider": True,
        "local_http_post_count": len(provider.posts),
        "dispatch_kinds": dict(kinds),
        "reference_visible_calibration_calls": len(calibration),
        "background_pairs": len(probes) if not expect_stop else 0,
        "tool_mode": tool_mode,
        "tool_execution_count": len(tool_executions),
        "unsafe_action_not_admitted": unsafe_action and admitted_cap == REFERENCE_CAP,
        "concurrent_foreground_verified": overlap_verified,
        "admitted_cap_after_restart": admitted_cap,
        "background_failure_stopped_worker": stopped,
        "shadow_rate": 0.02,
        "shadow_sampling_seed": 31,
        "accounting": snapshot.summary(),
    }
    _private_write(output / "summary.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fail-probe", action="store_true")
    parser.add_argument(
        "--tool-mode",
        choices=("match", "different", "malformed-json", "invalid-envelope"),
    )
    args = parser.parse_args(argv)
    os.environ.update(
        PYTHON_DOTENV_DISABLED="1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        TAU2_DATA_DIR=str(args.source.resolve() / "data"),
        AGENTC_OPTIMIZE="1",
        AGENTC_ENABLED_RULES="OutputBudget",
        AGENTC_OPTIMIZE_EXPLORATION="1",
        AGENTC_OPTIMIZE_SHADOW="0.02",
        AGENTC_OPTIMIZE_MAX_OVERHEAD_MS="50",
    )

    def network_guard(event, arguments):
        if event == "socket.connect" and (
            not isinstance(arguments[1], tuple) or arguments[1][0] != "127.0.0.1"
        ):
            raise RuntimeError("E0 fixture forbids external network")

    sys.addaudithook(network_guard)
    print(
        json.dumps(
            run(
                args.source.resolve(),
                args.output.resolve(),
                fail_probe=args.fail_probe,
                tool_mode=args.tool_mode,
            ),
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
