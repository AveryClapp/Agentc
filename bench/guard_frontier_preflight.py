"""Predeclare engineered answers, replay the production Rust guard, audit results."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from bench.campaign_accounting import digest
from bench.frozen_policy_preflight import MODES
from bench.guard_evidence import compare_receipt_outputs, file_sha, require
from bench.guard_frontier import RATES, THRESHOLDS, analyze, fixture_quality
from bench.tau_task_worker import TauTaskWorker, private_write

SOURCE_FILES = (
    "bench/guard_frontier.py",
    "bench/guard_frontier_preflight.py",
    "bench/guard_evidence.py",
    "crates/agentc-optimizer/examples/guard_frontier_replay.rs",
    "crates/agentc-optimizer/src/plan_guard.rs",
    "crates/agentc-optimizer/src/plan_profile.rs",
    "crates/agentc-optimizer/src/schema.rs",
    "crates/agentc-optimizer/src/execution_plan.rs",
    "Cargo.lock",
    "Cargo.toml",
    "crates/agentc-optimizer/Cargo.toml",
)


def reply(text):
    return {
        "choices": [
            {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ]
    }


def fixtures(*, repetitions, horizon, mode, sdk_files):
    require(
        type(repetitions) is int and 1 <= repetitions <= 1000,
        "invalid engineering repetitions",
    )
    require(
        type(horizon) is int and 1 <= horizon <= 1000, "invalid engineering horizon"
    )
    scenarios, comparisons = [], []
    for family in MODES:
        for harmful in (True, False):
            for i in range(20):
                expected = [i * 100 + item * 13 + 7 for item in range(10)]
                reference = [f"ANSWER:{answer}" for answer in expected]
                if harmful:
                    wrong = 1 if i % 2 else 10
                    candidate = [
                        f"ANSWER:{answer + (item < wrong)}"
                        for item, answer in enumerate(expected)
                    ]
                else:
                    extras = (0, 2, 20, 60)[i % 4]
                    suffix = " ".join(f"explanation{j}" for j in range(extras))
                    candidate = [
                        text + (" " + suffix if suffix else "") for text in reference
                    ]
                scenario_id = f"{family}-{'harmful' if harmful else 'benign'}-{i:02d}"
                scenario = {
                    "scenario_id": scenario_id,
                    "family": family,
                    "expected_answers": expected,
                    "reference_outputs": reference,
                    "candidate_before_outputs": reference,
                    "candidate_after_outputs": candidate,
                    "change_at_call": 26 if harmful and i % 3 == 0 else 1,
                    "reference_error_every": 3 if harmful and i % 5 == 0 else None,
                    "execution_plan_id": hashlib.sha256(
                        ("engineered-guard-plan:" + family).encode()
                    ).hexdigest(),
                    "runtime_version": {
                        "provider_protocol": "litellm.completion.v1",
                        "target_model_id": "engineered-candidate",
                        "target_model_version": "fixture-v1",
                        "price_table_version": "modeled-engineering-prices-v1",
                    },
                    "divergence_before": 0.0,
                }
                assert (fixture_quality(expected, candidate) < 1.0) is harmful
                scenarios.append(scenario)
                comparisons.append(
                    {
                        "candidate": reply("\n".join(candidate)),
                        "reference": reply("\n".join(reference)),
                        "divergence_mode": mode,
                        "text_action_contract": None,
                    }
                )
    values = compare_receipt_outputs(
        comparisons, python=Path(sys.executable), expected_sdk_files=sdk_files
    )
    for scenario, divergence in zip(scenarios, values, strict=True):
        require(divergence is not None, "fixture output could not be compared")
        scenario["divergence_after"] = divergence
    return {
        "schema_version": 1,
        "record_type": "predeclared_guard_engineering_fixtures",
        "evidence_kind": "engineering_fixture",
        "quality_scorer": "single-answer-marker-v1",
        "identity_kind": "engineered_replay; no provider plan admission",
        "divergence_mode": mode,
        "contract": {
            "schema_version": 1,
            "evidence_kind": "engineering_fixture",
            "divergence_mode": mode,
            "horizon": horizon,
            "repetitions": repetitions,
            "seed": 2026101201,
            "rates": list(RATES),
            "thresholds": list(THRESHOLDS),
            "cost_model": {
                "kind": "modeled; no provider invoices",
                "reference_usd": "0.01",
                "candidate_usd": "0.001",
            },
        },
        "scenarios": scenarios,
    }


def run(binary: Path, output: Path, *, repetitions=84, horizon=100, mode="lexical"):
    repo = Path(__file__).resolve().parent.parent
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    source = {name: file_sha(repo / name) for name in SOURCE_FILES}
    runtime = TauTaskWorker._runtime_manifest()
    fixture = fixtures(
        repetitions=repetitions,
        horizon=horizon,
        mode=mode,
        sdk_files=runtime["sdk_files"],
    )
    fixture_sha = digest(fixture)
    native_input = {k: v for k, v in fixture["contract"].items() if k != "cost_model"}
    native_input["divergence_mode"] = mode
    native_input["scenarios"] = [
        {
            k: scenario[k]
            for k in (
                "scenario_id",
                "family",
                "execution_plan_id",
                "runtime_version",
                "divergence_before",
                "divergence_after",
                "change_at_call",
                "reference_error_every",
            )
        }
        for scenario in fixture["scenarios"]
    ]
    private_write(output / "fixtures.json", fixture)
    private_write(output / "native-input.json", native_input)
    binary_sha = file_sha(binary)
    private_write(
        output / "enrollment.json",
        {
            "record_type": "guard_engineering_frontier_enrollment",
            "source_stage": "E0",
            "source_files": source,
            "binary_sha256": binary_sha,
            "sdk_runtime": runtime,
            "fixture_sha256": fixture_sha,
            "native_input_sha256": digest(native_input),
            "expected_sequence_count": len(fixture["scenarios"])
            * repetitions
            * len(RATES)
            * len(THRESHOLDS),
            "sequences_per_operating_point": len(fixture["scenarios"]) * repetitions,
            "quality_labels_sent_to_controller": False,
            "physical_provider_calls": 0,
        },
    )
    started = time.monotonic()
    with (output / "native-run.log").open("x") as log:
        os.chmod(log.name, 0o600)
        subprocess.run(
            [str(binary), str(output / "native-input.json"), str(output / "native")],
            check=True,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    require(
        file_sha(binary) == binary_sha
        and all(file_sha(repo / name) == expected for name, expected in source.items()),
        "frontier source changed after enrollment",
    )
    require(
        json.loads((output / "fixtures.json").read_bytes()) == fixture,
        "fixture labels changed during replay",
    )
    report = analyze(output / "native/sequences.jsonl", fixture)
    completion = json.loads((output / "native/completion.json").read_bytes())
    require(
        report["sequence_count"] == completion["sequences"],
        "native replay completion count differs",
    )
    report.update(
        native_sequences_sha256=file_sha(output / "native/sequences.jsonl"),
        elapsed_engineering_seconds=time.monotonic() - started,
        full_replay_sequence_floor_met=len(fixture["scenarios"]) * repetitions
        >= 10_000,
    )
    private_write(output / "frontier.json", report)
    summary = {
        "record_type": "guard_engineering_frontier_completion",
        "source_stage": "E0",
        "divergence_mode": mode,
        "sequences": report["sequence_count"],
        "sequences_per_operating_point": len(fixture["scenarios"]) * repetitions,
        "frontier_sha256": digest(report),
        "physical_provider_calls": 0,
        "paper_evidence": False,
        "calibration_admission": False,
        "production_rate_points": [
            p for p in report["operating_points"] if p["rate"] == 0.02
        ],
    }
    private_write(output / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=84)
    parser.add_argument("--horizon", type=int, default=100)
    parser.add_argument(
        "--divergence-mode", choices=("lexical", "normalized"), default="lexical"
    )
    args = parser.parse_args(argv)

    def network_guard(event, arguments):
        if event == "socket.connect":
            raise RuntimeError("controller replay forbids network calls")

    sys.addaudithook(network_guard)
    summary = run(
        args.binary.resolve(),
        args.output.resolve(),
        repetitions=args.repetitions,
        horizon=args.horizon,
        mode=args.divergence_mode,
    )
    print(
        json.dumps({k: v for k, v in summary.items() if k != "production_rate_points"})
    )


if __name__ == "__main__":
    main()
