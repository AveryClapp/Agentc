"""Sequential selected-feedback replay through the real native guarded planner.

No API keys or transport calls. A matrix row is revealed only after a native
primary, leased exploration, or pre-sampled shadow request is issued. Gold is
used only by the separate evaluator, never by the replay controller. Observed
billed costs are cache-confounded counterfactuals, not deployed-policy costs.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import Counter
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from typing import Any

from bench.openrouter_frontier import CAP, CONTEXTS, ROOT, SOURCE_MODEL, load_tasks, make_call, outcome
from bench.openrouter_matrix import file_hash, load_module, score, write_json
from bench.openrouter_pilot import PilotError, digest, make_request, money


def lexical_divergence(a: str, b: str) -> float:
    """Exact production default: whitespace-token Jaccard, no gold/normalizer."""
    sa, sb = set(a.split()), set(b.split())
    return 1 - len(sa & sb) / len(sa | sb) if sa | sb else 0.0


def shadow_sample(seed: str, context: str, task_id: str, rate: float) -> bool:
    if not 0 <= rate <= 1:
        raise PilotError("invalid shadow probability")
    return int(digest([seed, context, task_id])[:16], 16) / 2**64 < rate


def public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {"task_id": task["task_id"], "prompt": task["prompt"], "meta": {"paragraphs": [
        {"title": p["title"], "sentences": p["sentences"]} for p in task["meta"]["paragraphs"]]}}


def payload_for(call: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    model = call["model"]
    if model not in manifest["endpoints"] or call.get("tools"):
        raise PilotError("native request is outside matrix coverage")
    parameters = call["parameters"]
    if parameters["max_output_tokens"] != CAP or parameters.get("temperature") != 0:
        raise PilotError("native request changes unmeasured generation parameters")
    return make_request(model, [manifest["endpoints"][model]["tag"]], call["messages"], max_tokens=CAP)


class OutcomeTable:
    """Outcome-only view: no expected answer, EM, F1, or supporting labels."""
    FIELDS = ("id", "task_id", "context", "phase", "model", "arm", "answer", "usage",
              "latency_ms", "cost_usd", "nominal_uncached_cost_usd", "request_sha256")

    def __init__(self, rows: list[dict[str, Any]]):
        self._rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        # No-op full/compress requests can have identical payloads. Always use
        # the designated full sample, independent of answers or realized costs.
        for row in sorted(rows, key=lambda r: (r["arm"] != "full", r["id"])):
            key = (row["context"], row["task_id"], row["request_sha256"])
            self._rows.setdefault(key, {k: row[k] for k in self.FIELDS})
        self.revealed: list[dict[str, Any]] = []

    def reveal(self, context: str, task_id: str, payload: dict[str, Any], scope: str) -> dict[str, Any]:
        if scope not in {"primary", "exploration", "shadow"}:
            raise PilotError("invalid outcome revelation scope")
        key = (context, task_id, digest(payload))
        if key not in self._rows:
            raise PilotError("issued native payload has no exact measured outcome")
        row = self._rows[key]
        self.revealed.append({"row_id": row["id"], "scope": scope, "request_sha256": key[2]})
        return row


def validate_matrix(manifest: dict[str, Any], rows: list[dict[str, Any]],
                    tasks: dict[str, dict[str, dict[str, Any]]], *, calibration_only: bool) -> list[dict[str, Any]]:
    schedule = [r for r in manifest["schedule"] if not calibration_only or r["phase"] != "holdout"]
    if len(rows) < len(schedule) or (not calibration_only and len(rows) != len(schedule)):
        raise PilotError("requested replay phase is not completely acquired")
    selected = rows[:len(schedule)]
    stage = "frontier-v2-" + digest(manifest)[:20]
    generations = set()
    for i, (item, row) in enumerate(zip(schedule, selected)):
        if any(row.get(k) != v for k, v in item.items()) or row["id"] != stage + f"-{i:05d}":
            raise PilotError("matrix result differs from frozen schedule")
        if row.get("paper_evidence") is not False or row.get("stage") != stage:
            raise PilotError("invalid matrix evidence scope")
        if row["generation_id"] in generations:
            raise PilotError("duplicated provider generation")
        generations.add(row["generation_id"])
        task = tasks[row["context"]][row["task_id"]]
        if row["expected"] != task["expected"] or any(row[k] != v for k, v in score(row["answer"], task["expected"]).items()):
            raise PilotError("matrix gold or score differs from frozen scorer")
        from bench.openrouter_contract import messages
        selected_call = row["native_plan"].get("call")
        request_messages = selected_call["messages"] if selected_call else messages(task, manifest["contract"])
        payload = make_request(row["model"], [row["provider_tag"]], request_messages, max_tokens=CAP)
        if digest(payload) != row["request_sha256"]:
            raise PilotError("matrix request fingerprint does not match its recorded native plan")
        e = manifest["endpoints"][row["model"]]
        if row["provider"] != e["provider_name"] or row["provider_tag"] != e["tag"]:
            raise PilotError("matrix provider differs from frozen endpoint")
        if money(row["cost_usd"]) != money(row["usage"]["cost"]):
            raise PilotError("matrix cost differs from provider accounting")
        nominal = money(e["pricing"]["prompt"]) * row["usage"]["prompt_tokens"] + money(e["pricing"]["completion"]) * row["usage"]["completion_tokens"]
        if nominal != money(row["nominal_uncached_cost_usd"]):
            raise PilotError("matrix nominal cost differs from catalog calculation")
    return selected


def replay_policy(native: Any, attention: Any, manifest: dict[str, Any], rows: list[dict[str, Any]],
                  tasks: dict[str, dict[str, dict[str, Any]]], policy: dict[str, Any], context: str,
                  *, restart_after_calibration: bool = False) -> dict[str, Any]:
    table = OutcomeTable(rows)
    chronology = list(dict.fromkeys((r["task_id"], r["phase"]) for r in rows if r["context"] == context))
    saved = {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
    for k in saved:
        os.environ.pop(k)
    os.environ.update(policy["settings"])
    decisions, last_phase, restarted = [], None, False
    try:
        with ExitStack() as stack:
            storage = stack.enter_context(tempfile.TemporaryDirectory(prefix="agentc-OFFLINE-policy-"))
            stack.callback(native.optimize_reset)
            def configure():
                native.optimize_configure(storage, catalog_json=json.dumps(manifest["catalog"]))
                if json.loads(native.optimize_model_catalog()) != manifest["catalog"]:
                    raise PilotError("replay native catalog differs")
            configure()
            for task_id, phase in chronology:
                if restart_after_calibration and last_phase == "calibration" and phase == "holdout":
                    native.optimize_flush()
                    native.optimize_reset()
                    configure()
                    restarted = True
                # Always expose attention on the original call. Rule whitelists,
                # not synthetic missing features, define the guarded ablations.
                item = {"task_id": task_id, "phase": phase, "context": context,
                        "model": SOURCE_MODEL, "arm": "compress"}
                call = make_call(public_task(tasks[context][task_id]), item, attention, source_model=SOURCE_MODEL)
                encoded = native.optimize_plan(json.dumps(call))
                plan = json.loads(encoded)
                if plan.get("kind") not in {"pass_through", "rewritten", "composed"} or not plan.get("agentc_observation_context"):
                    raise PilotError("unattributable or uncovered native replay plan")
                start = len(table.revealed)
                primary = table.reveal(context, task_id, payload_for(plan.get("call", call), manifest), "primary")
                token = native.optimize_observe(encoded, json.dumps(outcome(primary, call["call_site_id"])))
                if not token:
                    raise PilotError("replay primary observation failed")
                incurred = [primary]
                divergence = None
                exploration = plan.get("agentc_exploration_context")
                if exploration:
                    if plan["kind"] != "pass_through":
                        raise PilotError("native leases a candidate on a non-reference primary")
                    try:
                        candidate = table.reveal(context, task_id,
                            payload_for(exploration["candidate_plan"]["call"], manifest), "exploration")
                        incurred.append(candidate)
                        divergence = lexical_divergence(primary["answer"], candidate["answer"])
                        if not native.optimize_complete_exploration(exploration["lease_token"],
                                json.dumps(outcome(candidate, call["call_site_id"])), divergence):
                            raise PilotError("native exploration completion rejected")
                    except BaseException:
                        native.optimize_fail_exploration(exploration["lease_token"])
                        raise
                elif plan["kind"] != "pass_through" and shadow_sample(manifest["policy_replay"]["shadow_seed"],
                        context, task_id, float(policy["settings"]["AGENTC_OPTIMIZE_SHADOW"])):
                    reference = table.reveal(context, task_id, payload_for(call, manifest), "shadow")
                    incurred.append(reference)
                    divergence = lexical_divergence(primary["answer"], reference["answer"])
                    native.optimize_record_divergence(token, divergence)
                decisions.append({"task_id": task_id, "phase": phase, "context": context,
                    "primary_row_id": primary["id"], "primary_model": primary["model"],
                    "primary_request_sha256": primary["request_sha256"], "native_plan": plan,
                    "revealed": table.revealed[start:], "divergence_feedback": divergence,
                    "observed_billed_cost_noncausal_usd": str(sum((money(r["cost_usd"]) for r in incurred), Decimal(0))),
                    "nominal_uncached_cost_estimate_usd": str(sum((money(r["nominal_uncached_cost_usd"]) for r in incurred), Decimal(0)))})
                last_phase = phase
            native.optimize_flush()
    finally:
        native.optimize_reset()
        for k in list(os.environ):
            if k.startswith("AGENTC_"):
                os.environ.pop(k)
        os.environ.update(saved)
    return {"policy": policy["name"], "context": context, "settings": policy["settings"],
            "restart_after_calibration": restart_after_calibration, "restart_performed": restarted,
            "decisions": decisions, "revealed_calls": len(table.revealed), "paper_evidence": False}


def evaluate(trajectory: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Post-policy evaluator; this is the only native-replay path that reads gold scores."""
    by_id = {r["id"]: r for r in rows}
    references = {(r["context"], r["task_id"]): r for r in rows if r["model"] == SOURCE_MODEL and r["arm"] == "full"}
    phase_reports = []
    for phase in ("warmup", "calibration", "holdout"):
        decisions = [d for d in trajectory["decisions"] if d["phase"] == phase]
        if not decisions:
            continue
        chosen = [by_id[d["primary_row_id"]] for d in decisions]
        refs = [references[(d["context"], d["task_id"])] for d in decisions]
        scopes = Counter(r["scope"] for d in decisions for r in d["revealed"])
        phase_reports.append({"phase": phase, "tasks": len(decisions),
            "mean_f1": sum(r["f1"] for r in chosen) / len(chosen),
            "mean_em": sum(r["em"] for r in chosen) / len(chosen),
            "mean_f1_delta_vs_source": sum(a["f1"] - b["f1"] for a, b in zip(chosen, refs)) / len(chosen),
            "any_f1_losses_vs_source": sum(a["f1"] < b["f1"] for a, b in zip(chosen, refs)),
            "mean_positive_f1_loss_vs_source": sum(max(0, b["f1"] - a["f1"]) for a, b in zip(chosen, refs)) / len(chosen),
            "strict_em_losses_vs_source": sum(b["em"] == 1 and a["em"] == 0 for a, b in zip(chosen, refs)),
            "strict_em_gains_vs_source": sum(b["em"] == 0 and a["em"] == 1 for a, b in zip(chosen, refs)),
            "observed_billed_cost_noncausal_usd": str(sum((money(d["observed_billed_cost_noncausal_usd"]) for d in decisions), Decimal(0))),
            "nominal_uncached_cost_estimate_usd": str(sum((money(d["nominal_uncached_cost_estimate_usd"]) for d in decisions), Decimal(0))),
            "primary_calls": scopes["primary"], "exploration_calls": scopes["exploration"], "shadow_calls": scopes["shadow"],
            "primary_models": dict(Counter(r["model"] for r in chosen)),
            "plan_kinds": dict(Counter(d["native_plan"]["kind"] for d in decisions))})
    return {"policy": trajectory["policy"], "context": trajectory["context"], "phases": phase_reports,
            "total_observed_billed_cost_noncausal_usd": str(sum((money(d["observed_billed_cost_noncausal_usd"]) for d in trajectory["decisions"]), Decimal(0))),
            "total_nominal_uncached_cost_estimate_usd": str(sum((money(d["nominal_uncached_cost_estimate_usd"]) for d in trajectory["decisions"]), Decimal(0)))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("artifacts", "natural", "extended", "native", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--calibration-only", action="store_true")
    parser.add_argument("--restart-after-calibration", action="store_true")
    args = parser.parse_args()
    try:
        manifest = json.loads((args.artifacts / "manifest.json").read_text())
        if args.output.exists():
            raise PilotError("replay output already exists; use a new isolated report path")
        if (manifest["native_sha256"] != file_hash(args.native)
                or manifest["fixtures"] != {"natural": file_hash(args.natural), "extended": file_hash(args.extended)}
                or os.environ.get("PYTHONHASHSEED") != manifest["pythonhashseed"]):
            raise PilotError("replay native, fixture, or hash seed differs")
        if any(file_hash(ROOT / p) != h for p, h in manifest["source_files"].items()):
            raise PilotError("acquisition source changed before replay")
        tasks = load_tasks(args.natural, args.extended)
        rows = validate_matrix(manifest, json.loads((args.artifacts / "results.json").read_text()), tasks,
                               calibration_only=args.calibration_only)
        native = load_module("_native", args.native, native=True)
        attention = load_module("replay_attention", ROOT / "python/agentc/_attention.py")
        trajectories, reports = [], []
        for policy in manifest["policy_replay"]["specs"]:
            for context in CONTEXTS:
                trajectory = replay_policy(native, attention, manifest, rows, tasks, policy, context,
                                           restart_after_calibration=args.restart_after_calibration)
                trajectories.append(trajectory)
                report = evaluate(trajectory, rows)
                reports.append(report)
                print(json.dumps(report), flush=True)
        result = {"paper_evidence": False, "evaluation_kind": "offline_selected_feedback_replay",
            "manifest_sha256": digest(manifest), "consumed_rows_sha256": digest(rows),
            "replay_source_sha256": file_hash(Path(__file__)), "calibration_only": args.calibration_only,
            "restart_after_calibration": args.restart_after_calibration,
            "reports": reports, "trajectories": trajectories,
            "limitations": manifest["limitations"] + [
                "Native policies learn from selected unlabeled heldout outcomes; static comparators use calibration labels only.",
                "A 0.02 mean F1 margin is not a 2% probability-of-any-harm guarantee.",
                "No injected provider failures or drift are measured by this replay.",
                "Nominal totals reprice the same selected token counts; selection itself receives observed cache-confounded costs."]}
        write_json(args.output, result, immutable=True)
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Replay stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
