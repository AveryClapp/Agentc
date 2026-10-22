"""All frozen task IDs through the owned scheduler, using local fixture replies.

The default schedules two diagnostic arms twice for every one of the 150 tasks.
Only admitted, already-local images execute; unavailable cells retain null
scores. No remote provider, image download, or efficacy claim is made.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

from bench.campaign_accounting import money, read_snapshot
from bench.campaign_chat import ChatContract
from bench.campaign_transport import CampaignTransport
from bench.coding_submission import private_write
from bench.frozen_policy_preflight import configuration as fixture_configuration
from bench.joint_campaign import (
    PRIMARY_ARMS,
    build_schedule,
    digest_file,
    load_campaign,
    ordered_id_digest,
    run_campaign,
)
from bench.policy_calibration import ARMS, CalibrationContract, FrozenPolicyBundle
from bench.swe_agent_campaign_worker import (
    SweAgentCampaignWorkerSession,
    SweAgentTaskConfig,
)
from bench.swe_agent_preflight import CHEAP, STRONG, Provider
from bench.swe_agent_runtime import RuntimeBundle
from bench.swebench_cohort import AGENT_REVISION, DATASET_SHA256, require
from bench.swebench_environment import load_inputs
from bench.swebench_image_cache import CacheLimits, DockerCapacity, ImmutableImageCache
from bench.tau_campaign_preflight import FIXTURE_KEY


class ScheduledTaskProvider(Provider):
    def __init__(self):
        super().__init__("identity")

    def message(self, body):
        value = super().message(body)
        step = sum(m["role"] == "assistant" for m in body["messages"])
        require(step in (0, 1, 2), "fixture unexpectedly requested an extra agent step")
        name = "bash" if step == 0 else "submit"
        arguments = (
            json.dumps(
                {
                    "command": "printf 'agentc mechanics fixture\\n' > /testbed/agentc_mechanics_fixture.txt"
                }
            )
            if step == 0
            else "{}"
        )
        value["choices"][0]["message"]["tool_calls"][0]["function"].update(
            name=name, arguments=arguments
        )
        return value


def run(
    *,
    dataset,
    agent,
    evaluator,
    task_assets,
    image_pins,
    environment_report,
    runtime_packages,
    bootstrap_image,
    output,
    repetitions=2,
    arms=("trace_only_fixed_strong", "joint_guarded"),
    all_policies=False,
):
    import docker

    require(
        type(repetitions) is int and 2 <= repetitions <= 5,
        "invalid E0 repetition count",
    )
    selected = tuple(PRIMARY_ARMS) if all_policies else tuple(arms)
    require(
        selected and selected == tuple(a for a in PRIMARY_ARMS if a in selected),
        "diagnostic arms require a unique frozen-order subset",
    )
    require(
        all_policies or not set(selected) & set(ARMS),
        "calibrated arms require the explicit E0 policy fixture",
    )
    require(not output.exists(), "campaign preflight requires fresh output")
    inputs = load_inputs(
        dataset=dataset,
        agent=agent,
        evaluator=evaluator,
        task_assets=task_assets,
        image_pins=image_pins,
    )
    bundle = RuntimeBundle.load(runtime_packages)
    output.mkdir(mode=0o700)
    protocol = output / "protocol.md"
    protocol.write_bytes(
        (Path(__file__).parent / "repro/mlsys-2027-evaluation-protocol.md").read_bytes()
    )
    config = SweAgentTaskConfig(
        STRONG, CHEAP, network_policy="loopback_only", max_overhead_ms=50.0
    )
    task_ids = [t["task_id"] for t in inputs.cohort["tasks"]]
    workload = {
        "workload_id": "frozen-swe-verified",
        "family": "coding",
        "unengineered_upstream": True,
        "provenance": {
            "upstream_commit": AGENT_REVISION,
            "task_universe_sha256": DATASET_SHA256,
        },
        "task_ids": task_ids,
        "task_ids_sha256": ordered_id_digest(task_ids),
        "repetitions": repetitions,
        "split": "frozen-cohort-engineering-controls",
        "model_pair": {"strong": STRONG, "cheap": CHEAP},
        "quality_margin": -0.02,
        "network_policy": "loopback_only",
        "expected_spend_usd": 0,
        "worker_command": ["owned:frozen_swe_agent_v1"],
        "timeout_seconds": 1200,
        "worker_configuration": json.loads(json.dumps(asdict(config))),
    }
    sibling = {
        **workload,
        "workload_id": "fixture-sibling",
        "family": "engineering_fixture",
        "unengineered_upstream": False,
        "task_ids": ["fixture-001"],
        "task_ids_sha256": ordered_id_digest(["fixture-001"]),
        "network_policy": "forbidden",
        "worker_command": [sys.executable, "-m", "bench.joint_fixture_worker"],
        "worker_cwd": "{repo}",
        "worker_configuration": {},
        "timeout_seconds": 30,
    }
    spec = {
        "schema_version": 1,
        "campaign_id": "frozen-swe-cohort-scheduler-e0",
        "stage": "E0",
        "paper_evidence": False,
        "expected_spend_usd": 0,
        "protocol": {"path": str(protocol), "sha256": digest_file(protocol)},
        "arms": list(PRIMARY_ARMS),
        "diagnostic_arms": list(selected),
        "bootstrap_resamples": 20,
        "interpretation_limits": [
            "Local fixture replies and charges; no coding efficacy or provider evidence.",
            "All frozen task IDs retained; unavailable environments have null scores.",
            "The sibling workload is an engineering fixture.",
        ],
        "workloads": [workload, sibling],
    }
    policy = None
    if all_policies:
        contract = CalibrationContract(
            workload["workload_id"],
            json.dumps(workload["model_pair"]),
            config.output_cap,
            -0.02,
            "E0",
            "engineering_fixture",
            DATASET_SHA256,
            digest_file(protocol),
        )
        policy = FrozenPolicyBundle.engineering_fixture(
            contract,
            {mode: fixture_configuration(mode) for mode in ARMS},
            task_ids=tuple(task_ids),
        )
        spec["calibration_lock"] = policy.lock
    spec_path = output / "input.json"
    private_write(spec_path, spec)
    frozen = load_campaign(spec_path)
    cells = tuple(
        c for c in build_schedule(frozen) if c.workload_id == workload["workload_id"]
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
    provider = ScheduledTaskProvider().start()
    client = probe_client = None
    capacity = cache = None
    try:
        client = docker.from_env(timeout=60)
        probe_client = docker.from_env(timeout=5)
        capacity = DockerCapacity(probe_client, bootstrap_image, output)
        cache = ImmutableImageCache(
            client=client,
            output=output / "cache",
            limits=CacheLimits(max_pulls=0),
            capacity=capacity,
        )
        transport = CampaignTransport(
            ledger=ledger,
            expected_sha256=read_snapshot(ledger).sha256,
            key=FIXTURE_KEY,
            stage="local-frozen-swe-cohort-scheduler",
            stage_cap_usd="2",
            contracts=[
                ChatContract(
                    m, "openai", "OpenAI", m, 16384, 1024, cached_text_blocks=True
                )
                for m in (STRONG, CHEAP)
            ],
            http=provider.http,
        )
        campaign_output = output / "campaign"
        worker = SweAgentCampaignWorkerSession(
            transport=transport,
            inputs=inputs,
            environment_report=environment_report,
            bundle=bundle,
            output=output / "acquisition",
            campaign_output=campaign_output,
            workload_id=workload["workload_id"],
            cells=cells,
            config=config,
            image_cache=cache,
            policy_bundle=policy,
        )
        manifest = run_campaign(
            spec_path,
            campaign_output,
            worker_sessions={workload["workload_id"]: worker},
        )
        cache.close()
        capacity.close()
        snapshot = read_snapshot(ledger)
        transport.assert_complete()
        raw = [
            json.loads(line)
            for line in (campaign_output / "raw-records.jsonl").read_text().splitlines()
        ]
        tasks = [
            r
            for r in raw
            if r["record_type"] == "task"
            and r["workload_id"] == workload["workload_id"]
        ]
        calls = [
            r
            for r in raw
            if r["record_type"] == "call"
            and r["workload_id"] == workload["workload_id"]
        ]
        claims = [e for e in snapshot.events if e["event"] == "reserve"]
        bills = [e for e in snapshot.events if e["event"] == "result"]
        require(
            len(tasks) == len(cells) == 150 * repetitions * len(selected)
            and len(provider.posts) == len(claims) == len(bills) == len(calls),
            "scheduled cells or owned provider calls were omitted",
        )
        require(
            sum((money(c["cost_usd_exact"]) for c in calls), money("0"))
            == snapshot.state["known_usd"],
            "normalized campaign costs differ from owned bills",
        )
        summary = {
            "schema_version": 1,
            "record_type": "frozen_swe_agent_campaign_preflight",
            "assigned_task_count": 150,
            "scheduled_cohort_cells": len(cells),
            "visited_cohort_cells": len(tasks),
            "repetitions": repetitions,
            "diagnostic_arms": list(selected),
            "unknown_official_score_cells": sum(
                t["official_score"] is None for t in tasks
            ),
            "physical_fixture_calls": len(calls),
            "all_simulated_cost_usd": str(snapshot.state["known_usd"]),
            "campaign_manifest_sha256": digest_file(campaign_output / "manifest.json"),
            "campaign_status": manifest["completeness"]["status"],
            "worker_completion_sha256": digest_file(worker.output / "completion.json"),
            "image_pulls": cache.pull_count,
            "external_provider_calls": 0,
            "paper_evidence": False,
            "synthetic_billing": True,
        }
        private_write(output / "requests.json", provider.posts)
        private_write(output / "summary.json", summary)
        return summary
    finally:
        try:
            if cache is not None:
                cache.close()
        finally:
            try:
                if capacity is not None:
                    capacity.close()
            finally:
                if client is not None:
                    client.close()
                if probe_client is not None:
                    probe_client.close()
                provider.server.shutdown()
                provider.server.server_close()
                provider.thread.join(timeout=10)
                require(
                    not provider.thread.is_alive(),
                    "local fixture provider did not stop",
                )


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
    parser.add_argument("--bootstrap-image", required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--all-policies", action="store_true")
    parser.add_argument(
        "--arms",
        nargs="+",
        choices=PRIMARY_ARMS,
        default=("trace_only_fixed_strong", "joint_guarded"),
    )
    args = vars(parser.parse_args(argv))
    print(json.dumps(run(**args), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
