"""No-network protocol/preflight for the multi-step rules × routing study.

This is structural validation with synthetic outcomes, not a performance or
quality experiment. No transport, credentials, fixture gold, or legacy stub
agent is imported. A later live runner must preserve each arm's own history.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import tempfile
from collections import Counter
from copy import deepcopy
from pathlib import Path

from bench.openrouter_frontier import POLICY_SETTINGS, SOURCE_MODEL
from bench.openrouter_matrix import PROTOCOL, ROOT, file_hash, load_module, write_json
from bench.openrouter_pilot import PilotError, canonical, digest
from bench.openrouter_replay import public_task

STAGES = ("filter", "synthesize", "answer")
NON_ROUTING_RULES = ("ContextCompress", "StateDrop", "PromptDedup", "OutputBudget", "StructuredTruncation")
UNAVAILABLE = {
    "DeadOutputTruncation": "bd-0nfp: trace-wide evidence can contaminate a different call site",
    "CacheHit": "bd-qmi0/bd-k52s: guarded cold-start and cache attribution are not validated",
    "ParallelBranch": "bd-6m0o: no parallel peers in this dependent synchronous workflow",
}


def prompt_constants(path=ROOT / "bench/agents/research_planner.py"):
    """Read only literal prompts; importing the old agent could enable gold stubs."""
    names = {"FILTER_SYSTEM", "SYNTH_SYSTEM", "ANSWER_SYSTEM"}
    values = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    values[target.id] = ast.literal_eval(node.value)
    if values.keys() != names or not all(isinstance(v, str) and v for v in values.values()):
        raise PilotError("research workflow prompt contract changed")
    return values


def workflow_call(task, stage, history, attention, *, model=SOURCE_MODEL, run_identity="protocol", prompts=None):
    if stage not in STAGES or set(history) != set(STAGES[:STAGES.index(stage)]):
        raise PilotError("workflow needs exactly this arm's preceding stage outputs")
    if not all(isinstance(value, str) for value in history.values()):
        raise PilotError("workflow outputs must be strings")
    task = public_task(task)  # strip expected answer and supporting labels at seam
    prompts = prompt_constants() if prompts is None else prompts
    system_key = {"filter": "FILTER_SYSTEM", "synthesize": "SYNTH_SYSTEM", "answer": "ANSWER_SYSTEM"}[stage]
    messages = [{"role": "system", "content": prompts[system_key]}]
    deps = [{"kind": "literal"}]
    reads = []
    if stage == "filter":
        for paragraph in task["meta"]["paragraphs"]:
            messages.append({"role": "user", "content": paragraph["title"] + "\n" + " ".join(paragraph["sentences"])})
            deps.append({"kind": "literal"})
    else:
        messages.append({"role": "user", "content": history["filter"]})
        deps.append({"kind": "state", "key": "filter_result"})
        if stage == "synthesize":
            reads = ["filter_result"]
        else:
            messages.append({"role": "user", "content": history["synthesize"]})
            deps.append({"kind": "state", "key": "synthesis"})
            reads = ["synthesis"]
    span_id = digest([run_identity, task["task_id"], stage])[:16]
    messages.append({"role": "user", "content": "Question: " + task["prompt"]})
    deps.append({"kind": "user_input", "span_id": span_id})
    scores, follow = attention.compute_attention_scores(messages, None)
    return {"call_site_id": "research-rules-v1/" + stage,
        "trace_id": digest([run_identity, task["task_id"]])[:32], "span_id": span_id,
        "model": model, "messages": messages, "tools": [], "input_deps": deepcopy(deps), "occurrence_ix": 0,
        "parameters": {"max_output_tokens": 512, "temperature": 0.0, "extra": {
            "attention_scores": scores, "follow_on_tokens": follow, "dead_attention_epsilon": .15,
            "message_deps": deps, "window_state_reads": reads,
            "agentc_route_context": {"provider_protocol": PROTOCOL, "provider_namespace": "openrouter",
                "input_tokens_upper_bound": len(canonical(messages)) + 1024 + 64*len(messages),
                "input_tokens_upper_bound_basis": "json_utf8_bytes_v1", "image_input": False,
                "tool_calling": False, "structured_outputs": False, "streaming": False}}}}


def protocol_policies():
    # Preserve native per-rule accuracy budgets. Do not inherit the CC-only
    # study's global .05 override or choose new thresholds from heldout scores.
    common = {k: v for k, v in POLICY_SETTINGS.items() if k != "AGENTC_SHADOW_DIVERGENCE_BUDGET"}
    common.update(AGENTC_OPTIMIZE_EXPLORATION_CALLS_PER_SITE_24H="160", AGENTC_OPTIMIZE_MAX_REWRITE_DEPTH="3")
    rules = ",".join(NON_ROUTING_RULES)
    return [
        {"name": "original", "settings": {**common, "AGENTC_OPTIMIZE": "0", "AGENTC_OPTIMIZE_EXPLORATION": "0", "AGENTC_ENABLED_RULES": ""}},
        {"name": "historical_rules", "settings": {**common, "AGENTC_EVAL_PLANNER_MODE": "current_greedy", "AGENTC_COMPOSE": "0", "AGENTC_ENABLED_RULES": rules, "AGENTC_OPTIMIZE_EXPLORATION": "0"}},
        {"name": "guarded_rules", "settings": {**common, "AGENTC_ENABLED_RULES": rules}},
        {"name": "routing_only", "settings": {**common, "AGENTC_ENABLED_RULES": "ModelDowngrade"}},
        {"name": "joint", "settings": {**common, "AGENTC_ENABLED_RULES": rules + ",ModelDowngrade"}},
    ]


def activation(original, plan):
    selected = plan.get("call", original)
    rules = [r["rule"] for r in plan.get("rules", [])] if plan.get("kind") == "composed" else ([plan["rule"]] if "rule" in plan else [])
    return {"kind": plan["kind"], "selected_rules": rules,
        "messages_changed": selected["messages"] != original["messages"],
        "output_cap_changed": selected["parameters"]["max_output_tokens"] != original["parameters"]["max_output_tokens"],
        "model_changed": selected["model"] != original["model"],
        "original_messages": len(original["messages"]), "selected_messages": len(selected["messages"]),
        "selected_max_tokens": selected["parameters"]["max_output_tokens"],
        "executed_on_provider": False, "measured_token_savings": None}


def synthetic_outcome(call, stage):
    """Invented mechanical profile ONLY; never a result or cost measurement."""
    return {"call_site_id": call["call_site_id"], "executed_model_id": call["model"],
        "input_tokens": max(1, len(canonical(call["messages"]))//4),
        "output_tokens": min({"filter": 120, "synthesize": 24, "answer": 8}[stage], call["parameters"]["max_output_tokens"]),
        "latency_ms": 100, "cost_usd": .01 if call["model"] == SOURCE_MODEL else .001,
        "output_is_structured": False, "output_is_short": stage != "filter"}


def preflight(native, attention, catalog, task, repetitions=32):
    if repetitions < 3:
        raise PilotError("preflight requires warmup plus observed decisions")
    saved = {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
    prompts, records = prompt_constants(), []
    try:
        for policy in protocol_policies():
            for key in list(os.environ):
                if key.startswith("AGENTC_"):
                    os.environ.pop(key)
            os.environ.update(policy["settings"])
            with tempfile.TemporaryDirectory(prefix="agentc-NO-NETWORK-rules-") as storage:
                native.optimize_configure(storage, catalog_json=json.dumps(catalog))
                try:
                    for occurrence in range(repetitions):
                        history = {}
                        for stage in STAGES:
                            call = workflow_call(task, stage, history, attention, prompts=prompts,
                                run_identity=f"preflight/{policy['name']}/{occurrence}")
                            encoded = native.optimize_plan(json.dumps(call))
                            plan = json.loads(encoded)
                            if plan["kind"] not in {"pass_through", "rewritten", "composed"}:
                                raise PilotError("unavailable dispatch plan in workflow preflight")
                            selected = plan.get("call", call)
                            token = native.optimize_observe(encoded, json.dumps(synthetic_outcome(selected, stage)))
                            if policy["name"] != "original" and not token:
                                raise PilotError("missing attributable synthetic observation")
                            candidate = None
                            exploration = plan.get("agentc_exploration_context")
                            if exploration:
                                candidate_plan = exploration["candidate_plan"]
                                candidate = activation(call, candidate_plan)
                                if not native.optimize_complete_exploration(exploration["lease_token"],
                                        json.dumps(synthetic_outcome(candidate_plan["call"], stage)), 0.0):
                                    raise PilotError(f"synthetic exploration was rejected: {policy['name']}/{occurrence}/{stage}")
                            elif plan["kind"] != "pass_through":
                                # Structural preflight supplies perfect synthetic agreement.
                                # This is NOT the live shadow sampling/quality protocol.
                                native.optimize_record_divergence(token, 0.0)
                            records.append({"policy": policy["name"], "stage": stage, "occurrence": occurrence,
                                "primary": activation(call, plan), "exploration": candidate})
                            history[stage] = {"filter": "SYNTHETIC passage text; no semantic result. " * 40,
                                "synthesize": "SYNTHETIC draft; no semantic result.", "answer": "SYNTHETIC"}[stage]
                finally:
                    native.optimize_reset()
    finally:
        for key in list(os.environ):
            if key.startswith("AGENTC_"):
                os.environ.pop(key)
        os.environ.update(saved)
    counts = Counter((r["policy"], r["stage"], rule) for r in records for rule in r["primary"]["selected_rules"])
    return {"paper_evidence": False, "evaluation_kind": "no_network_structural_preflight_synthetic_outcomes",
        "provider_calls": 0, "quality_claim": None, "measured_cost_savings": None,
        "policies": protocol_policies(), "unavailable_rules": UNAVAILABLE,
        "selected_primary_rule_counts": [{"policy": p, "stage": s, "rule": r, "count": n} for (p, s, r), n in sorted(counts.items())],
        "decisions": records,
        "limitations": ["State read annotations are an explicit workload contract, not proof of semantic irrelevance.",
            "Synthetic identical feedback is only for structural exercise; no quality/safety/cost conclusions.",
            "StructuredTruncation needs real ToolOutput provenance; this workflow supplies none.",
            "PromptDedup is measured only if genuine duplicate passages occur; no duplicates are inserted to force it.",
            "Independent route-then-rewrite remains a separate calibration-frozen or isolated-controller baseline to implement.",
            "Selected output-cap changes do not establish actual output-token savings."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("frontier", "extended", "native", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        if os.environ.get("PYTHONHASHSEED") != "0":
            raise PilotError("set PYTHONHASHSEED=0")
        manifest = json.loads((args.frontier / "manifest.json").read_text())
        if file_hash(args.extended) != manifest["fixtures"]["extended"]:
            raise PilotError("preflight fixture changed")
        # A previously exposed warmup question, never new heldout labels.
        task_id = next(r["task_id"] for r in manifest["schedule"] if r["phase"] == "warmup")
        task = public_task(next(t for t in json.loads(args.extended.read_text()) if t["task_id"] == task_id))
        native = load_module("_native", args.native, native=True)
        attention = load_module("rules_attention", ROOT / "python/agentc/_attention.py")
        report = preflight(native, attention, manifest["catalog"], task)
        report.update(native_sha256=file_hash(args.native), protocol_source_sha256=file_hash(Path(__file__)),
            workflow_source_sha256=file_hash(ROOT / "bench/agents/research_planner.py"),
            frontier_manifest_sha256=digest(manifest), public_task_sha256=digest(task))
        write_json(args.output, report, immutable=True)
        print(json.dumps({k: report[k] for k in ("provider_calls", "evaluation_kind", "selected_primary_rule_counts")}))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Rule protocol preflight stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
