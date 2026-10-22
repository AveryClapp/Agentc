"""Recompute threshold decisions from closed engineering replay acquisitions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bench.campaign_accounting import digest
from bench.guard_admission import choose_thresholds, sampling_feasibility
from bench.guard_evidence import file_sha, require, strict_json
from bench.guard_frontier import analyze, fixture_quality
from bench.tau_task_worker import private_write


def run(acquisition: Path, output: Path):
    require(not output.exists(), "guard decision requires a fresh output directory")
    enrollment = strict_json((acquisition / "enrollment.json").read_bytes())
    fixture = strict_json((acquisition / "fixtures.json").read_bytes())
    summary = strict_json((acquisition / "summary.json").read_bytes())
    stored = strict_json((acquisition / "frontier.json").read_bytes())
    require(
        enrollment["source_stage"] == summary["source_stage"] == "E0"
        and summary["paper_evidence"] is False
        and summary["calibration_admission"] is False,
        "engineering frontier scope cannot become provider admission",
    )
    require(
        digest(fixture) == enrollment["fixture_sha256"]
        and digest(stored) == summary["frontier_sha256"]
        and file_sha(acquisition / "native/sequences.jsonl")
        == stored["native_sequences_sha256"],
        "closed frontier artifact hash mismatch",
    )
    # Recompute rather than trusting detection, disable or savings summary flags.
    frontier = analyze(acquisition / "native/sequences.jsonl", fixture)
    require(
        frontier["sequence_count"] == enrollment["expected_sequence_count"]
        and frontier["sequence_count"]
        == strict_json((acquisition / "native/completion.json").read_bytes())[
            "sequences"
        ],
        "frontier replay corpus is incomplete",
    )
    for key in ("operating_points", "family_points", "scenario_points"):
        require(
            len(frontier[key]) == len(stored[key])
            and all(
                all(current.get(field) == value for field, value in published.items())
                for current, published in zip(frontier[key], stored[key], strict=True)
            ),
            "guard summary differs from raw replay",
        )
    decision = choose_thresholds(frontier)
    losses = [
        fixture_quality(s["expected_answers"], s["reference_outputs"])
        - fixture_quality(s["expected_answers"], s["candidate_after_outputs"])
        for s in fixture["scenarios"]
    ]
    maximum_loss = max(losses)
    prices = fixture["contract"]["cost_model"]
    bounds = [
        sampling_feasibility(
            loss_per_call=maximum_loss,
            reference_cost_usd=prices["reference_usd"],
            candidate_cost_usd=prices["candidate_usd"],
            required_samples=n,
        )
        for n in (1, 2)
    ]
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    private_write(output / "threshold-decision.json", decision)
    private_write(output / "sampling-feasibility.json", bounds)
    result = {
        "record_type": "guard_admission_engineering_review",
        "source_stage": "E0",
        "acquisition_fixture_sha256": digest(fixture),
        "raw_frontier_sha256": digest(frontier),
        "decision_sha256": digest(decision),
        "bounds_sha256": digest(bounds),
        "disabled_families": [
            family
            for family, entry in decision["families"].items()
            if entry["disabled_for_benefit_experiment"]
        ],
        "physical_provider_calls": 0,
        "paper_evidence": False,
        "calibration_admission": False,
        "analysis_source_sha256": {
            name: file_sha(Path(__file__).parent / name)
            for name in (
                "guard_admission.py",
                "guard_admission_preflight.py",
                "guard_frontier.py",
            )
        },
    }
    private_write(output / "summary.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acquisition", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.acquisition.resolve(), args.output.resolve())))


if __name__ == "__main__":
    main()
