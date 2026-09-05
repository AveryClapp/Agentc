"""Paid structured-workload feasibility pilot and native joint microbenchmark.

Strict application grammar, raw lexical feedback, and unchanged native guards.
This controlled generated workload is not real-workload generalization evidence.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from bench import openrouter_joint_analysis as analysis
from bench import openrouter_joint_study as study
from bench import openrouter_rules_live as live
from bench.openrouter_contract import endpoints
from bench.openrouter_matrix import ROOT, file_hash, load_module, write_json
from bench.openrouter_pilot import Ledger, PilotError, digest, load_key, money, validate_schema_endpoint
from bench.openrouter_replay import public_task

WORKLOAD = "structured_relational_v1"
PILOT_KIND = "structured_schema_development_v1"
PILOT_SEED = 2026090501
STUDY_SEED = 2026090502
PILOT_CAP = "1"
TYPED_WORKLOAD = "typed_relational_v1"
TYPED_PILOT_KIND = "typed_schema_development_v1"
# Includes headroom for one $6.01536 Sonnet context reservation; not a spend
# target. Together with a possible future $24 native envelope this is <$31.98.
TYPED_PILOT_CAP = "7.9"
NATIVE_SHA = "84f9ecb542b5cb42c075a5ad8c536461dd48e3e45c9be25bc386ba7ce5e28501"
TEMPLATE_SHA = "dcb703739e460810825f0966c06371d2848a4d17346afbfea4e77c52a0548ace"
PILOT_ARMS = {
    "source": live.SOURCE_MODEL,
    "repeat": live.SOURCE_MODEL,
    "target": live.TARGET_MODEL,
}


def workload(manifest=None):
    return live.workload_adapter(manifest or {"workload": WORKLOAD})


def pilot_kind(manifest):
    return TYPED_PILOT_KIND if manifest.get("workload") == TYPED_WORKLOAD else PILOT_KIND


def pilot_cap(manifest):
    return TYPED_PILOT_CAP if manifest.get("workload") == TYPED_WORKLOAD else PILOT_CAP


def catalog_for(template, selected):
    catalog = deepcopy(template["catalog"])
    version = "structured-relational-v1-" + digest(selected)[:20]
    observed = datetime.now(timezone.utc).isoformat()
    catalog.update(
        catalog_version=version, price_table_version=version, observed_at_utc=observed
    )
    for target in catalog["targets"]:
        model = target["model_id"]
        endpoint = selected[model]
        url = "https://openrouter.ai/api/v1/models/" + model + "/endpoints"
        target.update(
            model_version=model + "@" + version + "/" + endpoint["tag"],
            max_output_tokens=512,
            context_window_tokens=min(65536, endpoint["context_length"]),
        )
        target["price"] = {
            "input_per_million_tokens_usd": float(
                money(endpoint["pricing"]["prompt"]) * 1000000
            ),
            "output_per_million_tokens_usd": float(
                money(endpoint["pricing"]["completion"]) * 1000000
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
    return catalog


def pilot_schedule(tasks):
    result = []
    for task in tasks:
        block = [
            {"task_id": task["task_id"], "phase": "development", "arm": arm}
            for arm in PILOT_ARMS
        ]
        result.extend(
            sorted(block, key=lambda row: digest(["structured-pilot-order-v1", row]))
        )
    return result


def pilot_gate(comparisons):
    """Prospectively fixed feasibility floor; not an efficacy or safety test."""
    if len(comparisons) != 18 or Counter(r["arm"] for r in comparisons) != {
        a: 6 for a in PILOT_ARMS
    }:
        return {"pass": False, "reason": "incomplete pilot"}
    invalid = sum(not all(r["valid_by_site"].values()) for r in comparisons)
    exact = {
        a: sum(r["score"]["em"] for r in comparisons if r["arm"] == a)
        for a in PILOT_ARMS
    }
    passed = (
        invalid == 0
        and exact["source"] >= 5
        and exact["repeat"] >= 5
        and exact["target"] >= 4
    )
    return {
        "pass": passed,
        "invalid_workflows": invalid,
        "exact_answers_by_arm": exact,
        "reason": "schema and basic task feasibility"
        if passed
        else "schema or basic task quality floor failed",
        "safety_guarantee": False,
    }


def pilot_comparisons(manifest, calls, decisions, tasks):
    by_id = {r["id"]: r for r in calls}
    comparisons = []
    if len(calls) != len(manifest["schedule"]) * 3 or len(decisions) != len(calls):
        raise PilotError("pilot comparison requires every scheduled call")
    for item in manifest["schedule"]:
        rows = {
            d["workflow_stage"]: by_id[d["primary_id"]]
            for d in decisions
            if all(d[k] == item[k] for k in ("task_id", "arm"))
        }
        if set(rows) != set(live.STAGES):
            raise PilotError("pilot comparison needs every own-history stage")
        comparisons.append(
            {
                **item,
                "valid_by_site": {
                    s: workload(manifest).parse(s, live.application_answer(manifest, r)) is not None
                    for s, r in rows.items()
                },
                "score": live.score_for(
                    manifest,
                    live.application_answer(manifest, rows["answer"]),
                    tasks[item["task_id"]]["expected"],
                    history={s: live.application_answer(manifest, r) for s, r in rows.items()},
                ),
            }
        )
    return comparisons


def verified_pilot(args):
    """Promotion uses reconstructed full histories/gate and authoritative costs."""
    data = {
        n: json.loads((args.pilot / n).read_text())
        for n in (
            "manifest.json",
            "calls.json",
            "decisions.json",
            "intents.json",
            "summary.json",
            "fixture.json",
        )
    }
    manifest, summary = data["manifest.json"], data["summary.json"]
    validate(
        SimpleNamespace(
            manifest_sha256=args.pilot_manifest_sha256,
            fixture=args.pilot / "fixture.json",
        ),
        manifest,
    )
    if manifest["kind"] != pilot_kind(manifest):
        raise PilotError("native study requires a schema pilot")
    tasks = {t["task_id"]: t for t in data["fixture.json"]}
    attention = load_module(
        "pilot_promotion_attention", ROOT / "python/agentc/_attention.py"
    )
    _, expected, failures = analysis.validate_journals(
        manifest,
        data["decisions.json"],
        data["calls.json"],
        data["intents.json"],
        tasks,
        attention,
    )
    events, _ = analysis.read_ledger_snapshot(args.ledger)
    finance = analysis.ledger_report(
        events,
        "rules-live-dev-v1-" + digest(manifest)[:20],
        data["calls.json"],
        expected,
    )
    comparisons = pilot_comparisons(
        manifest, data["calls.json"], data["decisions.json"], tasks
    )
    gate = pilot_gate(comparisons)
    if (
        failures
        or finance["failed_provider_results"]
        or not finance["snapshot_financially_complete"]
        or not summary["schedule_complete"]
        or not gate["pass"]
        or summary["manifest_sha256"] != digest(manifest)
        or summary["calls_sha256"] != digest(data["calls.json"])
        or summary["decisions_sha256"] != digest(data["decisions.json"])
        or summary["completed_calls"] != len(data["calls.json"])
        or summary["comparisons"] != comparisons
        or summary["gate"] != gate
        or money(summary["stage_accounting"]["paid_stage_cost_usd"])
        != money(finance["known_stage_usd"])
    ):
        raise PilotError(
            "pilot promotion requires consistent complete evidence and passing gate"
        )
    return data, finance


def prepare(args, key, *, pilot):
    if (args.output / "manifest.json").exists():
        raise PilotError("structured manifest is already frozen")
    template = json.loads(args.template.read_text())
    if digest(template) != TEMPLATE_SHA:
        raise PilotError("unreviewed protocol template")
    requested = {"workload": getattr(args, "workload", WORKLOAD)}
    adapter = workload(requested)
    typed = requested["workload"] == TYPED_WORKLOAD
    if typed and not pilot:
        raise PilotError("typed native study awaits independently reviewed context contract")
    if not pilot:
        pilot_data, pilot_finance = verified_pilot(args)
        pilot_manifest, pilot_calls, pilot_summary = (
            pilot_data[n] for n in ("manifest.json", "calls.json", "summary.json")
        )
        if file_hash(args.native) != NATIVE_SHA:
            raise PilotError(
                "native binary differs from independently reviewed runtime"
            )
    count = (
        6
        if pilot
        else study.CALIBRATION + study.WARMUP + study.TRAINING + study.HELDOUT
    )
    seed = PILOT_SEED if pilot else STUDY_SEED
    tasks = adapter.make_tasks(seed, count)
    if len(tasks) != count or len({t["task_id"] for t in tasks}) != count:
        raise PilotError("generated fixture is not unique and complete")
    selected = endpoints(key)
    selected = {m: selected[m] for m in (live.SOURCE_MODEL, live.TARGET_MODEL)}
    if typed:
        for model, endpoint in selected.items():
            validate_schema_endpoint(model, endpoint)
    manifest = deepcopy(template)
    manifest.update(
        workload=requested["workload"],
        source_files=live.sources(requested["workload"]),
        created_at=datetime.now(timezone.utc).isoformat(),
        generator_seed=seed,
        generator_count=count,
        format_variant=adapter.FORMAT_VARIANT,
        prompts=adapter.prompts(),
        template_manifest_sha256=TEMPLATE_SHA,
        endpoints=selected,
        catalog=catalog_for(template, selected),
        source_model=live.SOURCE_MODEL,
        target_model=live.TARGET_MODEL,
        excluded_question_ids=[],
        scoring=live.scoring_contract(requested),
        bounded_transport_retry=not typed,
        limitations=[
            "Stage-local questions v3 reuse the same six exposed development cases after failed v1/v2 pilots and a one-case formatting diagnostic; this is adaptive prompt development, not independent replication.",
            "Filter asks only for tenant document selection; synthesis asks only for tenant and requisition. The ten-cell diagnostic tested filter scope only; applying stage-local scope to synthesis is an additional unverified hypothesis screened by this pilot.",
            "The filter question omits redundant table-kind names already defined in the system prompt: listing every kind protected all distractors in the unchanged union-based attention guard. This wording differs from the successful one-case control and requires new feasibility evidence.",
            "Controlled generated procurement microbenchmark, not public real-workload or MLSys main-track efficacy evidence.",
            "Raw lexical comparison for valid outputs; invalid schema or upstream state receives maximal disagreement. Native evidence/risk thresholds remain unchanged, with no gold comparator.",
            "Exact byte output grammar is an application requirement; schema failures and shared wrong answers are not successful quality.",
            "Question-disjoint generation seeds separate schema pilot, calibration, training and heldout; same generator family remains.",
            "Historical menu is only ContextCompress/StateDrop; native full rule coverage and drift adaptation are not tested.",
            "All calibration, probes, shadows, failed attempts and heldout calls are charged; small samples cannot certify a damage bound.",
            "Independent global calibration may select Haiku and match the joint policy; no joint advantage is assumed.",
            "The original public-QA failed-admission study and its48unopened questions remain unchanged.",
        ],
    )
    if typed:
        for target in manifest["catalog"]["targets"]:
            target["capabilities"]["structured_outputs"] = True
        manifest.update(
            response_formats={s: adapter.schema_for(s) for s in live.STAGES},
            feedback_contract=adapter.FEEDBACK_CONTRACT,
            stop_on_invalid_application=True,
            native_execution_allowed=False,
            reservation_basis="entire_fresh_verified_endpoint_context_at_max_price_plus_output_v1",
            limitations=[
                "New typed-JSON application contract on the same six exposed development tasks, not a reinterpretation of the failed lexical pilots.",
                "Consume complete type-validated JSON values; reject duplicate keys, invalid types, null error, upstream-invalid state, refusal and truncation. No extraction, repair, coercion, case-folding or hidden-label oracle.",
                "Exact application-value agreement is distinct from raw-string similarity and from task correctness. No numerical native risk setting is changed.",
                "All 54 calls and 18 valid own-history workflows are required for the unchanged feasibility gate. Stop at the first application-invalid completion or provider failure; no automatic retry.",
                "Every stage uses the same fixed schema across source/repeat/target. Schemas are public domain types, not correct task answers.",
                "Provider-injected schema overhead has no documented numeric bound; reserve entire verified context at capped prices. A missing-cost failure keeps its full allowance.",
                "Native routing is not authorized by this pilot: client-byte context estimates do not prove bounds on opaque provider schema instructions. Resolve that separate contract before a native study.",
                "Compilation latency and grammar warmth are uncontrolled; charge all calls and describe latency observationally only. No healing or material router pipeline transformations are accepted.",
                "Generated adaptive feasibility screen, not joint-system efficacy, safety certification, generalization or MLSys main-track evidence.",
                "Original campaign $50 cap and all permanent uncertainty holds remain authoritative; stage cap is reservation headroom, not a target to spend.",
            ],
        )
    fixture = args.output / "fixture.json"
    write_json(fixture, tasks, immutable=True)
    manifest["fixture_sha256"] = file_hash(fixture)
    if pilot:
        manifest.update(
            kind=pilot_kind(manifest),
            schedule=pilot_schedule(tasks),
            stage_cap_usd=pilot_cap(manifest),
            pilot_arm_models=PILOT_ARMS,
            policies={
                a: deepcopy(manifest["policies"]["original"]) for a in PILOT_ARMS
            },
            training_cap_usd=pilot_cap(manifest),
            pilot_feasibility="all54calls; strict-valid all18workflows; source/repeat>=5of6 exact each; target>=4of6 exact",
        )
    else:
        structured_limitations = manifest["limitations"]
        manifest["schedule"] = live.schedule_for(
            [t["task_id"] for t in tasks],
            set(),
            study.CALIBRATION,
            study.WARMUP + study.TRAINING + study.HELDOUT,
        )
        manifest = study.finalize_manifest(manifest, {"closed_template": TEMPLATE_SHA})
        manifest.update(
            workload=WORKLOAD,
            scoring="strict_application_scalar_exact_v1",
            pilot_manifest_sha256=digest(pilot_manifest),
            pilot_calls_sha256=digest(pilot_calls),
            pilot_summary_sha256=digest(pilot_summary),
            pilot_known_cost_usd=pilot_finance["known_stage_usd"],
            pilot_retained_uncertainty_usd=pilot_finance["retained_uncertainty_usd"],
            limitations=structured_limitations,
        )
        manifest["limitations"] += [
            "This is a generated structured procurement workload, not the earlier public QA fixture described by legacy protocol prose.",
            "Strict application scalar scoring replaces permissive answer normalization; valid outputs use raw lexical comparison, invalid schema/upstream yields maximal disagreement, thresholds unchanged.",
        ]
    write_json(args.output / "manifest.json", manifest, immutable=True)
    return {
        "manifest_sha256": digest(manifest),
        "scheduled_calls_minimum": len(manifest["schedule"]) * 3,
        "stage_cap_usd": manifest["stage_cap_usd"],
        "kind": manifest["kind"],
    }


def validate(args, manifest):
    adapter = workload(manifest)
    if (
        digest(manifest) != args.manifest_sha256
        or manifest["source_files"] != live.sources(manifest["workload"])
        or manifest.get("format_variant") != adapter.FORMAT_VARIANT
        or manifest["prompts"] != adapter.prompts()
        or manifest["scoring"] != live.scoring_contract(manifest)
        or file_hash(args.fixture) != manifest["fixture_sha256"]
        or json.loads(args.fixture.read_text())
        != adapter.make_tasks(
            manifest["generator_seed"], manifest["generator_count"]
        )
    ):
        raise PilotError(
            "structured source, fixture, contract or external manifest identity changed"
        )
    if manifest["workload"] == TYPED_WORKLOAD:
        if (
            manifest["kind"] != TYPED_PILOT_KIND
            or manifest.get("response_formats") != {s: adapter.schema_for(s) for s in live.STAGES}
            or manifest.get("feedback_contract") != adapter.FEEDBACK_CONTRACT
            or manifest.get("bounded_transport_retry") is not False
            or manifest.get("stop_on_invalid_application") is not True
            or manifest.get("native_execution_allowed") is not False
            or manifest.get("reservation_basis") != "entire_fresh_verified_endpoint_context_at_max_price_plus_output_v1"
            or not all(t["capabilities"].get("structured_outputs") is True for t in manifest["catalog"]["targets"])
        ):
            raise PilotError("typed pilot application, capability or failure contract changed")
        for model, endpoint in manifest["endpoints"].items():
            validate_schema_endpoint(model, endpoint)
    if manifest["kind"] == pilot_kind(manifest):
        tasks = json.loads(args.fixture.read_text())
        if (
            manifest["generator_seed"] != PILOT_SEED
            or manifest["generator_count"] != 6
            or manifest["schedule"] != pilot_schedule(tasks)
            or manifest["stage_cap_usd"] != pilot_cap(manifest)
            or manifest["training_cap_usd"] != pilot_cap(manifest)
            or manifest["pilot_arm_models"] != PILOT_ARMS
        ):
            raise PilotError("schema pilot allocation or cap changed")
    else:
        study.validate_manifest(manifest)
        if manifest["generator_seed"] != STUDY_SEED:
            raise PilotError("native study cannot reuse pilot questions")


def run_pilot(args, key, manifest):
    tasks = {t["task_id"]: t for t in json.loads(args.fixture.read_text())}
    attention = load_module(
        "schema_pilot_attention", ROOT / "python/agentc/_attention.py"
    )
    acquisition = live.Acquisition(args, manifest, Ledger(args.ledger, key), key)
    if args.max_calls is not None and (
        args.max_calls < len(acquisition.calls) or args.max_calls < 1
    ):
        raise PilotError("pilot call cap cannot precede saved evidence")
    complete = False
    try:
        for workflow in manifest["schedule"]:
            history = {}
            for stage in live.STAGES:
                item = {**workflow, "workflow_stage": stage}
                call = live.workflow_for(
                    manifest,
                    public_task(tasks[item["task_id"]]),
                    stage,
                    history,
                    attention,
                    model=PILOT_ARMS[item["arm"]],
                    run_identity=acquisition.stage + "/" + item["arm"],
                    prompts=manifest["prompts"],
                )
                call = study.restrict_call(call, stage)
                plan = {"kind": "pass_through"}
                signature = acquisition.intent(item, call, plan)
                primary = acquisition.dispatch(item, "primary", call, signature)
                active = live.activation(call, plan)
                active["executed_on_provider"] = True
                acquisition.record(
                    {
                        **item,
                        "native_plan": plan,
                        "semantic_plan": signature,
                        "primary_id": primary["id"],
                        "incurred_ids": [primary["id"]],
                        "activation": active,
                        "divergence_feedback": None,
                    }
                )
                history[stage] = live.application_answer(manifest, primary)
                if manifest.get("stop_on_invalid_application") and workload(manifest).parse(stage, history[stage]) is None:
                    raise PilotError("typed feasibility failed at an invalid application completion")
        complete = True
    except live.CallLimit:
        pass
    finally:
        comparisons = (
            pilot_comparisons(manifest, acquisition.calls, acquisition.decisions, tasks)
            if complete
            else []
        )
        summary = {
            "kind": pilot_kind(manifest),
            "paper_evidence": False,
            "efficacy_claim": False,
            "manifest_sha256": digest(manifest),
            "calls_sha256": digest(acquisition.calls),
            "decisions_sha256": digest(acquisition.decisions),
            "completed_calls": len(acquisition.calls),
            "schedule_complete": complete,
            "comparisons": comparisons,
            "gate": pilot_gate(comparisons),
            "stage_accounting": live.stage_accounting(
                acquisition.ledger, acquisition.stage, acquisition.calls
            ),
            "limitations": manifest["limitations"],
        }
        write_json(args.output / "summary.json", summary)
    return {**summary, "ledger": acquisition.ledger.summary()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("prepare-pilot", "run-pilot", "prepare-study", "run-study")
    )
    for name in ("env-file", "ledger", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--pilot", type=Path)
    parser.add_argument("--pilot-manifest-sha256")
    parser.add_argument("--native", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--workload", choices=(WORKLOAD, TYPED_WORKLOAD), default=WORKLOAD)
    args = parser.parse_args()
    args.fixture = args.output / "fixture.json"
    try:
        key = load_key(args.env_file)
        if args.command.startswith("prepare-"):
            report = prepare(args, key, pilot=args.command == "prepare-pilot")
        else:
            manifest = json.loads((args.output / "manifest.json").read_text())
            validate(args, manifest)
            if (args.command == "run-pilot") != (manifest["kind"] == pilot_kind(manifest)):
                raise PilotError("runner and manifest disagree")
            report = (
                run_pilot(args, key, manifest)
                if args.command == "run-pilot"
                else live.run(args, key)
            )
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError):
        print(
            "Structured experiment stopped at validation, provider or budget boundary; no raw error output."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
