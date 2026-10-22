"""Measured Tau acquisition projected into the joint campaign record contract.

Native identities are copied from real plan envelopes. Uninstrumented requests
use an explicitly separate fixed-dispatch policy identity, never a manufactured
native observation handle. Optional provider details and missing safety sensors
remain null; a paper-stage worker cannot publish unknown safety as success.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from bench.campaign_accounting import digest, money
from bench.campaign_records import normalize_owned_calls
from bench.campaign_transport import CampaignStopped
from bench.joint_campaign import CampaignError, validate_worker_records
from bench.tau_task_worker import OPTIMIZED_ARMS, TauTaskWorker


def normalize_tau_cell(result: dict, calls: list[dict], request: dict) -> list[dict]:
    """Preserve one scored official cell and every physical billed invocation."""
    expected = {
        "task_id": request["task_id"],
        "arm": request["arm"],
        "repetition": request["repetition"],
        "seed": request["run_seed"],
        "ordinal": request["schedule_ordinal"],
    }
    if (
        result.get("record_type") != "tau_task_result"
        or any(result.get(key) != value for key, value in expected.items())
        or result.get("cell_id") != digest(expected)
        or result.get("calls_sha256") != digest(calls)
        or result.get("provider_call_count") != len(calls)
        or result.get("official_task") is not True
        or result.get("upstream_source_modified") is not False
    ):
        raise CampaignStopped("Tau acquisition does not match its scheduled cell")
    if request["network_policy"] == "loopback_only":
        if (
            result.get("synthetic_billing") is not True
            or request["stage"] != "E0"
            or request["paper_evidence"] is not False
        ):
            raise CampaignStopped(
                "loopback acquisition cannot become provider evidence"
            )
    elif (
        request["network_policy"] != "provider_allowed"
        or result.get("provider_accounting") is not True
        or result.get("synthetic_billing") is not False
    ):
        raise CampaignStopped("Tau acquisition lacks conforming provider accounting")
    if money(result["all_call_cost_usd"]) != sum(
        (money(call["billed_result"]["cost_usd"]) for call in calls), Decimal(0)
    ):
        raise CampaignStopped("Tau acquisition omits or duplicates billed charges")
    records = [
        {
            "schema_version": 1,
            "record_type": "task",
            "task_status": result["task_status"],
            "official_score": result["official_score"],
            "end_to_end_latency_ms": result["end_to_end_latency_ms"],
            "latency_scope": result["latency_scope"],
            "termination_reason": result["termination_reason"],
            "reward_info": result["reward_info"],
            "safety_failure": result["safety_failure"],
            "safety_report_sha256": result.get("safety_report_sha256"),
            "metric_availability": {
                "safety_failure": result["safety_metric_available"]
            },
            "unavailable_metric_reasons": {}
            if result["safety_metric_available"]
            else {
                "safety_failure": result.get("safety_unknown_reason")
                or "no_policy_safety_sensor"
            },
            "network_calls": 0,
            "cell_id": result["cell_id"],
            "calls_sha256": result["calls_sha256"],
            "provider_call_count": result["provider_call_count"],
            "all_call_cost_usd": result["all_call_cost_usd"],
            "acquisition_enrollment_sha256": result["enrollment_sha256"],
            "official_task_sha256": result["official_task_sha256"],
            "synthetic_billing": result["synthetic_billing"],
            "conformance": {
                "worker_kind": "pinned_tau_owned_acquisition_v1",
                "upstream_source_modified": False,
                "official_task": True,
                "official_score": True,
                "provider_accounting": result["provider_accounting"],
            },
        }
    ]
    records.extend(
        normalize_owned_calls(
            calls,
            request,
            cell_id=result["cell_id"],
            optimized_arms=OPTIMIZED_ARMS,
            fixed_schema="tau-fixed-dispatch-policy-v1",
            call_site_prefix="tau2.utils.llm_utils.generate/",
        )
    )
    return validate_worker_records(records, request)


class TauCampaignWorkerSession:
    """One pre-enrolled transport and ordered worker, kept alive by the runner."""

    def __init__(self, worker: TauTaskWorker, *, workload_id: str):
        self.worker, self.workload_id = worker, workload_id

    def __enter__(self):
        self.worker.__enter__()
        return self

    def run(self, request: dict) -> list[dict]:
        if request["stage"] in {"C", "P", "T"} or request["paper_evidence"]:
            raise CampaignError("Tau session has no measured policy-safety sensor")
        if request["workload_id"] != self.workload_id:
            raise CampaignError("Tau session cannot accept another workload")
        config = self.worker.config
        if request["model_pair"] != {
            "strong": config.strong_model,
            "cheap": config.cheap_model,
        }:
            raise CampaignError(
                "Tau session model pair differs from the frozen schedule"
            )
        if request["network_policy"] != config.network_policy:
            raise CampaignError(
                "Tau session network policy differs from its enrollment"
            )
        if (
            config.task_timeout_seconds is None
            or config.task_timeout_seconds > request["worker_timeout_seconds"]
        ):
            raise CampaignError(
                "Tau framework timeout is outside its scheduled cell bound"
            )
        cell = self.worker.cells[self.worker._next]
        if Path(request["storage_path"]).resolve() != self.worker.storage_for_cell(
            cell
        ):
            raise CampaignError(
                "Tau session storage differs from the scheduled arm store"
            )
        expected = {
            "task_id": cell.task_id,
            "arm": cell.arm,
            "repetition": cell.repetition,
            "run_seed": cell.seed,
            "schedule_ordinal": cell.ordinal,
        }
        if any(request.get(key) != value for key, value in expected.items()):
            raise CampaignError("Tau request differs from the next frozen cell")
        from bench.policy_calibration import ARMS, CalibrationTrialBundle

        if cell.arm in ARMS or self.worker.policy_bundle is not None:
            bundle = self.worker.policy_bundle
            if (
                bundle is None
                or request.get("calibration_lock") != bundle.lock
                or bundle.lock["sha256"] != self.worker._policy_lock_sha256
            ):
                raise CampaignError("Tau scheduled policy lock differs from enrollment")
            bundle.authorize(
                workload_id=self.workload_id,
                models=request["model_pair"],
                task_ids=config.task_ids,
                stage=request["stage"],
                paper_evidence=request["paper_evidence"],
                network_policy=request["network_policy"],
                output_cap=config.agent_output_cap,
                task_universe_sha256=request["workload_provenance"][
                    "task_universe_sha256"
                ],
                protocol_sha256=request["protocol_sha256"],
                quality_margin=request["quality_margin"],
                cell={
                    "task_id": cell.task_id,
                    "arm": cell.arm,
                    "repetition": cell.repetition,
                    "seed": cell.seed,
                    "ordinal": cell.ordinal,
                }
                | (
                    {"trial_id": request.get("calibration_trial_id")}
                    if isinstance(bundle, CalibrationTrialBundle)
                    else {}
                ),
            )
            self.worker._authorized_policy_cell = cell.cell_id
        result = self.worker.run_next()
        calls = json.loads(
            (self.worker.output / cell.cell_id / "calls.json").read_bytes()
        )
        return normalize_tau_cell(result, calls, request)

    def __exit__(self, *exc):
        return self.worker.__exit__(*exc)

    def call_receipts(self, cell_id):
        if (
            not isinstance(cell_id, str)
            or len(cell_id) != 64
            or any(c not in "0123456789abcdef" for c in cell_id)
        ):
            raise CampaignError("invalid calibration receipt cell")
        return json.loads((self.worker.output / cell_id / "calls.json").read_bytes())
