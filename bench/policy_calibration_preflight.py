"""Real Tau acquisition, fitting and validation with synthetic loopback billing.

E0 training here never supplies provider calibration, safety or held-out evidence.
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
from bench.coding_submission import private_write
from bench.joint_campaign import CampaignError
from bench.policy_calibration import (
    ARMS,
    CalibrationContract,
    CalibrationError,
    CalibrationTrialBundle,
    FrozenPolicyBundle,
    fit_policies,
    seal_policies,
)
from bench.policy_calibration_acquisition import acquire_trials
from bench.tau_campaign_binding import REVISION
from bench.tau_campaign_preflight import FIXTURE_KEY, source_manifest
from bench.tau_campaign_worker import TauCampaignWorkerSession
from bench.tau_judge import JudgeCoverageError
from bench.tau_task_preflight import TaskProvider
from bench.tau_task_worker import JUDGE_MODEL, TauCell, TauTaskConfig, TauTaskWorker


def training_registry(models):
    def configuration(target, cap):
        return {
            "strong_model": models["strong"],
            "cheap_model": models["cheap"],
            "divergence_threshold": 0.05,
            "static_plan": {
                "target_model": target,
                "rewrites": [{"rule": "OutputBudget", "max_output_tokens": cap}],
            },
            "router": None,
            "rewrite_by_target": {},
        }

    return {
        "reference": {"role": "reference", "mode": "reference"},
        "cheap-original": {"role": "cheap_original", "mode": "original"},
        **{
            name: {
                "role": role,
                "mode": "best_static_joint",
                "configuration": configuration(models[target], cap),
            }
            for name, role, target, cap in (
                ("static-cheap", "static_candidate", "cheap", 128),
                ("static-strong", "static_candidate", "strong", 256),
                ("rewrite-strong", "fixed_recipe", "strong", 384),
                ("rewrite-cheap", "fixed_recipe", "cheap", 128),
            )
        },
    }


def run(source: Path, output: Path, *, control="success"):
    from loguru import logger

    logger.remove()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    manifest = source_manifest(source)
    models = {
        "strong": "openai/gpt-4o-2024-11-20",
        "cheap": "openai/gpt-4o-mini-2024-07-18",
    }
    protocol = Path(__file__).parent / "repro/mlsys-2027-evaluation-protocol.md"
    contract = CalibrationContract(
        "tau-calibration",
        json.dumps(models),
        1024,
        -0.03,
        "E0",
        "engineering_fixture",
        manifest["files"]["data/tau2/domains/retail/tasks.json"],
        hashlib.sha256(protocol.read_bytes()).hexdigest(),
        task_partitions_json=json.dumps(
            {"training": ["0"], "validation": ["2"], "held_out": ["3"]}
        ),
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
            stage="local-policy-calibration",
            stage_cap_usd="0.1305" if control == "stage-cap" else "2",
            contracts=[
                ChatContract(model, "openai", "OpenAI", model, 16384, 1024)
                for model in {*models.values(), JUDGE_MODEL}
            ],
            http=provider.http,
        )

        def collect(bundle, name):
            root = output / name
            root.mkdir(mode=0o700)
            cells = tuple(
                TauCell(
                    c["task_id"], c["arm"], c["repetition"], c["seed"], c["ordinal"]
                )
                for c in bundle.cells
            )
            config = TauTaskConfig(
                task_ids=tuple(
                    contract.partitions[bundle.lock["selections"]["partition"]]
                ),
                task_inventory_sha256=contract.task_universe_sha256,
                strong_model=models["strong"],
                cheap_model=models["cheap"],
                user_model=models["cheap"],
                max_steps=10,
                task_timeout_seconds=120,
                network_policy="loopback_only",
                runtime_overrides=(("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS", "50"),),
            )
            worker = TauTaskWorker(
                transport=transport,
                source=source,
                output=root / "acquisition",
                config=config,
                cells=cells,
                storage_root=root / "state" / contract.workload_id,
                policy_bundle=bundle,
            )
            return acquire_trials(
                bundle,
                TauCampaignWorkerSession(worker, workload_id=contract.workload_id),
                transport=transport,
                output=root / "collection",
                family="retail_tool_use",
                upstream_commit=REVISION,
                timeout_seconds=120,
            )

        registry = training_registry(models)
        stopped = False
        try:
            training = collect(
                CalibrationTrialBundle.create(
                    contract, registry, partition="training", repetitions=1
                ),
                "training",
            )
            fit = fit_policies(contract, registry, training)
            private_write(output / "fit.json", fit)
            validation_registry = {
                "reference": {"role": "reference", "mode": "reference"},
                **{
                    mode: {
                        "role": "validation_policy",
                        "mode": mode,
                        "configuration": fit["configurations"][mode],
                    }
                    for mode in ARMS
                },
            }
            validation = collect(
                CalibrationTrialBundle.create(
                    contract,
                    validation_registry,
                    partition="validation",
                    fitted=fit,
                    repetitions=1,
                ),
                "validation",
            )
            lock = seal_policies(
                contract, fit, validation_registry, validation, held_out_task_ids=["3"]
            )
            FrozenPolicyBundle.from_lock(lock)
            private_write(output / "policy-lock.json", lock)
        except (CampaignStopped, CalibrationError, CampaignError, JudgeCoverageError):
            stopped = True
            if control == "success":
                raise
        snapshot = read_snapshot(ledger)
        assert stopped == (control != "success")
        assert snapshot.state["pending_usd"] == 0
        assert snapshot.state["known_usd"] == len(provider.posts) * money("0.0004")
        assert sum(e["event"] == "campaign_session" for e in snapshot.events) == 1
        assert (output / "policy-lock.json").exists() is (not stopped)
        report = {
            "schema_version": 1,
            "purpose": "receipt-backed calibration acquisition engineering control",
            "source_stage": "E0",
            "evidence_kind": "engineering_fixture",
            "paper_evidence": False,
            "control": control,
            "stopped": stopped,
            "paid_provider_calls": 0,
            "local_http_post_count": len(provider.posts),
            "known_fixture_charge_usd": str(snapshot.state["known_usd"]),
            "pending_usd": "0",
            "transport_session_id": transport.session_id,
            "journal_sha256": snapshot.sha256,
            "provider_calibration_completed": False,
            "source_closure_sha256": manifest["closure_sha256"],
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
        choices=("success", "stage-cap", "invalid-judge"),
        default="success",
    )
    args = parser.parse_args(argv)
    os.environ.update(PYTHON_DOTENV_DISABLED="1", LITELLM_LOCAL_MODEL_COST_MAP="True")

    def network_guard(event, arguments):
        if event == "socket.connect" and (
            not isinstance(arguments[1], tuple) or arguments[1][0] != "127.0.0.1"
        ):
            raise RuntimeError("calibration E0 forbids external network")

    sys.addaudithook(network_guard)
    print(
        json.dumps(
            run(args.source.resolve(), args.output.resolve(), control=args.control)
        )
    )


if __name__ == "__main__":
    main()
