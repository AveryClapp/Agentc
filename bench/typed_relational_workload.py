"""Prospective typed procurement application, not a repair of prior experiments.

Only complete, normally finished JSON values cross the application seam. Raw
provider receipts must remain separate; callers normalize finish status through
application_text before storing workflow history. No transport or label oracle.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, cast

from bench import structured_relational_workload as legacy
from bench.openrouter_matrix import PROTOCOL
from bench.openrouter_pilot import PilotError

Json = dict[str, Any]
STAGES = legacy.STAGES
VERSION = legacy.VERSION  # Keep the generator, fixtures, and task IDs unchanged.
WORKLOAD = "typed_relational_v1"
FORMAT_VARIANT = "json-schema-result-v1"
FEEDBACK_CONTRACT = "valid-typed-value-equality=0;otherwise=1-v1"
ERROR = '{"result":null}'
ERROR_INPUT = "ERROR_INPUT: preceding application output failed its typed contract."
MAX_OUTPUT_CHARS = 65536

make_tasks = cast(Callable[[int, int], list[Json]], legacy.make_tasks)
stage_question = cast(Callable[[Json, str], str], legacy.stage_question)
_public = cast(Callable[[Json], Json], legacy._public)
_check_history = cast(Callable[[str, dict[str, str]], None], legacy._check_history)

_FILTER_PATTERN = r"^D(0[1-9]|1[0-9]|20)$"
_ANSWER_PATTERN = r"^M(0[1-9]|[1-9][0-9])$"
_SYNTH_PROPERTIES: Json = {
    "sku": {"type": "string", "pattern": r"^K[0-9]{3}$"},
    "vendor": {"type": "string", "pattern": r"^V[0-9]{3}$"},
    "qty": {"type": "integer"},
    "unit": {"type": "integer"},
    "total": {"type": "integer"},
    "region": {"type": "string", "enum": ["EU", "US"]},
}
_RESULT_SCHEMAS: Json = {
    "filter": {
        "type": "array",
        "items": {"type": "string", "pattern": _FILTER_PATTERN},
    },
    "synthesize": {
        "type": "object",
        "properties": _SYNTH_PROPERTIES,
        "required": list(_SYNTH_PROPERTIES),
        "additionalProperties": False,
    },
    "answer": {"type": "string", "pattern": _ANSWER_PATTERN},
}
_JSON_RULE = (
    'Return one JSON object with the sole field "result", matching the supplied '
    'schema. No markdown or explanatory prose. Return {"result":null} if input '
    "is ERROR_INPUT or the operation cannot be completed from supplied data. "
)
_PROMPTS = {
    "FILTER_SYSTEM": (
        "You are the record-selection stage of a procurement application. "
        "Select exactly the three documents belonging to the requested tenant: "
        "its REQUESTS, CATALOG, and OFFERS documents. Do not solve the join or "
        "approval policy. Other tenants are unrelated, even if their keys match. "
        "The result is an array of exactly three distinct document IDs, already "
        "sorted ascending. IDs range D01 through D20. " + _JSON_RULE
    ),
    "SYNTH_SYSTEM": (
        "You are the relational-join stage of a procurement application. "
        "Use only the selected records for the requested tenant. Locate the "
        "requested requisition in REQUESTS, join its sku to CATALOG to obtain "
        "vendor, then join vendor to OFFERS to obtain integer unit price. "
        "Calculate total=qty*unit without tax or shipping adjustments; those are "
        "already included. Preserve the requisition region. Ignore the approval "
        "policy at this stage. The result is an object with exactly sku, vendor, "
        "qty, unit, total, region. K/V identifiers have exactly three digits. "
        "qty is an integer from 1 to 999; unit from 1 to 9999; total from 1 to "
        "9999999. region is EU or US. " + _JSON_RULE
    ),
    "ANSWER_SYSTEM": (
        "You are the approval-policy stage of a procurement application. "
        "The synthesis is the authoritative application fact. Apply the public "
        "approval table to its region and total, choosing exactly one approver. "
        "The earlier selected records are stale framework state: do not read, "
        "recompute, or correct the synthesis from them. The result is a string "
        "approver ID, M01 through M99, with exactly two digits. " + _JSON_RULE
    ),
}
_FINAL_OUTPUT_RULES = {
    "filter": "Return a JSON result array containing exactly three sorted, distinct IDs.",
    "synthesize": "Return a JSON result object containing the six requested fields.",
    "answer": "Return a JSON result string containing the approver ID.",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def prompts() -> dict[str, str]:
    return deepcopy(_PROMPTS)


def schema_for(stage: str) -> Json:
    """Return the full response_format wrapper; local checks exceed this subset."""
    if stage not in STAGES:
        raise PilotError("unknown typed workflow stage")
    return {
        "type": "json_schema",
        "json_schema": {
            "name": f"typed_procurement_{stage}_v1",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "result": {
                        "anyOf": [deepcopy(_RESULT_SCHEMAS[stage]), {"type": "null"}]
                    }
                },
                "required": ["result"],
                "additionalProperties": False,
            },
        },
    }


def contract_hash(prompt_values: dict[str, str] | None = None) -> str:
    """Version this module, reused public generator/renderer, schemas and prompts."""
    values = prompts() if prompt_values is None else prompt_values
    if set(values) != set(_PROMPTS) or not all(
        isinstance(v, str) and v for v in values.values()
    ):
        raise PilotError("typed prompt contract is incomplete")
    return _digest(
        {
            "version": VERSION,
            "workload": WORKLOAD,
            "format_variant": FORMAT_VARIANT,
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "legacy_source_sha256": hashlib.sha256(
                Path(legacy.__file__).read_bytes()
            ).hexdigest(),
            "prompts": values,
            "schemas": {stage: schema_for(stage) for stage in STAGES},
            "feedback": FEEDBACK_CONTRACT,
        }
    )


def _object(pairs: list[tuple[str, Any]]) -> Json:
    result: Json = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _result(text: Any, finish_reason: str) -> Any:
    if (
        finish_reason != "stop"
        or not isinstance(text, str)
        or len(text) > MAX_OUTPUT_CHARS
    ):
        return None
    try:
        value = json.loads(
            text, object_pairs_hook=_object, parse_constant=_reject_constant
        )
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or set(value) != {"result"}:
        return None
    return value["result"]


def _valid_result(stage: str, value: Any) -> bool:
    if stage == "filter":
        return (
            isinstance(value, list)
            and len(value) == 3
            and all(
                isinstance(v, str) and re.fullmatch(_FILTER_PATTERN, v) for v in value
            )
            and value == sorted(set(value))
        )
    if stage == "synthesize":
        return (
            isinstance(value, dict)
            and set(value) == set(_SYNTH_PROPERTIES)
            and all(
                isinstance(value[k], str) and re.fullmatch(pattern, value[k])
                for k, pattern in (("sku", r"K[0-9]{3}"), ("vendor", r"V[0-9]{3}"))
            )
            and all(
                type(value[k]) is int and 1 <= value[k] <= maximum
                for k, maximum in (("qty", 999), ("unit", 9999), ("total", 9999999))
            )
            and isinstance(value["region"], str)
            and value["region"] in ("EU", "US")
        )
    return isinstance(value, str) and re.fullmatch(_ANSWER_PATTERN, value) is not None


def parse_stage(stage: str, text: Any, *, finish_reason: str = "stop") -> Any:
    """Validate syntax/domain, never selection, arithmetic, policy, or gold."""
    if stage not in STAGES:
        raise PilotError("unknown typed workflow stage")
    value = _result(text, finish_reason)
    if not _valid_result(stage, value):
        return None
    return tuple(value) if stage == "filter" else value


parse = parse_stage


def parse_filter(text: Any, *, finish_reason: str = "stop") -> Any:
    return parse_stage("filter", text, finish_reason=finish_reason)


def parse_synthesis(text: Any, *, finish_reason: str = "stop") -> Any:
    return parse_stage("synthesize", text, finish_reason=finish_reason)


def parse_answer(text: Any, *, finish_reason: str = "stop") -> Any:
    return parse_stage("answer", text, finish_reason=finish_reason)


def application_text(
    raw_text: Any, finish_reason: str = "stop", *, stage: str | None = None
) -> str:
    """Return canonical valid application state, or the contagious null envelope.

    Infer the stage only when omitted: valid result domains (array/object/string)
    are disjoint. This function does not change the retained provider receipt.
    """
    stages = STAGES if stage is None else (stage,)
    for candidate_stage in stages:
        value = parse_stage(candidate_stage, raw_text, finish_reason=finish_reason)
        if value is not None:
            return _canonical({"result": value})
    return ERROR


def application_disagreement(
    stage: str,
    reference: Any,
    candidate: Any,
    *,
    upstream_valid: bool = True,
    reference_finish_reason: str = "stop",
    candidate_finish_reason: str = "stop",
) -> float:
    left = parse_stage(stage, reference, finish_reason=reference_finish_reason)
    right = parse_stage(stage, candidate, finish_reason=candidate_finish_reason)
    return float(
        upstream_valid is not True or left is None or right is None or left != right
    )


def normalized_disagreement(reference: Any, candidate: Any) -> float:
    """Stage-inferred typed equality for already status-normalized application state."""
    return min(
        application_disagreement(stage, reference, candidate) for stage in STAGES
    )


def history_valid(task: Json, stage: str, history: dict[str, str]) -> bool:
    _check_history(stage, history)
    public = _public(task)
    if any(parse_stage(prior, text) is None for prior, text in history.items()):
        return False
    if "filter" in history:
        return set(parse_filter(history["filter"])) <= {
            p["title"] for p in public["meta"]["paragraphs"]
        }
    return True


def score_answer(
    text: str, expected: str, *, history: dict[str, str] | None = None
) -> dict[str, float]:
    """Evaluator-only exact final scalar, after whole-path application validation."""
    answer = parse_answer(text)
    valid = (
        answer is not None
        and isinstance(expected, str)
        and re.fullmatch(_ANSWER_PATTERN, expected) is not None
    )
    if history is not None:
        if set(history) not in ({"filter", "synthesize"}, set(STAGES)):
            raise PilotError("scoring needs this arm's complete preceding history")
        valid = valid and all(
            parse_stage(stage, value) is not None for stage, value in history.items()
        )
        if "answer" in history:
            valid = valid and parse_answer(history["answer"]) == answer
    score = float(valid and answer == expected)
    return {"em": score, "f1": score}


def call(
    task: Json,
    stage: str,
    history: dict[str, str],
    attention: Any,
    *,
    model: str,
    run_identity: str,
    prompts: dict[str, str] | None = None,
) -> Json:
    """Preserve the 22/3/4 native IR; state contains validated application JSON."""
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
    deps: list[Json] = [{"kind": "literal"}]
    reads: list[str] = []
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
        selected = parse_filter(history["filter"])
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
                    "content": application_text(
                        history["synthesize"], stage="synthesize"
                    )
                    if valid
                    else ERROR_INPUT,
                }
            )
            deps.append({"kind": "state", "key": "synthesis"})
            reads = ["synthesis"]
    span_id = _digest([run_identity, task["task_id"], stage])[:16]
    messages.append(
        {
            "role": "user",
            "content": "Question: "
            + stage_question(task, stage)
            + ("\n" + ERROR_INPUT if not valid else "")
            + "\n"
            + _FINAL_OUTPUT_RULES[stage],
        }
    )
    deps.append({"kind": "user_input", "span_id": span_id})
    scores, follow = attention.compute_attention_scores(messages, None)
    response_format = schema_for(stage)
    # Client representation only: excludes opaque provider schema instructions,
    # so this is not a provider-context proof. Routing eligibility needs separate
    # review; dispatch also needs a reviewed schema-aware spending reservation.
    encoded = _canonical(
        {"messages": messages, "response_format": response_format}
    ).encode()
    return {
        "call_site_id": f"{WORKLOAD}/{namespace}/{stage}",
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
                "response_format": response_format,
                "typed_client_size_diagnostic": {
                    "json_utf8_bytes": len(encoded),
                    "scope": "client representation only; excludes opaque provider schema instructions; not a provider-context proof",
                },
                "typed_relational_contract": {
                    "sha256": namespace,
                    "upstream_valid": valid,
                },
                "agentc_route_context": {
                    "provider_protocol": PROTOCOL,
                    "provider_namespace": "openrouter",
                    "image_input": False,
                    "tool_calling": False,
                    "structured_outputs": True,
                    "streaming": False,
                },
            },
        },
    }
