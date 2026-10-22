"""Actual Requests coding and official grading through the owned E0 scheduler.

One local provider, journal and binding cover seven arms by default, or all ten
with declared E0 policies. The second workload is the engineering fixture. Synthetic responses and
charges cannot supply held-out quality, savings, latency or provider evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from bench.campaign_accounting import money, read_snapshot
from bench.campaign_chat import ChatContract
from bench.campaign_transport import CampaignStopped, CampaignTransport
from bench.coding_campaign_preflight import CHEAP, STRONG, Provider
from bench.coding_campaign_worker import (
    SUPPORTED_ARMS,
    CodingCampaignWorkerSession,
    CodingTaskConfig,
)
from bench.coding_submission import MINI_REVISION, private_write
from bench.joint_campaign import (
    PRIMARY_ARMS,
    build_schedule,
    digest_file,
    load_campaign,
    ordered_id_digest,
    run_campaign,
)
from bench.requests_fixture_preflight import DATASET_SHA256, TASK
from bench.tau_campaign_preflight import FIXTURE_KEY
from bench.policy_calibration import ARMS, CalibrationContract, FrozenPolicyBundle
from bench.frozen_policy_preflight import configuration as fixture_configuration


def run(
    *,
    source: Path,
    dataset: Path,
    pytest_wheels: Path,
    public_task: Path,
    output: Path,
    control: str,
    all_policies: bool = False,
):
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    protocol = output / "protocol.md"
    protocol.write_bytes(
        (Path(__file__).parent / "repro/mlsys-2027-evaluation-protocol.md").read_bytes()
    )
    config = CodingTaskConfig(
        STRONG,
        CHEAP,
        step_limit=3,
        network_policy="loopback_only",
        max_overhead_ms=50.0,
    )
    workload = {
        "workload_id": "mini-swe-requests",
        "family": "coding",
        "unengineered_upstream": True,
        "provenance": {
            "upstream_commit": MINI_REVISION,
            "task_universe_sha256": DATASET_SHA256,
        },
        "task_ids": [TASK],
        "task_ids_sha256": ordered_id_digest([TASK]),
        "repetitions": 2,
        "split": "engineering-control",
        "model_pair": {"strong": STRONG, "cheap": CHEAP},
        "quality_margin": -0.02,
        "network_policy": "loopback_only",
        "expected_spend_usd": 0,
        "worker_command": ["owned:mini_swe_requests_v1"],
        "timeout_seconds": 1200,
        "worker_configuration": asdict(config),
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
        "worker_configuration": {},
        "timeout_seconds": 30,
    }
    spec_path = output / "input.json"
    spec = {
        "schema_version": 1,
        "campaign_id": "owned-coding-scheduler-e0",
        "stage": "E0",
        "paper_evidence": False,
        "expected_spend_usd": 0,
        "protocol": {"path": "protocol.md", "sha256": digest_file(protocol)},
        "arms": list(PRIMARY_ARMS),
        "diagnostic_arms": [
            arm for arm in PRIMARY_ARMS if arm in SUPPORTED_ARMS or all_policies
        ],
        "bootstrap_resamples": 20,
        "workloads": [workload, fixture],
    }
    bundle = None
    if all_policies:
        contract = CalibrationContract(
            workload["workload_id"],
            json.dumps(workload["model_pair"]),
            1024,
            -0.02,
            "E0",
            "engineering_fixture",
            DATASET_SHA256,
            digest_file(protocol),
        )
        bundle = FrozenPolicyBundle.engineering_fixture(
            contract,
            {mode: fixture_configuration(mode) for mode in ARMS},
            task_ids=(TASK,),
        )
        spec["calibration_lock"] = bundle.lock
    private_write(spec_path, spec)
    cells = tuple(
        c
        for c in build_schedule(load_campaign(spec_path))
        if c.workload_id == workload["workload_id"]
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

    class ScheduledProvider(Provider):
        def message(self, body):
            value = super().message(body)
            if control == "failed-response":
                value["choices"][0]["finish_reason"] = "length"
            return value

    provider = ScheduledProvider("matching").start()
    campaign = output / "campaign"
    try:
        transport = CampaignTransport(
            ledger=ledger,
            expected_sha256=read_snapshot(ledger).sha256,
            key=FIXTURE_KEY,
            stage="local-owned-coding-scheduler",
            stage_cap_usd="0.1305" if control == "stage-cap" else "2",
            contracts=[
                ChatContract(m, "openai", "OpenAI", m, 16384, 1024)
                for m in (STRONG, CHEAP)
            ],
            http=provider.http,
        )
        worker = CodingCampaignWorkerSession(
            transport=transport,
            source=source,
            dataset=dataset,
            pytest_wheels=pytest_wheels,
            output=output / "acquisition",
            campaign_output=campaign,
            workload_id=workload["workload_id"],
            public_task=json.loads(public_task.read_bytes()),
            cells=cells,
            config=config,
            policy_bundle=bundle,
        )
        stopped, completed = False, None
        try:
            completed = run_campaign(
                spec_path,
                campaign,
                worker_sessions={workload["workload_id"]: worker},
            )
        except CampaignStopped:
            stopped = True
            if control == "success":
                raise
        snapshot = read_snapshot(ledger)
        claims = [e for e in snapshot.events if e["event"] == "reserve"]
        results = [e for e in snapshot.events if e["event"] == "result"]
        assert len(claims) == len(results) == len(provider.posts)
        assert sum(e["event"] == "campaign_session" for e in snapshot.events) == 1
        assert snapshot.state["pending_usd"] == 0
        assert snapshot.state["known_usd"] == sum(
            (money(r["cost_usd"]) for r in results), money("0")
        )
        assert stopped == (control != "success")
        assert (campaign / "manifest.json").exists() == (not stopped)
        assert (worker.output / "completion.json").exists() == (not stopped)
        if completed is not None:
            assert completed["completeness"]["status"] == "diagnostic_complete"
            assert completed["completeness"]["scheduled_runs"] == (
                40 if all_policies else 28
            )
            assert (
                completed["worker_execution_modes"][workload["workload_id"]]
                == "owned_session"
            )
            rows = [
                json.loads(line)
                for line in (campaign / "raw-records.jsonl").read_text().splitlines()
            ]
            calls = [
                r
                for r in rows
                if r["record_type"] == "call"
                and r["workload_id"] == workload["workload_id"]
            ]
            tasks = [
                r
                for r in rows
                if r["record_type"] == "task"
                and r["workload_id"] == workload["workload_id"]
            ]
            assert len(calls) == len(provider.posts) and len(tasks) == len(cells) == (
                20 if all_policies else 14
            )
            assert (
                sum((money(c["cost_usd_exact"]) for c in calls), money("0"))
                == snapshot.state["known_usd"]
            )
            assert all(
                t["official_score"] == 0 and t["safety_failure"] is None for t in tasks
            )
            assert all(c["retry_count"] == 0 for c in calls)
            assert not completed.get("decisions")
        report = {
            "schema_version": 1,
            "control": control,
            "stopped": stopped,
            "paper_evidence": False,
            "synthetic_billing": True,
            "purpose": "owned scheduling and official grading engineering control",
            "local_http_post_count": len(provider.posts),
            "known_fixture_charge_usd": str(snapshot.state["known_usd"]),
            "pending_usd": str(snapshot.state["pending_usd"]),
            "journal_sha256": snapshot.sha256,
            "original_paid_pilot_reused": False,
            "source_revision": MINI_REVISION,
        }
        private_write(output / "summary.json", report)
        return report
    finally:
        provider.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "dataset", "pytest-wheels", "public-task", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--control",
        choices=("success", "stage-cap", "failed-response"),
        default="success",
    )
    parser.add_argument("--all-policies", action="store_true")
    args = parser.parse_args(argv)
    os.environ.update(PYTHON_DOTENV_DISABLED="1", LITELLM_LOCAL_MODEL_COST_MAP="True")

    def network_guard(event, arguments):
        if event == "socket.connect" and (
            not isinstance(arguments[1], tuple) or arguments[1][0] != "127.0.0.1"
        ):
            raise RuntimeError("coding E0 schedule forbids external network")

    sys.addaudithook(network_guard)
    print(
        json.dumps(
            run(
                **{
                    k: v.resolve() if isinstance(v, Path) else v
                    for k, v in vars(args).items()
                }
            )
        )
    )


if __name__ == "__main__":
    main()
