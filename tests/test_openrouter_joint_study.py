"""No-network prospective design, budget, and heldout isolation checks."""

from copy import deepcopy
import json
import os
from unittest.mock import Mock

import pytest

from bench import openrouter_joint_study as study
from bench import openrouter_rules_live as live
from bench.openrouter_matrix import write_json
from bench.openrouter_pilot import PilotError, ProviderFailure
from bench.openrouter_rules_protocol import workflow_call
import test_openrouter_rules_live as shared

experiment = shared.experiment  # shared fake fixture; never provider inference


def full_manifest():
    return {
        "schedule": live.schedule_for([str(i) for i in range(150)], {"0"}, 16, 115),
        "excluded_question_ids": ["0"],
        "policies": {},
        "limitations": [],
    }


def test_allocation_disjoint_counterbalanced_and_untouched():
    manifest = study.finalize_manifest(full_manifest(), {"schedule": []})
    study.validate_manifest(manifest)
    phases = {
        phase: {r["task_id"] for r in manifest["schedule"] if r["phase"] == phase}
        for phase in ("calibration", "warmup", "training", "heldout")
    }
    assert {phase: len(ids) for phase, ids in phases.items()} == {
        "calibration": 16,
        "warmup": 3,
        "training": 64,
        "heldout": 48,
    }
    assert sum(map(len, phases.values())) == len(set.union(*phases.values())) == 131
    assert len(manifest["schedule"]) * 3 == 2166
    assert manifest["stage_cap_usd"] == "15" and manifest["training_cap_usd"] == "9"


@pytest.mark.parametrize(
    "mutation", ["overlap", "order", "arm", "guard", "cap", "menu"]
)
def test_schedule_or_guard_tampering_rejected(mutation):
    manifest = study.finalize_manifest(full_manifest(), {})
    if mutation == "overlap":
        manifest["excluded_question_ids"].append(manifest["schedule"][-1]["task_id"])
    elif mutation == "order":
        manifest["schedule"] = list(reversed(manifest["schedule"]))
    elif mutation == "arm":
        manifest["schedule"][-1]["arm"] = "original"
        manifest["schedule"][-2]["arm"] = "original"
    elif mutation == "guard":
        manifest["policies"]["joint"]["AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE"] = "1"
    elif mutation == "cap":
        manifest["stage_cap_usd"] = "16"
    else:
        manifest["site_rule_menu"]["answer"].append("OutputBudget")
    with pytest.raises(PilotError):
        study.validate_manifest(manifest)


def test_restricted_menu_is_structural_even_for_large_downstream_outputs():
    attention = Mock()
    attention.compute_attention_scores.side_effect = lambda messages, _: (
        [0.0] * len(messages),
        [],
    )
    task = {
        "task_id": "x",
        "prompt": "Public?",
        "expected": "PRIVATE_GOLD",
        "meta": {"paragraphs": []},
    }
    histories = {
        "filter": {},
        "synthesize": {"filter": "long " * 10000},
        "answer": {"filter": "long " * 10000, "synthesize": "long " * 10000},
    }
    for stage in live.STAGES:
        original = workflow_call(task, stage, histories[stage], attention)
        call = study.restrict_call(original, stage)
        assert call["messages"] == original["messages"]
        assert "PRIVATE_GOLD" not in json.dumps(call)
        extra = call["parameters"]["extra"]
        assert ("attention_scores" in extra) == (stage == "filter")
        assert extra["window_state_reads"] == (
            []
            if stage == "filter"
            else ["filter_result"]
            if stage == "synthesize"
            else ["synthesis"]
        )
        bad = {"kind": "rewritten", "rule": "OutputBudget", "call": deepcopy(call)}
        with pytest.raises(PilotError, match="menu"):
            study.validate_plan(call, bad)


def test_guarded_policy_thresholds_unchanged_and_only_three_rules_available():
    for arm, settings in study.policies().items():
        previous = live.policy_settings(arm)
        assert (
            settings["AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE"]
            == previous["AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE"]
            == "20"
        )
        assert "AGENTC_SHADOW_DIVERGENCE_BUDGET" not in settings
        assert set(filter(None, settings["AGENTC_ENABLED_RULES"].split(","))) <= {
            "ContextCompress",
            "StateDrop",
            "ModelDowngrade",
        }


def gate_fixture(pairs=20, selected=True):
    identity = {
        "key": {"call_site_version": "site", "execution_plan_id": "combo"},
        "divergence_threshold": 0.01,
    }
    decisions, calls, schedule = [], [], []
    for index in range(21):
        task = str(index)
        schedule.append({"phase": "training", "task_id": task, "arm": "joint"})
        for stage in live.STAGES:
            primary = f"{task}/{stage}/primary"
            candidate = f"{task}/{stage}/candidate"
            paired = stage == "filter" and index < pairs
            combo = selected and stage == "filter" and index == 20
            signature = {
                "candidate": {"model": live.TARGET_MODEL} if paired else None,
                "candidate_identity": identity if paired else None,
                "primary_identity": identity if combo else {},
                "primary_rules": ["ContextCompress", "ModelDowngrade"] if combo else [],
            }
            ids = [primary, candidate] if paired else [primary]
            decisions.append(
                {
                    "phase": "training",
                    "task_id": task,
                    "arm": "joint",
                    "workflow_stage": stage,
                    "semantic_plan": signature,
                    "divergence_feedback": 0 if paired else None,
                    "activation": {"model_changed": combo},
                    "primary_id": primary,
                    "incurred_ids": ids,
                }
            )
            calls.extend({"id": row_id, "finish_reason": "stop"} for row_id in ids)
    return {"schedule": schedule, "policies": study.policies()}, decisions, calls


@pytest.mark.parametrize(
    "pairs,selected,expected", [(19, True, False), (20, False, False), (20, True, True)]
)
def test_training_gate_needs_admitted_primary_and_twenty_exact_pairs(
    pairs, selected, expected
):
    report = study.admission_gate(*gate_fixture(pairs, selected))
    assert report["proceed_to_heldout"] is expected
    assert report["task_gold_used"] is False and report["efficacy_claim"] is False


def test_gate_does_not_pool_distinct_exact_plan_identities():
    manifest, decisions, calls = gate_fixture()
    decisions[0]["semantic_plan"]["candidate_identity"] = {
        "key": {"call_site_version": "site", "execution_plan_id": "different"}
    }
    assert not study.admission_gate(manifest, decisions, calls)["proceed_to_heldout"]


def test_incomplete_training_cannot_open_heldout():
    manifest, decisions, calls = gate_fixture()
    with pytest.raises(PilotError, match="complete scheduled training"):
        study.admission_gate(manifest, decisions[:-1], calls)


@pytest.fixture
def small_study(experiment, monkeypatch):
    args, manifest, tasks, ledger, native = experiment
    for name, value in (
        ("CALIBRATION", 1),
        ("WARMUP", 3),
        ("TRAINING", 2),
        ("HELDOUT", 2),
    ):
        monkeypatch.setattr(study, name, value)
    manifest["schedule"] = live.schedule_for([t["task_id"] for t in tasks], set(), 1, 7)
    manifest["excluded_question_ids"] = []
    manifest = study.finalize_manifest(manifest, {})
    manifest["runtime"] = {"commit": "fake"}
    write_json(args.output / "manifest.json", manifest)
    monkeypatch.setattr(live, "runtime_sources", lambda *_: {"commit": "fake"})
    # Pure reference policy isolates journal/phase control from native behavior.
    monkeypatch.setattr(
        native, "optimize_plan", lambda _: json.dumps({"kind": "pass_through"})
    )
    return args, manifest, tasks, ledger, native


def test_failed_gate_stops_before_any_heldout_request(small_study):
    args, manifest, tasks, ledger, native = small_study
    result = live.run(args, "fake-key")
    assert result["training_gate"]["proceed_to_heldout"] is False
    assert result["schedule_complete"] is False
    assert all(meta["phase"] != "heldout" for _, meta in ledger.requests)
    heldout_ids = {
        r["task_id"] for r in manifest["schedule"] if r["phase"] == "heldout"
    }
    assert all(meta["task_id"] not in heldout_ids for _, meta in ledger.requests)


def test_heldout_freezes_native_feedback_and_charges_only_primaries(
    small_study, monkeypatch
):
    args, manifest, tasks, ledger, native = small_study
    monkeypatch.setattr(
        study,
        "admission_gate",
        lambda *_: {"proceed_to_heldout": True, "synthetic_test_only": True},
    )
    settings = []
    original_configure = native.optimize_configure

    def configure(*args, **kwargs):
        settings.append(dict(os.environ))
        return original_configure(*args, **kwargs)

    monkeypatch.setattr(native, "optimize_configure", configure)
    result = live.run(args, "fake-key")
    assert result["schedule_complete"]
    assert len(native.observations) == (3 + 2) * 6 * 3
    assert all(
        s["AGENTC_OPTIMIZE_EXPLORATION"] == "0"
        and s["AGENTC_OPTIMIZE_SHADOW"] == "0.02"
        for s in settings[-12:]
    )
    heldout = [meta for _, meta in ledger.requests if meta["phase"] == "heldout"]
    assert len(heldout) == 2 * 6 * 3 and all(m["scope"] == "primary" for m in heldout)


def test_heldout_candidate_is_rejected_before_paid_dispatch(small_study, monkeypatch):
    args, _, _, ledger, native = small_study
    at_heldout = False

    def gate(*_):
        nonlocal at_heldout
        at_heldout = True
        return {"proceed_to_heldout": True}

    monkeypatch.setattr(study, "admission_gate", gate)

    def plan(encoded):
        if at_heldout:
            return json.dumps(
                {
                    "kind": "pass_through",
                    "agentc_exploration_context": {"candidate_plan": {}},
                }
            )
        return json.dumps({"kind": "pass_through"})

    monkeypatch.setattr(native, "optimize_plan", plan)
    with pytest.raises(PilotError, match="heldout must not"):
        live.run(args, "fake-key")
    assert all(meta["phase"] != "heldout" for _, meta in ledger.requests)


def test_heldout_rewritten_journals_do_not_expect_suppressed_shadows(
    small_study, monkeypatch
):
    args, manifest, _, ledger, native = small_study

    def plan(encoded):
        call = json.loads(encoded)
        rules = os.environ["AGENTC_ENABLED_RULES"]
        if "ModelDowngrade" not in rules:
            return json.dumps({"kind": "pass_through"})
        selected = deepcopy(call)
        selected["model"] = live.TARGET_MODEL
        return json.dumps(
            {"kind": "rewritten", "rule": "ModelDowngrade", "call": selected}
        )

    monkeypatch.setattr(native, "optimize_plan", plan)
    monkeypatch.setattr(
        study, "admission_gate", lambda *_: {"proceed_to_heldout": True}
    )
    monkeypatch.setattr(live, "shadow_sample", lambda *_: True)
    live.run(args, "fake-key")
    heldout = [meta for _, meta in ledger.requests if meta["phase"] == "heldout"]
    assert heldout and all(meta["scope"] == "primary" for meta in heldout)
    count = len(ledger.requests)
    result = live.run(args, "fake-key")
    assert result["schedule_complete"] and len(ledger.requests) == count


def test_training_and_total_caps_are_applied_before_dispatch(small_study, monkeypatch):
    args, _, _, ledger, _ = small_study
    monkeypatch.setattr(
        study, "admission_gate", lambda *_: {"proceed_to_heldout": True}
    )
    original = ledger.call
    seen = []

    def call(key, call_id, stage, cap, payload, metadata):
        seen.append((metadata["phase"], str(cap)))
        return original(key, call_id, stage, cap, payload, metadata)

    monkeypatch.setattr(ledger, "call", call)
    live.run(args, "fake-key")
    assert seen and all(
        cap == ("15" if phase == "heldout" else "9") for phase, cap in seen
    )


def test_transient_error_retries_exact_dispatch_before_native_observation(
    small_study, monkeypatch
):
    args, _, _, ledger, native = small_study
    args.max_calls = 1
    original = ledger.call
    attempted = []

    def call(*args):
        attempted.append(deepcopy(args))
        if len(attempted) == 1:
            raise ProviderFailure("timeout", {"kind": "timeout", "retryable": True})
        return original(*args)

    monkeypatch.setattr(ledger, "call", call)
    monkeypatch.setattr(live.time, "sleep", lambda _: None)
    result = live.run(args, "fake-key")
    assert attempted[0] == attempted[1] and len(attempted) == 2
    assert result["completed_calls"] == 1 and not native.observations  # calibration
