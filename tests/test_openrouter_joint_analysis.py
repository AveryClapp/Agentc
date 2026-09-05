"""Synthetic complete and stopped study journals; no API or native runtime."""

from copy import deepcopy
from decimal import Decimal
import hashlib

import pytest

from bench import openrouter_joint_analysis as analysis
from bench import openrouter_joint_study as study
from bench import openrouter_rules_live as live
from bench.openrouter_matrix import ROOT, file_hash, load_module, write_json
from bench.openrouter_pilot import PilotError, canonical, digest


@pytest.fixture(scope="module")
def full_data(tmp_path_factory):
    directory = tmp_path_factory.mktemp("joint-analysis-synthetic")
    tasks = [
        {
            "task_id": str(i),
            "prompt": "Public question " + str(i),
            "expected": "correct " + str(i),
            "meta": {
                "paragraphs": [{"title": "Public", "sentences": ["Public source."]}]
            },
        }
        for i in range(150)
    ]
    fixture = directory / "fixture.json"
    write_json(fixture, tasks)
    endpoints = {
        model: {
            "tag": "anthropic",
            "provider_name": "Anthropic",
            "name": "Anthropic | " + model,
            "pricing": {
                "prompt": "0.000001" if model == live.TARGET_MODEL else "0.000003",
                "completion": "0.000005",
            },
        }
        for model in (live.SOURCE_MODEL, live.TARGET_MODEL)
    }
    manifest = study.finalize_manifest(
        {
            "schedule": live.schedule_for(
                [t["task_id"] for t in tasks], {"0"}, 16, 115
            ),
            "excluded_question_ids": ["0"],
            "source_files": live.sources(),
            "native_sha256": "0" * 64,
            "fixture_sha256": file_hash(fixture),
            "prompts": live.prompt_constants(),
            "risk_margin": 0.02,
            "endpoints": endpoints,
            "scoring": "unchanged_normalized_raw_answer_EM_F1",
            "shadow_seed": "synthetic",
        },
        {},
    )
    by_task = {t["task_id"]: t for t in tasks}
    attention = load_module(
        "joint_analysis_test_attention", ROOT / "python/agentc/_attention.py"
    )
    stage_id = "rules-live-dev-v1-" + digest(manifest)[:20]
    calls, decisions, intents, ledger = [], [], [], []
    pairs = 0
    for workflow in manifest["schedule"]:
        history = {}
        arm, phase, task_id = (workflow[k] for k in ("arm", "phase", "task_id"))
        model = (
            arm.removeprefix("calibration/")
            if phase == "calibration"
            else live.TARGET_MODEL
            if arm == "sequential"
            else live.SOURCE_MODEL
        )
        for stage in live.STAGES:
            item = {**workflow, "workflow_stage": stage}
            call = study.restrict_call(
                live.workflow_call(
                    by_task[task_id],
                    stage,
                    history,
                    attention,
                    model=model,
                    prompts=manifest["prompts"],
                    run_identity=stage_id + "/" + arm,
                ),
                stage,
            )
            context = {
                "key": {"call_site_version": stage, "execution_plan_id": "reference"},
                "divergence_threshold": 0.05,
                "runtime_version": "synthetic",
                "call_site_id": call["call_site_id"],
            }
            plan = {"kind": "pass_through", "agentc_observation_context": context}
            if (
                arm == "joint"
                and phase in {"training", "heldout"}
                and stage == "filter"
            ):
                routed = deepcopy(call)
                routed["model"] = live.TARGET_MODEL
                routed["messages"] = [call["messages"][0], call["messages"][-1]]
                joint = {
                    "kind": "composed",
                    "call": routed,
                    "rules": [{"rule": "ContextCompress"}, {"rule": "ModelDowngrade"}],
                    "agentc_observation_context": {
                        **context,
                        "key": {
                            "call_site_version": stage,
                            "execution_plan_id": "joint",
                        },
                        "divergence_threshold": 0.02,
                    },
                }
                if pairs < 20:
                    plan["agentc_exploration_context"] = {
                        "candidate_plan": joint,
                        "lease_token": "synthetic-" + str(pairs),
                    }
                    pairs += 1
                else:
                    plan = joint
            signature = live.semantic_plan(call, plan, manifest)
            intents.append(
                {
                    **item,
                    "original_call": call,
                    "native_plan": plan,
                    "semantic_plan": signature,
                }
            )
            dispatches = [
                (
                    "calibration" if phase == "calibration" else "primary",
                    signature["primary"],
                )
            ]
            if signature["candidate"] is not None:
                dispatches.append(("exploration", signature["candidate"]))
            elif (
                phase != "heldout"
                and plan["kind"] != "pass_through"
                and live.shadow_sample(
                    manifest["shadow_seed"], arm + "/" + stage, task_id, 0.02
                )
            ):
                dispatches.append(("shadow", live.payload_for(call, manifest)))
            ids = []
            for scope, payload in dispatches:
                call_id = stage_id + "-" + digest([item, scope])[:24]
                ids.append(call_id)
                meta = analysis.metadata(item, scope, signature, payload, manifest)
                fingerprint = digest(
                    {"payload": payload, "metadata": meta, "stage": stage_id}
                )
                tokens = {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "cost": "0.001",
                    "prompt_tokens_details": {"cached_tokens": 0},
                    "is_byok": False,
                }
                router = {
                    "requested": payload["model"],
                    "attempt": 1,
                    "is_byok": False,
                    "endpoints": {
                        "available": [
                            {
                                "selected": True,
                                "provider": "Anthropic",
                                "model": payload["model"],
                            }
                        ]
                    },
                }
                answer = (
                    "correct " + task_id
                    if stage == "answer"
                    else arm + "/" + task_id + "/" + stage
                )
                result = {
                    "event": "result",
                    "id": call_id,
                    "stage": stage_id,
                    "attempt_id": "a-" + call_id,
                    "fingerprint": fingerprint,
                    "cost_usd": "0.001",
                    "latency_ms": 10,
                    "model": payload["model"],
                    "provider": "Anthropic",
                    "answer": answer,
                    "finish_reason": "stop",
                    "usage": tokens,
                    "generation_id": "g-" + call_id,
                    "router_metadata": router,
                    "metadata": meta,
                    "paper_evidence": False,
                }
                reserve = {
                    "event": "reserve",
                    "id": call_id,
                    "stage": stage_id,
                    "attempt_id": result["attempt_id"],
                    "fingerprint": fingerprint,
                    "upper_cost_usd": "0.01",
                    "request": payload,
                    "metadata": meta,
                }
                response = {
                    "event": "response",
                    "id": call_id,
                    "attempt_id": result["attempt_id"],
                    "response": {
                        "id": result["generation_id"],
                        "model": result["model"],
                        "provider": result["provider"],
                        "usage": tokens,
                        "openrouter_metadata": router,
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": answer},
                            }
                        ],
                    },
                }
                ledger.extend([reserve, response, result])
                nominal = (
                    Decimal(endpoints[payload["model"]]["pricing"]["prompt"]) * 20
                    + Decimal("0.000005") * 10
                )
                calls.append(
                    {
                        **item,
                        "scope": scope,
                        **{k: v for k, v in result.items() if k != "metadata"},
                        "request_sha256": digest(payload),
                        "decision_sha256": digest(signature),
                        "cached_input_tokens": 0,
                        "nominal_uncached_cost_usd": str(nominal),
                    }
                )
            active = live.activation(call, plan)
            active["executed_on_provider"] = True
            decisions.append(
                {
                    **item,
                    "native_plan": plan,
                    "semantic_plan": signature,
                    "primary_id": ids[0],
                    "incurred_ids": ids,
                    "activation": active,
                    "divergence_feedback": 0.0 if len(ids) == 2 else None,
                }
            )
            history[stage] = calls[-len(ids)]["answer"]
    gate = study.admission_gate(manifest, decisions, calls)
    assert gate["proceed_to_heldout"]
    static = live.static_selection(decisions, calls, by_task, manifest)
    summary = {
        "schedule_complete": True,
        "manifest_sha256": digest(manifest),
        "calls_sha256": digest(calls),
        "decisions_sha256": digest(decisions),
        "training_gate": gate,
    }
    return dict(
        manifest=manifest,
        decisions=decisions,
        calls=calls,
        intents=intents,
        fixture=fixture,
        expected_manifest_sha256=digest(manifest),
        gate=gate,
        static=static,
        summary=summary,
        ledger_events=ledger,
        ledger_sha256=digest(ledger),
        repetitions=100,
    )


def run(data):
    return analysis.analyze(**data)


def test_complete_all48_all6_with_paired_intervals_and_calibration_once(full_data):
    report = run(full_data)
    assert report["comparison_available"] and report["efficacy_claim"] is False
    assert report["recorded_decisions"] == 2166
    assert len(report["paired_questions"]) == 48 and len(report["comparisons"]) == 5
    assert all(len(p["arms"]) == 6 for p in report["paired_questions"])
    assert all(
        c["effects"]["f1"]["descriptive_95_percentile_interval"] == [0, 0]
        for c in report["comparisons"]
    )
    seq = next(r for r in report["costs_by_arm"] if r["arm"] == "sequential")
    calibration = next(
        r
        for r in seq["by_phase_and_scope"]
        if (r["phase"], r["scope"]) == ("calibration", "calibration")
    )
    assert calibration["records"] == 96 and Decimal(
        calibration["known_usd"]
    ) == Decimal(".096")
    total = sum(Decimal(r["all_phases"]["known_usd"]) for r in report["costs_by_arm"])
    assert total == Decimal(report["financial_snapshot"]["known_stage_usd"])


@pytest.mark.parametrize("kind", ["prefix", "no_ledger", "gate", "summary", "static"])
def test_incomplete_or_unverified_never_selects_favorable_intersection(full_data, kind):
    data = deepcopy(full_data)
    if kind == "prefix":
        data["decisions"].pop()
        data["intents"].pop()
        data["calls"].pop()
        data["ledger_events"] = data["ledger_events"][:-3]
    elif kind == "no_ledger":
        data["ledger_events"] = None
    else:
        data[kind] = None
    report = run(data)
    assert not report["comparison_available"]
    assert report["paired_questions"] == report["comparisons"] == []


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate",
        "missing_call",
        "phase",
        "request_hash",
        "nominal",
        "dispatch",
        "history",
        "activation",
    ],
)
def test_corrupt_journals_fail_closed(full_data, kind):
    data = deepcopy(full_data)
    if kind == "duplicate":
        data["calls"][-1]["generation_id"] = data["calls"][-2]["generation_id"]
    elif kind == "missing_call":
        data["calls"].pop(100)
    elif kind == "phase":
        data["intents"][-1]["phase"] = "training"
    elif kind == "request_hash":
        data["calls"][-1]["request_sha256"] = "changed"
    elif kind == "nominal":
        data["calls"][-1]["nominal_uncached_cost_usd"] = "0"
    elif kind == "dispatch":
        data["calls"][-1]["router_metadata"]["attempt"] = 2
    elif kind == "activation":
        data["decisions"][-1]["activation"]["model_changed"] = True
    else:
        data["intents"][-1]["original_call"]["messages"][1]["content"] = (
            "another arm's history"
        )
    with pytest.raises(PilotError):
        run(data)


def test_manifest_and_fixture_hashes_are_external_gates(full_data, tmp_path):
    data = deepcopy(full_data)
    data["expected_manifest_sha256"] = "wrong"
    with pytest.raises(PilotError, match="manifest"):
        run(data)
    data = deepcopy(full_data)
    data["fixture"] = tmp_path / "wrong.json"
    write_json(data["fixture"], [])
    with pytest.raises(PilotError, match="fixture"):
        run(data)


def test_provider_error_suppresses_all_scores_but_keeps_spend(full_data):
    data = deepcopy(full_data)
    data["calls"][-1]["finish_reason"] = "error"
    data["ledger_events"][-1]["finish_reason"] = "error"
    data["ledger_events"][-2]["response"]["choices"][0]["finish_reason"] = "error"
    report = run(data)
    assert not report["comparison_available"] and report["paired_questions"] == []
    assert "provider_failure_contaminates_results" in report["suppression_reasons"]
    assert Decimal(report["financial_snapshot"]["known_stage_usd"]) > 0


def test_missing_ledger_result_and_unrecorded_paid_result_are_visible(full_data):
    data = deepcopy(full_data)
    data["ledger_events"] = data["ledger_events"][:-1]
    report = run(data)
    assert not report["comparison_available"]
    assert report["financial_snapshot"]["artifact_ids_missing_from_ledger"] == [
        data["calls"][-1]["id"]
    ]
    data = deepcopy(full_data)
    data["decisions"].pop()
    data["calls"].pop()
    report = run(data)
    assert report["financial_snapshot"]["unincorporated_completed_ids"]
    assert not report["comparison_available"]


def test_failed_attempt_known_charge_plus_full_residual_not_double_counted(full_data):
    data = deepcopy(full_data)
    reserve = deepcopy(data["ledger_events"][-3])
    reserve["attempt_id"] = "failed-first-attempt"
    body = {
        "event": "response",
        "id": reserve["id"],
        "attempt_id": reserve["attempt_id"],
        "response": {"usage": {"cost": ".003"}},
    }
    failure = {
        "event": "attempt_failure",
        "id": reserve["id"],
        "stage": reserve["stage"],
        "attempt_id": reserve["attempt_id"],
        "reserve_sha256": digest(reserve),
        "budget_hold_usd": ".01",
        "reported_cost_usd": ".003",
    }
    data["ledger_events"][-3:-3] = [reserve, body, failure]
    report = run(data)
    finance = report["financial_snapshot"]
    assert report[
        "comparison_available"
    ]  # valid successful retry; failed text never observed
    assert Decimal(finance["retained_uncertainty_usd"]) == Decimal(".007")
    assert Decimal(finance["known_stage_usd"]) == sum(
        Decimal(r["cost_usd"]) for r in data["calls"]
    ) + Decimal(".003")
    assert finance["additional_attempt_records"][0]["reported_cost_known"] is True
    assert Decimal(finance["conservative_stage_commitment_usd"]) == sum(
        Decimal(r["cost_usd"]) for r in data["calls"]
    ) + Decimal(".01")


def test_paired_bootstrap_resamples_question_not_arms():
    rows = [{"joint_minus_control": 0.0, "other_effect": float(i)} for i in range(48)]
    report = analysis.paired_bootstrap(rows, 100)
    assert report == analysis.paired_bootstrap(rows, 100)
    assert report["joint_minus_control"]["descriptive_95_percentile_interval"] == [0, 0]
    assert report["other_effect"]["mean"] == 23.5


def test_read_only_ledger_snapshot_is_hash_bound_and_does_not_change_bytes(tmp_path):
    path = tmp_path / "ledger.jsonl"
    events = [{"event": "init", "key_id": "synthetic"}]
    raw = b"".join(canonical(e) + b"\n" for e in events)
    path.write_bytes(raw)
    assert analysis.read_ledger_snapshot(path) == (
        events,
        hashlib.sha256(raw).hexdigest(),
    )
    assert path.read_bytes() == raw


def test_empty_prefix_has_no_scores_or_financial_certification(full_data):
    data = deepcopy(full_data)
    for key in ("calls", "decisions", "intents"):
        data[key] = []
    data.update(ledger_events=None, summary=None, gate=None, static=None)
    report = run(data)
    assert report["recorded_calls"] == 0
    assert report["paired_questions"] == []
    assert report["financial_snapshot"] is None


def test_changed_imported_source_and_duplicate_fixture_rejected(full_data, tmp_path):
    data = deepcopy(full_data)
    data["manifest"]["source_files"] = {}
    data["expected_manifest_sha256"] = digest(data["manifest"])
    with pytest.raises(PilotError, match="dependencies"):
        run(data)
    data = deepcopy(full_data)
    import json

    tasks = json.loads(data["fixture"].read_text())
    tasks.append(tasks[0])
    data["fixture"] = tmp_path / "duplicate.json"
    write_json(data["fixture"], tasks)
    data["manifest"]["fixture_sha256"] = file_hash(data["fixture"])
    data["expected_manifest_sha256"] = digest(data["manifest"])
    with pytest.raises(PilotError, match="duplicate fixture"):
        run(data)


def test_durable_error_envelope_with_normal_stop_is_never_scored(full_data):
    data = deepcopy(full_data)
    data["ledger_events"][-2]["response"]["error"] = {"message": "PRIVATE_ERROR_BODY"}
    report = run(data)
    assert not report["comparison_available"] and report["comparisons"] == []
    assert "PRIVATE_ERROR_BODY" not in str(report)


def test_failed_attempt_unknown_charge_retains_full_bound_and_campaign_partition(
    full_data,
):
    data = deepcopy(full_data)
    reserve = deepcopy(data["ledger_events"][-3])
    reserve["attempt_id"] = "unknown-charge-attempt"
    failure = {
        "event": "attempt_failure",
        "id": reserve["id"],
        "stage": reserve["stage"],
        "attempt_id": reserve["attempt_id"],
        "reserve_sha256": digest(reserve),
        "budget_hold_usd": ".01",
        "reported_cost_usd": None,
    }
    data["ledger_events"][-3:-3] = [reserve, failure]
    other = {
        **reserve,
        "id": "earlier-campaign-call",
        "stage": "earlier-campaign-stage",
        "attempt_id": "earlier-attempt",
    }
    other_failure = {
        **failure,
        "id": other["id"],
        "stage": other["stage"],
        "attempt_id": other["attempt_id"],
        "reserve_sha256": digest(other),
    }
    data["ledger_events"][:0] = [other, other_failure]
    report = run(data)
    assert report["comparison_available"]
    finance = report["financial_snapshot"]
    assert Decimal(finance["retained_uncertainty_usd"]) == Decimal(".01")
    assert Decimal(finance["campaign_retained_uncertainty_usd"]) == Decimal(".02")
    assert not finance["additional_attempt_records"][0]["reported_cost_known"]
    assert sum(
        Decimal(r["all_phases"]["uncertainty_usd"]) for r in report["costs_by_arm"]
    ) == Decimal(".01")


def test_admission_counts_cannot_use_later_or_other_plan_pairs():
    identity = {"key": {"call_site_version": "filter", "execution_plan_id": "joint"}}
    selected = {
        "phase": "training",
        "arm": "joint",
        "workflow_stage": "filter",
        "semantic_plan": {
            "primary_identity": identity,
            "primary_rules": ["ContextCompress", "ModelDowngrade"],
            "primary": {"model": live.TARGET_MODEL, "max_tokens": 512},
            "candidate": None,
            "kind": "composed",
        },
        "divergence_feedback": None,
    }
    paired = deepcopy(selected)
    paired["semantic_plan"].update(
        primary_identity={},
        primary_rules=[],
        candidate={},
        candidate_identity=identity,
        kind="pass_through",
    )
    paired["divergence_feedback"] = 0.0
    with pytest.raises(PilotError, match="earlier"):
        analysis.admission_counts([selected] + [paired] * 20)
    with pytest.raises(PilotError, match="earlier"):
        analysis.admission_counts([paired] * 19 + [selected])
    report = analysis.admission_counts([paired] * 20 + [selected])
    assert (
        next(r for r in report if r["kind"] == "composed")[
            "earlier_recorded_pairs_at_first_selection"
        ]
        == 20
    )


def test_ledger_byte_reader_fails_instead_of_waiting_for_writer(tmp_path):
    import fcntl

    path = tmp_path / "busy.jsonl"
    path.write_text('{"event":"init","key_id":"synthetic"}\n')
    with path.open("a") as writer:
        fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PilotError, match="busy"):
            analysis.read_ledger_snapshot(path)


def test_real_ledger_and_acquisition_dispatch_round_trip_into_analyzer(
    full_data, tmp_path, monkeypatch
):
    """Exercise production fingerprint/persistence, mocking only network calls."""
    from types import SimpleNamespace
    from bench import openrouter_pilot as pilot

    data = deepcopy(full_data)
    args = SimpleNamespace(output=tmp_path / "artifacts", max_calls=None)
    ledger_path = tmp_path / "real-ledger-with-fake-provider.jsonl"
    key = "synthetic-test-string-not-a-provider-key"
    ledger = pilot.Ledger(ledger_path, key)
    monkeypatch.setattr(pilot, "account", lambda _: {"usage": 0})
    requests = []

    def fake_request(path, supplied_key, payload):
        assert path == "/chat/completions" and supplied_key == key
        requests.append(payload)
        return deepcopy(data["ledger_events"][1]["response"])

    monkeypatch.setattr(pilot, "request_json", fake_request)
    acquisition = live.Acquisition(args, data["manifest"], ledger, key)
    first = data["intents"][0]
    item = {k: first[k] for k in analysis.ITEM_KEYS}
    signature = acquisition.intent(item, first["original_call"], first["native_plan"])
    row = acquisition.dispatch(item, "calibration", first["original_call"], signature)
    acquisition.record(deepcopy(data["decisions"][0]))
    events, byte_hash = analysis.read_ledger_snapshot(ledger_path)
    reserve = next(event for event in events if event["event"] == "reserve")
    result = next(event for event in events if event["event"] == "result")
    assert row["fingerprint"] == reserve["fingerprint"] == result["fingerprint"]
    assert len(requests) == 1
    data.update(
        calls=acquisition.calls,
        decisions=acquisition.decisions,
        intents=acquisition.intents,
        ledger_events=events,
        ledger_sha256=byte_hash,
        gate=None,
        static=None,
        summary=None,
    )
    report = run(data)
    assert report["recorded_calls"] == 1
    assert report["financial_snapshot"]["snapshot_financially_complete"]
    assert Decimal(report["financial_snapshot"]["known_stage_usd"]) == Decimal(".001")
    assert report["financial_snapshot"]["ledger_sha256"] == byte_hash
    assert report["comparisons"] == []  # correctly incomplete, not rejected provenance
