"""Ten frozen benign formatting/role-shape controls, never a native study.

One already-exposed case; no suffixes, ERROR_INPUT, heldout, or automatic retry.
Ordinary output, reported reasoning and charges remain distinct. Full comparisons
require all ten responses and consistent durable accounting. No normalization.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
from types import SimpleNamespace

from bench import openrouter_rules_live as live
from bench import structured_relational_workload as workload
from bench.openrouter_attempts import accounting
from bench.openrouter_contract import endpoints
from bench.openrouter_joint_analysis import validate_row
from bench.openrouter_matrix import file_hash, write_json
from bench.openrouter_pilot import (
    Ledger,
    PilotError,
    digest,
    load_key,
    make_request,
    money,
    text_choice,
)
from bench.openrouter_replay import public_task

KIND = "structured_shape_scope_control_v1"
PREVIOUS_SHA = "14c942dd0c5ea45d877845e2060396d61c61112eed70e9ad225d3740c740dd87"
SEED = 2026090501
CAP = "0.50"
LITERAL = "D02|D06|D17"
MODELS = (live.SOURCE_MODEL, live.TARGET_MODEL)
FIELDS = ("condition", "model", "shape", "scope", "task_id")
LIMITATIONS = [
    "One already-exposed case and one sample/cell: diagnostic, not model ranking, reliability, safety or efficiency evidence.",
    "No native optimizer, dependent workflow, heldout, gold feedback, or promotion into a native study.",
    "Literal controls test copying under a separate minimal instruction; the 2x2 selection factorial retains existing filter system and final-user grammar.",
    "Single-user packing removes message boundaries with a fixed double-newline delimiter; it does not preserve native ContextCompress message granularity.",
    "Reported zero reasoning tokens does not prove absence of unreported internal computation; ordinary content is evaluated without extraction or repair.",
    "No automatic retry, including on a later invocation. A failed attempt blocks this stage and retains its full budget commitment in the shared ledger.",
    "Costs include all known failed-attempt charges and full uncertainty allowances; latency is descriptive and cache/order-confounded.",
]


def sources():
    return {
        **live.sources("structured_relational_v1"),
        "bench/openrouter_shape_control.py": file_hash(Path(__file__)),
    }


def first_task():
    return public_task(workload.make_tasks(SEED, 1)[0])


def messages(task, shape, scope):
    """Do not alter document/system bytes when changing representation/scope."""
    if shape not in ("single", "multi") or scope not in ("full", "local"):
        raise PilotError("unknown shape or question scope")
    task = deepcopy(public_task(task))
    if scope == "local":
        match = re.match(
            r"Tenant (T[A-Z0-9]+); requisition R[0-9]{3}\.", task["prompt"]
        )
        if not match:
            raise PilotError("public tenant question changed")
        task["prompt"] = (
            f"Tenant {match[1]}. Select its REQUESTS, CATALOG, and OFFERS document IDs."
        )
    # Attention values are unused: this adapter is only a message renderer here.
    attention = SimpleNamespace(
        compute_attention_scores=lambda values, _: ([0.0] * len(values), [])
    )
    rendered = workload.call(
        task,
        "filter",
        {},
        attention,
        model=MODELS[0],
        run_identity=KIND,
        prompts=workload.prompts(),
    )["messages"]
    if shape == "single":
        return [
            rendered[0],
            {
                "role": "user",
                "content": "\n\n".join(m["content"] for m in rendered[1:]),
            },
        ]
    return rendered


def schedule(task, selected):
    result = []
    for model in MODELS:
        literal_messages = [
            {
                "role": "system",
                "content": "Copy the requested literal exactly. " + workload._BYTE_RULE,
            },
            {
                "role": "user",
                "content": f"Copy exactly {LITERAL}. This is a copying task, not record selection. "
                "Return only the three IDs separated by vertical bars, without spaces or explanation.",
            },
        ]
        result.append(
            {
                "condition": "literal",
                "model": model,
                "shape": "single",
                "scope": "literal",
                "task_id": "literal-copy-control",
                "payload": make_request(
                    model, [selected[model]["tag"]], literal_messages, max_tokens=512
                ),
            }
        )
        for shape in ("single", "multi"):
            for scope in ("full", "local"):
                result.append(
                    {
                        "condition": "selection",
                        "model": model,
                        "shape": shape,
                        "scope": scope,
                        "task_id": task["task_id"],
                        "payload": make_request(
                            model,
                            [selected[model]["tag"]],
                            messages(task, shape, scope),
                            max_tokens=512,
                        ),
                    }
                )
    result.sort(
        key=lambda item: (
            item["condition"] != "literal",
            digest([KIND, {k: item[k] for k in FIELDS}]),
        )
    )
    return [{**row, "request_sha256": digest(row["payload"])} for row in result]


def validate_endpoints(selected):
    if set(selected) != set(MODELS):
        raise PilotError("control model pool changed")
    for model, endpoint in selected.items():
        if (
            endpoint["tag"] != "anthropic"
            or endpoint["provider_name"] != "Anthropic"
            or not isinstance(endpoint["name"], str)
            or len(endpoint["name"].split(" | ", 1)) != 2
            or not {"max_tokens", "temperature"}
            <= set(endpoint["supported_parameters"])
            or endpoint["context_length"] < 32768
            or endpoint["max_completion_tokens"] < 512
            or money(endpoint["pricing"]["prompt"]) > money("0.000006")
            or money(endpoint["pricing"]["completion"]) > money("0.000030")
        ):
            raise PilotError("control endpoint lacks frozen dispatch capability")


def prepare(args, key):
    if (args.output / "manifest.json").exists():
        raise PilotError("control manifest already frozen")
    previous = json.loads(args.previous.read_text())
    fixture = json.loads(args.fixture.read_text())
    task = first_task()
    if (
        digest(previous) != PREVIOUS_SHA
        or previous["source_files"] != live.sources("structured_relational_v1")
        or file_hash(args.fixture) != previous["fixture_sha256"]
        or fixture != workload.make_tasks(SEED, 6)
        or public_task(fixture[0]) != task
        or previous["schedule"][0]["task_id"] != task["task_id"]
    ):
        raise PilotError("control must use the first exposed frozen v2 case")
    selected = endpoints(key)  # Read-only reviewed catalog fetch; no inference.
    selected = {model: selected[model] for model in MODELS}
    validate_endpoints(selected)
    manifest = {
        "kind": KIND,
        "paper_evidence": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "previous_manifest_sha256": PREVIOUS_SHA,
        "previous_fixture_sha256": file_hash(args.fixture),
        "source_files": sources(),
        "stage_cap_usd": CAP,
        "retry_policy": "never",
        "task": task,
        "task_sha256": digest(task),
        "endpoints": selected,
        "schedule": schedule(task, selected),
        "scoring": "strict_three_ID_exact_no_normalization",
        "limitations": LIMITATIONS,
    }
    write_json(args.output / "manifest.json", manifest, immutable=True)
    return {
        "manifest_sha256": digest(manifest),
        "scheduled_calls": 10,
        "stage_cap_usd": CAP,
    }


def validate(manifest, expected_sha):
    if (
        digest(manifest) != expected_sha
        or manifest["kind"] != KIND
        or manifest["paper_evidence"] is not False
        or manifest["source_files"] != sources()
        or manifest["stage_cap_usd"] != CAP
        or manifest["retry_policy"] != "never"
        or manifest["previous_manifest_sha256"] != PREVIOUS_SHA
        or manifest["task"] != first_task()
        or manifest["task_sha256"] != digest(manifest["task"])
        or manifest["scoring"] != "strict_three_ID_exact_no_normalization"
        or manifest["limitations"] != LIMITATIONS
    ):
        raise PilotError("control manifest, source or scope changed")
    validate_endpoints(manifest["endpoints"])
    if manifest["schedule"] != schedule(manifest["task"], manifest["endpoints"]):
        raise PilotError("control frozen payloads or order changed")


def stage_id(manifest):
    return "shape-control-v1-" + digest(manifest)[:20]


def metadata(item, manifest):
    endpoint = manifest["endpoints"][item["model"]]
    return {
        **{k: item[k] for k in FIELDS},
        "scope_kind": "standalone_control",
        "manifest_sha256": digest(manifest),
        "dispatch_contract": {
            "provider_name": endpoint["provider_name"],
            "endpoint_model": endpoint["name"].split(" | ", 1)[1],
        },
    }


def snapshot(ledger):
    with ledger.locked() as handle:
        return ledger.read(handle)


def response_fields(response):
    choice = text_choice(response)
    message = choice["message"]
    reasoning = message.get("reasoning")
    details = message.get("reasoning_details")
    return {
        "native_finish_reason": choice.get("native_finish_reason"),
        "service_tier": response.get("service_tier"),
        "reasoning_present": "reasoning" in message,
        "reasoning_is_null": reasoning is None,
        "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else None,
        "reasoning_details_count": len(details) if isinstance(details, list) else None,
        "content_chars": len(message["content"]),
        "content_words": len(message["content"].split()),
    }


def row_from_ledger(index, item, result, response, manifest):
    payload, meta = item["payload"], metadata(item, manifest)
    if (
        result["event"] != "result"
        or result.get("metadata") != meta
        or result["id"] != stage_id(manifest) + f"-{index:02d}"
        or result["stage"] != stage_id(manifest)
        or result.get("paper_evidence") is not False
        or result.get("finish_reason") not in {"stop", "length"}
        or not isinstance(result.get("generation_id"), str)
        or not result["generation_id"]
    ):
        raise PilotError("durable result identity is invalid")
    choice = text_choice(response)
    if (
        response.get("id") != result["generation_id"]
        or response.get("model") != result["model"]
        or response.get("provider") != result["provider"]
        or choice["message"]["content"] != result["answer"]
        or choice["finish_reason"] != result["finish_reason"]
        or response.get("usage") != result["usage"]
        or response.get("openrouter_metadata") != result["router_metadata"]
    ):
        raise PilotError("durable result differs from its provider response")
    price = manifest["endpoints"][item["model"]]["pricing"]
    usage = result["usage"]
    row = {
        **{k: item[k] for k in FIELDS},
        **{k: v for k, v in result.items() if k not in {"key_id", "metadata", "at"}},
        "schedule_index": index,
        "request_sha256": item["request_sha256"],
        "cached_input_tokens": usage.get("prompt_tokens_details", {}).get(
            "cached_tokens"
        ),
        "nominal_uncached_cost_usd": str(
            money(price["prompt"]) * usage["prompt_tokens"]
            + money(price["completion"]) * usage["completion_tokens"]
        ),
        "response_diagnostics": response_fields(response),
    }
    validate_row(row, payload, meta, manifest)
    return row


def verified_rows(events, manifest):
    """Validate every saved request/result, including a crash-before-artifact tail."""
    stage = stage_id(manifest)
    expected = {
        stage + f"-{i:02d}": (i, item) for i, item in enumerate(manifest["schedule"])
    }
    state = accounting(events)
    relevant = [
        e
        for e in events
        if e.get("stage") == stage or e.get("id", "").startswith(stage + "-")
    ]
    reserves, responses, results = {}, {}, {}
    for event in relevant:
        call_id = event.get("id")
        if call_id not in expected:
            raise PilotError("ledger contains unscheduled control call")
        index, item = expected[call_id]
        if event["event"] == "reserve":
            if call_id in reserves or event.get("stage") != stage:
                raise PilotError("control repeated a request despite no-retry policy")
            if event["request"] != item["payload"] or event["metadata"] != metadata(
                item, manifest
            ):
                raise PilotError(
                    "durable control reservation changed payload or metadata"
                )
            if event["fingerprint"] != digest(
                {
                    "payload": item["payload"],
                    "metadata": metadata(item, manifest),
                    "stage": stage,
                }
            ):
                raise PilotError("durable control fingerprint changed")
            reserves[call_id] = event
        elif event["event"] == "response":
            if call_id in responses:
                raise PilotError("duplicate control response")
            responses[call_id] = event
        elif event["event"] == "result":
            results[call_id] = event
    ordered_ids = list(expected)
    if (
        list(reserves) != ordered_ids[: len(reserves)]
        or list(results) != ordered_ids[: len(results)]
    ):
        raise PilotError("durable control requests/results are not an exact prefix")
    rows, generations = [], set()
    for call_id, result in results.items():
        reserve, response = reserves.get(call_id), responses.get(call_id)
        if (
            not reserve
            or not response
            or not reserve.get("attempt_id")
            or any(
                row.get("attempt_id") != reserve["attempt_id"]
                for row in (response, result)
            )
        ):
            raise PilotError("control response has no exact attempt attribution")
        index, item = expected[call_id]
        row = row_from_ledger(index, item, result, response["response"], manifest)
        if row["generation_id"] in generations:
            raise PilotError("provider generation reused across control calls")
        generations.add(row["generation_id"])
        rows.append(row)
    return rows, state


def finance(events, manifest, rows):
    state = accounting(events)
    stage = stage_id(manifest)
    completed = {r["id"] for r in state["results"].values() if r["stage"] == stage}
    recorded = {r["id"] for r in rows}
    pending = [r for r in state["pending"] if r["stage"] == stage]
    failed = [r for r in state["terminals"].values() if r["stage"] == stage]
    known = state["known_by_stage"].get(stage, Decimal(0))
    held = state["holds_by_stage"].get(stage, Decimal(0))
    pending_cost = sum((money(r["upper_cost_usd"]) for r in pending), Decimal(0))
    return {
        "ledger_events_sha256": digest(events),
        "known_stage_usd": str(known),
        "artifact_billed_subtotal_usd": str(
            sum((money(r["cost_usd"]) for r in rows), Decimal(0))
        ),
        "retained_uncertainty_usd": str(held),
        "pending_upper_bound_usd": str(pending_cost),
        "conservative_stage_commitment_usd": str(known + held + pending_cost),
        "failed_attempts": len(failed),
        "failed_call_ids": sorted({r["id"] for r in failed}),
        "unincorporated_completed_ids": sorted(completed - recorded),
        "missing_result_ids": sorted(recorded - completed),
        "pending_call_ids": sorted(r["id"] for r in pending),
        "campaign_known_usd": str(state["known_usd"]),
        "campaign_retained_uncertainty_usd": str(state["holds_usd"]),
        "campaign_conservative_commitment_usd": str(
            state["known_usd"] + state["holds_usd"] + state["pending_usd"]
        ),
    }


def expected_selection(task):
    """Evaluator-only public relation query, never called to build dispatch data."""
    tenant = re.match(r"Tenant (T[A-Z0-9]+);", task["prompt"])[1]
    chosen = [
        p["title"]
        for p in task["meta"]["paragraphs"]
        if p["sentences"][0].startswith(f"tenant={tenant}|")
    ]
    answer = "|".join(sorted(chosen))
    if workload.parse("filter", answer) is None:
        raise PilotError("public selection oracle is malformed")
    return answer


def summarize(rows, manifest, events):
    costs = finance(events, manifest, rows)
    complete = len(rows) == 10 and not any(
        costs[k]
        for k in (
            "failed_attempts",
            "pending_call_ids",
            "unincorporated_completed_ids",
            "missing_result_ids",
        )
    )
    cells = []
    if complete:
        expected = expected_selection(manifest["task"])
        for row in rows:
            truth = LITERAL if row["condition"] == "literal" else expected
            valid = workload.parse("filter", row["answer"]) is not None
            cells.append(
                {
                    **{k: row[k] for k in FIELDS},
                    "strict_valid": valid,
                    "exact": valid and row["answer"] == truth,
                    "finish_reason": row["finish_reason"],
                    "prompt_tokens": row["usage"]["prompt_tokens"],
                    "completion_tokens": row["usage"]["completion_tokens"],
                    "reported_reasoning_tokens": row["usage"]
                    .get("completion_tokens_details", {})
                    .get("reasoning_tokens"),
                    "response_diagnostics": row["response_diagnostics"],
                    "cost_usd": row["cost_usd"],
                    "nominal_uncached_cost_usd": row["nominal_uncached_cost_usd"],
                }
            )
    return {
        "kind": KIND,
        "paper_evidence": False,
        "efficacy_claim": False,
        "manifest_sha256": digest(manifest),
        "rows_sha256": digest(rows),
        "completed_calls": len(rows),
        "scheduled_calls": 10,
        "schedule_complete": complete,
        "comparisons": cells,
        "stage_accounting": costs,
        "limitations": LIMITATIONS,
    }


def run(args, key):
    manifest = json.loads((args.output / "manifest.json").read_text())
    validate(manifest, args.manifest_sha256)
    rows_path = args.output / "calls.json"
    existing = json.loads(rows_path.read_text()) if rows_path.exists() else []
    if not isinstance(existing, list) or len(existing) > 10:
        raise PilotError("control saved rows are not a bounded prefix")
    ledger = Ledger(args.ledger, key)
    events = snapshot(ledger)
    durable, _ = verified_rows(events, manifest)
    if existing != durable[: len(existing)] or len(existing) > len(durable):
        raise PilotError("saved control rows differ from durable ledger")
    limit = args.max_calls
    if limit is not None and (
        type(limit) is not int or not max(1, len(durable)) <= limit <= 10
    ):
        raise PilotError(
            "control call limit precedes saved evidence or exceeds schedule"
        )
    rows = list(existing)
    try:
        if any(
            r["stage"] == stage_id(manifest)
            for r in accounting(events)["terminals"].values()
        ):
            raise PilotError("control stage failed; retries are not authorized")
        for index, item in enumerate(manifest["schedule"]):
            if limit is not None and index >= limit:
                break
            if index >= len(durable):
                ledger.call(
                    key,
                    stage_id(manifest) + f"-{index:02d}",
                    stage_id(manifest),
                    money(CAP),
                    item["payload"],
                    metadata(item, manifest),
                )
                events = snapshot(ledger)
                durable, _ = verified_rows(events, manifest)
            row = durable[index]
            if index < len(existing):
                if row != existing[index]:
                    raise PilotError("cached control evidence changed")
            else:
                extended = [*rows, row]
                write_json(rows_path, extended)
                rows = extended
            print(
                json.dumps(
                    {
                        "completed": index + 1,
                        "scheduled": 10,
                        "model": item["model"],
                        "condition": item["condition"],
                        "shape": item["shape"],
                        "scope": item["scope"],
                    }
                ),
                flush=True,
            )
    finally:
        events = snapshot(ledger)
        report = summarize(rows, manifest, events)
        write_json(args.output / "summary.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    for name in ("env-file", "ledger", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--max-calls", type=int)
    args = parser.parse_args()
    try:
        if args.command == "prepare" and (
            args.previous is None or args.fixture is None
        ):
            raise PilotError("prepare needs frozen previous manifest and fixture")
        if args.command == "run" and not args.manifest_sha256:
            raise PilotError("run needs independently supplied manifest SHA")
        key = load_key(args.env_file)
        report = prepare(args, key) if args.command == "prepare" else run(args, key)
        # Raw output is durable in artifacts; console contains accounting only.
        print(
            json.dumps(
                {k: v for k, v in report.items() if k != "comparisons"}, indent=2
            )
        )
        return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError):
        print(
            "Shape control stopped at validation, provider or budget boundary; details retained privately."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
