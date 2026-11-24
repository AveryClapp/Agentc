"""End-to-end tau2 driver on the mock provider ($0, no network)."""

from __future__ import annotations

from collections import Counter

import pytest

pytest.importorskip("tau2")

import agentc  # noqa: E402
from agentc.change_safety.records import ForkRecord, TrajectoryRecord, read_records  # noqa: E402

from bench.change_safety.mock_provider import MockProvider  # noqa: E402
from bench.change_safety.run import E0_PINS, build_catalog  # noqa: E402
from bench.change_safety.spend import SpendLedger  # noqa: E402
from bench.change_safety.tau2_driver import (  # noqa: E402
    Tau2Study,
    Tau2StudyConfig,
    current_actor,
    load_tasks,
    register_mock_provider,
)


@pytest.fixture
def study(tmp_path, study_env):
    register_mock_provider(MockProvider(actor=current_actor, noise=0.2))
    agentc.init(storage_path=str(tmp_path / "agentc"))
    config = Tau2StudyConfig(stage="E0", agent_model="mock/agent-strong", user_model="mock/user", max_steps=24)
    with Tau2Study(config=config, output=tmp_path, ledger=SpendLedger(tmp_path / "spend.jsonl", cap_usd=0.0), embed=False) as s:
        yield s, tmp_path
    agentc.shutdown()


def test_baseline_forks_and_rollouts(study):
    s, out = study
    catalog = build_catalog(E0_PINS, ["C0", "C1", "X2", "X3", "B2"])
    task = load_tasks()[0]
    base = s.run(task=task, repetition=0, arm="baseline", catalog=catalog)
    roll = s.run(task=task, repetition=0, arm="X3", catalog=catalog)
    records = read_records(str(out / "records.jsonl"))
    forks = [r for r in records if isinstance(r, ForkRecord)]
    trajectories = [r for r in records if isinstance(r, TrajectoryRecord)]
    assert [t.arm for t in trajectories] == ["baseline", "X3"]
    assert len(forks) == base.eligible_calls > 0
    assert [f.key.call_index for f in forks] == list(range(base.eligible_calls))
    assert set(Counter(f.arm for f in forks)) <= set(catalog)
    assert all(f.fork is not None for f in forks if f.change_applied)
    assert roll.changed_calls == roll.eligible_calls > 0
    # Agentc observed the reference calls under the recorded call-site ID, so
    # the OutputBudget rule (C6) can read that profile; nothing was rewritten.
    from agentc.change_safety.changes import apply_change, make_change

    request = {"model": "mock/agent-strong", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1024}
    c6 = apply_change(make_change("C6"), request, call_site_id=s.dispatcher.call_site)
    assert c6.applied, c6.ineligible_reason
    assert c6.evidence["profile"]["call_site_id"] == s.dispatcher.call_site
    assert c6.evidence["profile"]["n_observations"] >= base.eligible_calls


def test_refuses_without_study_environment(tmp_path, monkeypatch, study_env):
    monkeypatch.setenv("AGENTC_OPTIMIZE", "true")
    with pytest.raises(RuntimeError, match="must be off"):
        Tau2Study(
            config=Tau2StudyConfig(stage="E0", agent_model="mock/a", user_model="mock/u"),
            output=tmp_path,
            ledger=SpendLedger(tmp_path / "s.jsonl", cap_usd=0.0),
        )
