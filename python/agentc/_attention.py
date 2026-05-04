"""Online token-overlap attention proxy for ContextCompress.

The optimizer's ``ContextCompress`` rule reads two fields from
``parameters.extra``:

- ``attention_scores`` — one ``f32`` per input message. Scores ≤
  ``DEAD_ATTENTION_EPSILON`` (1e-4) are drop candidates.
- ``follow_on_tokens`` — tokens the rule must preserve in the output.
  Subsequent compression must keep at least one occurrence of each.

This module computes both from a cheap signal: token-overlap with the
"salient signal" of the current call, which is

1. **Multi-turn** — when the current trace already has spans in the
   per-process store, we union their decoded input/output tokens. The
   intuition: anything the agent has already said is what it'll keep
   referring to.
2. **Single-turn** — when there are no priors, the most recent
   ``role="user"`` message of the current call is the salient signal.
   For QA workloads (HotpotQA, RAG) this is the question itself, which
   is precisely the thing distractor paragraphs *don't* share tokens
   with.

Tokenization is intentionally cheap (regex word tokens, ≥3 chars,
lowercased, minus a small stopword set). The proxy doesn't need exact
LLM-token alignment — it scores at the *content word* level, which is
what compression decisions actually hinge on.

Failure modes are all fail-open: SQLite read errors, tokenizer
exceptions, missing trace context — every one falls through to a sane
default rather than raising. We never break a user call because the
proxy hit a snag.
"""

from __future__ import annotations

import logging
import math
import re
from collections import OrderedDict
from typing import Any

log = logging.getLogger("agentc.attention")

# ~50 high-frequency English words. Cheap heuristic — proper stopword
# lists are language-specific and the proxy doesn't need that level of
# rigor; we just want to keep "the/and/of" from inflating overlap.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "are", "but", "not", "you", "all", "can",
        "had", "her", "was", "one", "our", "out", "day", "get", "has",
        "him", "his", "how", "man", "new", "now", "old", "see", "two",
        "way", "who", "boy", "did", "its", "let", "put", "say", "she",
        "too", "use", "with", "this", "that", "they", "from", "have",
        "what", "when", "your", "which", "their", "would", "there",
        "could", "other", "some", "than", "then", "them", "these",
        "into", "also", "been", "were", "will", "more", "most", "such",
        "only", "very", "much", "such", "just", "any", "way", "may",
        "any", "should", "where", "after", "before", "while",
    }
)

_TOKEN_RE = re.compile(r"\b[a-z0-9_]{3,}\b")

# Process-local cache for prior-trace tokens. Trace tokens grow
# monotonically within a trace, so we key on (trace_id, last_span_id_seen)
# and re-tokenize only spans new since the last hit. Cap is small —
# we expect at most a few concurrent traces per process.
_CACHE_CAP = 64
_trace_token_cache: "OrderedDict[tuple[str, str], frozenset[str]]" = OrderedDict()


def _tokenize(text: str) -> set[str]:
    """Return the lowercase content-word token set for ``text``.

    Tokens are 3+ alnum/underscore runs; stopwords are stripped.
    Returns ``set()`` on any failure — the caller treats an empty set as
    "no signal" and skips scoring.
    """
    if not text:
        return set()
    try:
        toks = _TOKEN_RE.findall(text.lower())
    except (TypeError, AttributeError):
        return set()
    return {t for t in toks if t not in _STOPWORDS}


def _last_user_tokens(messages: list[dict[str, Any]]) -> set[str]:
    """Tokenize the most recent ``role="user"`` message in the call."""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            return _tokenize(str(msg.get("content", "")))
    return set()


def _read_prior_spans(trace_id: str) -> list[dict[str, Any]]:
    """Pull span content rows for ``trace_id`` from the per-process DB.

    Wraps the FFI call so any import / lock / SQLite error fails open.
    """
    try:
        from agentc import _native
    except ImportError:
        return []
    try:
        return _native.read_trace_content(trace_id) or []
    except BaseException:
        log.debug("read_trace_content failed (suppressed)", exc_info=True)
        return []


def _prior_trace_tokens(trace_id: str) -> set[str]:
    """Decode + tokenize prior-span content for ``trace_id``.

    Cached on ``(trace_id, last_span_id)`` so repeat calls within the
    same trace don't re-decompress and re-tokenize the whole history.
    """
    rows = _read_prior_spans(trace_id)
    if not rows:
        return set()
    last_id = rows[-1].get("span_id", "")
    cache_key = (trace_id, str(last_id))
    cached = _trace_token_cache.get(cache_key)
    if cached is not None:
        _trace_token_cache.move_to_end(cache_key)
        return set(cached)

    out: set[str] = set()
    for row in rows:
        for field in ("input_messages", "output_messages"):
            blob = row.get(field)
            if not blob:
                continue
            try:
                parsed = _decode_messages(blob)
            except (ValueError, TypeError):
                continue
            for msg in parsed:
                if isinstance(msg, dict):
                    out |= _tokenize(str(msg.get("content", "")))

    _trace_token_cache[cache_key] = frozenset(out)
    if len(_trace_token_cache) > _CACHE_CAP:
        _trace_token_cache.popitem(last=False)
    return out


def _decode_messages(raw: Any) -> list[Any]:
    """Decode an ``input_messages`` / ``output_messages`` field.

    The native side may return either a JSON string (uncompressed) or a
    pre-parsed list. Returns ``[]`` on any decode error.
    """
    import json

    if isinstance(raw, list):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        return json.loads(raw)
    return []


def _salient_signal(messages: list[dict[str, Any]], trace_id: str | None) -> set[str]:
    """Pick the salient signal: prior-trace union if available, else the
    most recent user message of the current call."""
    if trace_id:
        prior = _prior_trace_tokens(trace_id)
        if prior:
            return prior
    return _last_user_tokens(messages)


def compute_attention_scores(
    messages: list[dict[str, Any]],
    trace_id: str | None,
) -> tuple[list[float], list[str]]:
    """Compute per-message attention scores and a follow-on token list.

    Returns ``(scores, follow_on)``:

    - ``scores[i]`` ∈ [0, 1] is the fraction of content-word tokens in
      ``messages[i].content`` that overlap with the salient signal.
      Empty / structural messages get 1.0 (protected from drop).
    - ``follow_on`` is the salient token set, sorted, used by the rule
      to forbid drops that strip the question's own vocabulary.

    Returns ``([], [])`` when no salient signal can be derived — the
    optimizer will see the empty arrays and refuse to fire the rule.
    """
    salient = _salient_signal(messages, trace_id)
    if not salient:
        return [], []

    # --- First pass: tokenize all messages and compute per-token document
    # frequency so we can IDF-weight the overlap score.  Tokens that appear
    # in many messages (e.g. "director" across 10 HotpotQA paragraphs) are
    # weak discriminators; IDF downweights them relative to tokens unique to
    # a few messages ("scottish", "1960") which are strong discriminators.
    per_msg_tokens: list[set[str]] = []
    doc_freq: dict[str, int] = {}
    n_docs = 0
    for msg in messages:
        if not isinstance(msg, dict):
            per_msg_tokens.append(set())
            continue
        toks = _tokenize(str(msg.get("content", "")))
        per_msg_tokens.append(toks)
        if toks:
            n_docs += 1
            for tok in toks:
                doc_freq[tok] = doc_freq.get(tok, 0) + 1

    if n_docs == 0:
        return [1.0] * len(messages), []

    # Add-1 smoothed IDF: log((n+1)/(df+1)) + 1.  Unknown tokens receive
    # maximum weight; tokens in every document approach weight 1.0.
    def _idf(tok: str) -> float:
        return math.log((n_docs + 1) / (doc_freq.get(tok, 0) + 1)) + 1.0

    salient_idf_total = sum(_idf(t) for t in salient)
    if salient_idf_total == 0.0:
        return [1.0] * len(messages), []

    # --- Second pass: score = IDF-weighted overlap / salient_idf_total.
    # A message that contains ALL salient tokens scores 1.0 (the question
    # message itself in single-turn mode); a distractor that only shares
    # high-frequency tokens scores near zero.
    scores: list[float] = []
    for msg, toks in zip(messages, per_msg_tokens):
        if not isinstance(msg, dict):
            scores.append(1.0)
            continue
        if not toks:
            scores.append(1.0)
            continue
        w_overlap = sum(_idf(t) for t in toks if t in salient)
        scores.append(w_overlap / salient_idf_total)

    # Follow-on tokens: drop tokens that appear in more than half the
    # messages — they are not discriminative and would protect everything.
    # Skip the filter for very short calls where we lack signal.
    n_msgs = len(messages)
    if n_msgs < 4:
        follow_on = sorted(salient)
    else:
        threshold = n_msgs // 2
        follow_on = [
            tok for tok in sorted(salient)
            if sum(1 for s in per_msg_tokens if tok in s) <= threshold
        ]
    return scores, follow_on


def _clear_cache() -> None:
    """Test hook — drop cached trace tokens between fixtures."""
    _trace_token_cache.clear()
