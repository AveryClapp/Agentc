"""Change catalog C0-C6, X1-X3, B1-B2 (protocol Section 4)."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from agentc.change_safety.changes import (
    CHANGE_IDS,
    COMPACT_BETA,
    CONTEXT_MANAGEMENT_BETA,
    apply_change,
    catalog,
    check_conformance,
    extract_applied_edits,
    extract_compaction_blocks,
    make_change,
)
from agentc.change_safety.records import AppliedChange, ChangeSpec, canonical_json

SONNET = "anthropic/claude-sonnet-4-5-20250929"
SONNET_46 = "anthropic/claude-sonnet-4-6"
GPT = "openai/gpt-5.4-2026-03-05"

POLICY = (
    "# Banking agent policy\n\n"
    "You are a  customer service agent.   Verify identity before   any account change.\n\n\n\n"
    "Never disclose another customer's data.\t\n"
)
ACCOUNT = {
    "user_id": "u_1042",
    "name": {"first": "Ada", "last": "Ng"},
    "accounts": [
        {"id": "chk_1", "type": "checking", "balance": 1520.25, "status": "active"},
        {"id": "sav_9", "type": "savings", "balance": 88000.0, "status": "frozen"},
    ],
    "notes": "Customer  prefers   email.",
}
ARTICLE = "Wire transfers over $10,000 require a callback verification. " * 40


def _tau2_messages() -> list[dict[str, Any]]:
    """A banking_knowledge-shaped history built with tau2's own converter."""
    tau2_message = pytest.importorskip("tau2.data_model.message")
    llm_utils = pytest.importorskip("tau2.utils.llm_utils")
    m = tau2_message
    history = [
        m.SystemMessage(role="system", content=POLICY),
        m.UserMessage(role="user", content="Hi, I need to unfreeze my savings account."),
        m.AssistantMessage(
            role="assistant",
            content=None,
            tool_calls=[m.ToolCall(id="call_1", name="get_user_details", arguments={"user_id": "u_1042"})],
        ),
        m.ToolMessage(id="call_1", role="tool", content=json.dumps(ACCOUNT)),
        m.AssistantMessage(
            role="assistant",
            content=None,
            tool_calls=[m.ToolCall(id="call_2", name="search_knowledge", arguments={"query": "unfreeze"})],
        ),
        m.ToolMessage(id="call_2", role="tool", content=ARTICLE),
        m.AssistantMessage(role="assistant", content="I can help.  Can you confirm your date of birth?"),
        m.UserMessage(role="user", content="Sure, it is 1990-04-02."),
    ]
    messages: list[dict[str, Any]] = llm_utils.to_litellm_messages(history)
    return messages


def tau2_request(model: str = SONNET) -> dict[str, Any]:
    return {
        "model": model,
        "messages": _tau2_messages(),
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"{name} tool",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }
            for name in ("get_user_details", "search_knowledge")
        ],
        "tool_choice": "auto",
        "temperature": 0.0,
        "num_retries": 3,
    }


def apply_checked(spec: ChangeSpec, request: dict[str, Any], **kwargs: Any) -> AppliedChange:
    before = copy.deepcopy(request)
    result = apply_change(spec, request, **kwargs)
    assert request == before, "apply_change mutated its input"
    assert check_conformance(spec, request, result) == []
    if result.applied:
        assert result.ineligible_reason is None
    else:
        assert result.ineligible_reason
        assert canonical_json(result.request) == canonical_json(request)
    return result


# ---------------------------------------------------------------------------
# Catalog and registry
# ---------------------------------------------------------------------------


def full_catalog() -> tuple[ChangeSpec, ...]:
    return catalog(
        cheaper_tier_model="anthropic/claude-haiku-4-5-20251001",
        newer_version_model=SONNET_46,
        smallest_model="anthropic/claude-haiku-4-5-20251001",
        clear_trigger_input_tokens=200,
        clear_keep_tool_uses=1,
        compaction_trigger_input_tokens=50_000,
    )


def test_catalog_covers_every_change_with_its_family() -> None:
    specs = full_catalog()
    assert tuple(s.change_id for s in specs) == CHANGE_IDS
    families = {s.change_id: s.family for s in specs}
    assert families["C0"] == "aa_control" and families["C2"] == "model_migration"
    assert {families[c] for c in ("C3", "C4", "C5")} == {"context_removal"}
    assert families["C6"] == "output_cap"
    assert {families[c] for c in ("X1", "X2", "X3")} == {"seeded_harm"}
    assert {families[c] for c in ("B1", "B2")} == {"benign"}
    assert specs[8].params["max_chars"] == 200
    assert len({s.identity for s in specs}) == len(specs)


@pytest.mark.parametrize(
    ("change_id", "params"),
    [
        ("C1", {}),
        ("C3", {"trigger_input_tokens": 100}),
        ("C4", {"trigger_input_tokens": 49_999}),
        ("X2", {"max_chars": -1}),
        ("Z9", {}),
    ],
)
def test_make_change_rejects_invalid_parameters(change_id: str, params: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        make_change(change_id, **params)


def test_apply_change_rejects_spec_in_the_wrong_family() -> None:
    with pytest.raises(ValueError):
        apply_change(ChangeSpec("C0", "benign"), tau2_request())


# ---------------------------------------------------------------------------
# C0, C1, C2, X1
# ---------------------------------------------------------------------------


def test_c0_identity_is_canonically_identical() -> None:
    request = tau2_request()
    result = apply_checked(make_change("C0"), request)
    assert result.applied
    assert canonical_json(result.request) == canonical_json(request)
    assert result.request is not request
    assert result.evidence["aa_control"] is True


@pytest.mark.parametrize("change_id", ["C1", "C2", "X1"])
def test_model_swaps_change_only_the_model(change_id: str) -> None:
    target = "anthropic/claude-haiku-4-5-20251001"
    result = apply_checked(make_change(change_id, target_model=target), tau2_request())
    assert result.applied
    assert result.request["model"] == target
    assert result.evidence == {"source_model": SONNET, "target_model": target}


def test_model_swap_refuses_same_model_and_cross_provider() -> None:
    same = apply_checked(make_change("C1", target_model=SONNET), tau2_request())
    assert not same.applied
    cross = apply_checked(make_change("C1", target_model=GPT), tau2_request())
    assert not cross.applied and "provider" in (cross.ineligible_reason or "")


def test_conformance_flags_an_undeclared_edit() -> None:
    spec = make_change("C1", target_model="anthropic/claude-haiku-4-5-20251001")
    request = tau2_request()
    result = apply_change(spec, request)
    tampered = AppliedChange(
        request={**result.request, "temperature": 1.0}, applied=True, evidence={}
    )
    assert check_conformance(spec, request, tampered) == ["touched undeclared field 'temperature'"]


# ---------------------------------------------------------------------------
# C3, C4 and the LiteLLM Anthropic transport
# ---------------------------------------------------------------------------


def anthropic_body(**extra: Any) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5-20250929",
        "content": [{"type": "text", "text": "Thanks, verifying now."}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 900, "output_tokens": 6},
        **extra,
    }


def send_through_litellm(
    request: dict[str, Any], body: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str], Any]:
    """Run LiteLLM's real Anthropic request/response transformation, no network."""
    import litellm
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    captured: dict[str, Any] = {}

    def fake_post(url: str, data: Any = None, json: Any = None, headers: Any = None, **_: Any) -> httpx.Response:
        captured.update(data=data, json=json, headers=headers)
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    client = HTTPHandler()
    kwargs = {k: v for k, v in request.items() if k != "num_retries"}
    with patch.object(client, "post", side_effect=fake_post):
        response = litellm.completion(**kwargs, api_key="sk-ant-test", client=client)
    raw = captured["data"] if captured["data"] is not None else captured["json"]
    sent = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    return sent, dict(captured["headers"]), response


def test_c3_reaches_anthropic_through_litellm_and_edits_are_read_back() -> None:
    spec = make_change("C3", trigger_input_tokens=200, keep_tool_uses=1)
    result = apply_checked(spec, tau2_request())
    assert result.applied
    edit = {
        "type": "clear_tool_uses_20250919",
        "trigger": {"type": "input_tokens", "value": 200},
        "keep": {"type": "tool_uses", "value": 1},
    }
    assert result.request["context_management"] == {"edits": [edit]}
    assert result.evidence["trigger_expected_to_fire"] is True
    assert result.evidence["projected_cleared_tool_results"] == 1
    assert result.evidence["projected_cleared_chars"] == len(json.dumps(ACCOUNT))

    applied = [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 1, "cleared_input_tokens": 80}]
    sent, headers, response = send_through_litellm(
        result.request, anthropic_body(context_management={"applied_edits": applied})
    )
    assert sent["context_management"] == {"edits": [edit]}
    assert CONTEXT_MANAGEMENT_BETA in headers["anthropic-beta"]
    assert any(block.get("type") == "tool_result" for m in sent["messages"] for block in m["content"] if isinstance(block, dict))
    assert extract_applied_edits(response) == tuple(applied)


def test_unchanged_request_has_no_edit_and_no_applied_edits() -> None:
    sent, headers, response = send_through_litellm(tau2_request(), anthropic_body())
    assert "context_management" not in sent
    assert CONTEXT_MANAGEMENT_BETA not in headers.get("anthropic-beta", "")
    assert extract_applied_edits(response) is None
    assert extract_compaction_blocks(response) is None


def test_c3_optional_parameters_follow_the_documented_shape() -> None:
    spec = make_change(
        "C3",
        trigger_input_tokens=30_000,
        keep_tool_uses=3,
        clear_at_least_input_tokens=5_000,
        exclude_tools=["search_knowledge"],
        clear_tool_inputs=True,
    )
    edit = apply_checked(spec, tau2_request()).request["context_management"]["edits"][0]
    assert edit["clear_at_least"] == {"type": "input_tokens", "value": 5_000}
    assert edit["exclude_tools"] == ["search_knowledge"]
    assert edit["clear_tool_inputs"] is True


@pytest.mark.parametrize("change_id", ["C3", "C4"])
def test_provider_context_editing_is_ineligible_off_anthropic(change_id: str) -> None:
    spec = make_change(change_id, trigger_input_tokens=60_000, keep_tool_uses=2)
    result = apply_checked(spec, tau2_request(GPT))
    assert not result.applied
    assert "context_management" not in result.request


def test_c4_openai_chat_completions_is_ineligible_with_reason() -> None:
    result = apply_checked(make_change("C4", trigger_input_tokens=60_000), tau2_request(GPT))
    assert "Responses API" in (result.ineligible_reason or "")


def test_c4_openai_responses_request_gets_responses_compaction() -> None:
    request = {"model": GPT, "input": [{"role": "user", "content": "hi"}]}
    result = apply_checked(make_change("C4", trigger_input_tokens=60_000), request)
    assert result.applied
    assert result.request["context_management"] == [{"type": "compaction", "compact_threshold": 60_000}]


def test_c4_unsupported_anthropic_model_is_ineligible() -> None:
    result = apply_checked(make_change("C4", trigger_input_tokens=60_000), tau2_request(SONNET))
    assert not result.applied
    assert "compact_20260112" in (result.ineligible_reason or "")


def test_c4_reaches_anthropic_through_litellm_with_compact_beta() -> None:
    result = apply_checked(make_change("C4", trigger_input_tokens=60_000), tau2_request(SONNET_46))
    edit = {"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 60_000}}
    assert result.request["context_management"] == {"edits": [edit]}
    assert result.evidence["trigger_expected_to_fire"] is False
    block = {"type": "compaction", "content": "Summary: customer asked to unfreeze savings."}
    body = anthropic_body(content=[block, {"type": "text", "text": "Continuing."}])
    sent, headers, response = send_through_litellm(result.request, body)
    assert sent["context_management"] == {"edits": [edit]}
    assert COMPACT_BETA in headers["anthropic-beta"]
    assert CONTEXT_MANAGEMENT_BETA not in headers["anthropic-beta"]
    assert extract_compaction_blocks(response) == (block,)


def test_extract_applied_edits_reads_raw_bodies_and_empty_lists() -> None:
    assert extract_applied_edits({"context_management": {"applied_edits": []}}) == ()
    assert extract_applied_edits({"content": []}) is None
    assert extract_applied_edits(object()) is None


def test_existing_context_management_is_not_overwritten() -> None:
    request = {**tau2_request(SONNET_46), "context_management": {"edits": []}}
    for spec in (
        make_change("C3", trigger_input_tokens=200, keep_tool_uses=1),
        make_change("C4", trigger_input_tokens=60_000),
    ):
        assert not apply_checked(spec, request).applied


# ---------------------------------------------------------------------------
# C5, C6 through the native rules
# ---------------------------------------------------------------------------


@pytest.fixture
def native_optimizer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from agentc import _native

    _native.optimize_reset()
    monkeypatch.setenv("AGENTC_OPTIMIZE", "1")
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "0")
    _native.optimize_configure(str(tmp_path / ".agentc"))
    yield _native
    _native.optimize_reset()


def observe(native: Any, call_site_id: str, output_tokens: int, n: int = 1) -> None:
    for _ in range(n):
        native.optimize_observe(
            '{"kind":"pass_through"}',
            json.dumps(
                {
                    "call_site_id": call_site_id,
                    "model": SONNET,
                    "input_tokens": 2000,
                    "output_tokens": output_tokens,
                    "latency_ms": 100,
                    "cost_usd": 0.01,
                }
            ),
        )


def test_c6_caps_output_from_the_live_profile_below_hot_threshold(native_optimizer: Any) -> None:
    observe(native_optimizer, "tau2.agent:generate:1", output_tokens=100, n=1)
    request = tau2_request()
    result = apply_checked(make_change("C6"), request, call_site_id="tau2.agent:generate:1")
    assert result.applied, result.ineligible_reason
    assert result.request["max_tokens"] in (120, 121)
    assert result.evidence["output_token_cap"] == result.request["max_tokens"]
    assert result.evidence["previous_cap"] is None
    assert result.evidence["profile"]["window_observations"] == 1
    assert {k for k in result.request if result.request[k] != request.get(k)} == {"max_tokens"}


def test_c6_cold_profile_is_a_legitimate_ineligible_fork(native_optimizer: Any) -> None:
    result = apply_checked(make_change("C6"), tau2_request(), call_site_id="tau2.cold:site:1")
    assert not result.applied
    assert "rule_does_not_apply" in (result.ineligible_reason or "")


def test_native_changes_need_a_call_site(native_optimizer: Any) -> None:
    for change_id in ("C5", "C6"):
        result = apply_checked(make_change(change_id), tau2_request())
        assert not result.applied and "call_site_id" in (result.ineligible_reason or "")


def test_c6_does_not_record_an_application(native_optimizer: Any) -> None:
    observe(native_optimizer, "tau2.agent:generate:2", output_tokens=100, n=1)
    before = json.loads(native_optimizer.optimize_propose_rule(
        json.dumps({"call_site_id": "tau2.agent:generate:2", "trace_id": "00" * 16,
                    "span_id": "00" * 8, "model": SONNET, "messages": []}),
        "OutputBudget",
    ))
    assert before["status"] == "proposed"
    assert before["profile"]["n_observations"] == 1


def long_tau2_request() -> dict[str, Any]:
    """Old, unrelated tool results dominate the prompt (> 8 KB).

    The old action carries text: the attention proxy scores an empty
    tool-call-only assistant message as live, and ContextCompress never splits
    an exchange, so tau2 exchanges whose assistant content is None are retained.
    """
    request = tau2_request()
    messages = request["messages"]
    filler = json.dumps({"transactions": [{"merchant": f"store{i}", "amount": i} for i in range(400)]})
    old_exchange = [
        {"role": "assistant", "content": "Let me list your recent transactions.", "tool_calls": [
            {"id": "call_0", "name": "list_transactions", "type": "function",
             "function": {"name": "list_transactions", "arguments": "{}"}}]},
        {"role": "tool", "content": filler, "tool_call_id": "call_0"},
    ]
    current_exchange = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_3", "name": "verify_identity", "type": "function",
             "function": {"name": "verify_identity", "arguments": '{"dob": "1990-04-02"}'}}]},
        {"role": "tool", "content": '{"verified": true}', "tool_call_id": "call_3"},
    ]
    # The agent is called right after a tool result, as in tau2's loop.
    request["messages"] = messages[:2] + old_exchange + messages[2:] + current_exchange
    return request


def test_c5_removes_whole_native_messages_by_index(native_optimizer: Any) -> None:
    request = long_tau2_request()
    result = apply_checked(make_change("C5"), request, call_site_id="tau2.agent:generate:3",
                           trace_id_hex="ab" * 16)
    assert result.applied, result.ineligible_reason
    removed = result.evidence["removed_message_indices"]
    assert removed == [2, 3]  # the stale exchange, action and result together
    kept = result.evidence["retained_message_indices"]
    assert result.request["messages"] == [request["messages"][i] for i in kept]
    assert result.evidence["chars_removed"] == sum(
        len(request["messages"][i]["content"] or "") for i in removed
    )
    assert result.evidence["removed_roles"] == ["assistant", "tool"]
    assert result.evidence["tokens_removed_estimate"] >= result.evidence["chars_removed"] // 4


def test_c5_short_prompt_is_not_applicable(native_optimizer: Any) -> None:
    result = apply_checked(make_change("C5"), tau2_request(), call_site_id="tau2.agent:generate:4")
    assert not result.applied
    assert "rule_does_not_apply" in (result.ineligible_reason or "")


# ---------------------------------------------------------------------------
# X2, X3
# ---------------------------------------------------------------------------


def test_x2_truncates_only_tool_results() -> None:
    request = tau2_request()
    result = apply_checked(make_change("X2"), request)
    assert result.applied
    tools_before = [m for m in request["messages"] if m["role"] == "tool"]
    tools_after = [m for m in result.request["messages"] if m["role"] == "tool"]
    assert all(len(m["content"]) <= 200 for m in tools_after)
    assert [m["tool_call_id"] for m in tools_after] == [m["tool_call_id"] for m in tools_before]
    expected = sum(max(len(m["content"]) - 200, 0) for m in tools_before)
    assert result.evidence["chars_removed"] == expected
    assert result.evidence["tokens_removed_estimate"] == -(-expected // 4)
    assert "chars/4" in result.evidence["token_estimate_method"]


def test_x2_ineligible_when_nothing_exceeds_the_limit() -> None:
    assert not apply_checked(make_change("X2", max_chars=100_000), tau2_request()).applied


def test_x3_removes_every_system_message() -> None:
    request = tau2_request()
    request["messages"].append({"role": "system", "content": "late reminder"})
    result = apply_checked(make_change("X3"), request)
    assert result.applied
    assert all(m["role"] != "system" for m in result.request["messages"])
    assert result.evidence["system_messages_removed"] == 2
    assert result.evidence["chars_removed"] == len(POLICY) + len("late reminder")


def test_x3_ineligible_without_system_prompt() -> None:
    request = tau2_request()
    request["messages"] = request["messages"][1:]
    assert not apply_checked(make_change("X3"), request).applied


# ---------------------------------------------------------------------------
# B1, B2
# ---------------------------------------------------------------------------


def test_b1_reverses_json_keys_and_preserves_meaning_and_format() -> None:
    request = tau2_request()
    result = apply_checked(make_change("B1"), request)
    assert result.applied
    tool = result.request["messages"][3]
    assert json.loads(tool["content"]) == ACCOUNT
    assert list(json.loads(tool["content"])) == sorted(ACCOUNT, reverse=True)
    assert tool["content"] == json.dumps(json.loads(tool["content"]))  # same layout
    assert result.request["messages"][5] == request["messages"][5]  # non-JSON untouched
    assert result.evidence["tool_results_reordered"] == 1
    assert result.evidence["format_preserved"] is True
    assert apply_change(make_change("B1"), request).request == result.request  # deterministic


def test_b1_ineligible_without_json_tool_results() -> None:
    request = tau2_request()
    request["messages"][3]["content"] = "plain text"
    assert not apply_checked(make_change("B1"), request).applied


def test_b2_normalizes_text_but_not_tool_call_arguments_or_json_values() -> None:
    request = tau2_request()
    request["messages"][3]["content"] = json.dumps(ACCOUNT, indent=4)
    result = apply_checked(make_change("B2"), request)
    assert result.applied
    system = result.request["messages"][0]["content"]
    assert "  " not in system and "\n\n\n" not in system and system == system.strip()
    assert result.request["messages"][2]["tool_calls"] == request["messages"][2]["tool_calls"]
    # JSON with whitespace inside a string value must keep its parsed meaning.
    assert json.loads(result.request["messages"][3]["content"]) == ACCOUNT
    assert result.request["messages"][6]["content"] == "I can help. Can you confirm your date of birth?"


def test_conformance_rejects_meaning_changes_for_benign_controls() -> None:
    request = tau2_request()
    spec = make_change("B1")
    result = apply_change(spec, request)
    messages = copy.deepcopy(result.request["messages"])
    messages[3]["content"] = json.dumps({**ACCOUNT, "user_id": "u_9"})
    broken = AppliedChange(request={**result.request, "messages": messages}, applied=True)
    assert check_conformance(spec, request, broken)
