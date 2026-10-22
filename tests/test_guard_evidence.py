"""Raw owned receipts, retained windows and complete-plan identity cannot mix."""

import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench.campaign_accounting import digest
from bench.campaign_transport import CampaignStopped
from bench.frozen_policy_preflight import configuration
from bench.guard_evidence import (
    MAX_AGE_US,
    TABLES,
    GuardEvidenceError,
    audit_checkpoint,
    collect_checkpoint,
    compare_receipt_outputs,
    conformal_upper_p95,
    read_closed_checkpoint,
    strict_json,
)
from bench.policy_calibration import configuration_sha256

MODE = "best_static_joint"
CFG = configuration(MODE)
LOCK = "a" * 64
IDENTITY = {"call_site_version": "b" * 64, "execution_plan_id": "c" * 64}
RUNTIME = {
    "provider_protocol": "litellm.completion.v1",
    "target_model_id": CFG["cheap_model"],
    "target_model_version": CFG["cheap_model"],
    "price_table_version": "explicit-unit-price-table",
}


def fixture():
    """Unit records only; actual native acquisition is the separate E0 control."""
    context = {
        "key": IDENTITY,
        "runtime_version": RUNTIME,
        "divergence_threshold": 0.05,
    }
    plan = {
        "kind": "rewritten",
        "rule": "OutputBudget",
        "agentc_observation_context": context,
        "agentc_selection_audit": {
            "planner_mode": MODE,
            "policy": {
                "configuration_sha256": configuration_sha256(CFG),
                "calibration_lock_sha256": LOCK,
            },
        },
    }
    raw_plan = json.dumps(plan)
    tables = {table: [] for table in TABLES}
    tables["execution_plan_profile"] = [
        {
            **IDENTITY,
            **RUNTIME,
            "n_observations": 20,
            "n_paired_observations": 20,
            "window_observations": 20,
            "paired_observations": 20,
            "divergence_upper_p95": 0.0,
            "last_paired_at": 120,
            "updated_at": 120,
        }
    ]
    tables["execution_plan_guard"] = [
        {
            **IDENTITY,
            **RUNTIME,
            "divergence_threshold": 0.05,
            "divergence_exposure": 0.0,
            "window_samples": 0,
            "updated_at": 120,
        }
    ]
    calls, comparisons = [], {}
    for i in range(1, 21):
        tables["execution_plan_observation"].append(
            {
                **IDENTITY,
                **RUNTIME,
                "sample_sequence": i,
                "observed_at": 100 + i,
                "input_tokens": 100,
                "output_tokens": 50,
                "cost_usd": 0.00123,
            }
        )
        tables["execution_plan_divergence_observation"].append(
            {
                **IDENTITY,
                **RUNTIME,
                "sample_sequence": i,
                "plan_observation_sequence": i,
                "observed_at": 100 + i,
                "divergence": 0.0,
            }
        )
        for kind in ("primary", "shadow"):
            call_id = f"{kind}-{i}"
            metadata = {
                "actor": "agent",
                "kind": kind,
                "task_id": "unit",
                "arm": MODE,
                "cell_id": f"cell-{i}",
                "turn_id": "turn",
                "requested_model": CFG["strong_model"],
            }
            if kind == "primary":
                metadata["native_plan_json"] = raw_plan
            else:
                metadata.update(
                    parent_native_plan_json=raw_plan,
                    parent_observation_token=json.dumps(
                        {
                            "guard_eligible": True,
                            "divergence_threshold": 0.05,
                            "plan_observation": {
                                "key": IDENTITY,
                                "runtime_version": RUNTIME,
                                "sequence": i,
                            },
                        }
                    ),
                )
                comparisons[call_id] = 0.0
            model = CFG["cheap_model"] if kind == "primary" else CFG["strong_model"]
            body = {
                "model": model,
                "messages": [{"role": "user", "content": "unit"}],
                "max_tokens": 128,
            }
            usage = {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cost": "0.00123",
            }
            response = {"id": call_id, "usage": usage}
            bill = {
                "id": call_id,
                "attempt_id": call_id,
                "metadata": metadata,
                "reserve_sha256": "d" * 64,
                "usage": usage,
                "model": model,
                "cost_usd": "0.00123",
                "latency_ms": 12.0,
                "router_metadata": {"attempt": 1},
                "generation_id": call_id,
                "session_id": "unit-session",
            }
            calls.append(
                {
                    "call_id": call_id,
                    "attempt_id": call_id,
                    "call_index": len(calls),
                    "metadata": metadata,
                    "request": body,
                    "request_digest": digest(body),
                    "response_digest": digest(response),
                    "http_receipt": {
                        "id": call_id,
                        "attempt_id": call_id,
                        "reserve_sha256": "d" * 64,
                        "response": response,
                    },
                    "billed_result": bill,
                    "planning": None,
                    "planning_cost_attributed_to_this_call": False,
                    "candidate_count": None,
                }
            )
    return {"database_sha256": "e" * 64, "tables": tables}, calls, comparisons


def audit(checkpoint, calls, comparisons, *, now_us=120):
    return audit_checkpoint(
        checkpoint,
        calls,
        mode=MODE,
        configuration=CFG,
        calibration_lock_sha256=LOCK,
        session_id="unit-session",
        now_us=now_us,
        comparison_values=comparisons,
    )


def test_retained_pairs_are_receipt_joined_but_never_an_admission():
    checkpoint, calls, compared = fixture()
    result = audit(checkpoint, calls, compared)
    assert result["plans"][0]["paired_window_qualifies"] is True
    assert result["calibration_admission"] is False
    assert result["physical_calls"] == 40
    assert result["all_call_cost_usd"] == "0.04920"
    assert result["shadow_cost_usd"] == "0.02460"
    assert "task_damage_frontier" in result["unmeasured_requirements"]


def test_lifetime_count_cannot_replace_missing_retained_samples():
    checkpoint, calls, compared = fixture()
    tables = checkpoint["tables"]
    tables["execution_plan_divergence_observation"].pop()
    profile = tables["execution_plan_profile"][0]
    profile.update(paired_observations=19, last_paired_at=119)
    result = audit(checkpoint, calls, compared)
    assert result["plans"][0]["lifetime_paired_observations"] == 20
    assert result["plans"][0]["paired_window_qualifies"] is False
    assert "insufficient_retained_pairs" in result["plans"][0]["rejection_reasons"]


def test_unpaired_outcome_eviction_does_not_erase_retained_safety_evidence():
    checkpoint, calls, compared = fixture()
    checkpoint["tables"]["execution_plan_observation"].clear()
    checkpoint["tables"]["execution_plan_profile"][0]["window_observations"] = 0
    assert (
        audit(checkpoint, calls, compared)["plans"][0]["retained_paired_observations"]
        == 20
    )


def test_stale_pair_window_cannot_qualify():
    checkpoint, calls, compared = fixture()
    result = audit(checkpoint, calls, compared, now_us=121 + MAX_AGE_US)
    assert result["plans"][0]["paired_window_qualifies"] is False
    assert "paired_window_stale_or_missing" in result["plans"][0]["rejection_reasons"]


def test_noneligible_user_and_judge_costs_do_not_supply_native_samples():
    checkpoint, calls, compared = fixture()
    for actor in ("user", "judge"):
        call = deepcopy(calls[0])
        metadata = call["metadata"]
        metadata.update(actor=actor, turn_id=actor)
        metadata.pop("native_plan_json")
        call.update(call_id=actor, attempt_id=actor, call_index=len(calls))
        for envelope in (call["billed_result"], call["http_receipt"]):
            envelope.update(id=actor, attempt_id=actor)
        calls.append(call)
    result = audit(checkpoint, calls, compared)
    assert result["physical_calls"] == 42
    assert result["all_call_cost_usd"] == "0.05166"
    assert result["plans"][0]["retained_paired_observations"] == 20


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-shadow",
        "receipt-cost",
        "different-runtime",
        "wrong-summary",
        "wrong-sequence",
        "wrong-mode",
        "wrong-policy",
        "duplicate-receipt",
        "unowned-comparison",
        "orphan-profile",
        "different-divergence",
        "missing-table",
        "missing-outcome",
        "future-pair",
    ],
)
def test_inconsistent_native_or_physical_evidence_is_rejected(mutation):
    checkpoint, calls, compared = fixture()
    tables = checkpoint["tables"]
    if mutation == "missing-shadow":
        compared.pop(calls.pop()["attempt_id"])
    elif mutation == "receipt-cost":
        calls[0]["billed_result"]["cost_usd"] = "0"
    elif mutation == "different-runtime":
        tables["execution_plan_divergence_observation"][0]["target_model_version"] = (
            "other"
        )
    elif mutation == "wrong-summary":
        tables["execution_plan_profile"][0]["divergence_upper_p95"] = 0.05
    elif mutation == "wrong-sequence":
        token = json.loads(calls[1]["metadata"]["parent_observation_token"])
        token["plan_observation"]["sequence"] = 21
        calls[1]["metadata"]["parent_observation_token"] = json.dumps(token)
    elif mutation == "wrong-mode":
        calls[0]["metadata"]["arm"] = "route_then_rewrite"
    elif mutation == "wrong-policy":
        plan = json.loads(calls[0]["metadata"]["native_plan_json"])
        plan["agentc_selection_audit"]["policy"]["configuration_sha256"] = "f" * 64
        calls[0]["metadata"]["native_plan_json"] = json.dumps(plan)
    elif mutation == "duplicate-receipt":
        calls.append(deepcopy(calls[-1]))
    elif mutation == "unowned-comparison":
        compared["extra"] = 0.0
    elif mutation == "orphan-profile":
        p = deepcopy(tables["execution_plan_profile"][0])
        p["execution_plan_id"] = "f" * 64
        tables["execution_plan_profile"].append(p)
    elif mutation == "different-divergence":
        compared["shadow-1"] = 0.5
    elif mutation == "missing-table":
        tables.pop("execution_plan_guard")
    elif mutation == "missing-outcome":
        tables["execution_plan_observation"].pop()
    else:
        tables["execution_plan_divergence_observation"][0]["observed_at"] = 121
    with pytest.raises((GuardEvidenceError, CampaignStopped)):
        audit(checkpoint, calls, compared)


def test_conformal_upper_quantile_matches_native_small_and_fifty_sample_rule():
    assert conformal_upper_p95([]) is None
    assert conformal_upper_p95([0.1, 0.5]) == 0.5
    assert conformal_upper_p95([i / 50 for i in range(50)]) == 48 / 50


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_ambiguous_or_nonfinite_evidence_json_is_rejected(text):
    with pytest.raises(GuardEvidenceError):
        strict_json(text)


def test_unsettled_database_is_not_read_as_a_closed_checkpoint(tmp_path):
    db = tmp_path / "cost_model.db"
    db.write_bytes(b"unit")
    Path(str(db) + "-wal").write_bytes(b"unsettled")
    with pytest.raises(GuardEvidenceError, match="unsettled"):
        read_closed_checkpoint(db)


def reply(text):
    return {
        "choices": [
            {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ]
    }


def test_sdk_comparator_reconstructs_actual_text_and_shell_actions_without_execution():
    cases = [
        {
            "candidate": reply("same answer"),
            "reference": reply("same answer"),
            "divergence_mode": "lexical",
            "text_action_contract": None,
        },
        {
            "candidate": reply("Kansas"),
            "reference": reply("Texas"),
            "divergence_mode": "normalized",
            "text_action_contract": None,
        },
        {
            "candidate": reply("prose\n```bash\nprintf x\n```"),
            "reference": reply("```bash\nprintf x\n```"),
            "divergence_mode": "lexical",
            "text_action_contract": {
                "settings_version": "text-action-settings-v1:" + "a" * 64
            },
        },
    ]
    assert compare_receipt_outputs(cases, python=Path(sys.executable)) == [
        0.0,
        1.0,
        0.0,
    ]


def test_embedding_comparison_without_model_provenance_cannot_be_reconstructed():
    with pytest.raises(GuardEvidenceError, match="model evidence"):
        compare_receipt_outputs(
            [{"divergence_mode": "embedding"}], python=Path(sys.executable)
        )


def collector_fixture(monkeypatch):
    from bench import guard_evidence

    checkpoint, calls, comparisons = fixture()
    events = []
    for call in calls:
        events.extend(
            [
                {
                    "event": "reserve",
                    "session_id": "unit-session",
                    "attempt_id": call["attempt_id"],
                    "metadata": deepcopy(call["metadata"]),
                    "request": deepcopy(call["request"]),
                },
                {**deepcopy(call["http_receipt"]), "event": "response"},
                {**deepcopy(call["billed_result"]), "event": "result"},
            ]
        )
    for call in calls:
        call["http_receipt"]["event"] = "response"
        call["billed_result"]["event"] = "result"
    snapshot = SimpleNamespace(
        events=events,
        sha256="f" * 64,
        byte_count=1234,
        state={"holds_usd": "12.275019", "pending_usd": "0"},
    )
    monkeypatch.setattr(guard_evidence, "read_snapshot", lambda path: snapshot)
    monkeypatch.setattr(
        guard_evidence, "read_closed_checkpoint", lambda path: checkpoint
    )
    monkeypatch.setattr(
        guard_evidence,
        "compare_receipt_outputs",
        lambda *a, **k: list(comparisons.values()),
    )
    return calls, snapshot


def collect(calls, *, sdk=None):
    return collect_checkpoint(
        Path("explicit-unit-native-fixture"),
        calls,
        ledger=Path("explicit-unit-journal-fixture"),
        python=Path(sys.executable),
        expected_sdk_files={"explicit-unit-sdk": "unit"} if sdk is None else sdk,
        mode=MODE,
        configuration=CFG,
        calibration_lock_sha256=LOCK,
        session_id="unit-session",
        now_us=120,
    )


def test_collector_retains_global_historical_uncertainty_while_joining_settled_owned_calls(
    monkeypatch,
):
    calls, snapshot = collector_fixture(monkeypatch)
    result = collect(calls)
    assert result["audit"]["journal_sha256"] == snapshot.sha256
    assert snapshot.state["holds_usd"] == "12.275019"
    assert result["audit"]["calibration_admission"] is False


@pytest.mark.parametrize(
    "mutation",
    ["request", "response", "bill", "missing-reserve", "missing-http", "missing-sdk"],
)
def test_collector_rejects_receipts_that_do_not_match_the_journal(
    monkeypatch, mutation
):
    calls, snapshot = collector_fixture(monkeypatch)
    if mutation == "request":
        calls[0]["request"]["messages"][0]["content"] = "changed"
    elif mutation == "response":
        calls[0]["http_receipt"]["response"]["id"] = "changed"
    elif mutation == "bill":
        calls[0]["billed_result"]["cost_usd"] = "0"
    elif mutation == "missing-reserve":
        snapshot.events.pop(0)
    elif mutation == "missing-http":
        snapshot.events.pop(1)
    with pytest.raises(GuardEvidenceError):
        collect(calls, sdk={} if mutation == "missing-sdk" else None)


def test_comparison_sdk_must_match_the_enrollment():
    with pytest.raises(GuardEvidenceError, match="SDK differs"):
        compare_receipt_outputs(
            [], python=Path(sys.executable), expected_sdk_files={"foreign": "0" * 64}
        )


def test_comparator_keeps_tool_arguments_and_ignores_only_volatile_call_ids():
    def tool(value, call_id):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "type": "function",
                                "id": call_id,
                                "function": {
                                    "name": "get_order_details",
                                    "arguments": json.dumps({"order_id": value}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    first = tool("#1", "volatile-1")
    common = {
        "divergence_mode": "lexical",
        "text_action_contract": None,
        "candidate": first,
    }
    assert compare_receipt_outputs(
        [
            {**common, "reference": tool("#1", "volatile-2")},
            {**common, "reference": tool("#2", "volatile-3")},
        ],
        python=Path(sys.executable),
    ) == [0.0, 1.0]


def test_comparator_preserves_anthropic_tool_input_objects():
    common = {
        "content": [
            {
                "type": "tool_use",
                "id": "volatile",
                "name": "get_order_details",
                "input": {"order_id": "#1"},
            }
        ]
    }
    assert compare_receipt_outputs(
        [
            {
                "candidate": common,
                "reference": deepcopy(common),
                "divergence_mode": "lexical",
                "text_action_contract": None,
            }
        ],
        python=Path(sys.executable),
    ) == [0.0]
