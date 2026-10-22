"""Frozen, tool-aware Chat Completions contracts for fresh campaign calls.

Transport validity is separate from task quality. Refusals, length stops and
malformed function arguments remain application outcomes with their real bill.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from bench.campaign_accounting import (
    HARD_CAP_USD,
    AccountingError,
    accounting,
    canonical,
    digest,
    json_object,
    money,
)

LEGACY_POLICY = "fresh-structured-calls-v1"
POLICY = "fresh-structured-calls-v2"
ACTORS = {"agent", "user", "judge"}
CALL_KINDS = {"primary", "exploration", "shadow", "fallback", "validation"}
PARAMETERS = {
    "max_tokens",
    "temperature",
    "top_p",
    "seed",
    "stop",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
    "frequency_penalty",
    "presence_penalty",
}


@dataclass(frozen=True)
class ChatContract:
    model: str
    provider_tag: str
    provider_name: str
    endpoint_model: str
    context_tokens: int
    max_output_tokens: int = 2048
    prompt_usd_per_token: str = "0.000006"
    completion_usd_per_token: str = "0.000030"
    cached_text_blocks: bool = False

    def __post_init__(self) -> None:
        for value in (self.model, self.provider_tag, self.endpoint_model):
            if not isinstance(value, str) or not re.fullmatch(r"[\w./:-]+", value):
                raise AccountingError("invalid frozen endpoint identity")
        if not isinstance(self.provider_name, str) or not self.provider_name:
            raise AccountingError("missing frozen provider identity")
        if (
            type(self.context_tokens) is not int
            or self.context_tokens <= 0
            or type(self.max_output_tokens) is not int
            or not 0 < self.max_output_tokens <= min(2048, self.context_tokens)
        ):
            raise AccountingError("invalid frozen serving capacity")
        if not 0 < money(self.prompt_usd_per_token) <= Decimal("0.000006"):
            raise AccountingError("invalid frozen prompt price ceiling")
        if not 0 < money(self.completion_usd_per_token) <= Decimal("0.000030"):
            raise AccountingError("invalid frozen completion price ceiling")
        if type(self.cached_text_blocks) is not bool:
            raise AccountingError("invalid cached text-block contract")

    def record(self) -> dict[str, Any]:
        value = asdict(self)
        if not self.cached_text_blocks:
            value.pop("cached_text_blocks")  # Preserve existing contract bytes.
        return value

    def bound(self, payload: dict[str, Any]) -> Decimal:
        # Tools and schemas can introduce provider-side tokens. Reserve the
        # entire pinned serving context, rather than guessing those additions.
        return self.context_tokens * money(self.prompt_usd_per_token) + payload[
            "max_tokens"
        ] * money(self.completion_usd_per_token)

    def provider(self) -> dict[str, Any]:
        return {
            "only": [self.provider_tag],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "max_price": {
                "prompt": float(money(self.prompt_usd_per_token) * 1_000_000),
                "completion": float(money(self.completion_usd_per_token) * 1_000_000),
                "request": 0,
            },
        }


def _tool_calls(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise AccountingError("invalid function call envelope")
    ids = set()
    for call in value:
        if (
            not isinstance(call, dict)
            or not {"id", "type", "function"}.issubset(call)
            or set(call) - {"id", "type", "function", "name"}
        ):
            raise AccountingError("unsupported function call envelope")
        function = call["function"]
        if (
            call["type"] != "function"
            or not isinstance(call["id"], str)
            or not call["id"]
            or call["id"] in ids
            or not isinstance(function, dict)
            or set(function) != {"name", "arguments"}
            or not isinstance(function["name"], str)
            or not function["name"]
            or not isinstance(function["arguments"], str)
        ):
            raise AccountingError("invalid function call identity")
        if "name" in call and call["name"] != function["name"]:
            raise AccountingError(
                "redundant function name differs from its declaration"
            )
        ids.add(call["id"])
        # Do not parse/repair arguments: the application must see the actual
        # provider output, including invalid JSON and invalid tool names.
    return value


def _text_content(value: Any, cached_text_blocks: bool) -> bool:
    if isinstance(value, str):
        return True
    if (
        not cached_text_blocks
        or not isinstance(value, list)
        or not 0 < len(value) <= 128
    ):
        return False
    return all(
        isinstance(block, dict)
        and not set(block) - {"type", "text", "cache_control"}
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and (
            "cache_control" not in block
            or block["cache_control"] == {"type": "ephemeral"}
        )
        for block in value
    )


def _messages(value: Any, *, cached_text_blocks: bool = False) -> None:
    if not isinstance(value, list) or not 0 < len(value) <= 128:
        raise AccountingError("unbounded or missing chat history")
    pending, used = set(), set()
    for message in value:
        if not isinstance(message, dict):
            raise AccountingError("invalid chat message")
        role = message.get("role")
        allowed = {"role", "content", "name"}
        if role == "assistant":
            allowed |= {"tool_calls", "refusal"}
        elif role == "tool":
            allowed |= {"tool_call_id"}
        elif role not in {"system", "developer", "user"}:
            raise AccountingError("unsupported chat role")
        if set(message) - allowed:
            raise AccountingError("unsupported chat message fields")
        if "name" in message and not isinstance(message["name"], str):
            raise AccountingError("invalid chat message name")
        content = message.get("content")
        calls = message.get("tool_calls")
        refusal = message.get("refusal")
        if role != "assistant" and not _text_content(content, cached_text_blocks):
            raise AccountingError("only text and client function tools are enrolled")
        if role == "assistant":
            if content is not None and not _text_content(content, cached_text_blocks):
                raise AccountingError("unsupported assistant content")
            if refusal is not None and not isinstance(refusal, str):
                raise AccountingError("invalid refusal envelope")
            if content is None and not calls and refusal is None:
                raise AccountingError("empty assistant envelope")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise AccountingError("tool result has no pending function call")
            pending.remove(call_id)
        else:
            if pending:
                raise AccountingError("function call results are incomplete")
            if calls is not None:
                ids = {call["id"] for call in _tool_calls(calls)}
                if ids & used:
                    raise AccountingError("function call identity reused in history")
                pending |= ids
                used |= ids
    if pending:
        raise AccountingError("function call results are incomplete")


def prepare_request(contract: ChatContract, request: dict[str, Any]) -> dict[str, Any]:
    """Copy the real request without flattening messages or rewriting parameters."""
    if not isinstance(request, dict) or set(request) - {
        "model",
        "messages",
        "stream",
        *PARAMETERS,
    }:
        raise AccountingError("request has unenrolled provider features")
    if request.get("model") != contract.model:
        raise AccountingError("request model differs from frozen contract")
    if "stream" in request and request["stream"] is not False:
        raise AccountingError("only non-streaming calls are enrolled")
    try:
        payload = json.loads(canonical(request))
    except (ValueError, TypeError, UnicodeError):
        raise AccountingError("request is not finite JSON") from None
    _messages(payload.get("messages"), cached_text_blocks=contract.cached_text_blocks)
    output = payload.get("max_tokens")
    if type(output) is not int or not 0 < output <= contract.max_output_tokens:
        raise AccountingError("request output cap is outside frozen capacity")
    for name, low, high in (
        ("temperature", 0, 2),
        ("top_p", 0, 1),
        ("frequency_penalty", -2, 2),
        ("presence_penalty", -2, 2),
    ):
        if name in payload:
            value = payload[name]
            if (
                type(value) not in {int, float}
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise AccountingError("invalid sampling parameter")
    if "seed" in payload and type(payload["seed"]) is not int:
        raise AccountingError("invalid sampling seed")
    if (
        "parallel_tool_calls" in payload
        and type(payload["parallel_tool_calls"]) is not bool
    ):
        raise AccountingError("invalid parallel tool setting")
    if "stop" in payload:
        stop = payload["stop"]
        if not (
            isinstance(stop, str)
            or isinstance(stop, list)
            and 0 < len(stop) <= 4
            and all(isinstance(s, str) for s in stop)
        ):
            raise AccountingError("invalid stop sequences")
    names = set()
    if "tools" in payload:
        tools = payload["tools"]
        if not isinstance(tools, list) or not 0 < len(tools) <= 128:
            raise AccountingError("unbounded client tools")
        for tool in tools:
            if (
                not isinstance(tool, dict)
                or set(tool) != {"type", "function"}
                or tool["type"] != "function"
            ):
                raise AccountingError("only client function tools are enrolled")
            function = tool["function"]
            if (
                not isinstance(function, dict)
                or set(function) - {"name", "description", "parameters", "strict"}
                or not isinstance(function.get("name"), str)
                or not function["name"]
                or function["name"] in names
                or not isinstance(function.get("parameters"), dict)
                or "strict" in function
                and type(function["strict"]) is not bool
                or "description" in function
                and not isinstance(function["description"], str)
            ):
                raise AccountingError("invalid client function declaration")
            names.add(function["name"])
    if "tool_choice" in payload:
        choice = payload["tool_choice"]
        if isinstance(choice, dict):
            if (
                set(choice) != {"type", "function"}
                or choice["type"] != "function"
                or not isinstance(choice["function"], dict)
                or set(choice["function"]) != {"name"}
                or choice["function"]["name"] not in names
            ):
                raise AccountingError("invalid forced tool choice")
        elif not isinstance(choice, str) or choice not in {"auto", "none", "required"}:
            raise AccountingError("invalid tool choice")
        if not names:
            raise AccountingError("tool choice without enrolled client tools")
    if "response_format" in payload:
        form = payload["response_format"]
        if not isinstance(form, dict):
            raise AccountingError("invalid response format")
        if form.get("type") in {"text", "json_object"}:
            if set(form) != {"type"}:
                raise AccountingError("invalid response format")
        elif form.get("type") == "json_schema" and set(form) == {"type", "json_schema"}:
            schema = form["json_schema"]
            if (
                not isinstance(schema, dict)
                or set(schema) - {"name", "description", "schema", "strict"}
                or not isinstance(schema.get("name"), str)
                or not schema["name"]
                or not isinstance(schema.get("schema"), dict)
                or "strict" in schema
                and type(schema["strict"]) is not bool
            ):
                raise AccountingError("invalid response schema envelope")
        else:
            raise AccountingError("unsupported response format")
    payload.update(
        stream=False,
        transforms=[],
        service_tier="default",
        provider=contract.provider(),
    )
    if len(canonical(payload)) > 65536:
        raise AccountingError("request exceeds the frozen byte bound")
    return payload


def validate_endpoint(
    contract: ChatContract, payload: dict[str, Any], endpoint: Any
) -> None:
    """Recheck unique identity, supported parameters and every published price tier."""
    if not isinstance(endpoint, dict):
        raise AccountingError("missing live serving endpoint")
    context = endpoint.get("context_length")
    if (
        endpoint.get("tag") != contract.provider_tag
        or endpoint.get("provider_name") != contract.provider_name
        or endpoint.get("name", "").split(" | ")[-1] != contract.endpoint_model
        or type(context) is not int
        or not contract.max_output_tokens <= context <= contract.context_tokens
    ):
        raise AccountingError("live endpoint differs from frozen serving identity")
    supported = endpoint.get("supported_parameters")
    required = set(payload) & PARAMETERS
    if payload.get("response_format", {}).get("type") == "json_schema":
        required.add("structured_outputs")
    if not isinstance(supported, list) or not required.issubset(supported):
        raise AccountingError("live endpoint lacks requested capabilities")
    pricing = endpoint.get("pricing")
    inactive = {
        "input_cache_read",
        "input_cache_write",
        "input_cache_write_1h",
        "web_search",
        "discount",
    }
    if not isinstance(pricing, dict) or set(pricing) - {
        "prompt",
        "completion",
        "request",
        "overrides",
        *inactive,
    }:
        raise AccountingError("unknown live pricing dimension")
    overrides = pricing.get("overrides", [])
    if not isinstance(overrides, list):
        raise AccountingError("invalid live pricing tiers")
    for rates in [pricing, *overrides]:
        if (
            not isinstance(rates, dict)
            or set(rates)
            - {
                "prompt",
                "completion",
                "request",
                "min_prompt_tokens",
                *inactive,
                "overrides",
            }
            or rates is not pricing
            and (
                type(rates.get("min_prompt_tokens")) is not int
                or rates["min_prompt_tokens"] < 0
                or "overrides" in rates
            )
        ):
            raise AccountingError("invalid live pricing tier")
        if (
            money(rates.get("prompt", pricing.get("prompt")))
            > money(contract.prompt_usd_per_token)
            or money(rates.get("completion", pricing.get("completion")))
            > money(contract.completion_usd_per_token)
            or money(rates.get("request", pricing.get("request", "0"))) != 0
        ):
            raise AccountingError("live price exceeds frozen reservation basis")
        for name in inactive & rates.keys():
            money(rates[name])
    maximum = endpoint.get("max_completion_tokens")
    if maximum is not None and (
        type(maximum) is not int or maximum < payload["max_tokens"]
    ):
        raise AccountingError("live endpoint cannot honor the output cap")


def completion_record(
    claim: dict[str, Any], body: Any, contract: ChatContract
) -> dict[str, Any]:
    """Validate billing/routing; never treat a transport receipt as task success."""
    if not isinstance(body, dict) or body.get("error") is not None:
        raise AccountingError("provider returned an error envelope")
    choices = body.get("choices")
    if (
        not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], dict)
    ):
        raise AccountingError("provider choice attribution is missing")
    choice = choices[0]
    message = choice.get("message")
    if (
        choice.get("error") is not None
        or choice.get("finish_reason")
        not in {"stop", "length", "tool_calls", "content_filter"}
        or not isinstance(message, dict)
        or message.get("role") != "assistant"
    ):
        raise AccountingError("provider completion envelope is invalid")
    content, calls, refusal = (
        message.get("content"),
        message.get("tool_calls"),
        message.get("refusal"),
    )
    if (
        content is not None
        and not isinstance(content, str)
        or refusal is not None
        and not isinstance(refusal, str)
    ):
        raise AccountingError("unsupported provider completion content")
    if calls is not None:
        _tool_calls(calls)
    if choice["finish_reason"] == "tool_calls" and not calls:
        raise AccountingError("provider omitted its function calls")
    if (
        content is None
        and not calls
        and refusal is None
        and choice["finish_reason"] != "content_filter"
    ):
        raise AccountingError("provider omitted its completion")
    usage, router = body.get("usage"), body.get("openrouter_metadata")
    if not isinstance(usage, dict) or not isinstance(router, dict):
        raise AccountingError("provider omitted billing or routing metadata")
    cost = money(usage.get("cost"))
    if cost > money(claim["upper_cost_usd"]) or usage.get("is_byok") is not False:
        raise AccountingError("provider violated reserved billing contract")
    for name, bound in (
        ("prompt_tokens", contract.context_tokens),
        ("completion_tokens", claim["request"]["max_tokens"]),
    ):
        if type(usage.get(name)) is not int or not 0 <= usage[name] <= bound:
            raise AccountingError("provider token counts violate serving bounds")
    available = router.get("endpoints", {}).get("available", [])
    selected = [
        e for e in available if isinstance(e, dict) and e.get("selected") is True
    ]
    if (
        body.get("model") != contract.model
        or body.get("provider") != contract.provider_name
        or router.get("requested") != contract.model
        or router.get("strategy") != "direct"
        or type(router.get("attempt")) is not int
        or router["attempt"] != 1
        or router.get("is_byok") is not False
        or len(selected) != 1
        or selected[0].get("provider") != contract.provider_name
        or selected[0].get("model") != contract.endpoint_model
        or router.get("pipeline", []) != []
    ):
        raise AccountingError("provider violated frozen routing contract")
    attempts = router.get("attempts")
    if attempts is not None and attempts != [
        {
            "provider": contract.provider_name,
            "model": contract.endpoint_model,
            "status": 200,
        }
    ]:
        raise AccountingError("provider performed an unenrolled retry")
    generation = body.get("id")
    if not isinstance(generation, str) or not generation:
        raise AccountingError("missing provider generation identity")
    return {
        "event": "result",
        **{
            name: claim[name]
            for name in (
                "id",
                "stage",
                "attempt_id",
                "fingerprint",
                "metadata",
                "session_id",
            )
        },
        "reserve_sha256": digest(claim),
        "cost_usd": str(cost),
        "message": message,
        "finish_reason": choice["finish_reason"],
        "usage": usage,
        "model": body["model"],
        "provider": body["provider"],
        "generation_id": generation,
        "router_metadata": router,
        "paper_evidence": False,
    }


def validate_metadata(metadata: Any) -> None:
    required = {"actor", "kind", "task_id", "arm", "turn_id"}
    if (
        not isinstance(metadata, dict)
        or set(metadata)
        - required
        - {
            "plan_token",
            "native_plan_json",
            "parent_native_plan_json",
            "observation_token",
            "parent_observation_token",
            "exploration_lease_token",
            "cell_id",
            "requested_model",
            "text_action_comparator_version",
            "text_action_settings_version",
            "calibration_trial_id",
            "calibration_authorization_sha256",
            "judge_purpose",
            "judge_evidence_sha256",
        }
        or not required.issubset(metadata)
        or metadata["actor"] not in ACTORS
        or metadata["kind"] not in CALL_KINDS
        or any(not isinstance(value, str) or not value for value in metadata.values())
    ):
        raise AccountingError("missing or invalid workload call attribution")
    trial_fields = {"calibration_trial_id", "calibration_authorization_sha256"}
    if trial_fields & metadata.keys():
        if not trial_fields <= metadata.keys() or not re.fullmatch(
            r"[0-9a-f]{64}", metadata["calibration_authorization_sha256"]
        ):
            raise AccountingError("invalid calibration call attribution")
    judge_fields = {"judge_purpose", "judge_evidence_sha256"}
    if judge_fields & metadata.keys():
        if (
            not judge_fields <= metadata.keys()
            or metadata["actor"] != "judge"
            or metadata["judge_purpose"] != "tau-retail-safety-v1"
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["judge_evidence_sha256"])
        ):
            raise AccountingError("invalid policy review attribution")


def check_funding(
    state: dict[str, Any],
    stage: str,
    stage_cap: Any,
    bound: Decimal,
    observation: Any,
    baseline: Decimal,
) -> None:
    _check_funding(state, stage, stage_cap, bound, observation, baseline, legacy=False)


def key_balance_liabilities(state: dict[str, Any]) -> Decimal:
    """Keep every owned charge until the key balance attests its inclusion.

    Aggregate usage can include external spending and cannot prove that any
    particular campaign debit is reflected in a lagged remaining balance.
    """
    return sum((state[name] for name in ("known_usd", "holds_usd", "pending_usd")), Decimal(0))


def _check_funding(
    state: dict[str, Any],
    stage: str,
    stage_cap: Any,
    bound: Decimal,
    observation: Any,
    baseline: Decimal,
    *,
    legacy: bool,
) -> None:
    if not isinstance(observation, dict):
        raise AccountingError("missing live account observation")
    usage = money(observation.get("usage"))
    if (
        usage < baseline
        or money(observation.get("byok_usage")) != 0
        or observation.get("limit_reset") is not None
    ):
        raise AccountingError("account usage cannot be cumulatively reconciled")
    known, holds, pending = (
        state[name] for name in ("known_usd", "holds_usd", "pending_usd")
    )
    effective = max(known + holds + pending, usage - baseline + holds + pending)
    stage_total = (
        state["known_by_stage"].get(stage, Decimal(0))
        + state["holds_by_stage"].get(stage, Decimal(0))
        + sum(
            (
                money(r["upper_cost_usd"])
                for r in state["pending"]
                if r["stage"] == stage
            ),
            Decimal(0),
        )
    )
    if effective + bound > HARD_CAP_USD or stage_total + bound > money(stage_cap):
        raise AccountingError("call exceeds cumulative campaign or stage funding")
    remaining = observation.get("limit_remaining")
    # Historical records retain the admission rule that originally issued them.
    # New work cannot use an unrelated aggregate usage offset to clear charges.
    liabilities = (
        max(Decimal(0), known - (usage - baseline)) + holds + pending
        if legacy
        else key_balance_liabilities(state)
    )
    if remaining is not None and money(remaining) < liabilities + bound:
        raise AccountingError("key balance cannot cover unconfirmed liabilities")


def observed_usage_floor(events: list[dict[str, Any]], baseline: Decimal) -> Decimal:
    observations = [baseline]
    for event in events:
        if "account_usage_before_usd" in event:
            observations.append(money(event["account_usage_before_usd"]))
        if event["event"] == "reserve" and "account_observation" in event:
            observations.append(money(event["account_observation"].get("usage")))
    return max(observations)


def validate_campaign_events(events: list[dict[str, Any]], *, ledger_path: Any) -> None:
    """Revalidate every new dispatch/receipt without weakening legacy accounting."""
    first = next(i for i, e in enumerate(events) if e["event"] == "campaign_session")
    state = accounting(events[:first])
    origins = [e for e in events[:first] if e["event"] == "origin"]
    if len(origins) != 1:
        raise AccountingError("fresh campaign requires its original account origin")
    baseline = money(origins[0]["usage_usd"])
    usage_floor = observed_usage_floor(events[:first], baseline)
    stages = {e.get("stage") for e in events[:first] if e.get("stage")}
    used_ids = {e.get("id") for e in events[:first] if e.get("id")}
    generations = {
        e.get("generation_id") for e in events[:first] if e.get("generation_id")
    }
    generations.update(
        e["response"].get("id")
        for e in events[:first]
        if e["event"] == "response"
        and isinstance(e.get("response"), dict)
        and isinstance(e["response"].get("id"), str)
        and e["response"]["id"]
    )
    duplicate_responses = set()
    sessions, active, claims, responses, ended = set(), None, {}, {}, set()
    # Totals update incrementally after the legacy prefix. This avoids replaying
    # the original 100 MB history once per fresh reservation.
    state = {
        **state,
        "pending": list(state["pending"]),
        "known_by_stage": dict(state["known_by_stage"]),
        "holds_by_stage": dict(state["holds_by_stage"]),
    }
    failed = bool(state["failed_calls"] or state["unsafe_ids"])
    for event in events[first:]:
        kind = event["event"]
        if kind == "campaign_session":
            if (
                event.get("policy") not in {POLICY, LEGACY_POLICY}
                or event.get("ledger_binding") != digest(str(ledger_path.resolve()))
                or not isinstance(event.get("session_id"), str)
                or not event["session_id"]
                or event["session_id"] in sessions
                or not isinstance(event.get("stage"), str)
                or not event["stage"]
                or event["stage"] in stages
                or state["pending"]
                or failed
                or not 0 < money(event.get("stage_cap_usd")) <= HARD_CAP_USD
            ):
                raise AccountingError(
                    "invalid, reused or unresolved campaign enrollment"
                )
            contracts = event.get("contracts")
            if not isinstance(contracts, dict) or not contracts:
                raise AccountingError("campaign lacks frozen endpoint contracts")
            for model, value in contracts.items():
                if ChatContract(**value).model != model:
                    raise AccountingError("campaign contract model differs")
            active = event
            stages.add(event["stage"])
            sessions.add(event["session_id"])
            continue
        if (
            active is None
            or event.get("session_id") != active["session_id"]
            or kind not in {"reserve", "response", "result", "attempt_failure"}
        ):
            raise AccountingError("event is outside the fresh campaign lifecycle")
        if kind == "reserve":
            request = event["request"]
            contract = ChatContract(**active["contracts"][request["model"]])
            original = {
                key: value
                for key, value in request.items()
                if key not in {"stream", "transforms", "service_tier", "provider"}
            }
            validate_metadata(event.get("metadata"))
            bound = money(event["upper_cost_usd"])
            if (
                failed
                or event["id"] in used_ids
                or event["stage"] != active["stage"]
                or prepare_request(contract, original) != request
                or bound != contract.bound(request)
                or event["fingerprint"]
                != digest(
                    {
                        "payload": request,
                        "metadata": event["metadata"],
                        "stage": event["stage"],
                    }
                )
            ):
                raise AccountingError(
                    "fresh reservation differs from frozen call contract"
                )
            validate_endpoint(contract, request, event.get("endpoint_observation"))
            observed = money(event["account_observation"].get("usage"))
            if observed < usage_floor:
                raise AccountingError("cumulative account usage regressed")
            usage_floor = observed
            _check_funding(
                state,
                active["stage"],
                active["stage_cap_usd"],
                bound,
                event.get("account_observation"),
                baseline,
                legacy=active["policy"] == LEGACY_POLICY,
            )
            claims[event["id"]] = event
            used_ids.add(event["id"])
            state["pending"].append(event)
            state["pending_usd"] += bound
            continue
        claim = claims.get(event.get("id"))
        if (
            claim is None
            or event["id"] in ended
            or event.get("reserve_sha256") != digest(claim)
            or event.get("attempt_id") != claim["attempt_id"]
            or event.get("stage") != claim["stage"]
        ):
            raise AccountingError("fresh receipt has no unique live reservation")
        if kind == "response":
            if event["id"] in responses:
                raise AccountingError("duplicate fresh provider response")
            try:
                raw = base64.b64decode(event["raw_base64"], validate=True)
            except (ValueError, TypeError, binascii.Error):
                raise AccountingError("fresh response lost its raw HTTP body") from None
            decoded = {}
            try:
                decoded = json_object(raw)
            except AccountingError:
                pass
            if (
                event.get("response") != decoded
                or type(event.get("http_status")) is not int
                or not 100 <= event["http_status"] <= 599
                or event.get("cache_status") is not None
                and not isinstance(event["cache_status"], str)
            ):
                raise AccountingError(
                    "fresh response differs from its raw HTTP receipt"
                )
            responses[event["id"]] = event
            generation = decoded.get("id")
            if isinstance(generation, str) and generation:
                if generation in generations:
                    duplicate_responses.add(event["id"])
                generations.add(generation)
            continue
        bound = money(claim["upper_cost_usd"])
        if kind == "result":
            receipt = responses.get(event["id"])
            if (
                receipt is None
                or receipt.get("http_status") != 200
                or receipt.get("cache_status") not in {None, "MISS"}
            ):
                raise AccountingError("result lacks an uncached durable HTTP response")
            contract = ChatContract(**active["contracts"][claim["request"]["model"]])
            expected = completion_record(claim, receipt["response"], contract)
            if (
                any(event.get(key) != value for key, value in expected.items())
                or event["id"] in duplicate_responses
            ):
                raise AccountingError(
                    "fresh result differs from its unique provider receipt"
                )
            generations.add(event["generation_id"])
            charge = money(event["cost_usd"])
        else:
            failed = True
            charge = money(event.get("reported_cost_usd") or "0")
            if money(event.get("budget_hold_usd")) != bound:
                raise AccountingError("fresh failure reduced its retained allowance")
            residual = max(Decimal(0), bound - charge)
            state["holds_usd"] += residual
            state["holds_by_stage"][claim["stage"]] = (
                state["holds_by_stage"].get(claim["stage"], Decimal(0)) + residual
            )
        state["known_usd"] += charge
        state["known_by_stage"][claim["stage"]] = (
            state["known_by_stage"].get(claim["stage"], Decimal(0)) + charge
        )
        state["pending"].remove(claim)
        state["pending_usd"] -= bound
        ended.add(event["id"])
