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

def create_db(path: str, is_canonical: bool = False) -> None:
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
