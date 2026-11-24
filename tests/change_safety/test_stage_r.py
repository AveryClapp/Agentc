"""Stage R converter on the real archived September protected study (no network)."""

from __future__ import annotations

import json
import subprocess
from collections import Counter

import pytest

from agentc.change_safety.records import ForkRecord, TrajectoryRecord, from_line, read_records, to_line
from bench.change_safety import stage_r

pytestmark = pytest.mark.skipif(
    subprocess.run(
        ["git", "-C", str(stage_r.REPO_ROOT), "rev-parse", "--verify", "-q", stage_r.DEFAULT_REF],
        capture_output=True,
    ).returncode
    != 0,
    reason=f"archive ref {stage_r.DEFAULT_REF} not available in this clone",
)

#: Census table in the archived README: (stage, rules, model label) -> pairs.
EXPECTED_PLAN_PAIRS = {
    ("filter", ("ContextCompress",), "Sonnet"): 20,
    ("filter", ("ModelDowngrade",), "Haiku"): 20,
    ("filter", ("ContextCompress", "ModelDowngrade"), "Haiku"): 20,
    ("synthesize", ("ModelDowngrade",), "Haiku"): 20,
    ("answer", ("StateDrop", "ModelDowngrade"), "Haiku"): 20,
    ("answer", ("ModelDowngrade",), "Haiku"): 16,
    ("answer", ("StateDrop",), "Sonnet"): 14,
}


@pytest.fixture(scope="module")
def data():
    return stage_r.load(stage_r.Archive())


@pytest.fixture(scope="module")
def converted(data):
    return stage_r.convert(data, embed=False)


def test_counts_match_readme_and_protocol(converted):
    records, report = converted
    forks = [r for r in records if isinstance(r, ForkRecord)]
    trajs = [r for r in records if isinstance(r, TrajectoryRecord)]
    assert report["tasks"] == 63
    assert report["archived_call_counts"] == {"source": 189, "probe": 130, "HR": 189}
    assert len(forks) == report["probe_pairs"]["converted"] == 130
    assert report["probe_pairs"]["skipped"] == 0
    assert report["probe_pairs"]["planned_candidates_without_probe_call"] == 0
    assert len(trajs) == 126
    assert Counter(t.arm for t in trajs) == {"baseline": 63, stage_r.FIXED_ARM: 63}
    assert len({t.task_id for t in trajs}) == 63
    assert {f.key.task_id for f in forks} <= {t.task_id for t in trajs}


def test_plan_arms_match_archived_census(converted):
    _, report = converted
    observed = {}
    for arm, info in report["arms"].items():
        (plan,) = info["plans"].values()
        assert arm == stage_r.plan_arm(plan["execution_plan_id"])
        observed[(plan["workflow_stage"], tuple(plan["rules"]), plan["model_label"])] = info["pairs"]
    assert observed == EXPECTED_PLAN_PAIRS


def test_no_aa_control_in_archive(converted):
    records, report = converted
    assert report["aa_control"]["present"] is False
    assert all(r.arm != "C0" for r in records if isinstance(r, ForkRecord))


def test_records_round_trip(converted):
    records, _ = converted
    for record in records:
        line = to_line(record)
        assert from_line(line) == record
        assert to_line(from_line(line)) == line


def test_rewards_and_outcomes(converted):
    records, report = converted
    trajs = [r for r in records if isinstance(r, TrajectoryRecord)]
    assert all(0.0 <= t.reward <= 1.0 for t in trajs)
    correct = Counter()
    for t in trajs:
        correct[t.arm] += t.reward
        assert t.model_calls == t.eligible_calls == 3
        assert t.tool_calls == 0
        assert t.changed_calls == (3 if t.arm == stage_r.FIXED_ARM else 0)
        assert t.input_tokens is not None and t.output_tokens is not None
    # README: plain Sonnet 58/63, HR 61/63.
    assert correct == {"baseline": 58.0, stage_r.FIXED_ARM: 61.0}
    assert report["trajectories"]["label_cross_check_mismatches"] == []
    assert report["trajectories"]["token_sum_mismatches_vs_analysis"] == []


def test_fork_responses_are_recorded_calls(converted, data):
    records, _ = converted
    calls = {row["id"]: row for row in data["calls"]}
    seen_probes = set()
    for fork in (r for r in records if isinstance(r, ForkRecord)):
        evidence = fork.change_evidence
        source, probe = calls[evidence["source_call_id"]], calls[evidence["probe_call_id"]]
        assert source["arm"] == "source" and probe["arm"] == "probe"
        assert source["pair_id"] == probe["pair_id"] == evidence["pair_id"]
        assert fork.fork is not None
        assert fork.baseline == stage_r.response_from_call(source)
        assert fork.fork == stage_r.response_from_call(probe)
        assert fork.baseline.text == source["answer"] and fork.fork.text == probe["answer"]
        assert fork.key.task_id == source["task_id"] == probe["task_id"]
        assert fork.key.call_index == stage_r.WORKFLOW_STAGES.index(source["workflow_stage"])
        assert probe["id"] not in seen_probes
        seen_probes.add(probe["id"])


def test_signals_recomputed_and_agree_with_archive(converted):
    records, report = converted
    for fork in (r for r in records if isinstance(r, ForkRecord)):
        s = fork.signals
        assert s.S3 is None  # embed=False
        assert s.S4 == 0.0  # text vs text on a tool-free workload
        archived = s.components["archived_divergence"]
        assert archived in (0.0, 1.0)
        # Archived binary divergence is set-level application disagreement;
        # normalized Jaccard ignores filter ID order, so the two agree here.
        assert (archived > 0) == (s.S2 > 0)
    assert report["archived_divergence_vs_recomputed_S2"] == {
        "archived=0,S2>0=False": 126,
        "archived=1,S2>0=True": 4,
    }


def test_fixed_composite_relabels_hr_components(data):
    records, report = stage_r.convert(data, embed=False, fixed_composite=True)
    forks = [r for r in records if isinstance(r, ForkRecord)]
    composite = [f for f in forks if f.arm == stage_r.FIXED_ARM]
    assert len(forks) == 130 and len(composite) == 60
    assert {f.change_identity for f in composite} == {report["fixed_arm"]["change_identity"]}
    assert Counter(f.key.call_index for f in composite) == {0: 20, 1: 20, 2: 20}


def test_run_writes_outputs(tmp_path):
    report = stage_r.run(tmp_path, embed=False)
    records = read_records(str(tmp_path / "records.jsonl"))
    assert len(records) == report["records_written"] == 256
    saved = json.loads((tmp_path / "conversion_report.json").read_text())
    assert saved["probe_pairs"]["converted"] == 130
    assert saved["development_evidence_only"] is True
