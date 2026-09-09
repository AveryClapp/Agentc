"""Predeclared, post-completion description of the protected development draw.

All 63 matched tasks remain in the analysis. Source and HR use their own complete
histories; an invalid stage makes that workflow's exact accuracy zero. Native
probe outputs never form a synthetic workflow or an independent task cluster.
The existing whole-task percentile bootstrap uses 10,000 replicates and seed
2026090605, with no tuning arguments. Source/probe learning precedes fixed HR, so
time/cache and the disclosed offline input exposure preclude adaptive or held-out
efficacy claims. Exact-plan sample counts are not current guard/admission state.

The caller supplies an already-read original journal and independently recorded
raw artifact hashes. This module has no Ledger/key/provider or write operation.
It opens labels only after both complete phase artifacts have independently
replayed against their exact audited prefixes, including full financial checks.
"""

from collections import Counter
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

from bench import openrouter_joint_analysis as bootstrap
from bench import openrouter_protected_audit as audit
from bench import openrouter_protected_fixture as fixture
from bench import openrouter_protected_pair as pair
from bench import openrouter_protected_study as study
from bench import typed_relational_workload as typed
from bench.openrouter_attempts import accounting
from bench.openrouter_matrix import ROOT, file_hash
from bench.openrouter_pilot import HARD_CAP, PilotError, canonical, digest, money

KIND = "protected-complete-development-analysis-v1"
MANIFEST_SHA256 = "c5a38945224decfbc1ea03b582213f992b7742c7179121529bc7a3f5de85d477"
MANIFEST_FILE_SHA256 = (
    "5d8ed2f4be4bd6eb21594bcd12b7b76fc6bebe96d474cfc1da8f357dbdecc5ae"
)
CLUSTERS, REPLICATES, BOOTSTRAP_SEED, PAIR_FLOOR = 63, 10000, 2026090605, 20
ARTIFACT_NAMES = (
    "summary.json",
    "audit.json",
    "intents.json",
    "calls.json",
    "protected-events.json",
)
ARMS = ("source", "HR")
PROTOCOL = {
    "kind": KIND,
    "manifest_sha256": MANIFEST_SHA256,
    "manifest_file_sha256": MANIFEST_FILE_SHA256,
    "clusters": CLUSTERS,
    "cluster_unit": "whole matched source/HR task; never probes or calls",
    "replicates": REPLICATES,
    "bootstrap_seed": BOOTSTRAP_SEED,
    "interval": "existing whole-task percentile bootstrap, two-sided 95 percent",
    "contrast_direction": "HR minus source",
    "accuracy": "exact final scalar after all own-history stages validate; invalid is zero",
    "workflow_cost": "sum of three actual provider charges; probes excluded and separately charged",
    "native_pair_floor": PAIR_FLOOR,
    "filtering": "none; preserve all 63 clusters including invalid workflows",
    "zero_source_cost_ratio": "report null aggregate ratio; do not exclude any task",
    "adaptive_primary_or_savings_claim": False,
    "current_guard_or_admission_assessed": False,
}


def _require(condition, message):
    if not condition:
        raise PilotError(message)


def _same(left, right, message):
    _require(canonical(left) == canonical(right), message)


def _json(raw):
    def unique(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate protected analysis JSON key")
            result[key] = value
        return result

    def invalid(value):
        raise PilotError("nonfinite protected analysis JSON")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def _read(path, expected):
    _require(
        isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected),
        "an external exact raw artifact hash is required",
    )
    raw = Path(path).read_bytes()
    _require(
        hashlib.sha256(raw).hexdigest() == expected,
        "protected analysis raw artifact changed",
    )
    return _json(raw)


def _phase(output, manifest, bundle, phase, events, bridge, materializer, hashes):
    values = {
        name: _read(output / phase / name, hashes[phase + "/" + name])
        for name in ARTIFACT_NAMES
    }
    summary, saved = values["summary.json"], values["audit.json"]
    _require(
        summary["schedule_complete"] is True
        and summary["error_type"] is None
        and saved["schedule_complete"] is True
        and saved["integrity_verified"] is True,
        "incomplete or failed protected acquisition cannot open labels",
    )
    _require(
        saved["labels_opened"] is False and summary["labels_opened"] is False,
        "acquisition contains premature label use",
    )
    count = saved["ledger_event_count"]
    _require(
        type(count) is int
        and 0 < count <= len(events)
        and saved["ledger_events_sha256"] == digest(events[:count]),
        "protected audited original journal prefix changed",
    )
    sid = study.stage_id(manifest, phase)
    identities = {row["id"] for row in values["calls.json"]}
    for row in events[count:]:
        _require(
            not (
                row.get("stage") == sid
                or row.get("id") in identities
                or str(row.get("id", "")).startswith(sid + "/")
                or (
                    row.get("event") == pair.EVENT
                    and row.get("binding", {}).get("run_id") == sid
                )
                or (
                    row.get("event") == study.CLAIM_EVENT
                    and row.get("selection_sha256") == manifest["selection_sha256"]
                    and row.get("phase") == phase
                )
            ),
            "later activity invalidates the supposedly terminal protected phase",
        )
    prefix = events[:count]
    verified = audit.audit_phase(
        manifest,
        bundle,
        phase,
        values["intents.json"],
        values["calls.json"],
        values["protected-events.json"],
        prefix,
        bridge,
        materializer,
    )
    _same(
        saved, verified, "saved protected audit differs from independent reconstruction"
    )
    _require(
        verified["schedule_complete"] is True
        and verified["integrity_verified"] is True
        and not verified["learning"]["ambiguous_native_claim_ids"]
        and not verified["learning"]["incomplete_pair_ids"],
        "protected acquisition has incomplete or ambiguous native evidence",
    )
    finance = verified["finance"]
    _require(
        not finance["pending_ids"]
        and not finance["failed_attempt_ids"]
        and not finance["unsafe_failed_attempt_ids"]
        and money(finance["retained_uncertainty_usd"]) == 0
        and money(finance["pending_upper_bound_usd"]) == 0,
        "protected phase has unresolved or failed financial evidence",
    )
    expected_summary = {
        "kind": study.KIND,
        "phase": phase,
        "manifest_sha256": digest(manifest),
        "schedule_complete": True,
        "completed_calls": len(values["calls.json"]),
        "artifact_files": {
            name: hashes[phase + "/" + name]
            for name in ("intents.json", "calls.json", "protected-events.json")
        },
        "known_cost_usd": finance["known_stage_usd"],
        "retained_uncertainty_usd": finance["retained_uncertainty_usd"],
        "pending_upper_usd": finance["pending_upper_bound_usd"],
        "financial_snapshot": study.financial_state(accounting(prefix)),
        "error_type": None,
        "efficacy_claim": False,
        "paper_evidence": False,
        "labels_opened": False,
    }
    _same(summary, expected_summary, "protected terminal summary or charges changed")
    cost = sum((money(row["cost_usd"]) for row in values["calls.json"]), Decimal(0))
    _require(
        cost == money(finance["known_stage_usd"]),
        "successful acquisition rows do not include every phase charge",
    )
    return values, verified


def _inventory(manifest, bundle, phases):
    pilot, primary = phases["pilot"][0]["calls.json"], phases["study"][0]["calls.json"]
    pilot_audit, study_audit = phases["pilot"][1], phases["study"][1]
    _require(
        pilot_audit["pilot_format_gate"] is True and len(pilot) == 36,
        "all 36 pilot calls must pass the frozen format gate",
    )
    selection = bundle["selection"]
    _require(
        len(selection["study_ids"]) == CLUSTERS
        and len(set(selection["study_ids"])) == CLUSTERS,
        "protected analysis requires all 63 original task clusters",
    )
    _same(
        manifest["fixture_exposure"],
        study.exposure_disclosure(selection),
        "required offline exposure disclosure changed",
    )
    _require(
        manifest["fixture_exposure_sha256"] == digest(manifest["fixture_exposure"])
        and manifest["protocol"]["per_plan_evidence_target"] == PAIR_FLOOR,
        "protected disclosure or evidence floor changed",
    )
    counts = Counter(row["arm"] for row in primary)
    _require(
        set(counts) <= {*ARMS, "probe"}
        and counts["source"] == CLUSTERS * 3
        and counts["HR"] == CLUSTERS * 3
        and counts["probe"] <= 140,
        "protected acquisition inventory is not complete source/HR plus bounded probes",
    )
    expected = manifest["schedule"]["study"]
    _same(
        [
            {key: row[key] for key in ("phase", "arm", "task_id", "workflow_stage")}
            for row in primary
            if row["arm"] != "probe"
        ],
        expected,
        "fixed source/HR order or cluster membership changed",
    )
    _require(
        study_audit["completed_scheduled_steps"] == CLUSTERS * 6,
        "protected workflow schedule is incomplete",
    )
    learning = study_audit["learning"]
    _require(
        learning["learning_evidence_reported"] is True
        and learning["current_guard_state_assessed"] is False
        and learning["admission_assessed"] is False
        and learning["unknown_primary_remains_source"] is True,
        "native profile report changed its protected interpretation",
    )
    profiles = learning["exact_plan_profiles"]
    _require(
        sum(row["paired_observations"] for row in profiles) == counts["probe"],
        "paid probes differ from exact verified native feedback counts",
    )
    return pilot, primary, profiles


def _cell(rows, expected):
    _require(
        [row["workflow_stage"] for row in rows] == list(typed.STAGES),
        "a workflow lacks its three ordered own-history calls",
    )
    history = {
        row["workflow_stage"]: typed.application_text(
            row["answer"], row["finish_reason"], stage=row["workflow_stage"]
        )
        for row in rows
    }
    return {
        "exact_accuracy": typed.score_answer(
            history["answer"], expected, history=history
        )["em"],
        "valid_by_stage": {
            stage: typed.parse_stage(stage, history[stage]) is not None
            for stage in typed.STAGES
        },
        "workflow_cost_usd": str(
            sum((money(row["cost_usd"]) for row in rows), Decimal(0))
        ),
        "prompt_tokens": sum(row["usage"]["prompt_tokens"] for row in rows),
        "completion_tokens": sum(row["usage"]["completion_tokens"] for row in rows),
    }


def analyze(output, public_fixture, events, bridge, materializer, *, artifact_sha256):
    """Return a description; never read a ledger, dispatch, or write output.

    ``artifact_sha256`` contains exactly phase/name for both pilot and study and
    all five ARTIFACT_NAMES. These are caller-supplied independent raw hashes,
    not hashes silently minted from whatever this function happens to find.
    """
    output, public_fixture = Path(output), Path(public_fixture)
    expected_keys = {
        phase + "/" + name for phase in study.PHASES for name in ARTIFACT_NAMES
    }
    _require(
        isinstance(artifact_sha256, dict) and set(artifact_sha256) == expected_keys,
        "complete external protected artifact hash inventory required",
    )
    manifest = _read(output / "manifest.json", MANIFEST_FILE_SHA256)
    _require(digest(manifest) == MANIFEST_SHA256, "wrong frozen acquisition manifest")
    # Summary-only early stop is intentionally before any label file access.
    for phase in study.PHASES:
        summary = _read(
            output / phase / "summary.json", artifact_sha256[phase + "/summary.json"]
        )
        _require(
            summary["schedule_complete"] is True and summary["error_type"] is None,
            "incomplete or failed protected acquisition cannot open labels",
        )
    _require(
        isinstance(events, list)
        and events
        and isinstance(events[0].get("key_id"), str)
        and events[0]["key_id"]
        and all(row.get("key_id") == events[0]["key_id"] for row in events),
        "original journal snapshot has missing or mixed credential binding",
    )
    bundle = fixture.load_bundle(public_fixture)  # Public files only; no generator.
    phases = {
        phase: _phase(
            output,
            manifest,
            bundle,
            phase,
            events,
            bridge,
            materializer,
            artifact_sha256,
        )
        for phase in study.PHASES
    }
    pilot_rows, primary, profiles = _inventory(manifest, bundle, phases)
    current = accounting(events)
    _require(
        current["known_usd"] + current["holds_usd"] + current["pending_usd"]
        <= HARD_CAP,
        "original campaign exceeds its retained full ceiling",
    )

    # First and only label open. Check these exact raw bytes before parsing them.
    labels = _read(
        public_fixture / "labels.json", bundle["provenance"]["labels_sha256"]
    )
    selection = bundle["selection"]
    _require(
        isinstance(labels, dict)
        and set(labels) == set(selection["pilot_ids"] + selection["study_ids"])
        and all(
            isinstance(value, dict)
            and set(value) == {"expected"}
            and isinstance(value["expected"], str)
            and re.fullmatch(r"M(?:0[1-9]|[1-9][0-9])", value["expected"])
            for value in labels.values()
        ),
        "frozen label inventory or scalar domain changed",
    )
    paired = []
    for task_id in selection["study_ids"]:
        cells = {
            arm: _cell(
                [
                    row
                    for row in primary
                    if row["task_id"] == task_id and row["arm"] == arm
                ],
                labels[task_id]["expected"],
            )
            for arm in ARMS
        }
        paired.append(
            {
                "task_id": task_id,
                "cells": cells,
                "HR_minus_source_accuracy": cells["HR"]["exact_accuracy"]
                - cells["source"]["exact_accuracy"],
                "HR_minus_source_cost_usd": str(
                    money(cells["HR"]["workflow_cost_usd"])
                    - money(cells["source"]["workflow_cost_usd"])
                ),
            }
        )
    cluster_rows = [
        {
            "source_accuracy": row["cells"]["source"]["exact_accuracy"],
            "HR_accuracy": row["cells"]["HR"]["exact_accuracy"],
            "HR_minus_source_accuracy": row["HR_minus_source_accuracy"],
            "source_workflow_cost_usd": float(
                row["cells"]["source"]["workflow_cost_usd"]
            ),
            "HR_workflow_cost_usd": float(row["cells"]["HR"]["workflow_cost_usd"]),
            "HR_minus_source_cost_usd": float(row["HR_minus_source_cost_usd"]),
        }
        for row in paired
    ]
    totals = {
        arm: sum(
            (money(row["cost_usd"]) for row in primary if row["arm"] == arm), Decimal(0)
        )
        for arm in (*ARMS, "probe")
    }
    pilot_cost = sum((money(row["cost_usd"]) for row in pilot_rows), Decimal(0))
    all_in = pilot_cost + sum(totals.values(), Decimal(0))
    return {
        "kind": KIND,
        "analysis_protocol": deepcopy(PROTOCOL),
        "analysis_protocol_sha256": digest(PROTOCOL),
        "analysis_source_files": {
            path: file_hash(ROOT / path)
            for path in (
                "bench/openrouter_protected_analysis.py",
                "bench/openrouter_joint_analysis.py",
                "bench/typed_relational_workload.py",
            )
        },
        "manifest_sha256": digest(manifest),
        "manifest_file_sha256": MANIFEST_FILE_SHA256,
        "artifact_sha256": deepcopy(artifact_sha256),
        "labels_sha256": bundle["provenance"]["labels_sha256"],
        "audited_prefixes": {
            phase: {
                "event_count": phases[phase][1]["ledger_event_count"],
                "events_sha256": phases[phase][1]["ledger_events_sha256"],
            }
            for phase in study.PHASES
        },
        "original_current_snapshot": {
            "event_count": len(events),
            "events_sha256": digest(events),
            **study.financial_state(current),
        },
        "schedule_complete": True,
        "paired_task_count": len(paired),
        "paired_tasks": paired,
        "descriptive_paired_intervals": bootstrap.paired_bootstrap(
            cluster_rows, repetitions=REPLICATES, seed=BOOTSTRAP_SEED
        ),
        "quality_transitions": dict(
            Counter(
                "HR_gain"
                if row["HR_minus_source_accuracy"] > 0
                else "HR_loss"
                if row["HR_minus_source_accuracy"] < 0
                else "tie"
                for row in paired
            )
        ),
        "accuracy_totals": {
            arm: {
                "correct": sum(row["cells"][arm]["exact_accuracy"] for row in paired),
                "tasks": CLUSTERS,
            }
            for arm in ARMS
        },
        "costs": {
            "pilot_usd": str(pilot_cost),
            "source_workflows_usd": str(totals["source"]),
            "native_probes_usd": str(totals["probe"]),
            "source_learning_block_usd": str(totals["source"] + totals["probe"]),
            "fixed_HR_workflows_usd": str(totals["HR"]),
            "all_acquisition_usd": str(all_in),
            "fixed_HR_to_source_workflow_cost_ratio": float(
                totals["HR"] / totals["source"]
            )
            if totals["source"]
            else None,
            "workflow_comparison_excludes_probes_but_acquisition_total_includes_them": True,
        },
        "call_counts": {
            "pilot": len(pilot_rows),
            **dict(Counter(row["arm"] for row in primary)),
        },
        "native_learning": {
            "profiles": [
                {
                    **deepcopy(row),
                    "evidence_target": PAIR_FLOOR,
                    "count_floor_met": row["paired_observations"] >= PAIR_FLOOR,
                    "pairs_short_of_floor": max(
                        0, PAIR_FLOOR - row["paired_observations"]
                    ),
                }
                for row in profiles
            ],
            "only_observed_exact_profiles_reported": True,
            "current_guard_or_admission_assessed": False,
            "sample_floor_is_not_acceptance": True,
        },
        "fixture_exposure": deepcopy(manifest["fixture_exposure"]),
        "limitations": [
            *manifest["limitations"],
            "Bootstrap intervals describe these 63 development clusters, not unseen-task generalization.",
        ],
        "labels_opened": True,
        "pilot_accuracy_reported": False,
        "adaptive_savings_claim": False,
        "native_primary_promotion": False,
        "efficacy_claim": False,
        "paper_evidence": False,
    }
