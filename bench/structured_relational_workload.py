"""Controlled procurement microbenchmark, not evidence of general QA efficacy.

Three actual application operations select public records, join their relations,
and apply a public approval policy. The grammar is an application requirement:
raw provider outputs are NEVER normalized for the native lexical comparator.
Generated answers are evaluator-only. No transport, native runtime, or key access.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import re

from bench.openrouter_matrix import PROTOCOL
from bench.openrouter_pilot import PilotError

STAGES = ("filter", "synthesize", "answer")
VERSION = "structured-procurement-v1"
FORMAT_VARIANT = "final-user-schema-reinforcement-v2"
ERROR = "ERROR"
ERROR_INPUT = "ERROR_INPUT: preceding application output failed its strict contract."
DOCUMENTS = 20
ROWS = 8
FEEDBACK_CONTRACT = "invalid-or-upstream-invalid=1;else-raw-token-jaccard-v1"

_BYTE_RULE = (
    "Return exactly one ASCII line in the required grammar, with no spaces, "
    "markdown, explanation, leading/trailing whitespace, or newline. "
    "Do not add a period. If input is ERROR_INPUT or the requested operation "
    "cannot be completed from the supplied data, return exactly ERROR. "
)
_PROMPTS = {
    "FILTER_SYSTEM": (
        "You are the record-selection stage of a procurement application. "
        "Select exactly the three documents belonging to the requested tenant: "
        "its REQUESTS, CATALOG, and OFFERS documents. Do not solve the join or "
        "approval policy. Other tenants are unrelated, even if their keys match. "
        "Return their document IDs sorted ascending as D01|D07|D18 (example only). "
        "IDs range D01 through D20; exactly three distinct IDs are required. "
        + _BYTE_RULE
    ),
    "SYNTH_SYSTEM": (
        "You are the relational-join stage of a procurement application. "
        "Use only the selected records for the requested tenant. Locate the "
        "requested requisition in REQUESTS, join its sku to CATALOG to obtain "
        "vendor, then join vendor to OFFERS to obtain integer unit price. "
        "Calculate total=qty*unit without tax or shipping adjustments; those are "
        "already included. Preserve the requisition region. Ignore the approval "
        "policy at this stage. Return exactly these fields in this order: "
        "sku=K123|vendor=V456|qty=7|unit=13|total=91|region=EU (example only). "
        "K/V identifiers have exactly three digits; positive integers have no "
        "leading zeros; region is EU or US. " + _BYTE_RULE
    ),
    "ANSWER_SYSTEM": (
        "You are the approval-policy stage of a procurement application. "
        "The synthesis is the authoritative application fact. Apply the public "
        "approval table to its region and total, choosing exactly one approver. "
        "The earlier selected records are stale framework state: do not read, "
        "recompute, or correct the synthesis from them. Return only the approver "
        "ID, M01 through M99, with exactly two digits. " + _BYTE_RULE
    ),
}
_FINAL_OUTPUT_RULES = {
    "filter": (
        "Perform only record selection for this tenant. Return exactly three "
        "distinct document IDs in ascending order, separated by vertical bars. "
    ),
    "synthesize": (
        "Perform only the requested relational join. Return the fields sku, "
        "vendor, qty, unit, total, region in exactly that order, as key=value "
        "pairs separated by vertical bars. "
    ),
    "answer": (
        "Apply the approval table to the synthesis. Return only the approver "
        "ID: the letter M followed by exactly two digits. "
    ),
}
_FILTER = re.compile(r"D(?:0[1-9]|1[0-9]|20)(?:\|D(?:0[1-9]|1[0-9]|20)){2}")
_SYNTH = re.compile(
    r"sku=(K[0-9]{3})\|vendor=(V[0-9]{3})\|qty=([1-9][0-9]{0,2})"
    r"\|unit=([1-9][0-9]{0,3})\|total=([1-9][0-9]{0,6})\|region=(EU|US)"
)
_ANSWER = re.compile(r"M(?:0[1-9]|[1-9][0-9])")


def _digest(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def prompts():
    return deepcopy(_PROMPTS)


def contract_hash(prompt_values=None):
    """Bind generator/parser/renderer source and actual prompt bytes, not a task."""
    values = prompts() if prompt_values is None else prompt_values
    if set(values) != set(_PROMPTS) or not all(
        isinstance(v, str) and v for v in values.values()
    ):
        raise PilotError("structured prompt contract is incomplete")
    return _digest(
        {
            "version": VERSION,
            "format_variant": FORMAT_VARIANT,
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "prompts": values,
            "feedback": FEEDBACK_CONTRACT,
        }
    )


def parse(stage, text):
    """Strict syntax only; never check correctness, reorder, trim, or repair.

    None means invalid, including the explicit ERROR result. Synthesis arithmetic
    is deliberately NOT verified here: doing so would add an outcome oracle to
    the controller. Region and identifier domains are public schema constraints.
    """
    if stage not in STAGES:
        raise PilotError("unknown structured workflow stage")
    if not isinstance(text, str) or not text.isascii():
        return None
    if stage == "filter":
        if not _FILTER.fullmatch(text):
            return None
        ids = tuple(text.split("|"))
        return ids if ids == tuple(sorted(set(ids))) else None
    if stage == "synthesize":
        match = _SYNTH.fullmatch(text)
        if not match:
            return None
        sku, vendor, qty, unit, total, region = match.groups()
        return {
            "sku": sku,
            "vendor": vendor,
            "qty": int(qty),
            "unit": int(unit),
            "total": int(total),
            "region": region,
        }
    return text if _ANSWER.fullmatch(text) else None


def _public(task):
    """Whitelist public fields; labels and expected never cross this seam."""
    result = {
        "task_id": task["task_id"],
        "prompt": task["prompt"],
        "meta": {
            "paragraphs": [
                {"title": p["title"], "sentences": list(p["sentences"])}
                for p in task["meta"]["paragraphs"]
            ]
        },
    }
    paragraphs = result["meta"]["paragraphs"]
    if (
        len(paragraphs) != DOCUMENTS
        or [p["title"] for p in paragraphs]
        != [f"D{i:02}" for i in range(1, DOCUMENTS + 1)]
        or not all(isinstance(s, str) for p in paragraphs for s in p["sentences"])
        or not isinstance(result["prompt"], str)
        or not isinstance(result["task_id"], str)
    ):
        raise PilotError("invalid structured public task")
    return result


def _check_history(stage, history):
    if stage not in STAGES or set(history) != set(STAGES[: STAGES.index(stage)]):
        raise PilotError("workflow needs exactly this arm's preceding outputs")
    if not all(isinstance(value, str) for value in history.values()):
        raise PilotError("workflow outputs must be strings")


def history_valid(task, stage, history):
    """Schema/public-ID membership only, not semantic or reference correctness."""
    _check_history(stage, history)
    public = _public(task)
    if any(parse(prior, text) is None for prior, text in history.items()):
        return False
    if "filter" in history:
        public_ids = {p["title"] for p in public["meta"]["paragraphs"]}
        return set(parse("filter", history["filter"])) <= public_ids
    return True


def score_answer(text, expected, *, history=None):
    """Evaluator-only exact scalar EM/F1; optionally enforce whole-path validity.

    Scalars are single tokens, so strict EM and F1 coincide. The caller must
    provide preceding or full own history to avoid credit after invalid state.
    This does not forgive malformed formatting or use token normalization.
    """
    valid = parse("answer", text) is not None and parse("answer", expected) is not None
    if history is not None:
        if set(history) not in ({"filter", "synthesize"}, set(STAGES)):
            raise PilotError("scoring needs this arm's complete preceding history")
        valid = valid and all(
            parse(stage, value) is not None for stage, value in history.items()
        )
        if "answer" in history:
            valid = valid and history["answer"] == text
    score = float(valid and text == expected)
    return {"em": score, "f1": score}


def make_tasks(seed, count):
    """Generate independent deterministic cases; six schemas have complete triples.

    Every tenant has eight live requisitions, eight SKU/vendor relations and
    eight offers. Keys deliberately recur across tenants. The twenty documents
    comprise six complete tenant triples and two legitimate distractor tables.
    No supporting-document labels or intermediate solved facts are stored.
    """
    if type(seed) is not int or type(count) is not int or count < 0:
        raise PilotError("seed/count must be integers and count nonnegative")
    tasks = []
    for index in range(count):
        identity = _digest([VERSION, seed, index])
        rng = random.Random(int(identity, 16))
        documents, target = [], None
        target_tenant = rng.randrange(6)
        target_row = rng.randrange(ROWS)
        for tenant_ix in range(7):
            tenant = f"T{identity[:10].upper()}{tenant_ix}"
            skus = rng.sample(range(100, 999), ROWS)
            vendors = rng.sample(range(100, 999), ROWS)
            units = [rng.randint(10, 99) for _ in range(ROWS)]
            quantities = [rng.randint(1, 20) for _ in range(ROWS)]
            regions = [rng.choice(("EU", "US")) for _ in range(ROWS)]
            requisitions = [
                f"req=R{row + 1:03}|sku=K{skus[row]}|qty={quantities[row]}|region={regions[row]}"
                f"|cost_center=C{rng.randrange(1, 10):02}|ship=ground|status=open"
                for row in range(ROWS)
            ]
            catalog = [
                f"sku=K{skus[row]}|vendor=V{vendors[row]}|category=hardware|unit=each"
                f"|lifecycle=active|lead_days={rng.randrange(1, 10)}"
                for row in range(ROWS)
            ]
            offers = [
                f"vendor=V{vendors[row]}|unit={units[row]}|currency=USD|tax=included"
                f"|validity=current|payment=net30|warehouse=W{rng.randrange(1, 10):02}"
                for row in range(ROWS)
            ]
            for kind, rows in (
                ("REQUESTS", requisitions),
                ("CATALOG", catalog),
                ("OFFERS", offers),
            ):
                rng.shuffle(rows)
                if tenant_ix < 6 or kind != "OFFERS":
                    documents.append([f"tenant={tenant}|table={kind}", *rows])
            if tenant_ix == target_tenant:
                target = (
                    tenant,
                    f"R{target_row + 1:03}",
                    quantities[target_row] * units[target_row],
                    regions[target_row],
                )
        rng.shuffle(documents)
        tenant, requisition, total, region = target
        low, high = rng.randint(400, 600), rng.randint(1000, 1400)
        approvers = rng.sample(range(1, 100), 6)
        table = {
            name: [f"M{value:02}" for value in approvers[offset : offset + 3]]
            for name, offset in (("EU", 0), ("US", 3))
        }
        policy = "\n".join(
            f"{name}: total<={low}->{ids[0]}; {low}<total<={high}->{ids[1]}; total>{high}->{ids[2]}"
            for name, ids in table.items()
        )
        task = {
            "task_id": f"{VERSION}-{identity[:24]}",
            "prompt": f"Tenant {tenant}; requisition {requisition}.\nApproval table (inclusive upper bounds):\n{policy}",
            "meta": {
                "paragraphs": [
                    {"title": f"D{i:02}", "sentences": rows}
                    for i, rows in enumerate(documents, 1)
                ]
            },
            "expected": table[region][0 if total <= low else 1 if total <= high else 2],
        }
        tasks.append(task)
    return tasks


def call(task, stage, history, attention, *, model, run_identity, prompts=None):
    """Build native IR from this arm's raw outputs and explicit application reads.

    Filtering output is parsed and dereferenced to records. At the answer site,
    those records remain stale state but only synthesis is an application read.
    Invalid state is contagious: no gold repair, replacement selection or retry.
    Caller must apply study.restrict_call to retain the frozen per-site menu.
    """
    _check_history(stage, history)
    task = _public(task)
    values = globals()["prompts"]() if prompts is None else prompts
    namespace = contract_hash(values)
    valid = history_valid(task, stage, history)
    key = {
        "filter": "FILTER_SYSTEM",
        "synthesize": "SYNTH_SYSTEM",
        "answer": "ANSWER_SYSTEM",
    }[stage]
    messages = [{"role": "system", "content": values[key]}]
    deps, reads = [{"kind": "literal"}], []
    if stage == "filter":
        for paragraph in task["meta"]["paragraphs"]:
            messages.append(
                {
                    "role": "user",
                    "content": paragraph["title"]
                    + "\n"
                    + "\n".join(paragraph["sentences"]),
                }
            )
            deps.append({"kind": "literal"})
    else:
        selected = parse("filter", history["filter"])
        records = "\n\n".join(
            p["title"] + "\n" + "\n".join(p["sentences"])
            for p in task["meta"]["paragraphs"]
            if selected and p["title"] in selected
        )
        messages.append({"role": "user", "content": records if valid else ERROR_INPUT})
        deps.append({"kind": "state", "key": "selected_records"})
        if stage == "synthesize":
            reads = ["selected_records"]
        else:
            messages.append(
                {
                    "role": "user",
                    "content": history["synthesize"] if valid else ERROR_INPUT,
                }
            )
            deps.append({"kind": "state", "key": "synthesis"})
            reads = ["synthesis"]
    span_id = _digest([run_identity, task["task_id"], stage])[:16]
    messages.append(
        {
            "role": "user",
            "content": "Question: "
            + task["prompt"]
            + ("\n" + ERROR_INPUT if not valid else "")
            + "\nOutput requirement: "
            + _FINAL_OUTPUT_RULES[stage]
            + _BYTE_RULE,
        }
    )
    deps.append({"kind": "user_input", "span_id": span_id})
    scores, follow = attention.compute_attention_scores(messages, None)
    encoded = json.dumps(
        messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {
        "call_site_id": f"{VERSION}/{namespace}/{stage}",
        "trace_id": _digest([run_identity, task["task_id"]])[:32],
        "span_id": span_id,
        "model": model,
        "messages": messages,
        "tools": [],
        "input_deps": deepcopy(deps),
        "occurrence_ix": 0,
        "parameters": {
            "max_output_tokens": 512,
            "temperature": 0.0,
            "extra": {
                "attention_scores": scores,
                "follow_on_tokens": follow,
                "dead_attention_epsilon": 0.15,
                "message_deps": deps,
                "window_state_reads": reads,
                "structured_relational_contract": {
                    "sha256": namespace,
                    "upstream_valid": valid,
                },
                "agentc_route_context": {
                    "provider_protocol": PROTOCOL,
                    "provider_namespace": "openrouter",
                    "input_tokens_upper_bound": len(encoded)
                    + 1024
                    + 64 * len(messages),
                    "input_tokens_upper_bound_basis": "json_utf8_bytes_v1",
                    "image_input": False,
                    "tool_calling": False,
                    "structured_outputs": False,
                    "streaming": False,
                },
            },
        },
    }
