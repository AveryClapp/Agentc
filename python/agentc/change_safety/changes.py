"""Change catalog for the change-safety study (protocol Section 4).

A change is a pure, deterministic function from one LiteLLM ``completion``
request (the kwargs the tau2 agent passes) to a changed request. Every change
deep-copies its input and never mutates it. An ineligible request yields
``AppliedChange(applied=False, ineligible_reason=...)`` carrying the unchanged
copy; it is never replaced by another change.

Provider request shapes (verified 2026-11-24):

- C3, Anthropic tool-result clearing
  (https://platform.claude.com/docs/en/build-with-claude/context-editing):
  ``context_management={"edits": [{"type": "clear_tool_uses_20250919",
  "trigger": {"type": "input_tokens", "value": T},
  "keep": {"type": "tool_uses", "value": K}}]}`` with beta header
  ``anthropic-beta: context-management-2025-06-27``.
- C4, Anthropic threshold compaction
  (https://platform.claude.com/docs/en/build-with-claude/compaction-threshold):
  ``context_management={"edits": [{"type": "compact_20260112",
  "trigger": {"type": "input_tokens", "value": T}}]}`` (T >= 50,000) with beta
  ``compact-2026-01-12``. OpenAI compaction
  (https://developers.openai.com/api/docs/guides/compaction) exists only on the
  Responses API as ``context_management=[{"type": "compaction",
  "compact_threshold": T}]``.

LiteLLM 1.82.6 lists ``context_management`` as a supported Anthropic param,
passes the Anthropic dict form through unchanged into the request body, and
adds the matching ``anthropic-beta`` value itself
(``AnthropicConfig._ensure_context_management_beta_header``). So C3 and C4 set
only the top-level ``context_management`` kwarg; no ``extra_body`` or
``extra_headers`` is needed. The response's ``context_management`` object is
surfaced at ``choices[0].message.provider_specific_fields`` and
``_hidden_params["provider_specific_fields"]`` (see
:func:`extract_applied_edits`).

C5 and C6 run the existing Rust rules through
``_native.optimize_propose_rule``: the rule's own proposal under the live
call-site profile, without admission or the hot threshold.

Context-removal evidence reports characters removed and an estimate of tokens
removed. The repository has no tokenizer-based estimator
(``_input_tokens_upper_bound`` is a byte upper bound), so tokens are estimated
as ``ceil(chars / 4)`` and the evidence says so.
"""

from __future__ import annotations

import copy
import json
import math
import re
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from agentc.change_safety.records import AppliedChange, ChangeSpec, canonical_json

# ---------------------------------------------------------------------------
# Provider constants (see module docstring for sources)
# ---------------------------------------------------------------------------

CLEAR_TOOL_USES_EDIT = "clear_tool_uses_20250919"
CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"
COMPACT_EDIT = "compact_20260112"
COMPACT_BETA = "compact-2026-01-12"
#: Minimum ``trigger.value`` Anthropic accepts for ``compact_20260112``.
COMPACTION_MIN_TRIGGER_TOKENS = 50_000
#: Models the compaction-threshold page listed as supported on 2026-11-24.
#: Notably excludes ``claude-sonnet-4-5-20250929`` and ``claude-opus-4-5-*``.
COMPACTION_SUPPORTED_MODELS = frozenset(
    {
        "claude-fable-5-2",
        "claude-mythos-5-2",
        "claude-fable-5-1",
        "claude-mythos-5-1",
        "claude-fable-5",
        "claude-mythos-5",
        "claude-mythos-preview",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-haiku-5-5",
        "claude-haiku-4-5-20251001",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    }
)

TOKEN_ESTIMATE_METHOD = "ceil(chars/4); no tokenizer-based estimator exists in the repo"
DEFAULT_TRUNCATE_CHARS = 200

# Agentc rule names, exactly as ``RewriteRule::name()`` returns them.
CONTEXT_COMPRESS_RULE = "ContextCompress"
OUTPUT_BUDGET_RULE = "OutputBudget"


def estimate_tokens(chars: int) -> int:
    """Rough token estimate for ``chars`` characters (see ``TOKEN_ESTIMATE_METHOD``)."""
    return math.ceil(chars / 4) if chars > 0 else 0


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

_FAMILIES: Mapping[str, str] = {
    "C0": "aa_control",
    "C1": "model_swap",
    "C2": "model_migration",
    "C3": "context_removal",
    "C4": "context_removal",
    "C5": "context_removal",
    "C6": "output_cap",
    "X1": "seeded_harm",
    "X2": "seeded_harm",
    "X3": "seeded_harm",
    "B1": "benign",
    "B2": "benign",
}

CHANGE_IDS: tuple[str, ...] = tuple(_FAMILIES)


def _positive_int(params: Mapping[str, Any], name: str, *, minimum: int = 1) -> int:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {value!r}")
    return value


def _validate_params(change_id: str, params: Mapping[str, Any]) -> None:
    if change_id in {"C1", "C2", "X1"}:
        target = params.get("target_model")
        if not isinstance(target, str) or not target:
            raise ValueError(f"{change_id} requires a non-empty target_model")
    elif change_id == "C3":
        _positive_int(params, "trigger_input_tokens")
        _positive_int(params, "keep_tool_uses", minimum=0)
        if "clear_at_least_input_tokens" in params:
            _positive_int(params, "clear_at_least_input_tokens")
        exclude = params.get("exclude_tools", [])
        if not isinstance(exclude, list) or not all(isinstance(t, str) for t in exclude):
            raise ValueError("exclude_tools must be a list of tool names")
        if not isinstance(params.get("clear_tool_inputs", False), bool):
            raise ValueError("clear_tool_inputs must be a bool")
    elif change_id == "C4":
        _positive_int(params, "trigger_input_tokens", minimum=COMPACTION_MIN_TRIGGER_TOKENS)
        if not isinstance(params.get("pause_after_compaction", False), bool):
            raise ValueError("pause_after_compaction must be a bool")
        instructions = params.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise ValueError("instructions must be a string")
    elif change_id == "X2":
        _positive_int(params, "max_chars", minimum=0)
    elif change_id in {"C5", "C6"}:
        if params.get("rule") != _NATIVE_RULES[change_id]:
            raise ValueError(f"{change_id} must name rule {_NATIVE_RULES[change_id]!r}")


_NATIVE_RULES: Mapping[str, str] = {"C5": CONTEXT_COMPRESS_RULE, "C6": OUTPUT_BUDGET_RULE}

_DEFAULT_PARAMS: Mapping[str, Mapping[str, Any]] = {
    "C5": {"rule": CONTEXT_COMPRESS_RULE, "gate": "optimizer-configured default"},
    "C6": {"rule": OUTPUT_BUDGET_RULE, "quantile": "p99 x 1.2 (rule default)"},
    "X2": {"max_chars": DEFAULT_TRUNCATE_CHARS},
    "B1": {"key_order": "reverse_sorted"},
    "B2": {"whitespace": "collapse_horizontal_runs_and_blank_lines_strip"},
}


def make_change(change_id: str, **params: Any) -> ChangeSpec:
    """Build the :class:`ChangeSpec` for ``change_id`` with validated parameters.

    Required parameters: ``target_model`` for C1, C2 and X1;
    ``trigger_input_tokens`` and ``keep_tool_uses`` for C3 (optional
    ``clear_at_least_input_tokens``, ``exclude_tools``, ``clear_tool_inputs``);
    ``trigger_input_tokens`` (>= 50,000) for C4 (optional
    ``pause_after_compaction``, ``instructions``). X2 takes ``max_chars``
    (default 200).
    """
    family = _FAMILIES.get(change_id)
    if family is None:
        raise ValueError(f"unknown change id {change_id!r}")
    merged = {**_DEFAULT_PARAMS.get(change_id, {}), **params}
    _validate_params(change_id, merged)
    return ChangeSpec(change_id=change_id, family=family, params=merged)


def catalog(
    *,
    cheaper_tier_model: str,
    newer_version_model: str,
    smallest_model: str,
    clear_trigger_input_tokens: int,
    clear_keep_tool_uses: int,
    compaction_trigger_input_tokens: int,
    truncate_chars: int = DEFAULT_TRUNCATE_CHARS,
) -> tuple[ChangeSpec, ...]:
    """The full C0-C6, X1-X3, B1-B2 catalog for one evaluated model's pins."""
    return (
        make_change("C0"),
        make_change("C1", target_model=cheaper_tier_model),
        make_change("C2", target_model=newer_version_model),
        make_change(
            "C3",
            trigger_input_tokens=clear_trigger_input_tokens,
            keep_tool_uses=clear_keep_tool_uses,
        ),
        make_change("C4", trigger_input_tokens=compaction_trigger_input_tokens),
        make_change("C5"),
        make_change("C6"),
        make_change("X1", target_model=smallest_model),
        make_change("X2", max_chars=truncate_chars),
        make_change("X3"),
        make_change("B1"),
        make_change("B2"),
    )


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Context:
    call_site_id: str | None
    trace_id_hex: str | None


_Transform = Callable[[Mapping[str, Any], dict[str, Any], _Context], AppliedChange]


def apply_change(
    spec: ChangeSpec,
    request: Mapping[str, Any],
    *,
    call_site_id: str | None = None,
    trace_id_hex: str | None = None,
) -> AppliedChange:
    """Apply ``spec`` to a LiteLLM ``completion`` request without mutating it.

    ``call_site_id`` (and optionally ``trace_id_hex``) are needed only by C5
    and C6, which read the live Agentc call-site profile.
    """
    if _FAMILIES.get(spec.change_id) != spec.family:
        raise ValueError(f"change {spec.change_id!r} is not in family {spec.family!r}")
    _validate_params(spec.change_id, spec.params)
    working = copy.deepcopy(dict(request))
    return _TRANSFORMS[spec.change_id](spec.params, working, _Context(call_site_id, trace_id_hex))


def _ineligible(request: dict[str, Any], reason: str, **evidence: Any) -> AppliedChange:
    return AppliedChange(
        request=request, applied=False, ineligible_reason=reason, evidence=evidence
    )


def provider_of(model: str) -> str:
    """LiteLLM provider prefix of ``model``, inferring bare Claude and GPT IDs."""
    if "/" in model:
        return model.split("/", 1)[0]
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    return "unknown"


def _bare_model(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


def _messages(request: Mapping[str, Any]) -> list[Any]:
    value = request.get("messages")
    return value if isinstance(value, list) else []


def _content_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(block["text"])
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return 0


def _removal_evidence(chars_removed: int, **extra: Any) -> dict[str, Any]:
    return {
        "chars_removed": chars_removed,
        "tokens_removed_estimate": estimate_tokens(chars_removed),
        "token_estimate_method": TOKEN_ESTIMATE_METHOD,
        **extra,
    }


# C0 -------------------------------------------------------------------------


def _identity(_: Mapping[str, Any], request: dict[str, Any], __: _Context) -> AppliedChange:
    return AppliedChange(
        request=request,
        applied=True,
        evidence={
            "aa_control": True,
            "note": "A/A control: the unchanged request, sent again as an independent resample",
        },
    )


# C1, C2, X1 -----------------------------------------------------------------


def _model_swap(params: Mapping[str, Any], request: dict[str, Any], _: _Context) -> AppliedChange:
    source = str(request.get("model", ""))
    target = str(params["target_model"])
    evidence = {"source_model": source, "target_model": target}
    if source == target:
        return _ineligible(request, "target_model equals the request's model", **evidence)
    if provider_of(source) != provider_of(target):
        return _ineligible(
            request,
            f"target provider {provider_of(target)!r} differs from source {provider_of(source)!r}",
            **evidence,
        )
    request["model"] = target
    return AppliedChange(request=request, applied=True, evidence=evidence)


# C3 -------------------------------------------------------------------------


def _clear_tool_uses_edit(params: Mapping[str, Any]) -> dict[str, Any]:
    edit: dict[str, Any] = {
        "type": CLEAR_TOOL_USES_EDIT,
        "trigger": {"type": "input_tokens", "value": int(params["trigger_input_tokens"])},
        "keep": {"type": "tool_uses", "value": int(params["keep_tool_uses"])},
    }
    if "clear_at_least_input_tokens" in params:
        edit["clear_at_least"] = {
            "type": "input_tokens",
            "value": int(params["clear_at_least_input_tokens"]),
        }
    if params.get("exclude_tools"):
        edit["exclude_tools"] = list(params["exclude_tools"])
    if params.get("clear_tool_inputs"):
        edit["clear_tool_inputs"] = True
    return edit


def _request_chars(request: Mapping[str, Any]) -> int:
    return len(canonical_json(_messages(request))) + len(canonical_json(request.get("tools")))


def _projected_tool_clearing(
    request: Mapping[str, Any], keep: int, exclude: Iterable[str]
) -> dict[str, Any]:
    """Client-side projection of what clearing would remove if it fires.

    Anthropic decides server-side; the authoritative amount is
    ``context_management.applied_edits`` in the response.
    """
    excluded = set(exclude)
    names: dict[str, str] = {}
    for message in _messages(request):
        if isinstance(message, dict) and message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict):
                    function = call.get("function") or {}
                    names[str(call.get("id"))] = str(function.get("name", call.get("name", "")))
    results = [
        m
        for m in _messages(request)
        if isinstance(m, dict)
        and m.get("role") == "tool"
        and names.get(str(m.get("tool_call_id"))) not in excluded
    ]
    cleared = results[: max(len(results) - keep, 0)]
    chars = sum(_content_chars(m.get("content")) for m in cleared)
    return {
        "projected_cleared_tool_results": len(cleared),
        "projected_cleared_chars": chars,
        "projected_cleared_tokens_estimate": estimate_tokens(chars),
    }


def _context_management_conflict(request: Mapping[str, Any]) -> str | None:
    if "context_management" in request:
        return "request already carries context_management"
    return None


def _clear_tool_results(
    params: Mapping[str, Any], request: dict[str, Any], _: _Context
) -> AppliedChange:
    model = str(request.get("model", ""))
    if provider_of(model) != "anthropic":
        return _ineligible(
            request,
            f"tool-result clearing is an Anthropic API feature; model {model!r} is not Anthropic",
            model=model,
        )
    conflict = _context_management_conflict(request)
    if conflict is not None:
        return _ineligible(request, conflict, model=model)
    edit = _clear_tool_uses_edit(params)
    request["context_management"] = {"edits": [edit]}
    request_chars = _request_chars(request)
    trigger = int(params["trigger_input_tokens"])
    return AppliedChange(
        request=request,
        applied=True,
        evidence={
            "model": model,
            "edit": edit,
            "anthropic_beta": CONTEXT_MANAGEMENT_BETA,
            "transport": "litellm kwarg context_management; LiteLLM adds the beta header",
            "request_chars": request_chars,
            "request_tokens_estimate": estimate_tokens(request_chars),
            "trigger_expected_to_fire": estimate_tokens(request_chars) >= trigger,
            "removal_source": "provider context_management.applied_edits (authoritative)",
            "token_estimate_method": TOKEN_ESTIMATE_METHOD,
            **_projected_tool_clearing(
                request, int(params["keep_tool_uses"]), params.get("exclude_tools", [])
            ),
        },
    )


# C4 -------------------------------------------------------------------------


def _is_responses_request(request: Mapping[str, Any]) -> bool:
    return "input" in request and "messages" not in request


def _compaction(params: Mapping[str, Any], request: dict[str, Any], _: _Context) -> AppliedChange:
    model = str(request.get("model", ""))
    provider = provider_of(model)
    trigger = int(params["trigger_input_tokens"])
    conflict = _context_management_conflict(request)
    if provider == "openai":
        if not _is_responses_request(request):
            return _ineligible(
                request,
                "OpenAI compaction exists only on the Responses API; "
                "this request uses chat completions",
                model=model,
            )
        if conflict is not None:
            return _ineligible(request, conflict, model=model)
        entry = {"type": "compaction", "compact_threshold": trigger}
        request["context_management"] = [entry]
        return AppliedChange(
            request=request,
            applied=True,
            evidence={"model": model, "edit": entry, "api": "openai.responses"},
        )
    if provider != "anthropic":
        return _ineligible(request, f"no provider compaction for model {model!r}", model=model)
    supported = params.get("supported_models")
    supported_set = frozenset(supported) if isinstance(supported, list) else COMPACTION_SUPPORTED_MODELS
    if _bare_model(model) not in supported_set:
        return _ineligible(
            request, f"model {model!r} does not support {COMPACT_EDIT}", model=model
        )
    if conflict is not None:
        return _ineligible(request, conflict, model=model)
    edit: dict[str, Any] = {
        "type": COMPACT_EDIT,
        "trigger": {"type": "input_tokens", "value": trigger},
    }
    if params.get("pause_after_compaction"):
        edit["pause_after_compaction"] = True
    if params.get("instructions") is not None:
        edit["instructions"] = params["instructions"]
    request["context_management"] = {"edits": [edit]}
    request_chars = _request_chars(request)
    return AppliedChange(
        request=request,
        applied=True,
        evidence={
            "model": model,
            "edit": edit,
            "anthropic_beta": COMPACT_BETA,
            "transport": "litellm kwarg context_management; LiteLLM adds the beta header",
            "request_chars": request_chars,
            "request_tokens_estimate": estimate_tokens(request_chars),
            "trigger_expected_to_fire": estimate_tokens(request_chars) >= trigger,
            "removal_source": "provider compaction block and usage.iterations",
            "token_estimate_method": TOKEN_ESTIMATE_METHOD,
        },
    )


# C5, C6 (Agentc rules through the native optimizer) --------------------------


def _native_rule(params: Mapping[str, Any], request: dict[str, Any], ctx: _Context) -> AppliedChange:
    rule = str(params["rule"])
    if not ctx.call_site_id:
        return _ineligible(request, f"{rule} needs the call_site_id of the live profile", rule=rule)
    try:
        from agentc import _native
        from agentc._patches._optimizer_glue import (
            LITELLM_COMPLETION_PROTOCOL,
            build_call_dict_openai,
        )

        # Projects the request exactly as the LiteLLM interceptor does before
        # ``plan_call`` (including the attention proxy ContextCompress reads).
        call = build_call_dict_openai(
            request,
            call_site_id=ctx.call_site_id,
            trace_id_hex=ctx.trace_id_hex or secrets.token_hex(16),
            span_id_hex=secrets.token_hex(8),
            provider_protocol=LITELLM_COMPLETION_PROTOCOL,
        )
        result = json.loads(_native.optimize_propose_rule(json.dumps(call), rule))
    except Exception as exc:  # projection or native failure is an ineligible fork
        return _ineligible(request, f"{rule} proposal unavailable: {exc!r}", rule=rule)
    if not isinstance(result, dict) or result.get("status") != "proposed":
        reason = result.get("reason") if isinstance(result, dict) else None
        return _ineligible(request, f"{rule} not applicable: {reason}", rule=rule)
    return _apply_native_mutation(request, call, result, rule)


def _apply_native_mutation(
    request: dict[str, Any],
    call: Mapping[str, Any],
    result: Mapping[str, Any],
    rule: str,
) -> AppliedChange:
    """Map a proposed Call back onto the native request.

    Messages are removed by original index (never by text), so tool-call
    metadata on retained messages is untouched. Only ``messages`` and the
    output-token cap are supported mutations; anything else is refused.
    """
    mutated = result.get("call")
    if not isinstance(mutated, dict):
        return _ineligible(request, f"{rule} returned no call", rule=rule)
    evidence: dict[str, Any] = {"rule": rule, "profile": result.get("profile")}
    original_params = call.get("parameters") or {}
    mutated_params = mutated.get("parameters") or {}
    unsupported = [
        name
        for name in ("temperature", "top_p", "stop")
        if original_params.get(name) != mutated_params.get(name)
    ]
    if mutated.get("model") != call.get("model"):
        unsupported.append("model")
    if unsupported:
        return _ineligible(request, f"{rule} changed unsupported fields {unsupported}", rule=rule)

    original_messages = list(call.get("messages") or [])
    new_messages = list(mutated.get("messages") or [])
    changed = False
    if new_messages != original_messages:
        indices = result.get("retained_message_indices")
        if not isinstance(indices, list) or len(indices) != len(new_messages):
            return _ineligible(request, f"{rule} removal cannot be mapped by index", rule=rule)
        if any(new_messages[pos] != original_messages[i] for pos, i in enumerate(indices)):
            return _ineligible(
                request, f"{rule} rewrote message content; only removal is representable",
                rule=rule,
            )
        native = _messages(request)
        kept = set(indices)
        removed = [i for i in range(len(native)) if i not in kept]
        chars = sum(_content_chars(native[i].get("content")) for i in removed)
        evidence.update(
            _removal_evidence(
                chars,
                retained_message_indices=indices,
                removed_message_indices=removed,
                removed_roles=[native[i].get("role") for i in removed],
            )
        )
        request["messages"] = [native[i] for i in indices]
        changed = True

    cap = mutated_params.get("max_output_tokens")
    if cap is not None and cap != original_params.get("max_output_tokens"):
        from agentc._patches._optimizer_glue import _openai_output_cap_field

        field = _openai_output_cap_field(request, str(request.get("model", "")))
        other = "max_tokens" if field == "max_completion_tokens" else "max_completion_tokens"
        evidence.update(
            {
                "cap_field": field,
                "previous_cap": original_params.get("max_output_tokens"),
                "output_token_cap": int(cap),
            }
        )
        request.pop(other, None)
        request[field] = int(cap)
        changed = True
    if not changed:
        return _ineligible(request, f"{rule} proposal left the request unchanged", **evidence)
    return AppliedChange(request=request, applied=True, evidence=evidence)


# X2 -------------------------------------------------------------------------


def _truncate_content(content: Any, max_chars: int) -> tuple[Any, int]:
    if isinstance(content, str):
        return content[:max_chars], max(len(content) - max_chars, 0)
    if isinstance(content, list):
        budget, removed, blocks = max_chars, 0, []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                text = block["text"]
                block = {**block, "text": text[:budget]}
                removed += max(len(text) - budget, 0)
                budget = max(budget - len(text), 0)
            blocks.append(block)
        return blocks, removed
    return content, 0


def _truncate_tool_results(
    params: Mapping[str, Any], request: dict[str, Any], _: _Context
) -> AppliedChange:
    max_chars = int(params["max_chars"])
    truncated = removed = 0
    for message in _messages(request):
        if isinstance(message, dict) and message.get("role") == "tool":
            message["content"], cut = _truncate_content(message.get("content"), max_chars)
            if cut:
                truncated += 1
                removed += cut
    evidence = _removal_evidence(removed, max_chars=max_chars, tool_results_truncated=truncated)
    if not truncated:
        return _ineligible(request, f"no tool result longer than {max_chars} chars", **evidence)
    return AppliedChange(request=request, applied=True, evidence=evidence)


# X3 -------------------------------------------------------------------------


def _remove_system_prompt(
    _: Mapping[str, Any], request: dict[str, Any], __: _Context
) -> AppliedChange:
    messages = _messages(request)
    system = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
    if not system:
        return _ineligible(request, "request has no system message")
    request["messages"] = [m for m in messages if not (isinstance(m, dict) and m.get("role") == "system")]
    chars = sum(_content_chars(m.get("content")) for m in system)
    return AppliedChange(
        request=request,
        applied=True,
        evidence=_removal_evidence(chars, system_messages_removed=len(system)),
    )


# B1 -------------------------------------------------------------------------

_JSON_FORMATS: tuple[Mapping[str, Any], ...] = tuple(
    {**layout, "ensure_ascii": ascii_only}
    for layout in ({}, {"separators": (",", ":")}, {"indent": 2}, {"indent": 4})
    for ascii_only in (True, False)
)


def _reverse_sorted_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _reverse_sorted_keys(value[k]) for k in sorted(value, reverse=True)}
    if isinstance(value, list):
        return [_reverse_sorted_keys(item) for item in value]
    return value


def _parse_json_container(text: str) -> Any | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, (dict, list)) else None


def _reorder_json_text(text: str) -> tuple[str, bool] | None:
    """Return ``(reordered, format_preserved)`` or None for non-JSON text."""
    value = _parse_json_container(text)
    if value is None:
        return None
    layout = next((f for f in _JSON_FORMATS if json.dumps(value, **f) == text), None)
    return json.dumps(_reverse_sorted_keys(value), **(layout or {})), layout is not None


def _reorder_tool_json(_: Mapping[str, Any], request: dict[str, Any], __: _Context) -> AppliedChange:
    reordered = untouched = 0
    format_preserved = True
    for message in _messages(request):
        if not (isinstance(message, dict) and message.get("role") == "tool"):
            continue
        content = message.get("content")
        result = _reorder_json_text(content) if isinstance(content, str) else None
        if result is None or result[0] == content:
            untouched += 1
            continue
        message["content"], preserved = result
        format_preserved = format_preserved and preserved
        reordered += 1
    evidence = {
        "key_order": "reverse_sorted",
        "tool_results_reordered": reordered,
        "tool_results_untouched": untouched,
        "format_preserved": format_preserved,
    }
    if not reordered:
        return _ineligible(request, "no JSON tool result whose key order would change", **evidence)
    return AppliedChange(request=request, applied=True, evidence=evidence)


# B2 -------------------------------------------------------------------------

_HORIZONTAL_RUN = re.compile(r"[ \t\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_whitespace(text: str) -> str:
    """Collapse horizontal whitespace runs, strip lines, keep at most one blank line."""
    lines = [_HORIZONTAL_RUN.sub(" ", line).strip() for line in text.replace("\r\n", "\n").split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _normalize_text_value(text: str, *, role: Any) -> str:
    normalized = normalize_whitespace(text)
    if role == "tool":
        parsed = _parse_json_container(text)
        # JSON string values may contain meaningful whitespace: only accept a
        # normalization that leaves the parsed value identical.
        if parsed is not None and _parse_json_container(normalized) != parsed:
            return text
    return normalized


def _normalize_messages(_: Mapping[str, Any], request: dict[str, Any], __: _Context) -> AppliedChange:
    changed_messages = 0
    chars_delta = 0
    for message in _messages(request):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        role = message.get("role")
        if isinstance(content, str):
            new: Any = _normalize_text_value(content, role=role)
        elif isinstance(content, list):
            new = [
                {**b, "text": _normalize_text_value(b["text"], role=role)}
                if isinstance(b, dict) and isinstance(b.get("text"), str)
                else b
                for b in content
            ]
        else:
            continue
        if new != content:
            chars_delta += _content_chars(content) - _content_chars(new)
            message["content"] = new
            changed_messages += 1
    evidence = {"messages_normalized": changed_messages, "whitespace_chars_removed": chars_delta}
    if not changed_messages:
        return _ineligible(request, "message text already normalized", **evidence)
    return AppliedChange(request=request, applied=True, evidence=evidence)


_TRANSFORMS: Mapping[str, _Transform] = {
    "C0": _identity,
    "C1": _model_swap,
    "C2": _model_swap,
    "C3": _clear_tool_results,
    "C4": _compaction,
    "C5": _native_rule,
    "C6": _native_rule,
    "X1": _model_swap,
    "X2": _truncate_tool_results,
    "X3": _remove_system_prompt,
    "B1": _reorder_tool_json,
    "B2": _normalize_messages,
}


# ---------------------------------------------------------------------------
# Provider response readers
# ---------------------------------------------------------------------------


def _provider_specific_fields(response: Any) -> list[Mapping[str, Any]]:
    """Candidate ``provider_specific_fields`` mappings of a LiteLLM response."""
    found: list[Mapping[str, Any]] = []
    try:
        message = response.choices[0].message
        fields = getattr(message, "provider_specific_fields", None)
        if isinstance(fields, Mapping):
            found.append(fields)
    except (AttributeError, IndexError, TypeError):
        pass
    hidden = getattr(response, "_hidden_params", None)
    if isinstance(hidden, Mapping) and isinstance(hidden.get("provider_specific_fields"), Mapping):
        found.append(hidden["provider_specific_fields"])
    if isinstance(response, Mapping):
        found.append(response)  # a raw Anthropic Messages body
    return found


def extract_applied_edits(response: Any) -> tuple[Mapping[str, Any], ...] | None:
    """Provider-reported ``context_management.applied_edits``, or None if absent.

    LiteLLM 1.82.6 copies the Anthropic response's ``context_management``
    object into ``choices[0].message.provider_specific_fields`` and
    ``_hidden_params["provider_specific_fields"]``. An empty tuple means the
    provider reported the object with no applied edit.
    """
    for fields in _provider_specific_fields(response):
        management = fields.get("context_management")
        if isinstance(management, Mapping):
            edits = management.get("applied_edits")
            if isinstance(edits, list):
                return tuple(dict(edit) for edit in edits if isinstance(edit, Mapping))
    return None


def extract_compaction_blocks(response: Any) -> tuple[Mapping[str, Any], ...] | None:
    """Anthropic ``compaction`` content blocks (C4), or None when there are none.

    LiteLLM surfaces them as ``provider_specific_fields["compaction_blocks"]``.
    """
    for fields in _provider_specific_fields(response):
        blocks = fields.get("compaction_blocks")
        if isinstance(blocks, list):
            return tuple(dict(block) for block in blocks if isinstance(block, Mapping))
    return None


# ---------------------------------------------------------------------------
# Pass-through conformance
# ---------------------------------------------------------------------------


def _changed_keys(before: Mapping[str, Any], after: Mapping[str, Any]) -> set[str]:
    return {
        key
        for key in set(before) | set(after)
        if key not in before
        or key not in after
        or canonical_json(before[key]) != canonical_json(after[key])
    }


def _without(message: Any, *keys: str) -> Any:
    if not isinstance(message, dict):
        return message
    return {k: v for k, v in message.items() if k not in keys}


def _same_length_message_check(
    before: list[Any],
    after: list[Any],
    content_ok: Callable[[Any, Any, Any], bool],
) -> list[str]:
    if len(before) != len(after):
        return ["message count changed"]
    problems = []
    for index, (old, new) in enumerate(zip(before, after)):
        if canonical_json(_without(old, "content")) != canonical_json(_without(new, "content")):
            problems.append(f"message {index}: fields other than content changed")
        elif isinstance(old, dict) and isinstance(new, dict):
            if old.get("content") != new.get("content") and not content_ok(
                old.get("role"), old.get("content"), new.get("content")
            ):
                problems.append(f"message {index}: content change not allowed")
    return problems


def _is_subsequence(after: list[Any], before: list[Any]) -> bool:
    remaining = iter(canonical_json(m) for m in before)
    return all(any(canonical_json(m) == old for old in remaining) for m in after)


def _tool_pairing_problems(messages: list[Any]) -> list[str]:
    problems: list[str] = []
    issued: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                issued.add(str(call.get("id")))
        if message.get("role") == "tool":
            call_id = str(message.get("tool_call_id"))
            if call_id not in issued:
                problems.append(f"orphan tool result {call_id}")
    return problems


def _text_equivalent(role: Any, old: Any, new: Any) -> bool:
    def text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(
                b["text"] for b in value if isinstance(b, dict) and isinstance(b.get("text"), str)
            )
        return ""

    if role == "tool" and isinstance(old, str) and isinstance(new, str):
        parsed = _parse_json_container(old)
        if parsed is not None:
            return bool(_parse_json_container(new) == parsed)
    return normalize_whitespace(text(old)) == normalize_whitespace(text(new))


def check_conformance(
    spec: ChangeSpec, original: Mapping[str, Any], applied: AppliedChange
) -> list[str]:
    """Violations of the change's declared footprint; an empty list conforms.

    An unapplied change must return the request canonically unchanged.
    """
    before = dict(original)
    after = dict(applied.request)
    changed = _changed_keys(before, after)
    if not applied.applied or spec.change_id == "C0":
        return [f"unexpected change to {sorted(changed)}"] if changed else []

    cid = spec.change_id
    allowed: set[str]
    if cid in {"C1", "C2", "X1"}:
        allowed = {"model"}
    elif cid in {"C3", "C4"}:
        allowed = {"context_management"}
    elif cid == "C5":
        allowed = {"messages"}
    elif cid == "C6":
        allowed = {"max_tokens", "max_completion_tokens"}
    else:
        allowed = {"messages"}
    problems = [f"touched undeclared field {k!r}" for k in sorted(changed - allowed)]

    old_messages, new_messages = _messages(before), _messages(after)
    if cid in {"C1", "C2", "X1"} and after.get("model") != spec.params["target_model"]:
        problems.append("model is not the declared target")
    elif cid in {"C3", "C4"}:
        if "context_management" in before or "context_management" not in after:
            problems.append("context_management was not newly added")
    elif cid == "C5":
        if not _is_subsequence(new_messages, old_messages) or len(new_messages) >= len(old_messages):
            problems.append("messages are not a strict subsequence of the original")
        problems.extend(_tool_pairing_problems(new_messages))
    elif cid == "X2":
        limit = int(spec.params["max_chars"])

        def truncated(role: Any, old: Any, new: Any) -> bool:
            if role != "tool":
                return False
            if isinstance(old, str) and isinstance(new, str):
                return old.startswith(new) and len(new) == min(len(old), limit)
            return isinstance(old, list) and isinstance(new, list)

        problems.extend(_same_length_message_check(old_messages, new_messages, truncated))
    elif cid == "X3":
        expected = [m for m in old_messages if not (isinstance(m, dict) and m.get("role") == "system")]
        if canonical_json(new_messages) != canonical_json(expected):
            problems.append("messages are not the original minus system messages")
    elif cid == "B1":

        def same_json(role: Any, old: Any, new: Any) -> bool:
            return (
                role == "tool"
                and isinstance(old, str)
                and isinstance(new, str)
                and _parse_json_container(old) is not None
                and _parse_json_container(old) == _parse_json_container(new)
            )

        problems.extend(_same_length_message_check(old_messages, new_messages, same_json))
    elif cid == "B2":
        problems.extend(_same_length_message_check(old_messages, new_messages, _text_equivalent))
    return problems


__all__ = [
    "CHANGE_IDS",
    "CLEAR_TOOL_USES_EDIT",
    "COMPACTION_MIN_TRIGGER_TOKENS",
    "COMPACTION_SUPPORTED_MODELS",
    "COMPACT_BETA",
    "COMPACT_EDIT",
    "CONTEXT_MANAGEMENT_BETA",
    "DEFAULT_TRUNCATE_CHARS",
    "TOKEN_ESTIMATE_METHOD",
    "apply_change",
    "catalog",
    "check_conformance",
    "estimate_tokens",
    "extract_applied_edits",
    "extract_compaction_blocks",
    "make_change",
    "normalize_whitespace",
    "provider_of",
]
