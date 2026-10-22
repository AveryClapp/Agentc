"""Collect registered calibration trials through one already owned session.

This produces training or validation records, never a held-out policy lock.
The caller supplies an enrolled transport; this module does not authorize spend.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from bench.campaign_accounting import canonical, digest, money, read_snapshot
from bench.campaign_transport import CampaignStopped
from bench.coding_submission import private_write
from bench.joint_campaign import (
    ScheduledRun,
    _owned_worker_context,
    _request_payload,
    validate_worker_records,
)
from bench.policy_calibration import CalibrationTrialBundle, _require, _rollouts


def acquire_trials(
    bundle: CalibrationTrialBundle,
    session,
    *,
    transport,
    output: Path,
    family: str,
    upstream_commit: str,
    timeout_seconds: int,
    expected_cell_usd=0,
):
    """Acquire the full paired matrix or retain an explicitly incomplete prefix."""
    _require(
        isinstance(bundle, CalibrationTrialBundle),
        "acquisition requires an unsealed trial authorization",
    )
    _require(
        type(timeout_seconds) is int and timeout_seconds > 0,
        "invalid calibration worker deadline",
    )
    expected = money(expected_cell_usd)
    contract = bundle.contract
    _require(
        contract.stage == "E0" or expected > 0,
        "provider calibration requires a frozen cell spend forecast",
    )
    worker = getattr(session, "worker", session)
    _require(
        worker.transport is transport and worker.policy_bundle.lock == bundle.lock,
        "calibration worker differs from the owned transport or authorization",
    )
    bundle.validate_cells(worker.cells)
    _require(
        isinstance(family, str)
        and family
        and isinstance(upstream_commit, str)
        and len(upstream_commit) == 40
        and set(upstream_commit) <= set("0123456789abcdef"),
        "invalid calibration workload provenance",
    )
    output = output.resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    before = read_snapshot(transport.ledger)
    value = bundle.lock["selections"]
    workload = {
        "workload_id": contract.workload_id,
        "family": family,
        "split": value["partition"],
        "model_pair": contract.models,
        "quality_margin": contract.quality_margin,
        "provenance": {
            "upstream_commit": upstream_commit,
            "task_universe_sha256": contract.task_universe_sha256,
        },
        "network_policy": "loopback_only"
        if contract.stage == "E0"
        else "provider_allowed",
        "expected_spend_usd": float(expected),
        "timeout_seconds": timeout_seconds,
        "worker_configuration": json.loads(json.dumps(asdict(worker.config))),
    }
    spec = {
        "campaign_id": "policy-calibration-" + bundle.lock["sha256"],
        "stage": contract.stage,
        "paper_evidence": contract.stage == "C",
        "protocol": {"sha256": contract.protocol_sha256},
        "calibration_lock": bundle.lock,
    }
    enrollment = {
        "schema_version": 1,
        "record_type": "calibration_acquisition_enrollment",
        "authorization": bundle.lock,
        "workload": workload,
        "transport_session_id": transport.session_id,
        "transport_stage": transport.stage,
        "journal_before_sha256": before.sha256,
        "stage_cap_usd": str(transport.cap),
        "provider_response_cache": False,
        "resume_allowed": False,
    }
    private_write(output / "enrollment.json", enrollment)
    rows = []
    try:
        with (output / "rollouts.jsonl").open("xb") as stream:
            import os

            os.chmod(stream.fileno(), 0o600)
            with _owned_worker_context(session) as owned:
                for cell in bundle.cells:
                    run = ScheduledRun(
                        contract.workload_id,
                        cell["task_id"],
                        cell["arm"],
                        cell["repetition"],
                        cell["seed"],
                        cell["ordinal"],
                    )
                    request = _request_payload(
                        spec,
                        workload,
                        run,
                        output,
                        repo_root=Path.cwd(),
                        spec_dir=output,
                    )
                    request["storage_path"] = str(
                        worker.storage_for_cell(worker.cells[cell["ordinal"]])
                    )
                    request.update(bundle.receipt_metadata(cell["ordinal"]))
                    started = time.perf_counter()
                    records = validate_worker_records(list(owned.run(request)), request)
                    elapsed = time.perf_counter() - started
                    if elapsed > timeout_seconds:
                        raise CampaignStopped(
                            "calibration cell exceeded its frozen deadline"
                        )
                    task = next(r for r in records if r["record_type"] == "task")
                    if contract.stage == "C" and money(
                        task["all_call_cost_usd"]
                    ) > expected * Decimal("1.25"):
                        raise CampaignStopped(
                            "calibration cell exceeded its frozen spend forecast"
                        )
                    calls = owned.call_receipts(task["cell_id"])
                    task = (
                        task
                        | bundle.receipt_metadata(cell["ordinal"])
                        | {"calibration_registry_sha256": digest(value["registry"])}
                    )
                    row = {"trial_id": cell["trial_id"], "task": task, "calls": calls}
                    rows.append(row)
                    stream.write(canonical(row) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            _rollouts(contract, value["registry"], rows, partition=value["partition"])
        after = read_snapshot(transport.ledger)
        bills = [
            e
            for e in after.events[len(before.events) :]
            if e["event"] == "result" and e.get("session_id") == transport.session_id
        ]
        calls = [call for row in rows for call in row["calls"]]
        _require(
            len(bills) == len(calls)
            and all(
                b.get("metadata", {}).get("calibration_authorization_sha256")
                == bundle.lock["sha256"]
                for b in bills
            ),
            "calibration omitted or mixed owned physical billing",
        )
        _require(
            after.state["known_usd"] - before.state["known_usd"]
            == sum(
                (money(row["task"]["all_call_cost_usd"]) for row in rows), Decimal(0)
            ),
            "calibration acquisition cost differs from the cumulative journal",
        )
        _require(
            after.state["pending_usd"] == 0, "calibration has unsettled financial calls"
        )
        private_write(output / "rollouts.json", rows)
        completion = {
            "schema_version": 1,
            "record_type": "calibration_acquisition_completion",
            "authorization_sha256": bundle.lock["sha256"],
            "partition": value["partition"],
            "source_stage": contract.stage,
            "evidence_kind": contract.evidence_kind,
            "synthetic_billing": contract.stage == "E0",
            "paper_evidence": contract.stage == "C",
            "sealed": False,
            "task_records": len(rows),
            "physical_calls": len(calls),
            "records_sha256": digest(rows),
            "journal_after_sha256": after.sha256,
            "acquisition_cost_usd": str(
                after.state["known_usd"] - before.state["known_usd"]
            ),
        }
        private_write(output / "completion.json", completion)
        return rows
    except BaseException as error:
        private_write(
            output / "failure.json",
            {
                "record_type": "calibration_acquisition_failure",
                "authorization_sha256": bundle.lock["sha256"],
                "completed_prefix_records": len(rows),
                "error_type": type(error).__name__,
                "sealed": False,
                "resume_allowed": False,
            },
        )
        raise
