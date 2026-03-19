"""Benchmark report generation.

Aggregates results from overhead, calibration, and validation into a
single benchmark report with pass/fail summary.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bench.calibrate import CalibrationReport, format_calibration_report
from bench.overhead import OverheadResult, format_overhead_report
from bench.validate import CorrectnessReport, format_validation_report


@dataclass
class BenchmarkReport:
    """Complete benchmark report."""

    timestamp: str = ""
    overhead_results: list[OverheadResult] = field(default_factory=list)
    calibration: CalibrationReport | None = None
    validation_results: list[CorrectnessReport] = field(default_factory=list)

    @property
    def overhead_passed(self) -> bool:
        return all(
            all(r.passes_budget().values()) for r in self.overhead_results
        )

    @property
    def calibration_passed(self) -> bool:
        return self.calibration is not None and self.calibration.passed

    @property
    def validation_passed(self) -> bool:
        return all(r.passed for r in self.validation_results)

    @property
    def all_passed(self) -> bool:
        return (
            self.overhead_passed
            and self.calibration_passed
            and self.validation_passed
        )


def generate_report(report: BenchmarkReport) -> str:
    """Generate a full human-readable benchmark report."""
    if not report.timestamp:
        report.timestamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    sections = [
        "AGENTC BENCHMARK REPORT",
        "=" * 60,
        f"Generated: {report.timestamp}",
        "",
    ]

    # Overhead section
    if report.overhead_results:
        sections.append(format_overhead_report(report.overhead_results))
        sections.append("")

    # Calibration section
    if report.calibration:
        sections.append(format_calibration_report(report.calibration))
        sections.append("")

    # Validation section
    if report.validation_results:
        sections.append(format_validation_report(report.validation_results))
        sections.append("")

    # Summary
    sections.append("=" * 60)
    sections.append("SUMMARY")
    sections.append(f"  Overhead:    {'PASS' if report.overhead_passed else 'FAIL'}")
    sections.append(
        f"  Calibration: {'PASS' if report.calibration_passed else 'FAIL'}"
    )
    sections.append(
        f"  Validation:  {'PASS' if report.validation_passed else 'FAIL'}"
    )
    sections.append(f"  OVERALL:     {'PASS' if report.all_passed else 'FAIL'}")

    return "\n".join(sections)


def save_report(report: BenchmarkReport, output_dir: Path) -> Path:
    """Save report to output directory in both text and JSON formats."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Text report
    text_path = output_dir / "benchmark_report.txt"
    text_path.write_text(generate_report(report))

    # JSON summary (machine-readable)
    json_path = output_dir / "benchmark_summary.json"
    summary: dict[str, Any] = {
        "timestamp": report.timestamp,
        "passed": report.all_passed,
        "overhead_passed": report.overhead_passed,
        "calibration_passed": report.calibration_passed,
        "validation_passed": report.validation_passed,
    }

    if report.calibration:
        summary["calibration"] = {
            "seed": report.calibration.seed,
            "detectors": [
                {
                    "name": d.detector_name,
                    "recommended_threshold": d.recommended_threshold,
                    "results": [
                        {
                            "threshold": r.threshold,
                            "precision": r.precision,
                            "recall": r.recall,
                            "f1": r.f1,
                        }
                        for r in d.results
                    ],
                }
                for d in report.calibration.detectors
            ],
        }

    if report.overhead_results:
        summary["overhead"] = {
            "tasks": len(report.overhead_results),
            "mean_per_call_us": sum(
                r.mean_per_call_overhead_us for r in report.overhead_results
            ) / max(len(report.overhead_results), 1),
        }

    if report.validation_results:
        total_checks = sum(
            len(r.results) for r in report.validation_results
        )
        total_passed = sum(
            r.pass_count for r in report.validation_results
        )
        summary["validation"] = {
            "tasks": len(report.validation_results),
            "checks": total_checks,
            "passed": total_passed,
        }

    json_path.write_text(json.dumps(summary, indent=2))

    return text_path
