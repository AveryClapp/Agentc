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
    """Load payload bytes from the active process or merged canonical store.

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

def optimize_configure(storage_path: str, config_path: str | None = None) -> str:
    """Build a fresh native optimizer rooted at ``storage_path``.

    Flushes and replaces any optimizer configured by an earlier Agentc
    lifecycle, and returns the path owned by the new native state.
    """
    ...

def optimize_configuration() -> str:
    """Inspect resolved optimizer settings and content-free invalid-config diagnostics."""
    ...

def optimize_text_action_comparator_version() -> str:
    """Return the compiled text-action identity/lease contract version."""
    ...

def optimize_storage_path() -> str:
    """Return the storage path owned by the current native optimizer."""
    ...

def optimize_evaluation_policy_version() -> str:
    """Frozen request-policy schema supported by the compiled executor."""
    ...

def optimize_provider_configuration_version() -> str:
    """Provider request settings understood by native identity and admission."""
    ...

def optimize_application_version() -> str:
    """Application revision declaration understood by native plan evidence."""
    ...

def optimize_divergence_metric_version(mode: str) -> str:
    """Identity version of the output metric, including embedding fallback."""
    ...

def optimize_output_validation_version() -> str:
    """Experimental pre-release validator identity understood by native plans."""
    ...

def optimize_record_output_validation_rejection(plan_json: str, contract_json: str, execution_id: str) -> bool:
    """Durably record one exact-plan rejection without adding reference pairs."""
    ...

def optimize_sdk_response_cache_key(call_json: str) -> str | None:
    """Project an eligible SDK request to its versioned native cache key JSON."""
    ...

def optimize_evaluation_planner_mode() -> str:
    """Return the evaluation mode frozen at native optimizer configuration.

    Values include joint_guarded (default), current_greedy, best_static_joint,
    route_then_rewrite, rewrite_then_route, and invalid. Frozen controls require
    their matching request declaration and never explore complete plans.
    """
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

def optimize_model_catalog() -> str:
    """Return the active versioned model catalog as JSON."""
    ...

def optimize_observe_multi_call(receipt_json: str) -> str:
    """Publish complete batch economics or retain originals awaiting billing."""
    ...

def optimize_original_provider_call(call_json: str) -> str:
    """Issue an observation identity for an untouched call, without planning."""
    ...

def optimize_record_provider_receipt(record_json: str) -> str:
    """Persist provider accounting or refresh an existing batch checkpoint."""
    ...

def optimize_compare_multi_call(reference_token: str, candidate_token: str) -> str:
    """Compare two complete, bound executions and charge their acquisition once."""
    ...

def optimize_compare_provider_graph(reference_token: str, candidate_token: str) -> str:
    """Compare saved complete graph results once, independently of billing."""
    ...

def optimize_provider_graph_sampling(request_json: str) -> str:
    """Issue bounded prospective graph sampling or finish its original pair."""
    ...

def optimize_discover_provider_graph_node(lease_id: str, position_json: str, source_json: str, observation_only: bool) -> str:
    """Issue and bind a native cold discovery instruction before SDK dispatch."""
    ...

def optimize_compare_provider_role_graph(reference_execution_id: str, candidate_execution_id: str) -> str:
    """Compare complete role-graph results under the live native quality policy."""
    ...

def optimize_begin_provider_graph(request_json: str) -> str:
    """Record a native pre-dispatch workflow start and optional frozen graph."""
    ...

def optimize_approve_provider_graph_node(lease_id: str, position_json: str, source_json: str, plan_json: str, request_sha256: str | None = None, transport_sha256: str | None = None) -> str:
    """Bind one SDK node and guarded request before its physical dispatch."""
    ...

def optimize_describe_provider_graph_node(position_json: str, source_json: str, plan_json: str) -> str:
    """Describe one issued physical plan without dispatch or admission."""
    ...

def optimize_materialize_provider_graph_node(lease_id: str, position_json: str, source_json: str, observation_only: bool) -> str:
    """Materialize the exact frozen graph plan for an upcoming SDK request."""
    ...

def optimize_prepare_provider_batch(batch_json: str, options_json: str) -> str:
    """Prepare immutable originals and admit only a fully qualified batch schedule."""
    ...

def optimize_observe(plan_json: str, outcome_json: str) -> str:
    """Feed an Outcome back and return an opaque observation token.

    `plan_json` is the JSON that `optimize_plan` returned. `outcome_json`
    carries input_tokens / output_tokens / latency_ms / cost_usd /
    output_is_structured / output_is_short. The returned token binds the exact
    plan profile, runtime version, and execution sequence. An empty string
    means observation failed open.
    """
    ...

def optimize_record_divergence(observation_token: str, divergence: float) -> None:
    """Attach a shadow divergence to an exact observed execution.

    The token is returned by `optimize_observe` and is opaque to Python.
    Duplicate token/value pairs are idempotent; stale or mismatched bindings
    and non-finite or out-of-range divergence are discarded. Solo plans also
    feed the compatibility rule guard. Internally fail-open.
    """
    ...

def optimize_complete_exploration(
    lease_token: str,
    outcome_json: str,
    divergence: float,
) -> bool:
    """Commit a leased candidate outcome and paired divergence.

    The token is opaque and binds the outcome to one durable exploration
    attempt and exact execution-plan profile. Returns False on invalid input
    or persistence failure and never raises internally.
    """
    ...

def optimize_fail_exploration(lease_token: str) -> bool:
    """Mark a leased counterfactual failed while retaining its spend attempt."""
    ...

def optimize_flush() -> None:
    """Flush buffered cost-model and guard-divergence writes to disk.

    Called on shutdown. Internally fail-open — any error is dropped.
    """
    ...
