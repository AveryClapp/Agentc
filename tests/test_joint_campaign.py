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
    _cost_per_task,
    _median_task_effect,
    _mcnemar,
    _paired_total_statistic,
    _worker_environment,
    build_schedule,
    derive_run_seed,
    digest_file,
    digest_value,
    load_campaign,
    main,
    ordered_arms,
    ordered_id_digest,
    run_campaign,
)
from bench.litellm_joint_preflight_worker import _arm_settings


def test_unavailable_scores_preserve_full_diagnostic_denominator(tmp_path):
    from bench.joint_fixture_worker import run as fixture_run
    from bench.joint_campaign import analyze_campaign, validate_worker_records

    campaign = _write_campaign(tmp_path)
    spec = json.loads(campaign.read_bytes())
    spec["diagnostic_arms"] = ["joint_guarded"]
    campaign.write_text(json.dumps(spec))
    captured = []

    class Session:
        def __enter__(self):
            return self

        def run(self, request):
            records = fixture_run(request)
            if request["repetition"] == 0:
                task = next(r for r in records if r["record_type"] == "task")
                task.update(
                    task_status="incomplete",
                    official_score=None,
                    metric_availability={"official_score": False},
                    unavailable_metric_reasons={"official_score": "image_unavailable"},
                )
                task["conformance"]["official_score"] = False
                records = [task]
            captured.append((records, request))
            return records

        def __exit__(self, *exc):
            pass

    output = tmp_path / "missing-scores"
    manifest = run_campaign(
        campaign,
        output,
        worker_sessions={
            name: Session() for name in ("fixture-stateful", "fixture-coding")
        },
    )
    assert manifest["completeness"]["status"] == "diagnostic_incomplete"
    assert manifest["completeness"]["schedule_complete"] is True
    assert manifest["completeness"]["official_score_unknown_tasks"] == 2
    analysis = json.loads((output / "analysis.json").read_text())
    for workload in analysis["workloads"].values():
        summary = workload["arms"]["joint_guarded"]
        assert summary["assigned_tasks"] == 2
        assert summary["official_score_unknown_tasks"] == 1
        assert summary["quality_mean"] is None and summary["full_pass_rate"] is None
    assert "unavailable" in (output / "report.md").read_text()
    raw = [
        json.loads(line)
        for line in (output / "raw-records.jsonl").read_text().splitlines()
    ]
    with pytest.raises(CampaignError, match="every assigned official score"):
        analyze_campaign(spec, raw)
    records, request = captured[0]
    task = records[0]
    for mutation in (
        {"task_status": "completed"},
        {"task_status": "failed"},
        {"metric_availability": {}},
        {"unavailable_metric_reasons": {"official_score": None}},
        {"conformance": {**task["conformance"], "official_score": True}},
    ):
        with pytest.raises(CampaignError):
            validate_worker_records([task | mutation], request)


def test_forbidden_worker_has_no_openrouter_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret-not-a-live-key")
    assert "OPENROUTER_API_KEY" not in _worker_environment("forbidden", tmp_path)
    assert _worker_environment("provider_allowed", tmp_path)["OPENROUTER_API_KEY"] == (
        "test-secret-not-a-live-key"
    )


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
                "expected_spend_usd": 0.5 if stage in {"C", "P", "T"} else 0.0,
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


def test_owned_sessions_are_entered_once_and_closed_before_success_is_sealed(tmp_path):
    from bench.joint_fixture_worker import run as fixture_run

    campaign = _write_campaign(tmp_path)
    output = tmp_path / "owned-output"
    sessions = {}
    for name in ("fixture-stateful", "fixture-coding"):

        class Session:
            entered = 0
            exited = 0
            requests = 0

            def __enter__(self):
                self.entered += 1
                return self

            def run(self, request):
                self.requests += 1
                return fixture_run(request)

            def __exit__(self, *exc):
                assert not (output / "manifest.json").exists()
                assert not (output / "analysis.json").exists()
                self.exited += 1

        sessions[name] = Session()
    manifest = run_campaign(campaign, output, worker_sessions=sessions)
    assert manifest["completeness"]["status"] == "complete"
    assert all(
        (s.entered, s.exited, s.requests) == (1, 1, 20) for s in sessions.values()
    )
    assert set(manifest["worker_execution_modes"].values()) == {"owned_session"}
    assert list((output / "worker-results").iterdir()) == []
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "raw-records.jsonl").stat().st_mode & 0o777 == 0o600


def test_failed_owned_completion_cannot_publish_success_and_owned_resume_is_forbidden(
    tmp_path,
):
    from bench.joint_fixture_worker import run as fixture_run
    from bench.campaign_transport import CampaignStopped

    campaign = _write_campaign(tmp_path)
    output = tmp_path / "failed-owned-output"

    class Session:
        def __enter__(self):
            return self

        def run(self, request):
            return fixture_run(request)

        def __exit__(self, *exc):
            raise CampaignStopped("unresolved owned billing")

    with pytest.raises(CampaignError, match="cannot resume"):
        run_campaign(
            campaign,
            output,
            resume=True,
            worker_sessions={"fixture-stateful": Session()},
        )
    assert not output.exists()
    with pytest.raises(CampaignStopped):
        run_campaign(campaign, output, worker_sessions={"fixture-stateful": Session()})
    assert not (output / "manifest.json").exists()


def test_owned_session_cannot_suppress_another_sessions_completion_failure(tmp_path):
    from bench.joint_fixture_worker import run as fixture_run
    from bench.campaign_transport import CampaignStopped

    campaign = _write_campaign(tmp_path)
    output = tmp_path / "suppressed-completion"

    class SuppressingSession:
        def __enter__(self):
            return self

        def run(self, request):
            return fixture_run(request)

        def __exit__(self, *exc):
            return True

    class FailingSession(SuppressingSession):
        def __exit__(self, *exc):
            raise CampaignStopped("unresolved owned billing")

    with pytest.raises(CampaignError, match="cannot suppress"):
        run_campaign(
            campaign,
            output,
            worker_sessions={
                "fixture-coding": SuppressingSession(),
                "fixture-stateful": FailingSession(),
            },
        )
    assert not (output / "manifest.json").exists()


@pytest.mark.parametrize(
    "change", ["paper_stage", "empty", "duplicate", "unknown", "order"]
)
def test_diagnostic_subset_cannot_replace_or_weaken_a_primary_campaign(
    tmp_path, change
):
    campaign = _write_campaign(tmp_path)
    spec = json.loads(campaign.read_bytes())
    spec["diagnostic_arms"] = ["trace_only_fixed_strong", "joint_guarded"]
    if change == "paper_stage":
        spec["stage"] = "C"
    elif change == "empty":
        spec["diagnostic_arms"] = []
    elif change == "duplicate":
        spec["diagnostic_arms"] = ["joint_guarded", "joint_guarded"]
    elif change == "unknown":
        spec["diagnostic_arms"] = ["made_up"]
    else:
        spec["diagnostic_arms"].reverse()
    campaign.write_text(json.dumps(spec))
    with pytest.raises(CampaignError, match="diagnostic_arms"):
        load_campaign(campaign)


def test_declared_diagnostic_completeness_is_separate_and_unknown_measurements_stay_unknown(
    tmp_path,
):
    from bench.joint_fixture_worker import run as fixture_run

    campaign = _write_campaign(tmp_path)
    spec = json.loads(campaign.read_bytes())
    spec["diagnostic_arms"] = ["joint_guarded"]
    campaign.write_text(json.dumps(spec))

    class Session:
        def __enter__(self):
            return self

        def run(self, request):
            records = fixture_run(request)
            for record in records:
                field = (
                    "safety_failure"
                    if record["record_type"] == "task"
                    else "candidate_count"
                )
                record.update(
                    {
                        field: None,
                        "metric_availability": {field: False},
                        "unavailable_metric_reasons": {field: "not_measured"},
                    }
                )
            return records

        def __exit__(self, *exc):
            pass

    output = tmp_path / "diagnostic"
    manifest = run_campaign(
        campaign,
        output,
        worker_sessions={
            name: Session() for name in ("fixture-stateful", "fixture-coding")
        },
    )
    assert manifest["completeness"]["status"] == "diagnostic_complete"
    assert manifest["schedule"]["arms"] == ["joint_guarded"]
    analysis = json.loads((output / "analysis.json").read_bytes())
    assert analysis["analysis_contract"]["competitive_comparisons"] is False
    assert all(
        w["arms"]["joint_guarded"]["safety_unknown_tasks"] == 2
        for w in analysis["workloads"].values()
    )


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
    assert (
        total(
            {
                "reference": list(reference.values()),
                "candidate": list(candidate.values()),
            }
        )
        == 6.0
    )


def test_costs_are_isolated_when_workloads_share_task_ids() -> None:
    first = {
        "workload_id": "banking",
        "arm": "joint_guarded",
        "task_id": "001",
        "repetition": 0,
    }
    second = {**first, "workload_id": "coding"}
    totals = _cost_per_task(
        [first, second],
        [
            {**first, "cost_usd": 1.0},
            {**second, "cost_usd": 3.0},
            {**first, "cost_usd": 0.5},
        ],
    )
    assert totals[("banking", "joint_guarded", "001", 0)] == 1.5
    assert totals[("coding", "joint_guarded", "001", 0)] == 3.0


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


def test_held_out_lock_binds_selections_and_requires_five_repetitions(
    tmp_path: Path,
) -> None:
    campaign = _write_campaign(tmp_path, stage="T", paper_evidence=True)
    raw = json.loads(campaign.read_text())
    selections = {"best_static_joint": {"target": "cheap-model", "rewrites": []}}
    raw["calibration_lock"] = {
        "selections": selections,
        "sha256": digest_value(selections),
    }
    for workload in raw["workloads"]:
        workload["network_policy"] = "provider_allowed"
        workload["unengineered_upstream"] = True
        workload["repetitions"] = 5
    campaign.write_text(json.dumps(raw))
    assert load_campaign(campaign)["calibration_lock"]["selections"] == selections

    raw["calibration_lock"]["selections"]["best_static_joint"]["target"] = (
        "strong-model"
    )
    campaign.write_text(json.dumps(raw))
    with pytest.raises(CampaignError, match="selections digest mismatch"):
        load_campaign(campaign)

    raw["calibration_lock"]["sha256"] = digest_value(
        raw["calibration_lock"]["selections"]
    )
    raw["workloads"][0]["repetitions"] = 2
    campaign.write_text(json.dumps(raw))
    with pytest.raises(CampaignError, match="repetitions must be an integer >= 5"):
        load_campaign(campaign)


def test_mcnemar_does_not_treat_repeats_as_independent_tasks() -> None:
    reference = {
        ("same-task", repetition): {"official_score": 1.0} for repetition in range(10)
    }
    candidate = {
        ("same-task", repetition): {"official_score": 0.0} for repetition in range(10)
    }
    summary = _mcnemar(reference, candidate)
    assert summary["reference_pass_candidate_fail"] == 10
    assert summary["p_value_two_sided_exact"] is None
    assert summary["pooled_repetitions"] is True
    assert len(summary["by_repetition"]) == 10
    assert all(
        repetition["p_value_two_sided_exact"] == 1.0
        for repetition in summary["by_repetition"].values()
    )


def test_mcnemar_keeps_exact_test_for_independent_tasks() -> None:
    reference = {(f"task-{task}", 0): {"official_score": 1.0} for task in range(10)}
    candidate = {(f"task-{task}", 0): {"official_score": 0.0} for task in range(10)}
    summary = _mcnemar(reference, candidate)
    assert summary["p_value_two_sided_exact"] == 2.0 / 2**10
    assert summary["pooled_repetitions"] is False


def test_one_command_emits_complete_raw_manifest_and_analysis(tmp_path: Path) -> None:
    campaign = _write_campaign(tmp_path)
    output = tmp_path / "output"
    manifest = run_campaign(campaign, output)
    assert manifest["completeness"]["status"] == "complete"
    assert manifest["completeness"]["task_records"] == 40
    assert manifest["completeness"]["scheduled_runs"] == 40
    assert manifest["completeness"]["distinct_unengineered_families"] == 0
    assert manifest["completeness"]["call_records"] >= 40
    assert manifest["spend"]["actual_spend_basis"] == (
        "network_forbidden_no_billed_calls"
    )
    assert manifest["spend"]["actual_spend_usd"] == 0.0
    assert manifest["spend"]["expected_usd"] == 0.0
    assert manifest["spend"]["stop_reason"] == "schedule_complete"
    assert manifest["spend"]["stop_threshold_usd"] == 0.0
    assert manifest["spend"]["threshold_exceeded"] is False
    assert manifest["spend"]["threshold_exceeded_workloads"] == []
    assert manifest["spend"]["recorded_cost_usd"] > 0.0
    assert all(
        workload_spend["actual_spend_usd"] == 0.0
        for workload_spend in manifest["spend"]["workloads"].values()
    )
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
    assert analysis["interpretation_limits"] == ["fixture only"]
    assert analysis["analysis_contract"]["protocol_go_decision"] is False
    report = (output / "report.md").read_text()
    assert "fixture only" in report
    assert "unadjusted exploratory intervals" in report
    assert "Selection-valid" not in report
    for result in analysis["workloads"].values():
        assert set(result["arms"]) == set(PRIMARY_ARMS)
        assert "resampled_best_control_joint_cost_advantage_usd" in result
        baseline_cost = result["arms"]["trace_only_fixed_strong"][
            "mean_cost_per_task_usd"
        ]
        for arm in PRIMARY_ARMS:
            difference = result["arms"][arm]["mean_cost_per_task_usd"] - baseline_cost
            assert result["paired_comparisons"][arm]["cost_delta_usd_vs_reference"][
                "estimate"
            ] == pytest.approx(difference)
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
        synergy = result["interaction"]["cost_synergy_usd_per_task"]["estimate"]
        assert result["negative_regimes"]["interaction_not_positive"] is (
            synergy <= 0.0
        )

    lines = (output / "raw-records.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert sum(record["record_type"] == "task" for record in records) == 40
    assert {record["arm"] for record in records} == set(PRIMARY_ARMS)
    assert not any(str(Path.home()) in line for line in lines)
    assert not any("API_KEY" in line for line in lines)


def test_validate_only_does_not_execute_workers_or_create_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = _write_campaign(tmp_path)
    output = tmp_path / "uncreated-output"

    def unexpected_execution(*args: Any, **kwargs: Any) -> None:
        pytest.fail("contract validation must not execute workers")

    monkeypatch.setattr("bench.joint_campaign.run_campaign", unexpected_execution)
    assert main([str(campaign), "--output", str(output), "--validate-only"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["worker_execution"] is False
    assert summary["schedule"]["runs"] == 40
    assert not output.exists()


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


def test_provider_spend_stops_campaign_and_seals_partial_manifest(
    tmp_path: Path,
) -> None:
    worker = tmp_path / "provider_worker.py"
    worker.write_text(
        """import argparse, json
p = argparse.ArgumentParser()
p.add_argument('--request', required=True)
p.add_argument('--output', required=True)
a = p.parse_args()
task = {
    'schema_version': 1,
    'record_type': 'task',
    'task_status': 'completed',
    'official_score': 1.0,
    'end_to_end_latency_ms': 1.0,
    'safety_failure': False,
    'network_calls': 1,
    'conformance': {
        'worker_kind': 'provider-accounting-test',
        'upstream_source_modified': False,
        'official_task': False,
        'official_score': False,
        'provider_accounting': True,
    },
}
call = {
    'schema_version': 1,
    'record_type': 'call',
    'call_index': 0,
    'requested_model': 'strong',
    'selected_model': 'strong',
    'call_site_id': 'test.site',
    'execution_plan_id': '1' * 64,
    'ordered_rewrites': [],
    'input_tokens': 1,
    'cached_input_tokens': 0,
    'output_tokens': 1,
    'reasoning_tokens': 0,
    'tool_tokens': 0,
    'retry_count': 0,
    'candidate_count': 1,
    'cost_usd': 1.0,
    'request_latency_ms': 1.0,
    'planning_overhead_us': 0.0,
    'eligible': True,
    'is_exploration': False,
    'is_shadow': False,
    'failed': False,
    'dispatch_fallback': False,
    'abstention_reason': None,
    'request_digest': '2' * 64,
    'response_digest': '3' * 64,
    'network_calls': 1,
}
with open(a.output, 'w') as out:
    out.write(json.dumps(task) + '\\n')
    out.write(json.dumps(call) + '\\n')
"""
    )
    campaign = _write_campaign(tmp_path, stage="E1")
    raw = json.loads(campaign.read_text())
    raw["expected_spend_usd"] = 0.02
    for workload in raw["workloads"]:
        workload["network_policy"] = "provider_allowed"
        workload["expected_spend_usd"] = 0.01
        workload["worker_command"] = [sys.executable, str(worker)]
    campaign.write_text(json.dumps(raw))
    output = tmp_path / "output"

    with pytest.raises(CampaignError, match="campaign stopped"):
        run_campaign(campaign, output)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["completeness"]["status"] == "stopped"
    assert manifest["completeness"]["task_records"] == 1
    assert manifest["completeness"]["missing_scheduled_runs"] == 39
    assert manifest["spend"]["actual_spend_usd"] == 1.0
    assert manifest["spend"]["threshold_exceeded"] is True
    assert manifest["spend"]["stop_reason"].startswith("spend_threshold_exceeded:")
    assert manifest["schedule"]["digest"]
    assert manifest["workloads"][0]["expected_spend_usd"] == 0.01
    with pytest.raises(CampaignError, match="stopped by its frozen spend contract"):
        run_campaign(campaign, output, resume=True)

    ledger = output / "raw-records.jsonl"
    ledger_before_recovery = ledger.read_bytes()
    (output / "manifest.json").unlink()
    with pytest.raises(CampaignError, match="existing ledger exceeds"):
        run_campaign(campaign, output, resume=True)
    assert ledger.read_bytes() == ledger_before_recovery
    recovered_manifest = json.loads((output / "manifest.json").read_text())
    assert recovered_manifest["completeness"]["task_records"] == 1


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

    clean = _agentc_source_context(repo, tmp_path / "output", paper_evidence=False)
    assert clean["agentc_git_dirty"] is False
    source.write_text("changed\n")
    dirty = _agentc_source_context(repo, tmp_path / "output", paper_evidence=False)
    assert dirty["agentc_git_dirty"] is True
    assert dirty["agentc_source_state_sha256"] != clean["agentc_source_state_sha256"]
    with pytest.raises(CampaignError, match="clean Agentc source tree"):
        _agentc_source_context(repo, tmp_path / "output", paper_evidence=True)
