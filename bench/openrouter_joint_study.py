"""Prospective, capped fixed-input-rules × routing development study.

This does not test the full rule set or certify a two-percentage-point damage
bound. Training is separate from untouched, frozen-feedback evaluation. The
training-only admission gate can stop the study without opening heldout tasks.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from bench import openrouter_rules_live as live
from bench.openrouter_matrix import ROOT
from bench.openrouter_pilot import PilotError, digest, load_key

KIND = "prospective_joint_input_rules_v1"
CALIBRATION, WARMUP, TRAINING, HELDOUT = 16, 3, 64, 48
SITE_RULES = {
    "filter": ["ContextCompress", "ModelDowngrade"],
    "synthesize": ["ModelDowngrade"],
    "answer": ["StateDrop", "ModelDowngrade"],
}


def restrict_call(call, stage):
    """Make the candidate bound structural, not an output-length assumption.

    ContextCompress requires attention_scores. StateDrop requires unread state
    annotations. Only filter carries the former; only answer carries the latter.
    The original prompts and actual provider-visible histories are unchanged.
    """
    result = deepcopy(call)
    if stage not in SITE_RULES:
        raise PilotError("uncovered study call site")
    extra = result["parameters"]["extra"]
    if stage != "filter":
        extra.pop("attention_scores", None)
        extra.pop("follow_on_tokens", None)
        extra.pop("dead_attention_epsilon", None)
    if stage != "answer":
        extra["window_state_reads"] = [
            dep["key"] for dep in extra["message_deps"] if dep["kind"] == "state"
        ]
    return result


def policies():
    result = {arm: live.policy_settings(arm) for arm in live.ARMS}
    for arm, settings in result.items():
        settings["AGENTC_ENABLED_RULES"] = (
            ""
            if arm == "original"
            else "ModelDowngrade"
            if arm == "routing_only"
            else "ContextCompress,StateDrop,ModelDowngrade"
            if arm == "joint"
            else "ContextCompress,StateDrop"
        )
        settings["AGENTC_OPTIMIZE_MAX_REWRITE_DEPTH"] = "2"
    return result


def validate_plan(call, plan):
    stage = call["call_site_id"].rsplit("/", 1)[-1]
    allowed = set(SITE_RULES[stage])
    choices = [plan]
    candidate = plan.get("agentc_exploration_context", {}).get("candidate_plan")
    if candidate:
        choices.append(candidate)
    for selected in choices:
        selected_call = selected.get("call", call)
        rules = set(live.activation(call, selected)["selected_rules"])
        if (
            not rules <= allowed
            or selected_call["parameters"]["max_output_tokens"] != 512
        ):
            raise PilotError("native plan escaped the fixed per-site rule menu")
        if ("ModelDowngrade" in rules) != (selected_call["model"] != call["model"]):
            raise PilotError("routing attribution differs from dispatched model")
        if selected["kind"] == "pass_through" and selected_call != call:
            raise PilotError("reference primary changed its request")


def finalize_manifest(manifest, aborted):
    manifest = deepcopy(manifest)
    grouped = {}
    for row in manifest["schedule"]:
        grouped.setdefault(row["task_id"], []).append(row)
    calibration = [
        task for task, rows in grouped.items() if rows[0]["phase"] == "calibration"
    ]
    others = [task for task in grouped if task not in calibration]
    if len(calibration) != CALIBRATION or len(others) != WARMUP + TRAINING + HELDOUT:
        raise PilotError(
            "study allocation must be 16 calibration, 3 warmup, 64 training, 48 heldout"
        )
    phases = {}
    for i, task in enumerate(others):
        phases[task] = (
            "warmup"
            if i < WARMUP
            else "training"
            if i < WARMUP + TRAINING
            else "heldout"
        )
    for row in manifest["schedule"]:
        if row["phase"] != "calibration":
            row["phase"] = phases[row["task_id"]]
    manifest.update(
        kind=KIND,
        policies=policies(),
        stage_cap_usd="15",
        training_cap_usd="9",
        maximum_reconstruction_age_seconds=28800,
        aborted_manifest_sha256=digest(aborted),
        bounded_transport_retry=True,
        site_rule_menu=deepcopy(SITE_RULES),
        allocation={
            "calibration": CALIBRATION,
            "warmup": WARMUP,
            "training": TRAINING,
            "heldout": HELDOUT,
        },
        heldout_feedback="none: exploration/shadows disabled; do not call native observe/divergence",
        training_gate="Complete training and at least one actually selected joint rewrite+route exact plan with >=20 source-paired observations; task gold is not consulted",
        candidate_identity="Fixed512 output cap; filter CC/route/CC+route, synth route, answer SD/route/SD+route; exact plan IDs never pooled",
        analysis_contract={
            "unit": "question; six matched arms and each arm's own complete workflow history",
            "quality": "unchanged normalized raw-answer EM/F1, paired differences on all48 scheduled heldout questions",
            "cost": "billed and nominal uncached; primary-only and full training/calibration/probe/shadow costs separately; failed-attempt charges and full residual holds never dropped",
            "comparison": "joint versus original, historical rules, guarded rules, routing-only, and independent global-calibration route then guarded rewrite",
            "incomplete": "no efficacy conclusion from a cost/provider-stopped or gate-stopped prefix",
            "uncertainty": "question-paired bootstrap intervals descriptive only;48questions do not certify2pp non-inferiority or universal joint advantage",
        },
    )
    manifest["limitations"] = [
        "Prospective development study, not full-rule ablation or a confirmatory MLSys result.",
        "Only ContextCompress at filter and StateDrop at answer are tested alongside routing; fixed512cap excludes OutputBudget identity churn prospectively.",
        "The historical arm is its current greedy planner restricted to this same menu, not every old optimization.",
        "64 training questions provide opportunity, not assurance, for20 exact-plan comparisons; guards remain unchanged.",
        "A failed training admission gate stops before heldout, demonstrating a learning limitation rather than joint efficacy.",
        "Heldout planning uses training profiles; no native outcome/divergence feedback, exploration, or shadows are added during evaluation.",
        "One public extended-context QA workload; campaign-fresh IDs do not establish no prior repository/model-training exposure.",
        "48questions cannot certify a2pp damage contract; descriptive intervals are not a safety or acceptance guarantee.",
        "Lexical divergence is not task correctness. State read annotations are an explicit workload contract, not semantic proof.",
        "Sequential uses labeled independent global-model calibration, not an adaptive per-site router; charge both calibration models.",
        "All training/probe/shadow/calibration costs and failed-attempt allowances are reported; cached provider charges and nominal uncached costs differ.",
        "Temperature0 does not guarantee deterministic providers; interleaving and implicit caches make latency diagnostic.",
        "Fresh stores replay paid journals on restart under an8h source-bound window; this is not evidence of native crash recovery.",
        "A15USD total envelope and9USD training ceiling may stop acquisition; no favorable partial intersection is promoted to efficacy.",
    ]
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest):
    if (
        manifest["kind"] != KIND
        or manifest["policies"] != policies()
        or manifest["site_rule_menu"] != SITE_RULES
        or manifest["stage_cap_usd"] != "15"
        or manifest["training_cap_usd"] != "9"
        or manifest["maximum_reconstruction_age_seconds"] != 28800
        or manifest.get("bounded_transport_retry") is not True
    ):
        raise PilotError("prospective study controls changed")
    counts = Counter()
    groups = {}
    for row in manifest["schedule"]:
        groups.setdefault(row["task_id"], []).append(row)
    for task, rows in groups.items():
        phases = {row["phase"] for row in rows}
        if len(phases) != 1 or task in manifest["excluded_question_ids"]:
            raise PilotError("study phases overlap or reuse an exposed question")
        phase = next(iter(phases))
        expected = (
            {"calibration/" + model for model in (live.SOURCE_MODEL, live.TARGET_MODEL)}
            if phase == "calibration"
            else set(live.ARMS)
        )
        if len(rows) != len(expected) or {row["arm"] for row in rows} != expected:
            raise PilotError(
                "each question must contain every scheduled arm exactly once"
            )
        counts[phase] += 1
    if counts != {
        "calibration": CALIBRATION,
        "warmup": WARMUP,
        "training": TRAINING,
        "heldout": HELDOUT,
    }:
        raise PilotError("prospective study allocation changed")
    order = ["calibration", "warmup", "training", "heldout"]
    if any(
        order.index(a["phase"]) > order.index(b["phase"])
        for a, b in zip(manifest["schedule"], manifest["schedule"][1:])
    ):
        raise PilotError("heldout must follow complete calibration and training")


def admission_gate(manifest, decisions, calls):
    """Training-only gate, with no tasks/labels or heldout outcomes as inputs."""
    training = [d for d in decisions if d["phase"] in {"warmup", "training"}]
    expected = {
        (r["task_id"], r["arm"], stage)
        for r in manifest["schedule"]
        if r["phase"] in {"warmup", "training"}
        for stage in live.STAGES
    }
    actual = [(d["task_id"], d["arm"], d["workflow_stage"]) for d in training]
    if len(actual) != len(expected) or set(actual) != expected:
        raise PilotError("admission gate requires complete scheduled training")
    by_id = {r["id"]: r for r in calls}
    pairs = Counter()
    selected = {}
    for decision in training:
        if decision["arm"] != "joint":
            continue
        signature = decision["semantic_plan"]
        if decision["divergence_feedback"] is not None:
            identity = (
                signature["candidate_identity"]
                if signature["candidate"]
                else signature["primary_identity"]
            )
            pairs[(decision["workflow_stage"], digest(identity))] += 1
        rules = set(signature["primary_rules"])
        if (
            "ModelDowngrade" in rules
            and rules & {"ContextCompress", "StateDrop"}
            and decision["activation"]["model_changed"]
        ):
            key = (decision["workflow_stage"], digest(signature["primary_identity"]))
            selected[key] = {
                "workflow_stage": key[0],
                "identity": signature["primary_identity"],
                "rules": sorted(rules),
                "selected_primary_id": decision["primary_id"],
            }
        for row_id in decision["incurred_ids"]:
            if by_id[row_id]["finish_reason"] not in {"stop", "length"}:
                raise PilotError("failed provider output cannot enter admission gate")
    admitted = [
        {**value, "source_paired_observations": pairs[key]}
        for key, value in sorted(selected.items())
        if pairs[key]
        >= int(manifest["policies"]["joint"]["AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE"])
    ]
    return {
        "manifest_sha256": digest(manifest),
        "training_decisions_sha256": digest(training),
        "proceed_to_heldout": bool(admitted),
        "admitted_joint_plans": admitted,
        "training_only": True,
        "task_gold_used": False,
        "efficacy_claim": False,
        "reason": "admitted joint plan observed during training"
        if admitted
        else "no actually selected joint plan with20 exact source-paired observations; stop without opening heldout",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    for name in ("env-file", "ledger", "fixture", "native", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--native-sha256")
    parser.add_argument("--runtime-commit")
    parser.add_argument(
        "--frontier",
        type=Path,
        default=ROOT / "bench/repro/openrouter-frontier-2026-09-04/manifest.json",
    )
    parser.add_argument(
        "--previous",
        type=Path,
        default=ROOT / "bench/repro/openrouter-pilot-2026-09-04/manifest.json",
    )
    parser.add_argument(
        "--aborted",
        type=Path,
        default=ROOT
        / "bench/repro/openrouter-rules-development-2026-09-05/manifest.json",
    )
    parser.add_argument("--max-calls", type=int)
    args = parser.parse_args()
    args.joint_study, args.calibration, args.development = (
        True,
        CALIBRATION,
        WARMUP + TRAINING + HELDOUT,
    )
    try:
        if args.max_calls is not None and args.max_calls < 0:
            raise PilotError("max-calls must be non-negative")
        if args.command == "prepare" and (
            not args.native_sha256 or not args.runtime_commit
        ):
            raise PilotError(
                "prepare requires independently reviewed runtime commit and binary hash"
            )
        key = load_key(args.env_file)
        result = (
            live.prepare(args, key)
            if args.command == "prepare"
            else live.run(args, key)
        )
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Joint input study stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
