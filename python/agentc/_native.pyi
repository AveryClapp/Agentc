"""Type stubs for the native Rust extension module (agentc._native)."""

from typing import Any

__version__: str

def write_span(span_dict: dict[str, Any]) -> None:
    """Write a single span to the native storage layer.

    Required keys: span_id, trace_id, name, kind, start_time.
    Optional keys: parent_span_id, end_time, status, model, provider,
        input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
        attributes, input_messages, output_messages.

    Raises:
        TypeError: If span_dict is not a dict.
        ValueError: If a required key is missing.
    """
    ...

def create_db(
    path: str,
    is_canonical: bool = False,
    capture_content: bool = True,
    capture_embeddings: bool = True,
) -> None:
    """Create or open a SQLite database at the given path.

    Creates the schema (spans, input_content, output_content tables) if new.
    If is_canonical is True, also creates the traces VIEW.
    """
    ...

def query_spans_by_trace(db_path: str, trace_id: str) -> list[dict[str, Any]]:
    """Query all spans for a given trace_id from a SQLite database.

    Returns a list of dicts, each representing a span.
    """
    ...

def read_trace_content(trace_id: str) -> list[dict[str, Any]]:
    """Read prior-span content for a trace from the per-process active DB.

    Returns rows ordered by start_time ascending with keys: span_id, trace_id,
    parent_span_id, start_time, input_messages (decompressed JSON string or
    None), output_messages (decompressed JSON string or None). Used by the
    attention proxy to build a multi-turn salient signal. Fail-open: returns
    [] when the per-process DB is not open.
    """
    ...

def merge_all_pending() -> dict[str, int]:
    """Merge pending per-process DBs into the canonical traces.db.

    Returns a dict with keys spans_merged, input_content_merged,
    output_content_merged. On non-unix platforms returns a zeroed dict.
    """
    ...

def cache_lookup(
    prompt_hash: bytes,
    model: str,
    parameters_hash: bytes,
    call_site_id: str,
    embedding: bytes | None = None,
    similarity: float | None = None,
) -> dict[str, Any] | None:
    """Look up a memoized response.

    Tries exact-hash first; if `embedding` is supplied and `similarity < 1.0`,
    falls back to LSH candidate retrieval with cosine rerank.

    Returns None on miss, error, or when memoization is not initialized.
    Hit dict keys: output_content_id, input_tokens, output_tokens,
    recorded_cost_usd, age_micros, source ('exact' or 'lsh'),
    similarity (LSH hits only).
    """
    ...

def cache_insert(
    prompt_hash: bytes,
    model: str,
    parameters_hash: bytes,
    call_site_id: str,
    output_bytes: bytes,
    input_tokens: int,
    output_tokens: int,
    recorded_cost_usd: float,
    ttl_seconds: int,
    embedding: bytes | None = None,
) -> None:
    """Insert a memoization entry.

    Writes output_bytes into the shared output_content table and records the
    cache row in memoization_cache. When `embedding` is provided (256 × f32
    little-endian bytes) also writes the 8-band LSH index + raw embedding
    atomically. Fails open on any internal error.
    """
    ...

def cache_invalidate(pattern: str) -> int:
    """Delete cache entries matching a SQL GLOB pattern on call_site_id.

    Pass '*' to wipe the whole cache. Returns the number of rows removed.
    """
    ...

def cache_stats() -> dict[str, int | float]:
    """Return aggregate cache statistics.

    Keys: entries, total_hits, estimated_savings_usd, bytes_on_disk.
    """
    ...

def cache_maintenance(max_entries: int = 0) -> dict[str, int | bool]:
    """Run TTL sweep + LRU eviction + opportunistic VACUUM.

    `max_entries` of 0 disables the LRU cap (the caller relies solely on TTL).
    Returns {ttl_rows, lru_rows, vacuumed}.
    """
    ...

def output_content_load(content_id: str) -> bytes | None:
    """Load a row from the shared `output_content` table by content_id.

    Returns None if the row is missing or the DB is not open.
    """
    ...

def embed_text_bytes(text: str) -> bytes | None:
    """Embed `text` into 256 × f32 little-endian bytes.

    Returns None if the embedder is unavailable. Used by the memoize decorator
    to compute the LSH query embedding.
    """
    ...

def canonicalize_prompt_bytes(prompt_json: bytes, provider: str) -> bytes:
    """Canonicalize a prompt via the Rust mirror adapter.

    Accepts JSON-encoded bytes and returns canonical UTF-8 JSON bytes.
    Exists for parity tests against the Python canonicalizer.
    """
    ...

def canonicalize_parameters_bytes(params_json: bytes) -> bytes:
    """Canonicalize parameters via the Rust mirror adapter."""
    ...

def optimize_configure(storage_path: str) -> str:
    """Build a fresh native optimizer rooted at ``storage_path``.

    Flushes and replaces any optimizer configured by an earlier Agentc
    lifecycle, and returns the path owned by the new native state.
    """
    ...

def optimize_storage_path() -> str:
    """Return the storage path owned by the current native optimizer."""
    ...

def optimize_reset() -> None:
    """Flush and drop the current optimizer so a later init can reconfigure."""
    ...

def optimize_plan(call_json: str) -> str:
    """Run the optimizer for one intercepted Call and return a JSON Plan.

    Fail-open: on any Rust panic, deserialization error, or misconfiguration
    the native side returns '{"kind":"pass_through"}'. The caller should
    treat that as "just run the original call."
    """
    ...

def optimize_observe(plan_json: str, outcome_json: str) -> None:
    """Feed an Outcome back into the cost model after a Plan dispatch.

    `plan_json` is the JSON that `optimize_plan` returned. `outcome_json`
    carries input_tokens / output_tokens / latency_ms / cost_usd /
    output_is_structured / output_is_short. Internally fail-open — any
    error is dropped.
    """
    ...

def optimize_record_divergence(call_site_id: str, rule: str, divergence: float) -> None:
    """Record a shadow-mode divergence sample for a rule at a call site.

    Feeds the accuracy budget: five consecutive over-threshold samples for
    `rule` at `call_site_id` disable the rule there. The cumulative estimate
    and current breach streak persist across lifecycle restarts. Non-finite or
    out-of-range divergence is discarded without mutating guard state.
    Internally fail-open — any error is dropped.
    """
    ...

def optimize_flush() -> None:
    """Flush buffered cost-model and guard-divergence writes to disk.

    Called on shutdown. Internally fail-open — any error is dropped.
    """
    ...
