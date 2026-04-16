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
) -> dict[str, Any] | None:
    """Look up a memoized response by exact-hash cache key.

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
) -> None:
    """Insert a memoization entry.

    Writes output_bytes into the shared output_content table and records the
    cache row in memoization_cache. Fails open on any internal error.
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

def canonicalize_prompt_bytes(prompt_json: bytes, provider: str) -> bytes:
    """Canonicalize a prompt via the Rust mirror adapter.

    Accepts JSON-encoded bytes and returns canonical UTF-8 JSON bytes.
    Exists for parity tests against the Python canonicalizer.
    """
    ...

def canonicalize_parameters_bytes(params_json: bytes) -> bytes:
    """Canonicalize parameters via the Rust mirror adapter."""
    ...
