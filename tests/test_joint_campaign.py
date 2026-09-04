from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bench.joint_campaign import (
    PRIMARY_ARMS,
    CampaignError,
    _agentc_source_context,
    _artifact_stem,
    _median_task_effect,
    _paired_total_statistic,
    build_schedule,
    derive_run_seed,
    digest_file,
    load_campaign,
    ordered_arms,
    ordered_id_digest,
    run_campaign,
)
from bench.litellm_joint_preflight_worker import _arm_settings


def _write_campaign(
    tmp_path: Path,
    *,
    stage: str = "E0",
    paper_evidence: bool = False,
    task_ids: list[str] | None = None,
) -> Path:
    protocol = tmp_path / "protocol.md"
    protocol.write_text("frozen test protocol\n")
    tasks = task_ids or ["task-001"]
    workloads: list[dict[str, Any]] = []
    for workload_id, family in (
        ("fixture-stateful", "stateful_tool_use"),
        ("fixture-coding", "software_engineering"),
    ):
        workloads.append(
            {
                "workload_id": workload_id,
                "family": family,
                "unengineered_upstream": False,
                "provenance": {
                    "upstream_commit": "1" * 40,
                    "task_universe_sha256": "2" * 64,
                },
                "split": "calibration",
                "worker_command": [
                    "{python}",
                    "-m",
                    "bench.joint_fixture_worker",
                ],
                "worker_cwd": "{repo}",
                "task_ids": tasks,
                "task_ids_sha256": ordered_id_digest(tasks),
                "repetitions": 2,
                "model_pair": {"strong": "strong-model", "cheap": "cheap-model"},
                "quality_margin": -0.03 if family == "stateful_tool_use" else -0.02,
                "network_policy": "forbidden",
                "timeout_seconds": 30,
                "worker_configuration": {
                    "interaction_strength": 0.2,
                    "input_token_scale": 1.0,
                },
            }
        )
    spec: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": "joint-campaign-test",
        "stage": stage,
        "paper_evidence": paper_evidence,
        "expected_spend_usd": 1.0 if stage in {"C", "P", "T"} else 0.0,
        "protocol": {"path": "protocol.md", "sha256": digest_file(protocol)},
        "arms": list(PRIMARY_ARMS),
        "bootstrap_resamples": 20,
        "workloads": workloads,
        "interpretation_limits": ["fixture only"],
    }
    path = tmp_path / "campaign-input.json"
    path.write_text(json.dumps(spec))
    return path


def test_seed_and_arm_order_are_deterministic() -> None:
    first = derive_run_seed("workload", "task", "joint_guarded", 3)
    assert first == derive_run_seed("workload", "task", "joint_guarded", 3)
    assert first != derive_run_seed("workload", "task", "joint_guarded", 4)
    order = ordered_arms("workload", "task", 0, PRIMARY_ARMS)
    assert order == ordered_arms("workload", "task", 0, PRIMARY_ARMS)
    assert set(order) == set(PRIMARY_ARMS)


def test_median_task_and_total_effects_keep_pairing() -> None:
    reference = {
        ("a", 0): {"metric": 1.0},
        ("a", 1): {"metric": 3.0},
        ("b", 0): {"metric": 0.0},
        ("b", 1): {"metric": 0.0},
    }
    candidate = {
        ("a", 0): {"metric": 3.0},
        ("a", 1): {"metric": 5.0},
        ("b", 0): {"metric": 1.0},
        ("b", 1): {"metric": 1.0},
    }
    assert _median_task_effect(reference, candidate, "metric") == 1.5
    total = _paired_total_statistic("metric", "reference", "candidate")
    assert total(
        {
            "reference": list(reference.values()),
            "candidate": list(candidate.values()),
        }
    ) == 6.0


def test_schedule_pairs_every_arm_and_repetition(tmp_path: Path) -> None:
    spec = load_campaign(_write_campaign(tmp_path, task_ids=["a", "b"]))
    schedule = build_schedule(spec)
    assert len(schedule) == 2 * 2 * 2 * len(PRIMARY_ARMS)
    assert len({run.key for run in schedule}) == len(schedule)
    assert [run.ordinal for run in schedule] == list(range(len(schedule)))


def test_artifact_stem_does_not_trust_task_id(tmp_path: Path) -> None:
    spec = load_campaign(_write_campaign(tmp_path, task_ids=["../../escape/me"]))
    run = build_schedule(spec)[0]
    stem = _artifact_stem(run)
    assert "/" not in stem
    assert ".." not in stem


@pytest.mark.parametrize("workload_id", ["../../escape", "/tmp/escape", "bad\\path"])
def test_workload_id_cannot_escape_state_directory(
    tmp_path: Path, workload_id: str
) -> None:
    campaign = _write_campaign(tmp_path)
    raw = json.loads(campaign.read_text())
    raw["workloads"][0]["workload_id"] = workload_id
    campaign.write_text(json.dumps(raw))
    with pytest.raises(CampaignError, match="portable identifier"):
        load_campaign(campaign)


def test_protocol_and_task_digests_are_enforced(tmp_path: Path) -> None:
    campaign = _write_campaign(tmp_path)
    raw = json.loads(campaign.read_text())
    raw["protocol"]["sha256"] = "0" * 64
    campaign.write_text(json.dumps(raw))
    with pytest.raises(CampaignError, match="protocol digest mismatch"):
        load_campaign(campaign)

    campaign = _write_campaign(tmp_path)
    raw = json.loads(campaign.read_text())
    raw["workloads"][0]["task_ids_sha256"] = "0" * 64
    campaign.write_text(json.dumps(raw))
    with pytest.raises(CampaignError, match="task_ids digest mismatch"):
        load_campaign(campaign)


def test_repository_e0_campaign_is_frozen_and_complete() -> None:
    spec = load_campaign(Path("bench/repro/joint-campaign-e0.json"))
    assert spec["paper_evidence"] is False
    assert spec["stage"] == "E0"
    assert len(build_schedule(spec)) == 40
    assert {
        workload["family"]
        for workload in spec["workloads"]
        if workload["unengineered_upstream"]
    } == {"stateful_tool_use_and_retrieval", "software_engineering"}


def test_current_greedy_uses_projected_composer_not_first_match() -> None:
    settings = _arm_settings(
        "current_greedy",
        strong_model="openai/strong",
        cheap_model="openai/cheap",
        task_id="task-001",
    )
    assert settings["optimize"] is True
    assert settings["compose"] is True
    assert settings["planner_mode"] == "current_greedy"
    assert settings["implementation"] == "projected_savings_greedy"


def test_held_out_stage_requires_calibration_lock(tmp_path: Path) -> None:
    campaign = _write_campaign(
        tmp_path,
        stage="P",
        paper_evidence=True,
    )
    raw = json.loads(campaign.read_text())
    for workload in raw["workloads"]:
        workload["network_policy"] = "provider_allowed"
    campaign.write_text(json.dumps(raw))
    with pytest.raises(CampaignError, match="calibration_lock"):
        load_campaign(campaign)


def test_one_command_emits_complete_raw_manifest_and_analysis(tmp_path: Path) -> None:
    campaign = _write_campaign(tmp_path)
    output = tmp_path / "output"
    manifest = run_campaign(campaign, output)
    assert manifest["completeness"]["status"] == "complete"
    assert manifest["completeness"]["task_records"] == 40
    assert manifest["completeness"]["scheduled_runs"] == 40
    assert manifest["completeness"]["distinct_unengineered_families"] == 0
    assert manifest["completeness"]["call_records"] >= 40
    assert manifest["spend"] == {
        "actual_spend_basis": "network_forbidden_no_billed_calls",
        "actual_spend_usd": 0.0,
        "expected_usd": 0.0,
        "recorded_cost_usd": manifest["spend"]["recorded_cost_usd"],
        "stop_reason": "schedule_complete",
        "stop_threshold_usd": 0.0,
        "threshold_exceeded": False,
    }
    assert manifest["spend"]["recorded_cost_usd"] > 0.0
    for artifact in (
        "campaign.json",
        "run-context.json",
        "raw-records.jsonl",
        "analysis.json",
        "report.md",
    ):
        assert (output / artifact).is_file()
        assert manifest["artifacts"][artifact] == digest_file(output / artifact)

    analysis = json.loads((output / "analysis.json").read_text())
    assert set(analysis["workloads"]) == {"fixture-stateful", "fixture-coding"}
    for result in analysis["workloads"].values():
        assert set(result["arms"]) == set(PRIMARY_ARMS)
        assert "selection_valid_joint_cost_advantage_usd" in result
        assert set(result["interaction"]["joint_vs_named_controls_cost_usd"]) == {
            "routing_only",
            "rewrite_only_fixed_strong",
            "best_static_joint",
            "route_then_rewrite",
            "rewrite_then_route",
            "current_greedy",
        }
        assert set(result["negative_regimes"]) == {
            "zero_opportunity",
            "abstention_dominant",
            "no_joint_efficiency_gain",
            "joint_quality_point_beyond_margin",
            "interaction_not_positive",
            "safety_failure_observed",
        }
        reference = result["paired_comparisons"]["trace_only_fixed_strong"]
        assert reference["quality_median_task_delta_vs_reference"] == 0.0
        assert reference["cost_median_task_delta_usd_vs_reference"] == 0.0
        assert reference["latency_median_task_delta_ms_vs_reference"] == 0.0
        for field in (
            "cost_total_delta_usd_vs_reference",
            "latency_total_delta_ms_vs_reference",
        ):
            assert reference[field] == {"estimate": 0.0, "low": 0.0, "high": 0.0}

    lines = (output / "raw-records.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert sum(record["record_type"] == "task" for record in records) == 40
    assert {record["arm"] for record in records} == set(PRIMARY_ARMS)
    assert not any(str(Path.home()) in line for line in lines)
    assert not any("API_KEY" in line for line in lines)


def test_cli_refuses_to_overwrite_nonempty_output(tmp_path: Path) -> None:
    campaign = _write_campaign(tmp_path)
    output = tmp_path / "occupied"
    output.mkdir()
    (output / "keep.txt").write_text("user data")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "bench.joint_campaign",
            str(campaign),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "not empty" in completed.stderr
    assert (output / "keep.txt").read_text() == "user data"


def test_resume_rejects_changed_campaign_and_poisoned_ledger(tmp_path: Path) -> None:
    campaign = _write_campaign(tmp_path)
    output = tmp_path / "output"
    run_campaign(campaign, output)

    changed = json.loads(campaign.read_text())
    changed["campaign_id"] = "different-campaign"
    campaign.write_text(json.dumps(changed))
    with pytest.raises(CampaignError, match="does not match"):
        run_campaign(campaign, output, resume=True)

    campaign = _write_campaign(tmp_path)
    ledger = output / "raw-records.jsonl"
    records = ledger.read_text().splitlines()
    first = json.loads(records[0])
    first["arm"] = "joint_guarded"
    records[0] = json.dumps(first)
    ledger.write_text("\n".join(records) + "\n")
    with pytest.raises(CampaignError, match="exactly one task record|unscheduled"):
        run_campaign(campaign, output, resume=True)


def test_resume_rejects_changed_source_context(tmp_path: Path) -> None:
    campaign = _write_campaign(tmp_path)
    output = tmp_path / "output"
    run_campaign(campaign, output)
    context_path = output / "run-context.json"
    context = json.loads(context_path.read_text())
    context["agentc_git_commit"] = "0" * 40
    context_path.write_text(json.dumps(context))
    with pytest.raises(CampaignError, match="source context does not match"):
        run_campaign(campaign, output, resume=True)


def test_completed_resume_preserves_worker_commands(tmp_path: Path) -> None:
    campaign = _write_campaign(tmp_path)
    output = tmp_path / "output"
    initial = run_campaign(campaign, output)
    resumed = run_campaign(campaign, output, resume=True)
    assert resumed["worker_commands"] == initial["worker_commands"]
    assert set(resumed["worker_commands"]) == {"fixture-stateful", "fixture-coding"}


def test_source_context_detects_worktree_drift(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.txt"
    source.write_text("frozen\n")
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "source.txt"],
        [
            "git",
            "-c",
            "user.name=Agentc Test",
            "-c",
            "user.email=agentc-test@example.invalid",
            "commit",
            "-qm",
            "freeze",
        ],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)

    clean = _agentc_source_context(
        repo, tmp_path / "output", paper_evidence=False
    )
    assert clean["agentc_git_dirty"] is False
    source.write_text("changed\n")
    dirty = _agentc_source_context(
        repo, tmp_path / "output", paper_evidence=False
    )
    assert dirty["agentc_git_dirty"] is True
    assert dirty["agentc_source_state_sha256"] != clean["agentc_source_state_sha256"]
    with pytest.raises(CampaignError, match="clean Agentc source tree"):
        _agentc_source_context(repo, tmp_path / "output", paper_evidence=True)
