"""Owned-session scheduler integration on official retail tasks, with local bills.

The second workload is the existing no-network engineering fixture. Seven arms
are the default diagnostic subset; all ten can run with declared E0 policies.
Neither control can become primary campaign or benchmark efficacy evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from bench.campaign_accounting import money, read_snapshot
from bench.campaign_chat import ChatContract
from bench.campaign_transport import CampaignStopped, CampaignTransport
from bench.joint_campaign import (
    PRIMARY_ARMS,
    build_schedule,
    digest_file,
    load_campaign,
    ordered_id_digest,
    run_campaign,
)
from bench.tau_campaign_binding import REVISION
from bench.tau_campaign_preflight import FIXTURE_KEY, source_manifest
from bench.tau_campaign_worker import TauCampaignWorkerSession
from bench.tau_judge import JudgeCoverageError
from bench.tau_task_preflight import TaskProvider
from bench.tau_task_worker import (
    JUDGE_MODEL,
    SUPPORTED_ARMS,
    TauCell,
    TauTaskConfig,
    TauTaskWorker,
    private_write,
)
from bench.policy_calibration import CalibrationContract, FrozenPolicyBundle
from bench.frozen_policy_preflight import configuration as fixture_configuration


def run(source: Path, output: Path, *, control="success", all_policies=False) -> dict:
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
        task_timeout_seconds=120,
        network_policy="loopback_only",
        runtime_overrides=(("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS", "50"),),
    )
    protocol = output / "protocol.md"
    protocol.write_bytes(
        (
            Path(__file__).resolve().parent / "repro/mlsys-2027-evaluation-protocol.md"
        ).read_bytes()
    )
    workload = {
        "workload_id": "tau-retail",
        "family": "retail_tool_use",
        "unengineered_upstream": True,
        "provenance": {"upstream_commit": REVISION, "task_universe_sha256": inventory},
        "task_ids": list(config.task_ids),
        "task_ids_sha256": ordered_id_digest(config.task_ids),
        "repetitions": 2,
        "model_pair": {"strong": config.strong_model, "cheap": config.cheap_model},
        "quality_margin": -0.03,
        "network_policy": "loopback_only",
        "expected_spend_usd": 0,
        "worker_command": ["owned:tau_retail_v1"],
        "timeout_seconds": 120,
    }
    fixture = {
        **workload,
        "workload_id": "fixture-sibling",
        "family": "engineering_fixture",
        "unengineered_upstream": False,
        "network_policy": "forbidden",
        "task_ids": ["fixture-001"],
        "task_ids_sha256": ordered_id_digest(["fixture-001"]),
        "worker_command": ["{python}", "-m", "bench.joint_fixture_worker"],
        "worker_cwd": "{repo}",
    }
    spec = {
        "schema_version": 1,
        "campaign_id": "owned-tau-scheduler-e0",
        "stage": "E0",
        "paper_evidence": False,
        "expected_spend_usd": 0,
        "protocol": {"path": "protocol.md", "sha256": digest_file(protocol)},
        "arms": list(PRIMARY_ARMS),
        "diagnostic_arms": list(PRIMARY_ARMS if all_policies else SUPPORTED_ARMS),
        "bootstrap_resamples": 20,
        "workloads": [workload, fixture],
    }
    bundle = None
    if all_policies:
        contract = CalibrationContract(
            workload["workload_id"],
            json.dumps(workload["model_pair"]),
            1024,
            -0.03,
            "E0",
            "engineering_fixture",
            inventory,
            digest_file(protocol),
        )
        bundle = FrozenPolicyBundle.engineering_fixture(
            contract,
            {
                mode: fixture_configuration(mode)
                for mode in (
                    "best_static_joint",
                    "route_then_rewrite",
                    "rewrite_then_route",
                )
            },
            task_ids=config.task_ids,
        )
        spec["calibration_lock"] = bundle.lock
    spec_path = output / "input.json"
    private_write(spec_path, spec)
    schedule = build_schedule(load_campaign(spec_path))
    cells = tuple(
        TauCell(run.task_id, run.arm, run.repetition, run.run_seed, run.ordinal)
        for run in schedule
        if run.workload_id == workload["workload_id"]
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
    try:
        transport = CampaignTransport(
            ledger=ledger,
            expected_sha256=read_snapshot(ledger).sha256,
            key=FIXTURE_KEY,
            stage="local-owned-tau-scheduler",
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
        campaign = output / "campaign"
        worker = TauTaskWorker(
            transport=transport,
            source=source,
            output=output / "acquisition",
            config=config,
            cells=cells,
            storage_root=campaign / "state" / workload["workload_id"],
            policy_bundle=bundle,
        )
        session = TauCampaignWorkerSession(worker, workload_id=workload["workload_id"])
        stopped = False
        try:
            completed = run_campaign(
                spec_path, campaign, worker_sessions={workload["workload_id"]: session}
            )
        except (CampaignStopped, JudgeCoverageError):
            stopped = True
            if control == "success":
                raise
        snapshot = read_snapshot(ledger)
        assert (
            len([e for e in snapshot.events if e["event"] == "campaign_session"]) == 1
        )
        assert snapshot.state["pending_usd"] == 0
        assert snapshot.state["known_usd"] == len(provider.posts) * money("0.0004")
        assert stopped == (control != "success")
        assert (campaign / "manifest.json").exists() == (not stopped)
        assert (worker.output / "completion.json").exists() == (not stopped)
        if not stopped:
            assert completed["completeness"]["status"] == "diagnostic_complete"
            assert completed["spend"]["actual_spend_usd"] == 0
            assert completed["spend"]["actual_spend_basis"] == (
                "mixed_network_policy_per_workload_accounting"
            )
            assert completed["completeness"]["scheduled_runs"] == (
                60 if all_policies else 42
            )
            if not all_policies:
                assert len(provider.posts) == 98
            assert (
                completed["worker_execution_modes"][workload["workload_id"]]
                == "owned_session"
            )
            rows = [
                json.loads(line)
                for line in (campaign / "raw-records.jsonl").read_text().splitlines()
            ]
            tau_calls = [
                r
                for r in rows
                if r["workload_id"] == workload["workload_id"]
                and r["record_type"] == "call"
            ]
            assert len(tau_calls) == len(provider.posts) and {
                r["transport_session_id"] for r in tau_calls
            } == {transport.session_id}
            analysis = json.loads((campaign / "analysis.json").read_bytes())
            assert analysis["analysis_contract"]["competitive_comparisons"] is False
            assert all(
                arm["safety_unknown_tasks"] == 4
                for arm in analysis["workloads"][workload["workload_id"]][
                    "arms"
                ].values()
            )
        report = {
            "control": control,
            "stage": "E0",
            "paper_evidence": False,
            "original_campaign_journal_accessed": False,
            "paid_provider_calls": 0,
            "local_http_post_count": len(provider.posts),
            "scheduled_runs": len(schedule),
            "stopped": stopped,
            "synthetic_billing": True,
            "accounting": snapshot.summary(),
            "source_closure_sha256": manifest["closure_sha256"],
        }
        private_write(output / "summary.json", report)
        return report
    finally:
        provider.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--all-policies", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--control",
        choices=("success", "invalid-judge", "stage-cap"),
        default="success",
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
            raise RuntimeError("owned E0 preflight forbids external network")

    sys.addaudithook(network_guard)
    report = run(
        args.source.resolve(),
        args.output.resolve(),
        control=args.control,
        all_policies=args.all_policies,
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("control", "local_http_post_count", "stopped")
            }
        )
    )


if __name__ == "__main__":
    main()
