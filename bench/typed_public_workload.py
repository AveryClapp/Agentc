"""Typed natural-text research workflow; no gold, transport, or routing proof.

JSON decoding is the application operation. Decoded text crosses stage seams
verbatim. Its semantic truth is not validated here; reference equality is not
registered as a safe semantic feedback contract for this natural-language task.
"""

from __future__ import annotations

import hashlib
import json
import re
import string
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from bench.openrouter_matrix import PROTOCOL
from bench.openrouter_pilot import PilotError, digest
from bench.openrouter_rules_protocol import STAGES, prompt_constants

ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = "typed_public_research_v1"
FORMAT_VARIANT = "json-schema-natural-text-result-v1"
ERROR = '{"result":null}'
ERROR_INPUT = "ERROR_INPUT: preceding application output failed its typed contract."
MAX_RESULT_BYTES = 8192
MAX_RAW_BYTES = 65536
EVALUATOR = {
    "url": "https://raw.githubusercontent.com/hotpotqa/hotpot/3635853403a8735609ee997664e1528f4480762a/hotpot_evaluate_v1.py",
    "sha256": "d35fc91a6db21d791dbdda11daf3856e9359f5701d54e3eefba20d88fecc02c0",
    "license": "Apache-2.0",
}
_JSON_RULE = (
    '\nSerialize the requested output as the string value of the sole field "result" '
    "in one JSON object matching the supplied schema. No markdown or text outside "
    "the object. Preserve the requested research operation within that string. "
    'If any supplied state is ERROR_INPUT, return {"result":null}; do not solve '
    "around failed upstream state. Return null also if the operation cannot be "
    "completed from supplied information. The string must be nonempty and no "
    "larger than 8192 UTF-8 bytes."
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def prompts() -> dict[str, str]:
    # AST extraction only: never execute the old research planner's gold stubs.
    return {key: value + _JSON_RULE for key, value in prompt_constants().items()}


def schema_for(stage: str) -> dict:
    if stage not in STAGES:
        raise PilotError("unknown public research stage")
    return {
        "type": "json_schema",
        "json_schema": {
            "name": f"typed_public_{stage}_v1",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "result": {"anyOf": [{"type": "string"}, {"type": "null"}]}
                },
                "required": ["result"],
                "additionalProperties": False,
            },
        },
    }


def contract_hash(prompt_values: dict[str, str] | None = None) -> str:
    values = prompts() if prompt_values is None else prompt_values
    if set(values) != {"FILTER_SYSTEM", "SYNTH_SYSTEM", "ANSWER_SYSTEM"} or not all(
        isinstance(v, str) and v for v in values.values()
    ):
        raise PilotError("incomplete public prompt contract")
    paths = (
        Path(__file__),
        ROOT / "bench/openrouter_rules_protocol.py",
        ROOT / "bench/agents/research_planner.py",
        ROOT / "python/agentc/_attention.py",
    )
    return digest(
        {
            "workload": WORKLOAD,
            "format_variant": FORMAT_VARIANT,
            "source_sha256": {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in paths
            },
            "prompts": values,
            "schemas": {stage: schema_for(stage) for stage in STAGES},
            "max_result_bytes": MAX_RESULT_BYTES,
            "max_raw_bytes": MAX_RAW_BYTES,
        }
    )


def _text(value: Any, maximum: int | None = None) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        encoded = value.encode("utf-8")
        return maximum is None or len(encoded) <= maximum
    except UnicodeEncodeError:
        return False


def public_task(task: dict) -> dict:
    """Validate only public fields; ignore rather than inspect evaluator metadata."""
    try:
        task_id, question, paragraphs = (
            task["task_id"],
            task["prompt"],
            task["meta"]["paragraphs"],
        )
        if (
            not isinstance(task_id, str)
            or re.fullmatch(r"hotpot_[0-9a-f]{24}", task_id) is None
            or not _text(question)
        ):
            raise PilotError("invalid public task identity or question")
        if not isinstance(paragraphs, list) or len(paragraphs) != 10:
            raise PilotError("public task must retain all ten paragraphs")
        result = []
        for paragraph in paragraphs:
            title, sentences = paragraph["title"], paragraph["sentences"]
            if (
                not _text(title)
                or not isinstance(sentences, list)
                or not sentences
                or not all(isinstance(s, str) and _text(s) for s in sentences)
            ):
                raise PilotError("invalid public paragraph")
            result.append({"title": title, "sentences": deepcopy(sentences)})
        return {"task_id": task_id, "prompt": question, "meta": {"paragraphs": result}}
    except (KeyError, TypeError) as exc:
        raise PilotError("malformed public task") from exc


def _check_history(stage: str, history: dict[str, str]) -> None:
    if (
        stage not in STAGES
        or not isinstance(history, dict)
        or set(history) != set(STAGES[: STAGES.index(stage)])
        or not all(isinstance(v, str) for v in history.values())
    ):
        raise PilotError("public workflow needs exactly its own preceding stage states")


def parse_stage(stage: str, text: Any, *, finish_reason: str = "stop") -> str | None:
    if stage not in STAGES:
        raise PilotError("unknown public research stage")
    if finish_reason != "stop" or not _text(text, MAX_RAW_BYTES):
        return None

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("nonfinite JSON")

    try:
        value = json.loads(text, object_pairs_hook=unique, parse_constant=nonfinite)
        if (
            not isinstance(value, dict)
            or set(value) != {"result"}
            or not _text(value["result"], MAX_RESULT_BYTES)
        ):
            return None
        return value["result"]  # no strip/normalization/extraction/repair
    except (ValueError, RecursionError):
        return None


parse = parse_stage


def application_text(
    raw_text: Any,
    finish_reason: str = "stop",
    *,
    stage: str | None = None,
    upstream_valid: bool = True,
) -> str:
    value = parse_stage(
        "answer" if stage is None else stage, raw_text, finish_reason=finish_reason
    )
    return (
        _canonical({"result": value})
        if upstream_valid is True and value is not None
        else ERROR
    )


def history_valid(task: dict, stage: str, history: dict[str, str]) -> bool:
    _check_history(stage, history)
    public_task(task)
    return all(parse_stage(prior, text) is not None for prior, text in history.items())


def call(
    task: dict,
    stage: str,
    history: dict[str, str],
    attention: Any,
    *,
    model: str,
    run_identity: str,
    prompts: dict[str, str] | None = None,
) -> dict:
    _check_history(stage, history)
    task = public_task(task)
    values = globals()["prompts"]() if prompts is None else prompts
    namespace = contract_hash(values)
    valid = history_valid(task, stage, history)
    key = {
        "filter": "FILTER_SYSTEM",
        "synthesize": "SYNTH_SYSTEM",
        "answer": "ANSWER_SYSTEM",
    }[stage]
    messages = [{"role": "system", "content": values[key]}]
    deps = [{"kind": "literal"}]
    reads = []
    if stage == "filter":
        for paragraph in task["meta"]["paragraphs"]:
            messages.append(
                {
                    "role": "user",
                    "content": paragraph["title"]
                    + "\n"
                    + " ".join(paragraph["sentences"]),
                }
            )
            deps.append({"kind": "literal"})
    else:
        messages.append(
            {
                "role": "user",
                "content": parse_stage("filter", history["filter"])
                if valid
                else ERROR_INPUT,
            }
        )
        deps.append({"kind": "state", "key": "filter_result"})
        if stage == "synthesize":
            reads = ["filter_result"]
        else:
            messages.append(
                {
                    "role": "user",
                    "content": parse_stage("synthesize", history["synthesize"])
                    if valid
                    else ERROR_INPUT,
                }
            )
            deps.append({"kind": "state", "key": "synthesis"})
            reads = ["synthesis"]
    span = digest([run_identity, task["task_id"], stage])[:16]
    messages.append({"role": "user", "content": "Question: " + task["prompt"]})
    deps.append({"kind": "user_input", "span_id": span})
    scores, follow = attention.compute_attention_scores(messages, None)
    response_format = schema_for(stage)
    return {
        "call_site_id": f"{WORKLOAD}/{namespace}/{stage}",
        "trace_id": digest([run_identity, task["task_id"]])[:32],
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
                "window_state_reads": reads,
                "response_format": response_format,
                "typed_public_contract": {"sha256": namespace, "upstream_valid": valid},
                "typed_client_size_diagnostic": {
                    "json_utf8_bytes": len(
                        _canonical(
                            {"messages": messages, "response_format": response_format}
                        ).encode()
                    ),
                    "scope": "client representation only; excludes opaque provider schema instructions; not a provider-context proof",
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


def _normalize_answer(text: str) -> str:
    """Offline port of HotpotQA's Apache-2.0 evaluator; see EVALUATOR pin."""
    lowered = text.lower()
    unpunctuated = "".join(ch for ch in lowered if ch not in set(string.punctuation))
    return " ".join(re.sub(r"\b(a|an|the)\b", " ", unpunctuated).split())


def score_answer(
    text: str, expected: str, *, history: dict[str, str] | None = None
) -> dict[str, float]:
    """Offline official answer EM/F1 only, never controller or application feedback."""
    if not isinstance(expected, str):
        raise PilotError("evaluator requires a separately supplied gold string")
    answer = parse_stage("answer", text)
    if history is not None:
        if set(history) not in ({"filter", "synthesize"}, set(STAGES)):
            raise PilotError("scoring requires this arm's complete preceding history")
        if any(
            parse_stage(stage, value) is None for stage, value in history.items()
        ) or (
            "answer" in history and parse_stage("answer", history["answer"]) != answer
        ):
            answer = None
    if answer is None:
        return {"em": 0.0, "f1": 0.0}
    prediction, gold = _normalize_answer(answer), _normalize_answer(expected)
    em = float(prediction == gold)
    if prediction != gold and (
        prediction in ("yes", "no", "noanswer") or gold in ("yes", "no", "noanswer")
    ):
        return {"em": em, "f1": 0.0}
    left, right = prediction.split(), gold.split()
    overlap = sum((Counter(left) & Counter(right)).values())
    if not overlap:
        return {"em": em, "f1": 0.0}
    precision, recall = overlap / len(left), overlap / len(right)
    return {"em": em, "f1": 2 * precision * recall / (precision + recall)}
