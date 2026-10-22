"""Run frozen SWE-agent tools and official grading with local fixture replies.

The fixture adds an unrelated text file and submits it through the unchanged
review-on-submit tool. Its expected unresolved result establishes mechanics,
not coding accuracy or optimization efficacy. No provider is contacted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from bench.campaign_accounting import digest, money, read_snapshot
from bench.campaign_chat import ChatContract
from bench.campaign_transport import CampaignTransport
from bench.coding_submission import private_write
from bench.swe_agent_binding import SweAgentCampaignBinding
from bench.swe_agent_grading import grade_task
from bench.swe_agent_preflight import STRONG, Provider
from bench.swe_agent_runtime import RuntimeBundle
from bench.swe_agent_task_worker import execution_version, run_task
from bench.swebench_environment import load_inputs
from bench.tau_campaign_preflight import FIXTURE_KEY


class TaskProvider(Provider):
    def __init__(self):
        super().__init__("identity")

    def message(self, body):
        answer = super().message(body)
        with self.lock:
            count = len(self.posts)
        if count == 1:
            name = "bash"
            arguments = json.dumps(
                {
                    "command": "printf 'agentc mechanics fixture\\n' > /testbed/agentc_mechanics_fixture.txt"
                }
            )
        elif count in (2, 3):
            name, arguments = "submit", "{}"
        else:
            raise ValueError("fixture unexpectedly requested an extra provider reply")
        function = answer["choices"][0]["message"]["tool_calls"][0]["function"]
        function.update(name=name, arguments=arguments)
        return answer


def run(
    *,
    dataset,
    agent,
    evaluator,
    task_assets,
    image_pins,
    environment_report,
    runtime_packages,
    output,
    task_id="psf__requests-1766",
):
    import agentc

    if output.exists():
        raise ValueError("preflight output must be fresh")
    inputs = load_inputs(
        dataset=dataset,
        agent=agent,
        evaluator=evaluator,
        task_assets=task_assets,
        image_pins=image_pins,
    )
    bundle = RuntimeBundle.load(runtime_packages)
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
    provider = TaskProvider().start()
    previous = {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
    for key in previous:
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
    try:
        transport = CampaignTransport(
            ledger=ledger,
            expected_sha256=read_snapshot(ledger).sha256,
            key=FIXTURE_KEY,
            stage="local-frozen-swe-agent-task",
            stage_cap_usd="2",
            contracts=[
                ChatContract(
                    STRONG,
                    "openai",
                    "OpenAI",
                    STRONG,
                    16384,
                    1024,
                    cached_text_blocks=True,
                )
            ],
            http=provider.http,
        )
        cell = digest({"fixture_task": task_id, "arm": "joint_guarded", "seed": 41})
        with SweAgentCampaignBinding(
            transport=transport,
            source=agent,
            models=frozenset({STRONG}),
            output_cap=1024,
            cohort_sha256=digest(inputs.cohort),
            execution_version=execution_version(bundle),
        ) as binding:
            agentc.init(storage_path=str(output / "runtime"), capture_content=False)
            submission = run_task(
                inputs=inputs,
                environment_report=environment_report,
                task_id=task_id,
                binding=binding,
                bundle=bundle,
                output=output / "task",
                requested_model=STRONG,
                arm="joint_guarded",
                cell_id=cell,
                optimize=True,
                run_seed=41,
            )
            binding.assert_complete()
            private_write(output / "plans.json", binding.plans)
        assert len(provider.posts) == 3 and len(binding.plans) == 3
        assert (
            submission["task_status"] == "completed"
            and submission["upstream_exit_status"] == "submitted"
        )
        assert "agentc_mechanics_fixture.txt" in submission["model_patch"]
        grade = grade_task(
            inputs=inputs,
            environment_report=environment_report,
            bundle=bundle,
            ledger=ledger,
            submission_path=output / "task/submission.json",
            output=output / "grading",
        )
        assert grade["reference_tests_complete"] and grade["preservation_passed"]
        assert grade["official_score"] == grade["protocol_score"] == 0
        accounting = transport.assert_complete()
        assert money(accounting["known_cost_usd"]) == money(
            grade["all_provider_cost_usd"]
        )
        private_write(output / "requests.json", provider.posts)
        report = {
            "schema_version": 1,
            "task_id": task_id,
            "full_upstream_agent_loop_executed": True,
            "unchanged_default_tools": True,
            "logical_queries": 3,
            "physical_fixture_calls": len(provider.posts),
            "actual_native_plans": len(binding.plans),
            "official_score": grade["official_score"],
            "all_fixture_cost_usd": grade["all_provider_cost_usd"],
            "runtime_bundle_sha256": bundle.sha256,
            "submission_sha256": digest(submission),
            "grade_sha256": digest(grade),
            "external_provider_calls": 0,
            "synthetic_billing": True,
            "paper_evidence": False,
        }
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
                os.environ.update(previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "dataset",
        "agent",
        "evaluator",
        "task-assets",
        "image-pins",
        "environment-report",
        "runtime-packages",
        "output",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--task-id", default="psf__requests-1766")
    args = parser.parse_args(argv)
    values = vars(args)
    values = {k: v.resolve() if isinstance(v, Path) else v for k, v in values.items()}
    print(json.dumps(run(**values)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
