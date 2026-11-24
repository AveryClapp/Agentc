"""Label-free call-level signals S1 to S5 (protocol Section 6.1).

Every signal compares a reference response with a forked response for the
same call and is a divergence in [0, 1]: 0 means no observable difference.
``None`` means the signal cannot be computed (for example a provider error,
or S3 without the embedding model) and must never be read as agreement.

S1 to S3 reuse the optimizer's existing metrics so that study values match
what the production guard measures:

- S1 ``lexical``: 1 - Jaccard over whitespace tokens.
- S2 ``normalized``: 1 - Jaccard after case, punctuation and article removal.
- S3 ``embedding``: cosine distance between model2vec embeddings.

Like the production guard, S1 to S3 score a response that contains tool calls
only as identical (0) or different (1).

S4 and S5 are new:

- S4 action disagreement. Two text replies agree (their wording is S1 to S3's
  job). A text reply and a tool call disagree completely. Two tool-call
  responses disagree completely unless they call the same multiset of tools;
  then S4 is 1 minus the mean per-call fraction of top-level argument fields
  whose canonical values match.
- S5 termination and length. 1 when exactly one response ends the
  conversation (a stop or transfer action) or exactly one was cut off at the
  output limit; otherwise ``min(1, |log2((o_f + 1) / (o_b + 1))| / 2)`` over
  output token counts, so a fourfold length change scores 1.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from agentc.change_safety.records import NormalizedResponse, SignalValues

#: Metric versions recorded with every study, matching the optimizer's names.
METRIC_VERSIONS = {
    "S1": "1",
    "S2": "normalized-jaccard-v2",
    "S3": "embedding-cosine-normalized-jaccard-v2",
    "S4": "action-agreement-v1",
    "S5": "termination-length-v1",
}

#: tau2 ``llm_agent`` end-of-conversation markers. Other workloads pass their own.
DEFAULT_END_TOOLS = frozenset({"transfer_to_human_agents", "done"})
DEFAULT_END_TOKENS = ("###STOP###",)


@dataclass(frozen=True)
class TerminationMarkers:
    end_tools: frozenset[str] = DEFAULT_END_TOOLS
    end_tokens: tuple[str, ...] = DEFAULT_END_TOKENS

    def ends(self, response: NormalizedResponse) -> bool:
        if any(action.name in self.end_tools for action in response.tool_calls):
            return True
        return any(token in response.text for token in self.end_tokens)


def _jaccard_divergence(sa: set[str], sb: set[str]) -> float:
    if not sa and not sb:
        return 0.0
    if not sa or not sb:
        return 1.0
    return 1.0 - len(sa & sb) / len(sa | sb)


def lexical_divergence(a: str, b: str) -> float:
    return _jaccard_divergence(set(a.split()), set(b.split()))


def normalized_divergence(a: str, b: str) -> float:
    from agentc._patches._optimizer_glue import _normalize_tokens

    return _jaccard_divergence(_normalize_tokens(a), _normalize_tokens(b))


def embedding_divergence(a: str, b: str) -> float | None:
    """Cosine distance, or ``None`` when the embedding model is unavailable.

    Unlike the production guard, the study never falls back to another metric:
    a silent fallback would mix two signals under one name.
    """
    from agentc._patches._optimizer_glue import _cosine_distance_from_bytes

    try:
        from agentc._native import embed_text_bytes

        ba, bb = embed_text_bytes(a), embed_text_bytes(b)
    except BaseException:
        return None
    if ba is None or bb is None:
        return None
    return min(1.0, max(0.0, _cosine_distance_from_bytes(bytes(ba), bytes(bb))))


def _same_action(a: NormalizedResponse, b: NormalizedResponse) -> bool:
    return a.tool_calls == b.tool_calls and a.text == b.text


def _arguments(arguments_json: str) -> Any:
    try:
        return json.loads(arguments_json)
    except ValueError:
        return arguments_json


def _field_agreement(a_json: str, b_json: str) -> float:
    a, b = _arguments(a_json), _arguments(b_json)
    if not isinstance(a, dict) or not isinstance(b, dict):
        return 1.0 if a == b else 0.0
    keys = set(a) | set(b)
    if not keys:
        return 1.0
    same = sum(
        1
        for key in keys
        if key in a
        and key in b
        and json.dumps(a[key], sort_keys=True) == json.dumps(b[key], sort_keys=True)
    )
    return same / len(keys)


def action_disagreement(
    a: NormalizedResponse, b: NormalizedResponse
) -> tuple[float | None, dict[str, Any]]:
    if a.kind == "error" or b.kind == "error":
        return None, {"reason": "error"}
    if a.kind != b.kind:
        return 1.0, {"type_match": False}
    if a.kind == "text":
        return 0.0, {"type_match": True}
    names_a = Counter(action.name for action in a.tool_calls)
    names_b = Counter(action.name for action in b.tool_calls)
    if names_a != names_b:
        return 1.0, {"type_match": True, "names_match": False}
    # Pair calls to the same tool in emitted order.
    pending: dict[str, list[str]] = {}
    for action in b.tool_calls:
        pending.setdefault(action.name, []).append(action.arguments_json)
    scores = [
        _field_agreement(action.arguments_json, pending[action.name].pop(0))
        for action in a.tool_calls
    ]
    agreement = sum(scores) / len(scores)
    return 1.0 - agreement, {
        "type_match": True,
        "names_match": True,
        "argument_agreement": agreement,
    }


def termination_divergence(
    a: NormalizedResponse,
    b: NormalizedResponse,
    markers: TerminationMarkers = TerminationMarkers(),
) -> tuple[float | None, dict[str, Any]]:
    if a.kind == "error" or b.kind == "error":
        return None, {"reason": "error"}
    ends_a, ends_b = markers.ends(a), markers.ends(b)
    cut_a, cut_b = a.finish_reason == "length", b.finish_reason == "length"
    components: dict[str, Any] = {
        "termination_differs": ends_a != ends_b,
        "truncation_differs": cut_a != cut_b,
    }
    if ends_a != ends_b or cut_a != cut_b:
        return 1.0, components
    if a.output_tokens is None or b.output_tokens is None:
        return None, {**components, "reason": "missing_output_tokens"}
    ratio = abs(math.log2((b.output_tokens + 1) / (a.output_tokens + 1)))
    components["output_token_log2_ratio"] = ratio
    return min(1.0, ratio / 2.0), components


def compute_signals(
    reference: NormalizedResponse,
    fork: NormalizedResponse | None,
    *,
    markers: TerminationMarkers = TerminationMarkers(),
    embed: bool = True,
) -> SignalValues:
    """All five signals for one reference/fork pair."""
    if fork is None or reference.kind == "error" or fork.kind == "error":
        return SignalValues(components={"reason": "error_or_missing_fork"})
    s4, s4_parts = action_disagreement(reference, fork)
    s5, s5_parts = termination_divergence(reference, fork, markers)
    if reference.kind == "tool_calls" or fork.kind == "tool_calls":
        structured = 0.0 if _same_action(reference, fork) else 1.0
        s1: float | None = structured
        s2: float | None = structured
        s3: float | None = structured
    else:
        s1 = lexical_divergence(reference.text, fork.text)
        s2 = normalized_divergence(reference.text, fork.text)
        s3 = embedding_divergence(reference.text, fork.text) if embed else None
    return SignalValues(
        S1=s1, S2=s2, S3=s3, S4=s4, S5=s5, components={"S4": s4_parts, "S5": s5_parts}
    )


SignalFunction = Callable[[NormalizedResponse, NormalizedResponse | None], SignalValues]

__all__ = [
    "DEFAULT_END_TOKENS",
    "DEFAULT_END_TOOLS",
    "METRIC_VERSIONS",
    "SignalFunction",
    "TerminationMarkers",
    "action_disagreement",
    "compute_signals",
    "embedding_divergence",
    "lexical_divergence",
    "normalized_divergence",
    "termination_divergence",
]
