"""Select, evaluate and seal baseline policies from paired acquisition records.

Router training uses original-request trajectories only. Rewrite choices use
fixed-target trials. The resulting static and sequential configurations require
separate scored validation rollouts before a lock can be sealed. Engineering
fixtures retain their E0 provenance and cannot enter a provider calibration lock.
"""

from __future__ import annotations

import json
import math
import argparse
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256

from bench.campaign_accounting import digest, money
from bench.campaign_records import normalize_owned_calls
from bench.campaign_transport import CampaignStopped
from bench.coding_submission import private_write

ARMS = ("best_static_joint", "route_then_rewrite", "rewrite_then_route")


class CalibrationError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise CalibrationError(message)


def _json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def configuration_sha256(value):
    return sha256(_json(value).encode()).hexdigest()


def policy_identity_sha256(mode, configuration):
    """Keep opposite execution orders separate even with identical parameters."""
    return digest(
        {"mode": mode, "configuration_sha256": configuration_sha256(configuration)}
    )


def validate_configuration(value, *, mode, models, output_cap):
    _require(
        isinstance(value, dict)
        and set(value)
        == {
            "strong_model",
            "cheap_model",
            "divergence_threshold",
            "static_plan",
            "router",
            "rewrite_by_target",
        },
        "invalid policy configuration fields",
    )
    _require(
        value["strong_model"] == models["strong"]
        and value["cheap_model"] == models["cheap"],
        "policy model pool differs",
    )
    _require(
        type(value["divergence_threshold"]) in (float, int)
        and value["divergence_threshold"] in (0.05, 0.10, 0.15, 0.20, 0.30, 0.50),
        "unregistered divergence threshold",
    )

    def recipe(steps):
        _require(isinstance(steps, list) and len(steps) <= 3, "invalid rewrite recipe")
        seen = set()
        for step in steps:
            _require(isinstance(step, dict), "invalid rewrite step")
            name = step.get("rule")
            _require(name not in seen, "duplicate rewrite family")
            seen.add(name)
            if name == "OutputBudget":
                _require(
                    set(step) == {"rule", "max_output_tokens"}
                    and type(step["max_output_tokens"]) is int
                    and 64 <= step["max_output_tokens"] <= output_cap,
                    "invalid frozen output cap",
                )
            elif name == "ContextCompress":
                _require(
                    set(step)
                    == {
                        "rule",
                        "min_prompt_bytes",
                        "min_dead_fraction",
                        "dead_attention_epsilon",
                    },
                    "invalid compression fields",
                )
                _require(
                    type(step["min_prompt_bytes"]) is int
                    and 0 <= step["min_prompt_bytes"] <= 2**32 - 1,
                    "invalid prompt bound",
                )
                _require(
                    all(
                        type(step[n]) in (float, int)
                        and math.isfinite(step[n])
                        and 0 <= step[n] <= 1
                        for n in ("min_dead_fraction", "dead_attention_epsilon")
                    ),
                    "invalid compression threshold",
                )
            else:
                _require(
                    name == "StateDrop" and set(step) == {"rule"},
                    "unregistered rewrite",
                )

    if mode == "best_static_joint":
        plan = value["static_plan"]
        _require(
            isinstance(plan, dict)
            and set(plan) == {"target_model", "rewrites"}
            and plan["target_model"] in models.values(),
            "invalid static target",
        )
        _require(
            value["router"] is None and value["rewrite_by_target"] == {},
            "static policy has conditional choices",
        )
        recipe(plan["rewrites"])
    else:
        _require(
            mode in ARMS and value["static_plan"] is None, "invalid sequential mode"
        )
        router = value["router"]
        _require(
            isinstance(router, dict)
            and set(router) == {"feature", "cheap_max_prompt_bytes"}
            and router["feature"] == "prompt_utf8_bytes_v1"
            and type(router["cheap_max_prompt_bytes"]) is int
            and 0 <= router["cheap_max_prompt_bytes"] <= 2**64 - 1,
            "invalid frozen router",
        )
        _require(
            isinstance(value["rewrite_by_target"], dict)
            and set(value["rewrite_by_target"]) == set(models.values()),
            "rewrite policies do not cover the pool",
        )
        for steps in value["rewrite_by_target"].values():
            recipe(steps)


@dataclass(frozen=True)
class CalibrationContract:
    workload_id: str
    models_json: str
    output_cap: int
    quality_margin: float
    stage: str
    evidence_kind: str
    task_universe_sha256: str
    protocol_sha256: str
    divergence_threshold: float = 0.05
    task_partitions_json: str | None = None

    @property
    def models(self):
        return json.loads(self.models_json)

    @property
    def partitions(self):
        return (
            _read_json(self.task_partitions_json)
            if self.task_partitions_json is not None
            else None
        )

    def validate(self):
        _require(
            isinstance(self.workload_id, str) and self.workload_id,
            "invalid calibration workload",
        )
        _require(
            type(self.divergence_threshold) in (float, int)
            and self.divergence_threshold in (0.05, 0.10, 0.15, 0.20, 0.30, 0.50),
            "unregistered contract guard threshold",
        )
        _require(
            self.stage in {"C", "E0"}
            and self.evidence_kind
            == ("provider_calibration" if self.stage == "C" else "engineering_fixture"),
            "invalid calibration provenance",
        )
        models = self.models
        _require(
            isinstance(models, dict)
            and set(models) == {"strong", "cheap"}
            and all(
                isinstance(m, str) and m.startswith("openai/") for m in models.values()
            )
            and len(set(models.values())) == 2,
            "invalid calibration model pool",
        )
        _require(
            type(self.output_cap) is int
            and 64 <= self.output_cap <= 2048
            and type(self.quality_margin) in (float, int)
            and math.isfinite(self.quality_margin)
            and -1 <= self.quality_margin <= 0,
            "invalid calibration bounds",
        )
        _require(
            all(
                isinstance(h, str)
                and len(h) == 64
                and set(h) <= set("0123456789abcdef")
                for h in (self.task_universe_sha256, self.protocol_sha256)
            ),
            "invalid calibration input digests",
        )
        partitions = self.partitions
        _require(
            self.stage == "E0" or partitions is not None,
            "provider calibration requires enrolled task partitions",
        )
        if partitions is not None:
            _require(
                isinstance(partitions, dict)
                and set(partitions) == {"training", "validation", "held_out"},
                "invalid enrolled task partitions",
            )
            ids = list(partitions.values())
            _require(
                all(
                    isinstance(group, list)
                    and group
                    and all(isinstance(t, str) and t for t in group)
                    and len(set(group)) == len(group)
                    for group in ids
                ),
                "invalid enrolled task inventory",
            )
            _require(
                sum(map(len, ids)) == len(set().union(*map(set, ids))),
                "enrolled task partitions overlap",
            )


def _feature(call):
    messages = call["request"]["messages"]
    _require(
        isinstance(messages, list)
        and all(
            isinstance(m, dict)
            and (m.get("content") is None or isinstance(m.get("content"), str))
            for m in messages
        ),
        "unsupported router training message shape",
    )
    return sum(len((message.get("content") or "").encode()) for message in messages)


def _rollouts(contract, registry, rows, *, partition):
    """Validate complete paired cells and actual physical billing before fitting."""
    contract.validate()
    _require(
        isinstance(registry, dict) and registry and isinstance(rows, list) and rows,
        "empty calibration corpus",
    )
    groups = defaultdict(dict)
    physical_ids = set()
    authorizations = set()
    for trial in registry.values():
        _require(
            isinstance(trial, dict)
            and trial.get("role")
            in {
                "reference",
                "cheap_original",
                "static_candidate",
                "fixed_recipe",
                "validation_policy",
            },
            "unregistered trial role",
        )
        if trial["role"] in {"static_candidate", "fixed_recipe"}:
            _require(
                trial.get("mode") == "best_static_joint",
                "independent training requires fixed-target recipes",
            )
    for row in rows:
        _require(
            isinstance(row, dict)
            and set(row) == {"trial_id", "task", "calls"}
            and row["trial_id"] in registry,
            "unknown calibration trial",
        )
        trial, task, calls = registry[row["trial_id"]], row["task"], row["calls"]
        _require(
            task.get("calibration_trial_id") == row["trial_id"]
            and task.get("calibration_registry_sha256") == digest(registry),
            "calibration task lacks its registered trial provenance",
        )
        authorization = task.get("calibration_authorization_sha256")
        _require(
            isinstance(authorization, str)
            and len(authorization) == 64
            and set(authorization) <= set("0123456789abcdef"),
            "invalid calibration authorization fingerprint",
        )
        authorizations.add(authorization)
        _require(
            task.get("record_type") == "task"
            and task.get("stage") == contract.stage
            and task.get("workload_id") == contract.workload_id,
            "foreign or held-out task records",
        )
        _require(
            task.get("synthetic_billing") is (contract.stage == "E0"),
            "fixture billing cannot be calibration evidence",
        )
        conformance = task.get("conformance", {})
        _require(
            conformance.get("official_task") is True
            and conformance.get("official_score") is True
            and conformance.get("upstream_source_modified") is False,
            "unconforming official acquisition",
        )
        if contract.stage == "C":
            _require(
                conformance.get("provider_accounting") is True
                and task.get("safety_failure") is False
                and task.get("metric_availability", {}).get("safety_failure") is True,
                "calibration lacks measured provider/safety conformance",
            )
        _require(
            type(task.get("official_score")) in (int, float)
            and math.isfinite(task["official_score"])
            and 0 <= task["official_score"] <= 1
            and task.get("task_status") in {"completed", "failed"},
            "incomplete or invalid calibration outcome",
        )
        _require(
            type(task.get("repetition")) is int
            and task["repetition"] >= 0
            and isinstance(task.get("task_id"), str)
            and task["task_id"],
            "invalid paired cell",
        )
        key = task["task_id"], task["repetition"]
        _require(key not in groups[row["trial_id"]], "duplicate calibration cell")
        _require(
            task.get("calls_sha256") == digest(calls)
            and task.get("provider_call_count") == len(calls),
            "task/call receipt mismatch",
        )
        try:
            normalized = normalize_owned_calls(
                calls,
                {
                    "task_id": task["task_id"],
                    "arm": task["arm"],
                },
                cell_id=task["cell_id"],
                optimized_arms=frozenset(),
                fixed_schema="calibration-fixed-dispatch-v1",
                call_site_prefix="calibration/",
            )
        except (CampaignStopped, KeyError, TypeError, ValueError) as error:
            raise CalibrationError("inconsistent owned calibration receipts") from error
        _require(
            calls
            and money(task["all_call_cost_usd"])
            == sum((money(c["cost_usd_exact"]) for c in normalized), Decimal(0)),
            "calibration omits physical call costs",
        )
        for call in calls:
            _require(
                call["metadata"].get("calibration_trial_id") == row["trial_id"]
                and call["metadata"].get("calibration_authorization_sha256")
                == authorization,
                "calibration relabeled a physical trial receipt",
            )
            identity = call["billed_result"]["session_id"], call["attempt_id"]
            _require(
                identity not in physical_ids,
                "calibration reuses a physical billing receipt",
            )
            physical_ids.add(identity)
        primary = [
            c
            for c in calls
            if c["metadata"]["actor"] == "agent"
            and c["metadata"]["kind"] in {"primary", "fallback"}
        ]
        _require(primary, "calibration has no agent decisions")
        if trial["role"] in {"reference", "cheap_original"}:
            target = contract.models[
                "strong" if trial["role"] == "reference" else "cheap"
            ]
            _require(
                task["arm"]
                == (
                    "unmodified_fixed_strong"
                    if trial["role"] == "reference"
                    else "fixed_cheap"
                )
                and all(
                    c["request"]["model"] == target
                    and c["metadata"]["requested_model"] == target
                    and c.get("planning") is None
                    and c["metadata"].get("native_plan_json") is None
                    for c in primary
                ),
                "original-request training has policy mutations",
            )
        else:
            cfg = trial["configuration"]
            validate_configuration(
                cfg,
                mode=trial["mode"],
                models=contract.models,
                output_cap=contract.output_cap,
            )
            _require(
                cfg["divergence_threshold"] == contract.divergence_threshold,
                "policy choices changed the independently selected guard threshold",
            )
            _require(
                task["arm"] == trial["mode"]
                and all(
                    (c.get("planning") or {})
                    .get("selection_audit", {})
                    .get("policy", {})
                    .get("configuration_sha256")
                    == configuration_sha256(cfg)
                    and c["planning"]["selection_audit"]["policy"].get(
                        "calibration_lock_sha256"
                    )
                    == authorization
                    and c["planning"]["selection_audit"]["planner_mode"]
                    == trial["mode"]
                    for c in primary
                ),
                "trial configuration differs from native execution",
            )
        groups[row["trial_id"]][key] = {
            "task": task,
            "calls": calls,
            "primary": primary,
            "cost": money(task["all_call_cost_usd"]),
        }
    _require(set(groups) == set(registry), "missing registered trial")
    _require(
        len(authorizations) == 1, "calibration mixed independent trial authorizations"
    )
    keys = set(next(iter(groups.values())))
    _require(
        all(set(group) == keys for group in groups.values()),
        "calibration matrix is incomplete or unpaired",
    )
    _require(
        contract.partitions is not None
        and {k[0] for k in keys} == set(contract.partitions[partition]),
        "calibration corpus differs from enrolled task partition",
    )
    if contract.stage == "C":
        _require(
            all(
                {k[1] for k in keys if k[0] == task} == set(range(5))
                for task in {k[0] for k in keys}
            ),
            "provider calibration requires five paired repetitions",
        )
    return dict(groups), keys


def _quality_delta(group, reference):
    # Repeated calls/repetitions cannot turn one task into multiple subjects.
    by_task = defaultdict(list)
    for key in reference:
        by_task[key[0]].append(
            group[key]["task"]["official_score"]
            - reference[key]["task"]["official_score"]
        )
    return sum(sum(values) / len(values) for values in by_task.values()) / len(by_task)


def _select(candidates, reference, margin):
    eligible = [
        (sum((r["cost"] for r in rows.values()), Decimal(0)), name)
        for name, rows in candidates.items()
        if _quality_delta(rows, reference) >= margin
    ]
    _require(eligible, "no calibration configuration meets the quality margin")
    return min(eligible)[1]


def fit_policies(contract, registry, rows):
    """Fit on training rollouts; returned configurations are not yet a lock."""
    groups, keys = _rollouts(contract, registry, rows, partition="training")
    _require(
        all(
            t["role"]
            in {"reference", "cheap_original", "static_candidate", "fixed_recipe"}
            for t in registry.values()
        ),
        "validation policies cannot enter training",
    )
    roles = defaultdict(list)
    for name, trial in registry.items():
        roles[trial["role"]].append(name)
    _require(
        len(roles["reference"]) == len(roles["cheap_original"]) == 1,
        "training requires original strong/cheap references",
    )
    reference, cheap = groups[roles["reference"][0]], groups[roles["cheap_original"][0]]
    static_ids = roles["static_candidate"]
    _require(static_ids, "no registered static candidates")
    static_id = _select(
        {n: groups[n] for n in static_ids}, reference, contract.quality_margin
    )
    static = registry[static_id]["configuration"]
    recipes, selected = {}, {"best_static_joint": static_id}
    for target, baseline in [
        (contract.models["strong"], reference),
        (contract.models["cheap"], cheap),
    ]:
        ids = [
            n
            for n in roles["fixed_recipe"]
            if registry[n]["configuration"]["static_plan"]["target_model"] == target
        ]
        _require(ids, "independent rewrite trials do not cover both targets")
        name = _select({n: groups[n] for n in ids}, baseline, contract.quality_margin)
        recipes[target] = registry[name]["configuration"]["static_plan"]["rewrites"]
        selected[target] = name
    features = {key: [_feature(c) for c in reference[key]["primary"]] for key in keys}
    thresholds = sorted(
        {0, *(feature for values in features.values() for feature in values)}
    )
    fits = []
    for threshold in thresholds:
        risk, gain = defaultdict(list), Decimal(0)
        for key in keys:
            routed = sum(v <= threshold for v in features[key])
            risk[key[0]].append(
                (
                    cheap[key]["task"]["official_score"]
                    - reference[key]["task"]["official_score"]
                )
                if routed
                else 0
            )
            gain += (
                (reference[key]["cost"] - cheap[key]["cost"])
                * Decimal(routed)
                / Decimal(len(features[key]))
            )
        delta = sum(sum(values) / len(values) for values in risk.values()) / len(risk)
        if delta >= contract.quality_margin:
            fits.append((-gain, threshold))
    _require(fits, "router training has no admissible threshold")
    negative_gain, threshold = min(fits)
    sequential = {
        "strong_model": contract.models["strong"],
        "cheap_model": contract.models["cheap"],
        "divergence_threshold": contract.divergence_threshold,
        "static_plan": None,
        "router": {
            "feature": "prompt_utf8_bytes_v1",
            "cheap_max_prompt_bytes": threshold,
        },
        "rewrite_by_target": recipes,
    }
    configurations = {
        "best_static_joint": static,
        "route_then_rewrite": sequential,
        "rewrite_then_route": sequential,
    }
    fitted = json.loads(
        _json(
            {
                "schema_version": 1,
                "record_type": "unsealed_policy_fit",
                "contract": contract.__dict__,
                "training_task_ids": sorted({k[0] for k in keys}),
                "training_records_sha256": digest(rows),
                "registry_sha256": digest(registry),
                "configurations": configurations,
                "selected_trials": selected,
                "router_training_objective": "estimated_trajectory_cost_gain; task_labels_inherited_by_original_requests",
                "router_estimated_training_gain_usd": str(-negative_gain),
                "sealed": False,
            }
        )
    )
    fitted["fit_sha256"] = digest(fitted)
    return fitted


def _validate_guard_admission(guard_admission, configurations):
    """Provider seals require raw measured admission, which no collector emits yet.

    The old summary-only interface could claim success with invented counts and
    booleans. Existing native paired and controller-frontier collectors are E0
    diagnostics; neither verifies provider quality, durability and the full
    operating point. Keep provider seals closed until that integration exists.
    """
    _require(
        isinstance(guard_admission, dict)
        and guard_admission.get("record_type") == "guard_calibration_admission"
        and guard_admission.get("source_stage") == "C"
        and guard_admission.get("synthetic_billing") is False,
        "provider lock requires measured guard calibration",
    )
    raise CalibrationError(
        "summary-only guard admissions are unsupported; raw provider frontier "
        "verification is required before sealing or loading a C policy lock"
    )


def seal_policies(
    contract,
    fitted,
    registry,
    validation_rows,
    *,
    held_out_task_ids,
    guard_admission=None,
):
    """Seal only after actual rollouts of every exact trained configuration."""
    _require(
        fitted.get("record_type") == "unsealed_policy_fit"
        and fitted.get("contract") == contract.__dict__
        and fitted.get("sealed") is False,
        "foreign or modified policy fit",
    )
    _require(
        fitted.get("fit_sha256")
        == digest({k: v for k, v in fitted.items() if k != "fit_sha256"}),
        "policy fit checksum mismatch",
    )
    groups, keys = _rollouts(
        contract, registry, validation_rows, partition="validation"
    )
    _require(
        all(t["role"] in {"reference", "validation_policy"} for t in registry.values()),
        "training trials cannot enter validation",
    )
    _require(
        isinstance(held_out_task_ids, list)
        and held_out_task_ids
        and all(isinstance(t, str) and t for t in held_out_task_ids)
        and len(set(held_out_task_ids)) == len(held_out_task_ids)
        and set(held_out_task_ids) == set(contract.partitions["held_out"]),
        "held-out inventory differs from enrollment",
    )
    _require(
        set(fitted["training_task_ids"]) == set(contract.partitions["training"]),
        "fitted training inventory differs from enrollment",
    )
    validation_tasks = sorted({k[0] for k in keys})
    _require(
        not set(validation_tasks) & set(fitted["training_task_ids"]),
        "training/validation task overlap",
    )
    _require(
        not (set(validation_tasks) | set(fitted["training_task_ids"]))
        & set(held_out_task_ids),
        "calibration/held-out task overlap",
    )
    references = [n for n, t in registry.items() if t["role"] == "reference"]
    _require(len(references) == 1, "validation lacks one fixed strong reference")
    reference = groups[references[0]]
    for mode in ARMS:
        trials = [
            n
            for n, t in registry.items()
            if t["mode"] == mode and t["role"] == "validation_policy"
        ]
        _require(
            len(trials) == 1
            and registry[trials[0]]["configuration"] == fitted["configurations"][mode],
            "validation did not execute the exact trained policy",
        )
        _require(
            _quality_delta(groups[trials[0]], reference) >= contract.quality_margin,
            "trained policy fails measured validation quality",
        )
    if contract.stage == "C":
        _validate_guard_admission(guard_admission, fitted["configurations"])
    selections = {
        "schema_version": 1,
        "record_type": "baseline_policy_selection",
        "evidence_kind": contract.evidence_kind,
        "source_stage": contract.stage,
        "contract": contract.__dict__,
        "training_task_ids": fitted["training_task_ids"],
        "validation_task_ids": validation_tasks,
        "held_out_task_ids": list(held_out_task_ids),
        "training_fit_sha256": digest(fitted),
        "validation_records_sha256": digest(validation_rows),
        "configurations": fitted["configurations"],
        "guard_admission": guard_admission,
        "trained_policies": True,
    }
    lock = {"selections": selections, "sha256": digest(selections)}
    FrozenPolicyBundle.from_lock(lock)
    return lock


@dataclass(frozen=True)
class CalibrationTrialBundle:
    """A registered acquisition schedule, never a held-out deployment lock."""

    lock_json: str

    def __post_init__(self):
        lock = _read_json(self.lock_json)
        _require(
            isinstance(lock, dict)
            and set(lock) == {"selections", "sha256"}
            and lock["sha256"] == digest(lock["selections"]),
            "trial authorization digest mismatch",
        )
        value = lock["selections"]
        _require(
            isinstance(value, dict)
            and set(value)
            == {
                "schema_version",
                "record_type",
                "sealed",
                "contract",
                "partition",
                "registry",
                "repetitions",
                "fitted",
                "cells",
            }
            and value["schema_version"] == 1
            and value["record_type"] == "calibration_trial_authorization"
            and value["sealed"] is False,
            "invalid unsealed trial authorization",
        )
        contract = CalibrationContract(**value["contract"])
        contract.validate()
        self._validate_registry(
            contract, value["registry"], value["partition"], value["fitted"]
        )
        _require(
            value["cells"]
            == self._schedule(
                contract, value["registry"], value["partition"], value["repetitions"]
            ),
            "trial schedule differs from enrollment",
        )

    @staticmethod
    def _validate_registry(contract, registry, partition, fitted):
        _require(
            contract.partitions is not None and partition in {"training", "validation"},
            "trial acquisition requires enrolled calibration partitions",
        )
        _require(
            isinstance(registry, dict)
            and registry
            and all(
                isinstance(k, str) and k and isinstance(v, dict)
                for k, v in registry.items()
            ),
            "invalid trial registry",
        )
        roles = defaultdict(list)
        for name, trial in registry.items():
            role, mode = trial.get("role"), trial.get("mode")
            roles[role].append(name)
            if role in {"reference", "cheap_original"}:
                _require(
                    set(trial) == {"role", "mode"}
                    and mode == ("reference" if role == "reference" else "original"),
                    "original trial has policy choices",
                )
            else:
                _require(
                    set(trial) == {"role", "mode", "configuration"}
                    and role
                    in {"static_candidate", "fixed_recipe", "validation_policy"},
                    "unregistered calibration trial",
                )
                _require(
                    role == "validation_policy" or mode == "best_static_joint",
                    "independent rewrite training requires fixed-target recipes",
                )
                validate_configuration(
                    trial["configuration"],
                    mode=mode,
                    models=contract.models,
                    output_cap=contract.output_cap,
                )
                _require(
                    trial["configuration"]["divergence_threshold"]
                    == contract.divergence_threshold,
                    "trial changed the independently selected guard threshold",
                )
        _require(
            len(roles["reference"]) == 1,
            "calibration lacks one original strong reference",
        )
        if partition == "training":
            _require(
                fitted is None
                and len(roles["cheap_original"]) == 1
                and roles["static_candidate"]
                and not roles["validation_policy"],
                "invalid independent training registry",
            )
            _require(
                {
                    registry[n]["configuration"]["static_plan"]["target_model"]
                    for n in roles["fixed_recipe"]
                }
                == set(contract.models.values()),
                "fixed rewrite trials do not cover both targets",
            )
        else:
            _require(
                not roles["cheap_original"]
                and not roles["static_candidate"]
                and not roles["fixed_recipe"],
                "training trials cannot enter validation acquisition",
            )
            _require(
                isinstance(fitted, dict)
                and fitted.get("record_type") == "unsealed_policy_fit"
                and fitted.get("sealed") is False
                and fitted.get("contract") == contract.__dict__
                and fitted.get("fit_sha256")
                == digest({k: v for k, v in fitted.items() if k != "fit_sha256"})
                and set(fitted["training_task_ids"])
                == set(contract.partitions["training"]),
                "validation acquisition requires the unchanged training fit",
            )
            _require(
                len(roles["validation_policy"]) == len(ARMS)
                and {registry[n]["mode"] for n in roles["validation_policy"]}
                == set(ARMS),
                "validation acquisition omits a policy order",
            )
            _require(
                all(
                    registry[n]["configuration"]
                    == fitted["configurations"][registry[n]["mode"]]
                    for n in roles["validation_policy"]
                ),
                "validation changed a fitted policy",
            )

    @staticmethod
    def _schedule(contract, registry, partition, repetitions):
        from bench.joint_campaign import derive_run_seed, ordered_arms

        _require(
            type(repetitions) is int
            and 1 <= repetitions <= 5
            and (contract.stage == "E0" or repetitions == 5),
            "invalid paired calibration repetition count",
        )
        cells = []
        for repetition in range(repetitions):
            for task_id in contract.partitions[partition]:
                for trial_id in ordered_arms(
                    contract.workload_id, task_id, repetition, sorted(registry)
                ):
                    trial = registry[trial_id]
                    arm = (
                        "unmodified_fixed_strong"
                        if trial["role"] == "reference"
                        else "fixed_cheap"
                        if trial["role"] == "cheap_original"
                        else trial["mode"]
                    )
                    cells.append(
                        {
                            "trial_id": trial_id,
                            "task_id": task_id,
                            "arm": arm,
                            "repetition": repetition,
                            "seed": derive_run_seed(
                                contract.workload_id, task_id, arm, repetition
                            ),
                            "ordinal": len(cells),
                        }
                    )
        return cells

    @classmethod
    def create(cls, contract, registry, *, partition, fitted=None, repetitions=5):
        contract.validate()
        cls._validate_registry(contract, registry, partition, fitted)
        value = {
            "schema_version": 1,
            "record_type": "calibration_trial_authorization",
            "sealed": False,
            "contract": contract.__dict__,
            "partition": partition,
            "registry": registry,
            "repetitions": repetitions,
            "fitted": fitted,
            "cells": cls._schedule(contract, registry, partition, repetitions),
        }
        return cls(_json({"selections": value, "sha256": digest(value)}))

    @property
    def lock(self):
        return json.loads(self.lock_json)

    @property
    def contract(self):
        return CalibrationContract(**self.lock["selections"]["contract"])

    @property
    def cells(self):
        return self.lock["selections"]["cells"]

    def cell(self, ordinal):
        _require(
            type(ordinal) is int and 0 <= ordinal < len(self.cells),
            "unknown calibration ordinal",
        )
        return self.cells[ordinal]

    def validate_cells(self, cells):
        actual = [
            {
                "task_id": c.task_id,
                "arm": c.arm,
                "repetition": c.repetition,
                "seed": c.seed if hasattr(c, "seed") else c.run_seed,
                "ordinal": c.ordinal,
            }
            for c in cells
        ]
        _require(
            actual
            == [{k: v for k, v in c.items() if k != "trial_id"} for c in self.cells],
            "worker cells differ from registered calibration schedule",
        )

    def storage_path(self, root, ordinal):
        cell = self.cell(ordinal)
        return (
            Path(root)
            / ("trial-" + digest(cell["trial_id"]))
            / cell["arm"]
            / f"rep-{cell['repetition']}"
        )

    def receipt_metadata(self, ordinal):
        return {
            "calibration_trial_id": self.cell(ordinal)["trial_id"],
            "calibration_authorization_sha256": self.lock["sha256"],
        }

    def authorize(
        self,
        *,
        workload_id,
        models,
        task_ids,
        stage,
        paper_evidence,
        network_policy,
        output_cap,
        task_universe_sha256,
        protocol_sha256,
        quality_margin,
        cell=None,
    ):
        contract = self.contract
        _require(
            workload_id == contract.workload_id
            and models == contract.models
            and output_cap == contract.output_cap
            and quality_margin == contract.quality_margin
            and task_universe_sha256 == contract.task_universe_sha256
            and protocol_sha256 == contract.protocol_sha256,
            "trial authorization differs from workload enrollment",
        )
        _require(
            stage == contract.stage
            and (
                stage == "E0"
                and paper_evidence is False
                and network_policy == "loopback_only"
                or stage == "C"
                and paper_evidence is True
                and network_policy == "provider_allowed"
            ),
            "unsealed calibration choices cannot enter held-out deployment",
        )
        _require(
            task_ids
            and set(task_ids)
            <= set(contract.partitions[self.lock["selections"]["partition"]]),
            "trial requested tasks outside its enrolled partition",
        )
        _require(
            isinstance(cell, dict)
            and "ordinal" in cell
            and cell == self.cell(cell["ordinal"]),
            "trial request differs from enrolled cell",
        )

    def policy(self, mode, *, schedule_ordinal=None):
        trial = self.lock["selections"]["registry"][
            self.cell(schedule_ordinal)["trial_id"]
        ]
        _require(
            trial["role"] not in {"reference", "cheap_original"}
            and trial["mode"] == mode,
            "trial has no such request policy",
        )
        return _native_policy(mode, self.lock["sha256"], trial["configuration"])


def _native_policy(mode, lock_sha256, configuration):
    from agentc import _native
    from agentc._evaluation_policy import FrozenEvaluationPolicy, VERSION

    _require(
        mode in ARMS and _native.optimize_evaluation_policy_version() == VERSION,
        "native frozen policy contract is unavailable",
    )
    return FrozenEvaluationPolicy.from_configuration(
        mode=mode, calibration_lock_sha256=lock_sha256, configuration=configuration
    )


@dataclass(frozen=True)
class FrozenPolicyBundle:
    """Immutable sealed choices with stage, task and model authorization gates."""

    lock_json: str

    def __post_init__(self):
        self._validate_lock(_read_json(self.lock_json))

    @classmethod
    def engineering_fixture(cls, contract, configurations, *, task_ids):
        """Declare mechanisms explicitly; never label these choices trained."""
        _require(
            contract.stage == "E0" and contract.evidence_kind == "engineering_fixture",
            "declared choices are E0 mechanisms only",
        )
        marker = digest(configurations)
        selections = {
            "schema_version": 1,
            "record_type": "baseline_policy_selection",
            "source_stage": "E0",
            "evidence_kind": "engineering_fixture",
            "selection_method": "declared_mechanism_configuration",
            "trained_policies": False,
            "contract": contract.__dict__,
            "configurations": configurations,
            "training_task_ids": ["fixture-declaration-training-" + marker],
            "validation_task_ids": ["fixture-declaration-validation-" + marker],
            "held_out_task_ids": list(task_ids),
        }
        return cls.from_lock({"selections": selections, "sha256": digest(selections)})

    @classmethod
    def from_lock(cls, lock):
        return cls(_json(lock))

    @staticmethod
    def _validate_lock(lock):
        _require(
            isinstance(lock, dict)
            and set(lock) == {"selections", "sha256"}
            and lock["sha256"] == digest(lock["selections"]),
            "policy lock digest mismatch",
        )
        selections = lock["selections"]
        _require(
            isinstance(selections, dict)
            and selections.get("schema_version") == 1
            and selections.get("record_type") == "baseline_policy_selection",
            "unknown policy lock schema",
        )
        try:
            contract = CalibrationContract(**selections["contract"])
            contract.validate()
        except (TypeError, ValueError) as error:
            raise CalibrationError("invalid locked calibration contract") from error
        _require(
            selections["source_stage"] == contract.stage
            and selections["evidence_kind"] == contract.evidence_kind,
            "policy lock changes evidence provenance",
        )
        partitions = [
            selections.get(name)
            for name in (
                "training_task_ids",
                "validation_task_ids",
                "held_out_task_ids",
            )
        ]
        _require(
            all(
                isinstance(ids, list)
                and ids
                and all(isinstance(t, str) and t for t in ids)
                and len(set(ids)) == len(ids)
                for ids in partitions
            ),
            "invalid locked task partitions",
        )
        _require(
            sum(len(ids) for ids in partitions)
            == len(set().union(*map(set, partitions))),
            "locked task partitions overlap",
        )
        if contract.partitions is not None:
            _require(
                all(
                    set(ids) == set(contract.partitions[name])
                    for name, ids in zip(
                        ("training", "validation", "held_out"), partitions
                    )
                ),
                "locked task partitions differ from enrollment",
            )
        _require(
            set(selections.get("configurations", {})) == set(ARMS),
            "policy lock omits an arm",
        )
        for mode, configuration in selections["configurations"].items():
            validate_configuration(
                configuration,
                mode=mode,
                models=contract.models,
                output_cap=contract.output_cap,
            )
            _require(
                configuration["divergence_threshold"] == contract.divergence_threshold,
                "locked policy changes the selected guard threshold",
            )
        if contract.stage == "C":
            _require(
                selections.get("trained_policies") is True
                and isinstance(selections.get("guard_admission"), dict),
                "provider lock lacks calibrated guard admission",
            )
            _validate_guard_admission(
                selections["guard_admission"], selections["configurations"]
            )

    @property
    def lock(self):
        return json.loads(self.lock_json)

    @property
    def contract(self):
        return CalibrationContract(**self.lock["selections"]["contract"])

    def authorize(
        self,
        *,
        workload_id,
        models,
        task_ids,
        stage,
        paper_evidence,
        network_policy,
        output_cap,
        task_universe_sha256,
        protocol_sha256,
        quality_margin,
        cell=None,
    ):
        contract = self.contract
        _require(
            workload_id == contract.workload_id
            and models == contract.models
            and output_cap == contract.output_cap
            and quality_margin == contract.quality_margin
            and task_universe_sha256 == contract.task_universe_sha256,
            "policy lock differs from workload enrollment",
        )
        _require(
            protocol_sha256 == contract.protocol_sha256,
            "policy lock differs from frozen protocol",
        )
        if contract.stage == "E0":
            _require(
                stage == "E0"
                and paper_evidence is False
                and network_policy == "loopback_only",
                "fixture policies cannot become provider or paper calibration",
            )
        else:
            _require(
                stage in {"P", "T"} and network_policy == "provider_allowed",
                "provider policies require held-out deployment",
            )
        _require(
            task_ids
            and set(task_ids) <= set(self.lock["selections"]["held_out_task_ids"]),
            "policy deployment task was not sealed as held out",
        )

    def policy(self, mode, *, schedule_ordinal=None):
        _require(mode in ARMS, "unknown frozen policy mode")
        return _native_policy(
            mode, self.lock["sha256"], self.lock["selections"]["configurations"][mode]
        )


def _read_json(raw):
    def object_pairs(pairs):
        result = dict(pairs)
        _require(len(result) == len(pairs), "duplicate calibration artifact key")
        return result

    return json.loads(
        raw,
        object_pairs_hook=object_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            CalibrationError("nonfinite calibration artifact number")
        ),
    )


def read_artifact(path):
    return _read_json(Path(path).read_bytes())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("fit", "seal"))
    for field in ("contract", "registry", "rollouts", "output"):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--fitted", type=Path)
    parser.add_argument("--held-out-tasks", type=Path)
    parser.add_argument("--guard-admission", type=Path)
    args = parser.parse_args(argv)
    contract = CalibrationContract(**read_artifact(args.contract))
    registry, rows = read_artifact(args.registry), read_artifact(args.rollouts)
    if args.operation == "fit":
        result = fit_policies(contract, registry, rows)
    else:
        _require(
            args.fitted is not None and args.held_out_tasks is not None,
            "sealing requires the fitted artifact and held-out inventory",
        )
        result = seal_policies(
            contract,
            read_artifact(args.fitted),
            registry,
            rows,
            held_out_task_ids=read_artifact(args.held_out_tasks),
            guard_admission=read_artifact(args.guard_admission)
            if args.guard_admission
            else None,
        )
        FrozenPolicyBundle.from_lock(result)
    private_write(args.output, result)
    print(
        _json(
            {
                "operation": args.operation,
                "source_stage": contract.stage,
                "evidence_kind": contract.evidence_kind,
                "artifact_sha256": digest(result),
                "provider_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
