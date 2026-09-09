"""Offline FinQA retrieve -> program -> execute application contract.

No transport, labels, controller feedback, or native admission. Two LLM calls,
not a padded three-call benchmark. The last step is a bounded interpreter, never
Python eval. Execution correctness is not correctness of the evidence/reasoning.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

VERSION = "finqa-retrieve-program-v1"
STAGES = ("retrieve", "program")
MAX_RAW_BYTES = 65536
MAX_STEPS = 32
OPS = (
    "add",
    "subtract",
    "multiply",
    "divide",
    "exp",
    "greater",
    "table_max",
    "table_min",
    "table_sum",
    "table_average",
)
_ID = re.compile(r"([^/\x00]+/[0-9]{4})/page_[0-9]+\.pdf-[0-9]+")
_JSON_RULE = (
    ' Return one JSON object with only "result": an array of strings, '
    "or null when unable. No markdown, prose, or additional keys."
)
_PROMPTS = {
    "retrieve": (
        "Select the evidence IDs needed to answer the question from the report. "
        "Select text or table-row IDs, not an answer. IDs form a set; do not "
        "invent IDs. Table header is supplied separately." + _JSON_RULE
    ),
    "program": (
        "Using only the retrieved report evidence, produce FinQA program tokens. "
        "Each step is four strings: operation followed by (, first argument, "
        "second argument, and ). End the array with EOF. Operations: add, "
        "subtract, multiply, divide, exp, greater, table_max, table_min, "
        "table_sum, table_average. Refer to earlier results as #0, #1, etc. "
        "Numbers may contain commas or a percent suffix; const_m1 is -1 and "
        "const_N is N. Table operations take an exact row label and none. "
        'Example tokens: ["subtract(","9","4",")","EOF"]. '
        "Use at most 32 steps. ERROR_INPUT requires a null result." + _JSON_RULE
    ),
}


class ContractError(ValueError):
    """Invalid public input or incomplete evaluation inventory."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def report_id(task_id: str) -> str:
    match = _ID.fullmatch(task_id) if isinstance(task_id, str) else None
    if match is None:
        raise ContractError("invalid FinQA source ID")
    return match[1]


def _text(value: Any, *, blank: bool = False) -> bool:
    if not isinstance(value, str) or (not blank and not value.strip()):
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_RAW_BYTES
    except UnicodeEncodeError:
        return False


def public_task(row: dict) -> dict:
    """Allowlist projection; do not iterate or copy gold/retriever metadata."""
    try:
        task_id = row["id"]
        report_id(task_id)
        question = row["qa"]["question"]
        before, after, table = row["pre_text"], row["post_text"], row["table"]
        if not _text(question) or any(
            not isinstance(items, list) or not all(_text(x, blank=True) for x in items)
            for items in (before, after)
        ):
            raise ContractError("invalid public question or text")
        if (
            not isinstance(table, list)
            or not table
            or any(
                not isinstance(r, list)
                or len(r) < 2
                or not all(_text(c, blank=True) for c in r)
                for r in table
            )
        ):
            raise ContractError("invalid public table")
        # No gold_inds, program, exe_ans, model_input, or retrieval predictions.
        return deepcopy(
            {
                "id": task_id,
                "qa": {"question": question},
                "pre_text": before,
                "post_text": after,
                "table": table,
            }
        )
    except (KeyError, TypeError) as exc:
        raise ContractError("malformed public FinQA row") from exc


def _evidence(task: dict) -> dict[str, str]:
    result = {f"pre:{i}": text for i, text in enumerate(task["pre_text"])}
    result.update(
        {f"table:{i}": canonical(row) for i, row in enumerate(task["table"]) if i > 0}
    )
    result.update({f"post:{i}": text for i, text in enumerate(task["post_text"])})
    return result


def schema_for(stage: str) -> dict:
    if stage not in STAGES:
        raise ContractError("unknown FinQA stage")
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "finqa_" + stage + "_v1",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "result": {
                        "anyOf": [
                            {"type": "array", "items": {"type": "string"}},
                            {"type": "null"},
                        ]
                    }
                },
                "required": ["result"],
                "additionalProperties": False,
            },
        },
    }


def _decode(raw: Any, finish_reason: str) -> list[str] | None:
    if finish_reason != "stop" or not _text(raw):
        return None

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError("nonfinite JSON")

    try:
        value = json.loads(
            raw, object_pairs_hook=unique, parse_constant=reject_constant
        )
        result = value.get("result") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or set(value) != {"result"}
            or not isinstance(result, list)
            or not result
            or not all(_text(x) for x in result)
        ):
            return None
        return result
    except (ValueError, RecursionError):
        return None


def parse_stage(
    task: dict, stage: str, raw: Any, *, finish_reason: str = "stop"
) -> list[str] | None:
    task = public_task(task)
    if stage not in STAGES:
        raise ContractError("unknown FinQA stage")
    result = _decode(raw, finish_reason)
    if result is None:
        return None
    if stage == "retrieve":
        if len(set(result)) != len(result) or not set(result) <= _evidence(task).keys():
            return None
        # The application explicitly interprets retrieval IDs as an unordered set.
        return sorted(result)
    if (
        result[-1] != "EOF"
        or len(result) < 5
        or (len(result) - 1) % 4
        or (len(result) - 1) // 4 > MAX_STEPS
    ):
        return None
    for i in range(0, len(result) - 1, 4):
        if result[i] not in {op + "(" for op in OPS} or result[i + 3] != ")":
            return None
    return result


def application_text(
    task: dict,
    stage: str,
    raw: Any,
    *,
    finish_reason: str = "stop",
    upstream_valid: bool = True,
) -> str:
    """Canonical application state; caller must pass the observed finish reason.

    Keep the raw provider receipt separately. Null is contagious: a plausible
    downstream answer never repairs a failed preceding application operation.
    """
    result = parse_stage(task, stage, raw, finish_reason=finish_reason)
    return canonical({"result": result if upstream_valid is True else None})


def call(
    task: dict,
    stage: str,
    history: dict[str, str],
    attention: Any,
    *,
    model: str,
    run_identity: str,
) -> dict:
    """Build one native-shaped request without dispatching or admitting a plan.

    History must belong to this arm and contain application_text outputs, not
    unvalidated provider receipts. Provenance of that assertion is a future
    runner's responsibility. No reference/probe output is substituted here.
    """
    task = public_task(task)
    if (
        stage not in STAGES
        or not isinstance(history, dict)
        or set(history) != set(STAGES[: STAGES.index(stage)])
    ):
        raise ContractError("expected exactly this arm's preceding stage history")
    if not _text(model) or not _text(run_identity):
        raise ContractError("model and run identity are required")
    available = _evidence(task)
    chosen = (
        list(available)
        if stage == "retrieve"
        else parse_stage(task, "retrieve", history["retrieve"])
    )
    valid = chosen is not None
    evidence = (
        {key: value for key, value in available.items() if key in chosen}
        if valid
        else None
    )
    namespace = digest(
        {
            "version": VERSION,
            "source": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
    )
    span = digest([run_identity, task["id"], stage])[:16]
    messages = [{"role": "system", "content": _PROMPTS[stage]}]
    deps = [{"kind": "literal"}]
    messages.append(
        {"role": "user", "content": "Table header: " + canonical(task["table"][0])}
    )
    deps.append({"kind": "literal"})
    if evidence is None:
        messages.append({"role": "user", "content": "ERROR_INPUT"})
        deps.append({"kind": "state", "key": "retrieved_evidence"})
    else:
        for key, value in evidence.items():
            messages.append({"role": "user", "content": key + "\n" + value})
            deps.append(
                {"kind": "literal"}
                if stage == "retrieve"
                else {"kind": "state", "key": "retrieved_evidence"}
            )
    messages.append({"role": "user", "content": "Question: " + task["qa"]["question"]})
    deps.append({"kind": "user_input", "span_id": span})
    scores, follow = attention.compute_attention_scores(messages, None)
    return {
        "call_site_id": f"{VERSION}/{namespace}/{stage}",
        "trace_id": digest([run_identity, task["id"]])[:32],
        "span_id": span,
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
                "window_state_reads": []
                if stage == "retrieve"
                else ["retrieved_evidence"],
                "response_format": schema_for(stage),
                "finqa_contract": {"sha256": namespace, "upstream_valid": valid},
                "agentc_route_context": {
                    "provider_protocol": "openrouter.chat.completions.v1",
                    "provider_namespace": "openrouter",
                    "image_input": False,
                    "tool_calling": False,
                    "structured_outputs": True,
                    "streaming": False,
                },
            },
        },
    }


def _number(text: str) -> float:
    text = text.replace(",", "")
    if text.startswith("const_"):
        text = "-1" if text == "const_m1" else text[6:]
    percent = text.endswith("%")
    value = float(text[:-1] if percent else text)
    value = value / 100 if percent else value
    if not math.isfinite(value):
        raise ValueError("nonfinite operand")
    return value


def _execute(tokens: list[str], table: list[list[str]]) -> float | str | None:
    results: list[float | str] = []

    def argument(token):
        if re.fullmatch(r"#[0-9]+", token):
            index = int(token[1:])
            if index >= len(results) or isinstance(results[index], str):
                raise ValueError("forward or nonnumeric reference")
            return results[index]
        return _number(token)

    try:
        for i in range(0, len(tokens) - 1, 4):
            op, left, right = tokens[i][:-1], tokens[i + 1], tokens[i + 2]
            if op.startswith("table_"):
                rows = [row for row in table if row[0] == left]
                if len(rows) != 1 or right != "none":
                    return None
                values = [
                    _number(c.replace("$", "").strip().split("(")[0].strip())
                    for c in rows[0][1:]
                ]
                value = {
                    "table_max": max,
                    "table_min": min,
                    "table_sum": sum,
                    "table_average": lambda xs: sum(xs) / len(xs),
                }[op](values)
            else:
                a, b = argument(left), argument(right)
                if op == "add":
                    value = a + b
                elif op == "subtract":
                    value = a - b
                elif op == "multiply":
                    value = a * b
                elif op == "divide":
                    value = a / b
                elif op == "exp":
                    value = a**b
                else:
                    value = "yes" if a > b else "no"
            if not isinstance(value, str) and (
                isinstance(value, complex) or not math.isfinite(value)
            ):
                return None
            results.append(value)
        result = results[-1]
        return result if isinstance(result, str) else round(result, 5)
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


def execute(task: dict, history: dict[str, str]) -> float | str | None:
    """Execute against the public table, as upstream; invalid state stays invalid."""
    task = public_task(task)
    if not isinstance(history, dict) or set(history) != set(STAGES):
        raise ContractError("execution requires the whole two-stage arm history")
    chosen = parse_stage(task, "retrieve", history["retrieve"])
    tokens = parse_stage(task, "program", history["program"])
    if chosen is None or tokens is None:
        return None
    return _execute(tokens, task["table"])


def score_inventory(
    tasks: list[dict],
    histories: dict[str, dict[str, str]],
    labels: dict[str, float | str],
) -> dict:
    """Offline-only, exact selected-ID denominator; no program-equivalence claim.

    Gold must be separately supplied. Official FinQA execution compares the
    five-decimal program result directly to exe_ans, not a substring/tolerance.
    Our application additionally rejects malformed/unbounded programs and
    ambiguous table-row labels.
    """
    public = [public_task(task) for task in tasks]
    ids = [task["id"] for task in public]
    if (
        not ids
        or len(ids) != len(set(ids))
        or set(histories) != set(ids)
        or set(labels) != set(ids)
    ):
        raise ContractError("evaluation requires every selected ID exactly once")
    if any(
        not (
            (type(x) in (int, float) and math.isfinite(x))
            or (type(x) is str and x in ("yes", "no"))
        )
        for x in labels.values()
    ):
        raise ContractError("invalid separately supplied execution label")
    rows = []
    for task in public:
        result = execute(task, histories[task["id"]])
        rows.append(
            {
                "id": task["id"],
                "valid": result is not None,
                "correct": result is not None and result == labels[task["id"]],
            }
        )
    correct = sum(row["correct"] for row in rows)
    return {
        "metric": "guarded-finqa-execution-accuracy-v1",
        "total": len(rows),
        "correct": correct,
        "accuracy": correct / len(rows),
        "rows": rows,
    }
