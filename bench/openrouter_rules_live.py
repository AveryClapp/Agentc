"""Capped, development-only on-policy rules × routing workflow acquisition.

Each arm consumes its own outputs. Native controllers see only selected paid
responses and their lexical comparisons, never task gold. The separate static
router uses labeled calibration workflows, with all candidate calls charged.
Resume rebuilds isolated native stores from the immutable paid event stream;
it fails closed if any provider-visible decision changes. This is not a test
of native crash recovery or a confirmatory evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from bench.openrouter_contract import endpoints
from bench.openrouter_frontier import SOURCE_MODEL, outcome
from bench.openrouter_matrix import ROOT, file_hash, load_module, score, write_json
from bench.openrouter_pilot import (
    Ledger,
    PilotError,
    ProviderFailure,
    digest,
    load_key,
    make_request,
    money,
)
from bench.openrouter_replay import lexical_divergence, public_task, shadow_sample
from bench.openrouter_rules_protocol import (
    STAGES,
    UNAVAILABLE,
    activation,
    prompt_constants,
    protocol_policies,
    workflow_call,
)

TARGET_MODEL = "anthropic/claude-haiku-4.5"
RUNTIME_COMMIT = "eb6d78a"
RUNTIME_SHA256 = "3cb3ae2c92ca1402115aade8b973cbd11ded10d1c5b9e8c6cc689f64de87ab59"
ARMS = (
    "original",
    "historical_rules",
    "guarded_rules",
    "routing_only",
    "sequential",
    "joint",
)


class CallLimit(Exception):
    """A requested prefix completed; no new dispatch is allowed."""


def sources():
    paths = (
        "bench/openrouter_rules_live.py",
        "bench/openrouter_rules_protocol.py",
        "bench/openrouter_pilot.py",
        "bench/openrouter_matrix.py",
        "bench/openrouter_frontier.py",
        "bench/openrouter_contract.py",
        "bench/openrouter_replay.py",
        "bench/agents/research_planner.py",
        "python/agentc/_attention.py",
        "bench/openrouter_attempts.py",
        "bench/openrouter_transport.py",
        "bench/openrouter_joint_study.py",
    )
    return {p: file_hash(ROOT / p) for p in paths}


def runtime_sources(runtime_commit=RUNTIME_COMMIT):
    """Bind the separately reviewed native build to immutable Git sources."""
    commit = subprocess.check_output(
        ["git", "rev-parse", runtime_commit], cwd=ROOT, text=True
    ).strip()
    paths = subprocess.check_output(
        [
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            commit,
            "Cargo.toml",
            "Cargo.lock",
            "crates",
        ],
        cwd=ROOT,
        text=True,
    ).splitlines()
    selected = [p for p in paths if p.endswith((".rs", "Cargo.toml", "Cargo.lock"))]
    hashes = {
        p: hashlib.sha256(
            subprocess.check_output(["git", "show", commit + ":" + p], cwd=ROOT)
        ).hexdigest()
        for p in selected
    }
    return {"commit": commit, "source_files": hashes}


def payload_for(call, manifest):
    params = call["parameters"]
    cap = params["max_output_tokens"]
    if (
        call["model"] not in manifest["endpoints"]
        or call.get("tools")
        or type(cap) is not int
        or not 1 <= cap <= 512
        or params.get("temperature") != 0
    ):
        raise PilotError("workflow request exceeds frozen model/parameter coverage")
    return make_request(
        call["model"],
        [manifest["endpoints"][call["model"]]["tag"]],
        call["messages"],
        max_tokens=cap,
    )


def policy_settings(arm):
    policies = {p["name"]: p["settings"] for p in protocol_policies()}
    # A static model is chosen from independent calibration first. Its own
    # guarded rewrite controller never consumes routing-only or joint feedback.
    return deepcopy(policies["guarded_rules" if arm == "sequential" else arm])


def schedule_for(ids, excluded, calibration, development):
    if calibration < 1 or development < 4 or len(ids) != len(set(ids)):
        raise PilotError(
            "need unique tasks, calibration, and at least four development workflows"
        )
    eligible = sorted(
        set(ids) - set(excluded), key=lambda t: digest(["rules-live-development-v1", t])
    )
    if len(eligible) < calibration + development:
        raise PilotError("insufficient unexposed development questions")
    schedule = []
    for i, task_id in enumerate(eligible[: calibration + development]):
        phase = (
            "calibration"
            if i < calibration
            else "warmup"
            if i < calibration + 3
            else "development"
        )
        arms = (
            ["calibration/" + m for m in (SOURCE_MODEL, TARGET_MODEL)]
            if phase == "calibration"
            else ARMS
        )
        block = [{"task_id": task_id, "phase": phase, "arm": arm} for arm in arms]
        block.sort(key=lambda r: digest(["rules-live-arm-order-v1", r]))
        schedule.extend(block)
    return schedule


def prepare(args, key):
    if (args.output / "manifest.json").exists():
        raise PilotError("workflow manifest already frozen")
    is_study = getattr(args, "joint_study", False)
    expected_native = args.native_sha256 if is_study else RUNTIME_SHA256
    if (
        file_hash(args.native) != expected_native
        or os.environ.get("PYTHONHASHSEED") != "0"
    ):
        raise PilotError("use reviewed native eb6d78a and PYTHONHASHSEED=0")
    frontier = json.loads(args.frontier.read_text())
    previous = json.loads(args.previous.read_text())
    tasks = json.loads(args.fixture.read_text())
    if file_hash(args.fixture) != frontier["fixtures"]["extended"]:
        raise PilotError("development requires the frozen extended-context fixture")
    excluded = set(frontier["excluded_question_ids"]) | {
        r["task_id"] for r in frontier["schedule"]
    }
    excluded.update(r["task_id"] for r in previous["schedule"])
    if is_study:
        aborted = json.loads(args.aborted.read_text())
        excluded.update(r["task_id"] for r in aborted["schedule"])
    schedule = schedule_for(
        [t["task_id"] for t in tasks], excluded, args.calibration, args.development
    )
    observed = datetime.now(timezone.utc).isoformat()
    current = endpoints(key)  # read-only live provider capability/price verification
    selected = {m: current[m] for m in (SOURCE_MODEL, TARGET_MODEL)}
    catalog = deepcopy(frontier["catalog"])
    catalog["targets"] = [t for t in catalog["targets"] if t["model_id"] in selected]
    version = "openrouter-rules-dev-v1-" + digest(selected)[:20]
    catalog.update(
        catalog_version=version, price_table_version=version, observed_at_utc=observed
    )
    for target in catalog["targets"]:
        model = target["model_id"]
        e = selected[model]
        url = "https://openrouter.ai/api/v1/models/" + model + "/endpoints"
        target.update(
            model_version=model + "@" + version + "/" + e["tag"],
            max_output_tokens=512,
            context_window_tokens=min(65536, e["context_length"]),
        )
        target["price"] = {
            "input_per_million_tokens_usd": float(
                money(e["pricing"]["prompt"]) * 1000000
            ),
            "output_per_million_tokens_usd": float(
                money(e["pricing"]["completion"]) * 1000000
            ),
            "table_version": version,
            "source_url": url,
            "observed_at_utc": observed,
        }
        target["provenance"] = {
            "catalog_version": version,
            "source_url": url,
            "observed_at_utc": observed,
        }
    manifest = {
        "schema_version": 1,
        "kind": "live_six_arm_development",
        "paper_evidence": False,
        "created_at": observed,
        "source_files": sources(),
        "native_sha256": file_hash(args.native),
        "runtime": runtime_sources(args.runtime_commit)
        if is_study
        else runtime_sources(),
        "fixture_sha256": file_hash(args.fixture),
        "context": "extended",
        "frontier_manifest_sha256": digest(frontier),
        "previous_manifest_sha256": digest(previous),
        "excluded_question_ids": sorted(excluded),
        "schedule": schedule,
        "catalog": catalog,
        "endpoints": selected,
        "stage_cap_usd": "1",
        "source_model": SOURCE_MODEL,
        "target_model": TARGET_MODEL,
        "maximum_reconstruction_age_seconds": 7200,
        "policies": {arm: policy_settings(arm) for arm in ARMS},
        "prompts": prompt_constants(),
        "shadow_seed": "rules-live-shadow-v1",
        "scoring": "unchanged_normalized_raw_answer_EM_F1",
        "sequential_selection": "cheapest whole-workflow calibration mean nominal cost with mean final F1 >= source minus 0.02; cost then model tie break",
        "risk_margin": 0.02,
        "unavailable_rules": UNAVAILABLE,
        "limitations": [
            "Development-only; no hypothesis test or joint-system benefit claimed from this screen.",
            "One public QA workload, fresh within this campaign; prior repository exposure is not ruled out.",
            "Historical arm is the safe non-routing subset, not every registered legacy rule.",
            "Sequential is a labeled-calibration global model followed by independent guarded rewrites, not an adaptive per-site router.",
            "All primary, exploration, shadow, and static-router calibration calls are charged.",
            "Controller sees actual billed cost; nominal uncached repricing is reported separately.",
            "Implicit provider caching and interleaved arm execution can affect billed cost; latency is diagnostic.",
            "Resume reconstructs isolated stores from paid observations and validates decisions; it is not native recovery evidence.",
            "Lexical divergence is not task correctness; selected token caps are not measured token savings.",
            "StateDrop read annotations are an explicit workload contract, not proof of semantic irrelevance.",
        ],
    }
    if is_study:
        from bench.openrouter_joint_study import finalize_manifest

        manifest = finalize_manifest(manifest, aborted)
    attention = load_module(
        "rules_prepare_attention", ROOT / "python/agentc/_attention.py"
    )
    by_id = {t["task_id"]: t for t in tasks}
    for task_id in {r["task_id"] for r in schedule}:
        payload_for(
            workflow_call(
                public_task(by_id[task_id]),
                "filter",
                {},
                attention,
                prompts=manifest["prompts"],
            ),
            manifest,
        )
    write_json(args.output / "manifest.json", manifest, immutable=True)
    return {
        "manifest_sha256": digest(manifest),
        "scheduled_workflows": len(schedule),
        "minimum_primary_calls": len(schedule) * 3,
        "stage_cap_usd": manifest["stage_cap_usd"],
    }


def phase_settings(manifest, arm, phase):
    settings = deepcopy(manifest["policies"][arm])
    if phase == "heldout":
        settings.update(AGENTC_OPTIMIZE_EXPLORATION="0", AGENTC_OPTIMIZE_SHADOW="0")
    return settings


def semantic_plan(call, plan, manifest):
    if manifest.get("kind") == "prospective_joint_input_rules_v1":
        from bench.openrouter_joint_study import validate_plan

        validate_plan(call, plan)
    if plan.get("kind") not in {"pass_through", "rewritten", "composed"}:
        raise PilotError("uncovered live dispatch plan")
    candidate = plan.get("agentc_exploration_context", {}).get("candidate_plan")
    if candidate and plan["kind"] != "pass_through":
        raise PilotError("exploration must accompany the reference primary")

    def identity(value):
        context = value.get("agentc_observation_context", {})
        return {
            k: context[k]
            for k in ("key", "divergence_threshold", "runtime_version", "call_site_id")
            if k in context
        }

    return {
        "kind": plan["kind"],
        "primary": payload_for(plan.get("call", call), manifest),
        "primary_rules": activation(call, plan)["selected_rules"],
        "primary_identity": identity(plan),
        "candidate_identity": identity(candidate) if candidate else None,
        "candidate": payload_for(candidate["call"], manifest) if candidate else None,
        "candidate_rules": activation(call, candidate)["selected_rules"]
        if candidate
        else [],
    }


def static_selection(decisions, calls, tasks, manifest):
    """Only labeled calibration workflows enter this non-native baseline."""
    by_id = {r["id"]: r for r in calls}
    expected_ids = {
        r["task_id"] for r in manifest["schedule"] if r["phase"] == "calibration"
    }
    cells = []
    for model in (SOURCE_MODEL, TARGET_MODEL):
        rows = [d for d in decisions if d["arm"] == "calibration/" + model]
        answers = [d for d in rows if d["workflow_stage"] == "answer"]
        if (
            len(rows) != len(expected_ids) * 3
            or len(answers) != len(expected_ids)
            or {d["task_id"] for d in answers} != expected_ids
        ):
            raise PilotError("static router requires complete independent calibration")
        f1 = sum(
            score(by_id[d["primary_id"]]["answer"], tasks[d["task_id"]]["expected"])[
                "f1"
            ]
            for d in answers
        ) / len(answers)
        cost = sum(
            (money(by_id[d["primary_id"]]["nominal_uncached_cost_usd"]) for d in rows),
            Decimal(0),
        )
        cells.append(
            {
                "model": model,
                "mean_final_f1": f1,
                "nominal_cost_usd": str(cost),
                "workflows": len(answers),
            }
        )
    source_f1 = next(c["mean_final_f1"] for c in cells if c["model"] == SOURCE_MODEL)
    eligible = [
        c for c in cells if c["mean_final_f1"] >= source_f1 - manifest["risk_margin"]
    ]
    selected = min(eligible, key=lambda c: (money(c["nominal_cost_usd"]), c["model"]))[
        "model"
    ]
    return {
        "model": selected,
        "cells": cells,
        "all_candidate_calibration_costs_charged": True,
        "safety_guarantee": False,
        "calibration_only": True,
    }


def summarize(decisions, calls, manifest, tasks):
    by_id = {r["id"]: r for r in calls}
    calibration = [r for r in calls if r["phase"] == "calibration"]
    reports = []
    for arm in ARMS:
        selected = [d for d in decisions if d["arm"] == arm]
        answers = [
            d
            for d in selected
            if d["workflow_stage"] == "answer" and d["phase"] == "development"
        ]
        charged = [r for r in calls if r["arm"] == arm] + (
            calibration if arm == "sequential" else []
        )
        reports.append(
            {
                "arm": arm,
                "completed_decisions": len(selected),
                "development_workflows": len(answers),
                "development_mean_f1": sum(
                    score(
                        by_id[d["primary_id"]]["answer"],
                        tasks[d["task_id"]]["expected"],
                    )["f1"]
                    for d in answers
                )
                / len(answers)
                if answers
                else None,
                "setup_inclusive_billed_usd": str(
                    sum((money(r["cost_usd"]) for r in charged), Decimal(0))
                ),
                "setup_inclusive_nominal_usd": str(
                    sum(
                        (money(r["nominal_uncached_cost_usd"]) for r in charged),
                        Decimal(0),
                    )
                ),
                "calls_by_scope": dict(Counter(r["scope"] for r in charged)),
                "primary_model_counts": dict(
                    Counter(by_id[d["primary_id"]]["model"] for d in selected)
                ),
                "selected_rule_counts": dict(
                    Counter(
                        r for d in selected for r in d["activation"]["selected_rules"]
                    )
                ),
                "candidate_rule_counts": dict(
                    Counter(
                        r
                        for d in selected
                        for r in d["semantic_plan"]["candidate_rules"]
                    )
                ),
                "truncated_calls": sum(r["finish_reason"] == "length" for r in charged),
                "cached_input_tokens": sum(
                    r["cached_input_tokens"] or 0 for r in charged
                ),
                "cache_accounting_missing_calls": sum(
                    r["cached_input_tokens"] is None for r in charged
                ),
            }
        )
    return {
        "paper_evidence": False,
        "evaluation_kind": manifest["kind"],
        "manifest_sha256": digest(manifest),
        "decisions_sha256": digest(decisions),
        "calls_sha256": digest(calls),
        "completed_calls": len(calls),
        "completed_decisions": len(decisions),
        "scheduled_decisions": len(manifest["schedule"]) * 3,
        "cost_usd": str(sum((money(r["cost_usd"]) for r in calls), Decimal(0))),
        "reports": reports,
        "limitations": manifest["limitations"],
    }


def stage_accounting(ledger, stage, calls):
    from bench.openrouter_attempts import accounting

    with ledger.locked() as handle:
        raw = ledger.read(handle)
    state = accounting(raw)
    events = [e for e in raw if e.get("stage") == stage]
    done = {e["id"]: e for e in events if e["event"] == "result"}
    artifact_ids = {r["id"] for r in calls}
    known = state["known_by_stage"].get(stage, Decimal(0))
    holds = state["holds_by_stage"].get(stage, Decimal(0))
    pending = [e for e in state["pending"] if e["stage"] == stage]
    return {
        "paid_stage_cost_usd": str(known),
        "retained_uncertainty_usd": str(holds),
        "conservative_stage_commitment_usd": str(
            known
            + holds
            + sum((money(e["upper_cost_usd"]) for e in pending), Decimal(0))
        ),
        "ledger_completed_calls": len(done),
        "unincorporated_completed_ids": sorted(set(done) - artifact_ids),
        "artifact_ids_missing_from_ledger": sorted(artifact_ids - set(done)),
        "unresolved_stage_calls": sorted(
            {e["id"] for e in pending}
            | {
                e["id"]
                for e in events
                if e.get("id") in state["failed_calls"] | state["unsafe_ids"]
            }
        ),
    }


class Acquisition:
    def __init__(self, args, manifest, ledger, key):
        self.args, self.manifest, self.ledger, self.key = args, manifest, ledger, key
        self.stage = "rules-live-dev-v1-" + digest(manifest)[:20]
        self.calls = self.read("calls.json")
        self.decisions = self.read("decisions.json")
        self.intents = self.read("intents.json")
        self.call_index = self.decision_index = 0
        self.validate_journals()

    def read(self, name):
        path = self.args.output / name
        value = json.loads(path.read_text()) if path.exists() else []
        if not isinstance(value, list):
            raise PilotError("invalid workflow journal")
        return value

    def validate_journals(self):
        """Reject invalid prefixes before even a cached Ledger.call is visited."""
        steps = [
            {**workflow, "workflow_stage": stage}
            for workflow in self.manifest["schedule"]
            for stage in STAGES
        ]
        if len(self.intents) > len(steps) or len(self.decisions) > len(self.intents):
            raise PilotError("workflow journal exceeds scheduled prefix")
        expected_calls, completed_lengths = [], []
        for i, intent in enumerate(self.intents):
            item = steps[i]
            if any(intent.get(k) != v for k, v in item.items()):
                raise PilotError("workflow intent differs from scheduled prefix")
            signature = semantic_plan(
                intent["original_call"], intent["native_plan"], self.manifest
            )
            if signature != intent["semantic_plan"]:
                raise PilotError("workflow intent signature changed")
            scopes = [
                (
                    "calibration" if item["phase"] == "calibration" else "primary",
                    signature["primary"],
                )
            ]
            if signature["candidate"] is not None:
                scopes.append(("exploration", signature["candidate"]))
            elif signature["kind"] != "pass_through" and shadow_sample(
                self.manifest["shadow_seed"],
                item["arm"] + "/" + item["workflow_stage"],
                item["task_id"],
                float(
                    phase_settings(self.manifest, item["arm"], item["phase"])[
                        "AGENTC_OPTIMIZE_SHADOW"
                    ]
                ),
            ):
                scopes.append(
                    ("shadow", payload_for(intent["original_call"], self.manifest))
                )
            ids = []
            for scope, payload in scopes:
                call_id = self.stage + "-" + digest([item, scope])[:24]
                ids.append(call_id)
                expected_calls.append(
                    {
                        **item,
                        "scope": scope,
                        "id": call_id,
                        "stage": self.stage,
                        "request_sha256": digest(payload),
                        "decision_sha256": digest(signature),
                    }
                )
            completed_lengths.append(len(expected_calls))
            if i < len(self.decisions):
                decision = self.decisions[i]
                if (
                    any(decision.get(k) != v for k, v in item.items())
                    or decision["semantic_plan"] != signature
                    or decision["primary_id"] != ids[0]
                    or decision["incurred_ids"] != ids
                ):
                    raise PilotError("workflow decision differs from intent prefix")
        if len(self.calls) > len(expected_calls):
            raise PilotError("workflow calls exceed intent prefix")
        if self.decisions and completed_lengths[len(self.decisions) - 1] > len(
            self.calls
        ):
            raise PilotError("workflow decision has missing charged calls")
        generations = set()
        for row, expected in zip(self.calls, expected_calls):
            if any(row.get(k) != v for k, v in expected.items()):
                raise PilotError("workflow call differs from intent prefix")
            generation = row.get("generation_id")
            if (
                not isinstance(generation, str)
                or not generation
                or generation in generations
            ):
                raise PilotError("workflow generation is missing or duplicated")
            generations.add(generation)

    def intent(self, item, call, plan):
        record = {
            **item,
            "original_call": call,
            "semantic_plan": semantic_plan(call, plan, self.manifest),
            "native_plan": plan,
        }
        index = self.decision_index
        if index < len(self.intents):
            if {k: v for k, v in self.intents[index].items() if k != "native_plan"} != {
                k: v for k, v in record.items() if k != "native_plan"
            }:
                raise PilotError(
                    "native decision changed during resume; no further dispatch"
                )
        else:
            self.intents.append(record)
            write_json(self.args.output / "intents.json", self.intents)
        return record["semantic_plan"]

    def dispatch(self, item, scope, call, signature):
        if self.args.max_calls is not None and self.call_index >= self.args.max_calls:
            raise CallLimit()
        payload = payload_for(call, self.manifest)
        e = self.manifest["endpoints"][call["model"]]
        metadata = {
            **item,
            "scope": scope,
            "manifest_sha256": digest(self.manifest),
            "decision_sha256": digest(signature),
            "dispatch_contract": {
                "provider_name": e["provider_name"],
                "endpoint_model": e["name"].split(" | ", 1)[1],
            },
        }
        call_id = self.stage + "-" + digest([item, scope])[:24]
        cap = self.manifest["stage_cap_usd"]
        if self.manifest.get("training_cap_usd") and item["phase"] != "heldout":
            cap = self.manifest["training_cap_usd"]
        while True:
            try:
                result = self.ledger.call(
                    self.key, call_id, self.stage, money(cap), payload, metadata
                )
                break
            except ProviderFailure as exc:
                if not self.manifest.get(
                    "bounded_transport_retry"
                ) or not exc.details.get("retryable"):
                    raise
                # Ledger, not this loop, owns exact-request binding and the
                # persisted two-attempt limit. No failed output is observed.
                delay = max(
                    5.0,
                    exc.details.get("retry_not_before_epoch", time.time())
                    - time.time(),
                    exc.details.get("retry_after_seconds", 0.0),
                )
                print(
                    json.dumps(
                        {
                            "transport_retry_wait_seconds": delay,
                            "call_id": call_id,
                            "failure_kind": exc.details["kind"],
                        }
                    ),
                    flush=True,
                )
                while delay > 0:
                    pause = min(delay, 30)
                    time.sleep(pause)
                    delay -= pause
        generation = result.get("generation_id")
        if (
            not isinstance(generation, str)
            or not generation
            or any(
                row["generation_id"] == generation and row["id"] != call_id
                for row in self.calls
            )
        ):
            raise PilotError("workflow generation is missing or duplicated")
        cached = result["usage"].get("prompt_tokens_details", {}).get("cached_tokens")
        if cached is not None and (
            type(cached) is not int
            or not 0 <= cached <= result["usage"]["prompt_tokens"]
        ):
            raise PilotError("invalid cached-token accounting")
        row = {
            **item,
            "scope": scope,
            **{
                k: v for k, v in result.items() if k not in {"key_id", "at", "metadata"}
            },
            "request_sha256": digest(payload),
            "decision_sha256": digest(signature),
            "cached_input_tokens": cached,
            "nominal_uncached_cost_usd": str(
                money(e["pricing"]["prompt"]) * result["usage"]["prompt_tokens"]
                + money(e["pricing"]["completion"])
                * result["usage"]["completion_tokens"]
            ),
        }
        if self.call_index < len(self.calls):
            if self.calls[self.call_index] != row:
                raise PilotError("workflow result differs from immutable ledger")
            row = self.calls[self.call_index]
        else:
            self.calls.append(row)
            write_json(self.args.output / "calls.json", self.calls)
            print(
                json.dumps(
                    {
                        "completed_calls": len(self.calls),
                        **item,
                        "scope": scope,
                        "model": row["model"],
                        "cost_usd": row["cost_usd"],
                    }
                ),
                flush=True,
            )
        self.call_index += 1
        return row

    def record(self, record):
        if self.decision_index < len(self.decisions):
            saved = self.decisions[self.decision_index]
            # Opaque issuance tokens and wall clocks are rebuilt, never forged.
            if {k: v for k, v in saved.items() if k != "native_plan"} != {
                k: v for k, v in record.items() if k != "native_plan"
            }:
                raise PilotError("reconstructed workflow feedback changed")
        else:
            self.decisions.append(record)
            write_json(self.args.output / "decisions.json", self.decisions)
        self.decision_index += 1


def run(args, key):
    manifest = json.loads((args.output / "manifest.json").read_text())
    study = manifest.get("kind") == "prospective_joint_input_rules_v1"
    if study:
        from bench.openrouter_joint_study import validate_manifest

        validate_manifest(manifest)
    runtime = (
        runtime_sources(manifest["runtime"]["commit"]) if study else runtime_sources()
    )
    if (
        manifest["source_files"] != sources()
        or manifest["native_sha256"] != file_hash(args.native)
        or manifest["runtime"] != runtime
        or manifest["fixture_sha256"] != file_hash(args.fixture)
        or os.environ.get("PYTHONHASHSEED") != "0"
    ):
        raise PilotError(
            "frozen workflow sources, native, fixture, or hash seed changed"
        )
    age = (
        datetime.now(timezone.utc) - datetime.fromisoformat(manifest["created_at"])
    ).total_seconds()
    if not 0 <= age <= manifest["maximum_reconstruction_age_seconds"]:
        raise PilotError(
            "development reconstruction window expired; no further dispatch"
        )
    tasks = {t["task_id"]: t for t in json.loads(args.fixture.read_text())}
    public = {task_id: public_task(task) for task_id, task in tasks.items()}
    attention = load_module(
        "live_rules_attention", ROOT / "python/agentc/_attention.py"
    )
    native = load_module("_native", args.native, native=True)
    acquisition = Acquisition(args, manifest, Ledger(args.ledger, key), key)
    saved = {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
    selector = None
    schedule_complete = False
    training_gate = None

    def clear_env():
        for k in list(os.environ):
            if k.startswith("AGENTC_"):
                os.environ.pop(k)

    try:
        with ExitStack() as stack:
            stores = {
                arm: stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="agentc-live-" + arm + "-")
                )
                for arm in ARMS
            }
            for workflow in manifest["schedule"]:
                arm = workflow["arm"]
                is_calibration = workflow["phase"] == "calibration"
                heldout = workflow["phase"] == "heldout"
                if heldout and training_gate is None:
                    from bench.openrouter_joint_study import admission_gate

                    training_gate = admission_gate(
                        manifest, acquisition.decisions, acquisition.calls
                    )
                    write_json(
                        args.output / "training-gate.json",
                        training_gate,
                        immutable=True,
                    )
                    if not training_gate["proceed_to_heldout"]:
                        raise CallLimit()
                if not is_calibration and selector is None:
                    selector = static_selection(
                        acquisition.decisions, acquisition.calls, tasks, manifest
                    )
                    write_json(
                        args.output / "static-selection.json", selector, immutable=True
                    )
                model = (
                    arm.removeprefix("calibration/")
                    if is_calibration
                    else selector["model"]
                    if arm == "sequential"
                    else SOURCE_MODEL
                )
                clear_env()
                os.environ.update(
                    phase_settings(
                        manifest,
                        "original" if is_calibration else arm,
                        workflow["phase"],
                    )
                )
                if not is_calibration:
                    native.optimize_configure(
                        stores[arm], catalog_json=json.dumps(manifest["catalog"])
                    )
                    if (
                        json.loads(native.optimize_model_catalog())
                        != manifest["catalog"]
                    ):
                        raise PilotError("native live catalog changed")
                history = {}
                try:
                    for stage in STAGES:
                        item = {**workflow, "workflow_stage": stage}
                        call = workflow_call(
                            public[workflow["task_id"]],
                            stage,
                            history,
                            attention,
                            model=model,
                            run_identity=acquisition.stage + "/" + arm,
                            prompts=manifest["prompts"],
                        )
                        if study:
                            from bench.openrouter_joint_study import restrict_call

                            call = restrict_call(call, stage)
                        encoded = (
                            json.dumps({"kind": "pass_through"})
                            if is_calibration
                            else native.optimize_plan(json.dumps(call))
                        )
                        plan = json.loads(encoded)
                        if heldout and plan.get("agentc_exploration_context"):
                            raise PilotError("heldout must not issue exploratory calls")
                        signature = acquisition.intent(item, call, plan)
                        primary = acquisition.dispatch(
                            item,
                            "calibration" if is_calibration else "primary",
                            plan.get("call", call),
                            signature,
                        )
                        incurred = [primary["id"]]
                        divergence = None
                        if not is_calibration and not heldout:
                            token = native.optimize_observe(
                                encoded,
                                json.dumps(outcome(primary, call["call_site_id"])),
                            )
                            if arm != "original" and not token:
                                raise PilotError(
                                    "unattributable live primary observation"
                                )
                            exploration = plan.get("agentc_exploration_context")
                            if exploration:
                                candidate = acquisition.dispatch(
                                    item,
                                    "exploration",
                                    exploration["candidate_plan"]["call"],
                                    signature,
                                )
                                incurred.append(candidate["id"])
                                divergence = lexical_divergence(
                                    primary["answer"], candidate["answer"]
                                )
                                if not native.optimize_complete_exploration(
                                    exploration["lease_token"],
                                    json.dumps(
                                        outcome(candidate, call["call_site_id"])
                                    ),
                                    divergence,
                                ):
                                    raise PilotError(
                                        "live exploration completion rejected"
                                    )
                            elif plan["kind"] != "pass_through" and shadow_sample(
                                manifest["shadow_seed"],
                                arm + "/" + stage,
                                workflow["task_id"],
                                float(
                                    manifest["policies"][arm]["AGENTC_OPTIMIZE_SHADOW"]
                                ),
                            ):
                                reference = acquisition.dispatch(
                                    item, "shadow", call, signature
                                )
                                incurred.append(reference["id"])
                                divergence = lexical_divergence(
                                    primary["answer"], reference["answer"]
                                )
                                native.optimize_record_divergence(token, divergence)
                        active = activation(call, plan)
                        active["executed_on_provider"] = True
                        acquisition.record(
                            {
                                **item,
                                "native_plan": plan,
                                "semantic_plan": signature,
                                "primary_id": primary["id"],
                                "incurred_ids": incurred,
                                "activation": active,
                                "divergence_feedback": divergence,
                            }
                        )
                        history[stage] = primary["answer"]
                finally:
                    if not is_calibration:
                        native.optimize_flush()
                        native.optimize_reset()
            if (
                acquisition.call_index != len(acquisition.calls)
                or acquisition.decision_index != len(acquisition.decisions)
                or acquisition.decision_index != len(acquisition.intents)
            ):
                raise PilotError(
                    "workflow reconstruction left an unconsumed journal suffix"
                )
            schedule_complete = True
    except CallLimit:
        pass
    finally:
        native.optimize_reset()
        clear_env()
        os.environ.update(saved)
        # Include partial-decision paid calls, even after a budget/transport stop.
        report = summarize(acquisition.decisions, acquisition.calls, manifest, tasks)
        report.update(
            schedule_complete=schedule_complete,
            reconstructed_calls=acquisition.call_index,
            reconstructed_decisions=acquisition.decision_index,
            training_gate=training_gate,
        )
        report["stage_accounting"] = stage_accounting(
            acquisition.ledger, acquisition.stage, acquisition.calls
        )
        report["artifact_cost_usd"] = report["cost_usd"]
        report["cost_usd"] = report["stage_accounting"]["paid_stage_cost_usd"]
        write_json(args.output / "summary.json", report)
    return {**report, "ledger": acquisition.ledger.summary()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    for name in ("env-file", "ledger", "fixture", "native", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
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
    parser.add_argument("--calibration", type=int, default=4)
    parser.add_argument("--development", type=int, default=8)
    parser.add_argument("--max-calls", type=int)
    args = parser.parse_args()
    try:
        if args.max_calls is not None and args.max_calls < 0:
            raise PilotError("max-calls must be non-negative")
        key = load_key(args.env_file)
        print(
            json.dumps(
                prepare(args, key) if args.command == "prepare" else run(args, key),
                indent=2,
                allow_nan=False,
            )
        )
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Live rules development stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
