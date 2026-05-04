# ContextCompress Attention Proxy — Design

**Status:** Validated, ready for implementation plan.
**Owner:** Avery Clapp
**Date:** 2026-05-03
**Related rule:** `crates/agentc-optimizer/src/rules/context_compress.rs`

## Problem

`ContextCompress` already reads `parameters.extra.attention_scores` (per-message
`f32`) and `parameters.extra.follow_on_tokens` (protected token list), and gates
on `prompt_bytes > 8KB` and `dead_fraction ≥ 0.30`. Nothing on the Python side
populates those fields, so the rule never fires. We need a cheap, principled
proxy that produces those fields end-to-end and a workload that exercises it.

## Approach (locked)

**Online token-overlap proxy**, two cases:

1. **Multi-turn** — when the current trace has prior spans in SQLite, score each
   input message by token-overlap with the union of prior spans' input + output
   tokens.
2. **Single-turn** — when there are no priors, score each input message by
   token-overlap with the most recent `role="user"` message of the current
   call.

`follow_on_tokens` = the salient signal token set itself (we forbid drops that
strip the question's own vocabulary).

## Workload — HotpotQA distractor

Single-turn QA. Each task ships 10 paragraphs (2 supporting, 8 distractor) +
gold answer. By construction distractors share few content words with the
question; supporting paragraphs share entities. This is the cleanest evaluation
of a "salient vs. dead" compression rule available off-the-shelf.

**Fixture** (`bench/fixtures/hotpot_distractor.json`, ~150 tasks, filtered to
total context > 6KB so ContextCompress has room to fire):

```json
{
  "tasks": [
    {
      "id": "hotpot_5a8b...",
      "prompt": "Were Scott Derrickson and Ed Wood of the same nationality?",
      "meta": {
        "paragraphs": [
          {"title": "...", "sentences": [...], "supporting": true},
          ... 9 more ...
        ],
        "gold_answer": "yes"
      }
    }
  ]
}
```

Source: `hotpot_qa` HF dataset, `distractor` config, `validation` split.

**Agent shape** (`bench/agents/hotpot_qa.py`): single `agentc.span("hotpot.answer")`,
single chat completion. System message + 10 paragraph-as-user messages + final
question-as-user message.

**Accuracy metric:** exact-match against `gold_answer` (lowercased, stripped).
Report `accuracy_delta_pp = baseline_em − agentc_em`. If `> 1.5pp` regression,
the rule does not ship.

## Proxy implementation

### Module layout

`python/agentc/_attention.py` — new file, single public entry point:

```python
def compute_attention_scores(
    messages: list[dict], trace_id: str | None
) -> tuple[list[float], list[str]]:
    salient = _salient_signal(messages, trace_id)
    if not salient:
        return [], []
    scores: list[float] = []
    for msg in messages:
        toks = _tokenize(msg.get("content", ""))
        if not toks:
            scores.append(1.0)  # protect structural messages
            continue
        scores.append(len(toks & salient) / len(toks))
    return scores, sorted(salient)
```

### Salient-signal selection

```python
def _salient_signal(messages, trace_id):
    if trace_id:
        prior = _prior_trace_tokens(trace_id)
        if prior:
            return prior
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return _tokenize(msg.get("content", ""))
    return set()
```

### Tokenization

`re.findall(r"\b[a-z0-9_]{3,}\b", text.lower())` minus a small (~50 word)
stopword set. Cheap, deterministic, no `tiktoken` dependency for the proxy.
The proxy doesn't need exact LLM token alignment — it scores at the *content
word* level, which is sufficient for compression decisions.

### SQLite read path

A new minimal PyO3 binding in `agentc-profiler`:

```rust
fn read_trace_content(trace_id: &str) -> PyResult<Vec<PyObject>>
// Returns rows with input_messages, output_messages JSON strings.
// Reads from the per-process active DB (multi-turn agents stay in one process).
```

Wrapped on the Python side with try/except — any FFI/schema issue falls
through to the single-turn signal cleanly.

### Per-call cost & memoization

Tokenizing prior spans every call is O(n_prior × avg_content). For a 20-step
agent with 4KB content per span (~80KB total) that is ~1ms in CPython, well
below noise. We still memoize:

```python
# Process-local, keyed by (trace_id, last_span_id_seen). Cap 64 entries.
_trace_token_cache: dict[tuple[str, str], frozenset[str]] = {}
```

Trace tokens grow monotonically within a trace, so we re-tokenize only spans
new since the last cache hit and union with the cached frozenset.

### Failure modes (all fail-open)

| Failure | Behavior |
|---|---|
| SQLite read fails | Fall back to single-turn salient. |
| Tokenizer raises | Log debug, return `([], [])` — rule won't fire. |
| `capture_content=False` (priors NULL) | Single-turn fallback (documented). |
| `get_current_span()` returns None | Single-turn fallback. |

We never let the proxy crash a user call.

## Integration point

In `python/agentc/_patches/_optimizer_glue.py::build_call_dict_openai`,
right after `input_deps` is built:

```python
trace_id = ctx.trace_id if (ctx := get_current_span()) else None
scores, follow_on = compute_attention_scores(messages, trace_id)
extra_obj["attention_scores"] = scores
extra_obj["follow_on_tokens"] = follow_on
extra_obj["message_deps"] = input_deps
extra_obj["window_state_reads"] = consume_state_reads()
```

Mirror the same wiring for the Anthropic glue when it lands.

## Tests

### Unit (`tests/test_attention.py`, ~8 tests)

- `_tokenize` strips stopwords, lowercases, ≥3-char filter.
- `_salient_signal` picks last user message when no trace history.
- `_salient_signal` picks prior-span union when trace has history.
- `compute_attention_scores` gives `1.0` to empty-content messages.
- HotpotQA-shape distractor paragraphs score ≤ 0.05.
- Supporting paragraphs score noticeably higher than distractors.
- SQLite-read failure → falls back to single-turn signal (no exception).
- Token cache invalidates when a new span lands in the trace.

### Integration (extend `tests/test_optimizer_glue.py`)

- `build_call_dict_openai` populates `parameters.extra.attention_scores`
  with `len(messages)` floats.
- `parameters.extra.follow_on_tokens` non-empty on a HotpotQA-shape call.
- `capture_content=False` still produces scores via single-turn fallback.

## Ablation

`bench/run_ablation.py` runs three arms on the 150-task HotpotQA fixture:

| Arm | ContextCompress | Other rules |
|---|---|---|
| `baseline` | off | off |
| `agentc-full` | on | on |
| `agentc-no-compress` | off | on |

Per-arm metrics:
- `total_input_tokens`, `total_output_tokens`, `total_cost_usd`
- `em_accuracy`, `accuracy_delta_pp` vs baseline
- `compression_fire_count`, `mean_compressed_fraction` (audit DB)

**Headline claim:** "ContextCompress reduces input tokens by N% on
HotpotQA-distractor with ≤M pp accuracy drop." Ship gate: M ≤ 1.5.

**Cost budget:** 3 arms × 150 tasks × ~$0.002/task ≈ $1.00. Under the
$5/experiment rule.

## Out of scope

- Real attention weights from an open-weight model (overkill for a proxy that
  just needs to identify obviously-dead context).
- Cross-process trace stitching for multi-turn signal — current design covers
  single-process agents, which is the realistic case for the workshop.
- Threshold sweep — ship `DEAD_ATTENTION_EPSILON=1e-4` and
  `MIN_DEAD_FRACTION=0.30` from the existing rule defaults; calibrate only
  if the headline number is borderline.
