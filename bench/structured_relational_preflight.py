"""Real native planner, invented perfect outputs/costs: structural evidence ONLY.

No provider/ledger/key access. The test-only public solver supplies hypothetical
identical results even when requests are rewritten. This cannot establish that
an LLM follows the grammar, keeps quality, reduces tokens, or saves real money.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
import os
from pathlib import Path
import platform
import re
import tempfile

from bench import openrouter_joint_study as study
from bench import openrouter_rules_live as live
from bench import structured_relational_workload as workload
from bench.openrouter_matrix import file_hash, load_module, write_json
from bench.openrouter_pilot import PilotError, digest
from bench.openrouter_replay import public_task, shadow_sample
from bench.openrouter_structured_study import NATIVE_SHA, TEMPLATE_SHA, WORKLOAD

SEED = 2026090588  # Separate structural cases; not development or study data.
ARRIVALS = 67


def synthetic_solver(root):
    """Extract one test helper without importing pytest or production gold code."""
    path = root / "tests/test_structured_relational_workload.py"
    tree = ast.parse(path.read_text())
    functions = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "reference"
    ]
    if len(functions) != 1:
        raise PilotError("test-only synthetic solver changed")
    namespace = {"re": re}
    exec(
        compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"),
        namespace,
    )
    return namespace["reference"], file_hash(path)


def message_bytes(call):
    return sum(len(m["content"].encode("utf-8")) for m in call["messages"])


def invented_outcome(call, text):
    """Bytes/4 and arbitrary model weights; NOT measured provider token counts."""
    tokens = max(1, message_bytes(call) // 4)
    output = max(1, len(text) // 4)
    multiplier = 3.0 if call["model"] == live.SOURCE_MODEL else 1.0
    return {
        "call_site_id": call["call_site_id"],
        "executed_model_id": call["model"],
        "input_tokens": tokens,
        "output_tokens": output,
        "latency_ms": 50 + tokens / 20,
        "cost_usd": multiplier * (tokens + 5 * output) / 1e6,
        "output_is_structured": False,
        "output_is_short": output <= 128,
    }


def identity(plan):
    context = plan.get("agentc_observation_context", {})
    return {
        k: context[k]
        for k in ("key", "divergence_threshold", "runtime_version", "call_site_id")
        if k in context
    }


def run(native, attention, manifest, root):
    solve, solver_hash = synthetic_solver(root)
    tasks = [public_task(t) for t in workload.make_tasks(SEED, ARRIVALS)]
    manifest = {**manifest, "workload": WORKLOAD, "prompts": workload.prompts()}
    saved = {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
    records, pairs, plan_meta, selected_counts = [], Counter(), {}, Counter()
    settings_by_arm = study.policies()
    try:
        for arm, settings in settings_by_arm.items():
            for key in list(os.environ):
                if key.startswith("AGENTC_"):
                    os.environ.pop(key)
            os.environ.update(settings)
            with tempfile.TemporaryDirectory(prefix="agentc-STRUCTURAL-ONLY-") as store:
                native.optimize_configure(
                    store, catalog_json=json.dumps(manifest["catalog"])
                )
                try:
                    for occurrence, task in enumerate(tasks):
                        hypothetical = solve(task)
                        history = {}
                        for stage in workload.STAGES:
                            # Synthetic sequential positive control assumes the global
                            # calibration selected Haiku; no real selection is asserted.
                            model = (
                                live.TARGET_MODEL
                                if arm == "sequential"
                                else live.SOURCE_MODEL
                            )
                            call = study.restrict_call(
                                live.workflow_for(
                                    manifest,
                                    task,
                                    stage,
                                    history,
                                    attention,
                                    model=model,
                                    run_identity=f"STRUCTURAL/{arm}",
                                    prompts=manifest["prompts"],
                                ),
                                stage,
                            )
                            assert (
                                len(call["messages"])
                                == {"filter": 22, "synthesize": 3, "answer": 4}[stage]
                            )
                            assert call["parameters"]["extra"][
                                "structured_relational_contract"
                            ]["upstream_valid"]
                            if stage == "filter":
                                assert message_bytes(call) > 8192
                            if stage == "answer":
                                assert len(call["messages"][1]["content"]) > 1700
                            encoded = native.optimize_plan(json.dumps(call))
                            plan = json.loads(encoded)
                            study.validate_plan(call, plan)
                            active = live.activation(call, plan)
                            primary_id = identity(plan)
                            primary_key = (arm, stage, digest(primary_id))
                            prior_pairs = pairs[primary_key]
                            if active["selected_rules"] and arm not in {
                                "historical_rules",
                                "original",
                            }:
                                assert prior_pairs >= 20, (arm, stage, prior_pairs)
                            selected_counts[
                                (
                                    arm,
                                    stage,
                                    "+".join(active["selected_rules"]) or "PassThrough",
                                )
                            ] += 1
                            primary = plan.get("call", call)
                            token = native.optimize_observe(
                                encoded,
                                json.dumps(
                                    invented_outcome(primary, hypothetical[stage])
                                ),
                            )
                            if arm != "original":
                                assert token
                            exploration = plan.get("agentc_exploration_context")
                            paired, pair_rules, scope = None, [], None
                            candidate_bytes = None
                            if exploration:
                                assert plan["kind"] == "pass_through"
                                candidate = exploration["candidate_plan"]
                                paired = identity(candidate)
                                pair_rules = live.activation(call, candidate)[
                                    "selected_rules"
                                ]
                                candidate_bytes = message_bytes(candidate["call"])
                                divergence = live.divergence_for(
                                    manifest,
                                    stage,
                                    call,
                                    hypothetical[stage],
                                    hypothetical[stage],
                                )
                                assert divergence == 0.0
                                assert native.optimize_complete_exploration(
                                    exploration["lease_token"],
                                    json.dumps(
                                        invented_outcome(
                                            candidate["call"], hypothetical[stage]
                                        )
                                    ),
                                    divergence,
                                )
                                scope = "exploration"
                            elif active["selected_rules"] and shadow_sample(
                                "STRUCTURAL-ONLY",
                                arm + "/" + stage,
                                task["task_id"],
                                float(settings["AGENTC_OPTIMIZE_SHADOW"]),
                            ):
                                paired, pair_rules, scope = (
                                    primary_id,
                                    active["selected_rules"],
                                    "shadow",
                                )
                                native.optimize_record_divergence(token, 0.0)
                            if paired:
                                key = (arm, stage, digest(paired))
                                pairs[key] += 1
                                plan_meta[key] = {
                                    "arm": arm,
                                    "stage": stage,
                                    "identity": paired,
                                    "rules": pair_rules,
                                }
                            records.append(
                                {
                                    "arm": arm,
                                    "stage": stage,
                                    "occurrence": occurrence,
                                    "task_id": task["task_id"],
                                    "primary_kind": plan["kind"],
                                    "primary_rules": active["selected_rules"],
                                    "primary_identity": primary_id,
                                    "primary_exact_pairs_before_selection": prior_pairs,
                                    "primary_model": primary["model"],
                                    "source_input_bytes": message_bytes(call),
                                    "primary_input_bytes": message_bytes(primary),
                                    "candidate_input_bytes": candidate_bytes,
                                    "paired_identity": paired,
                                    "paired_rules": pair_rules,
                                    "paired_scope": scope,
                                    "output_cap": primary["parameters"][
                                        "max_output_tokens"
                                    ],
                                }
                            )
                            history[stage] = hypothetical[stage]
                    native.optimize_flush()
                finally:
                    native.optimize_reset()
    finally:
        for key in list(os.environ):
            if key.startswith("AGENTC_"):
                os.environ.pop(key)
        os.environ.update(saved)
    return {
        "evaluation_kind": "no_network_actual_native_perfect_synthetic_positive_control",
        "paper_evidence": False,
        "provider_calls": 0,
        "measured_token_savings": None,
        "measured_cost_savings": None,
        "quality_or_safety_claim": None,
        "arrivals_per_arm": ARRIVALS,
        "generated_seed": SEED,
        "policies": settings_by_arm,
        "test_solver_source_sha256": solver_hash,
        "workload_contract_sha256": workload.contract_hash(),
        "public_tasks_sha256": digest(tasks),
        "decisions": records,
        "exact_pair_groups": [
            {**plan_meta[k], "pairs": n} for k, n in sorted(pairs.items()) if n
        ],
        "selected_primary_counts": [
            {"arm": a, "stage": s, "rules": r, "count": n}
            for (a, s, r), n in sorted(selected_counts.items())
        ],
        "limitations": [
            "All candidate and source outputs are invented identical solver results, independent of rewritten payload content.",
            "Native outcomes use invented bytes/4 token estimates and arbitrary model costs/latencies; byte removal is real, token/cost savings are not measured.",
            "Sequential assumes synthetic calibration chose Haiku. No paid calibration or routing superiority claim.",
            "Fresh stores, uninterrupted run, no persistent-recovery or drift evidence.",
            "67 arrivals check structural horizon only; noisy provider feedback can prevent every admission.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("native", "template", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if os.environ.get("PYTHONHASHSEED") != "0" or platform.machine() != "arm64":
        raise PilotError("preflight requires ARM Python and PYTHONHASHSEED=0")
    if file_hash(args.native) != NATIVE_SHA:
        raise PilotError("unreviewed native snapshot")
    template = json.loads(args.template.read_text())
    if digest(template) != TEMPLATE_SHA:
        raise PilotError("unreviewed template catalog")
    native = load_module("_native", args.native, native=True)
    attention = load_module(
        "preflight_structured_attention", root / "python/agentc/_attention.py"
    )
    report = run(native, attention, template, root)
    report.update(
        native_sha256=file_hash(args.native),
        template_sha256=digest(template),
        catalog_sha256=digest(template["catalog"]),
        script_sha256=file_hash(Path(__file__)),
        python_version=platform.python_version(),
        architecture=platform.machine(),
        integration_source_files=live.sources(WORKLOAD),
    )
    write_json(args.output, report, immutable=True)
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "provider_calls",
                    "arrivals_per_arm",
                    "selected_primary_counts",
                    "exact_pair_groups",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
