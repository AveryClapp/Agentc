"""Agentc: JIT optimization runtime for multi-step LLM agent workloads.

Zero-config profiling via `agentc record -- python my_agent.py`.
Programmatic usage via `agentc.init()` and `@agentc.trace`.
"""

from __future__ import annotations

from typing import Any

# Import the native Rust extension module.
from agentc._native import __version__  # noqa: F401
from agentc._native import create_db  # noqa: F401
from agentc._native import query_spans_by_trace  # noqa: F401
from agentc._native import write_span  # noqa: F401

# Import lifecycle management.
from agentc._lifecycle import init  # noqa: F401
from agentc._lifecycle import is_initialized  # noqa: F401
from agentc._lifecycle import shutdown  # noqa: F401

# Import span API.
from agentc._span import trace  # noqa: F401
from agentc._span import span_context as span  # noqa: F401

# Import propagation API.
from agentc._propagation import traced_executor  # noqa: F401
from agentc._propagation import get_trace_context  # noqa: F401
from agentc._propagation import attach_trace_context  # noqa: F401
from agentc._propagation import inject_trace_headers  # noqa: F401

__all__ = [
    "__version__",
    "init",
    "shutdown",
    "is_initialized",
    "trace",
    "span",
    "traced_executor",
    "get_trace_context",
    "attach_trace_context",
    "inject_trace_headers",
    "write_span",
    "create_db",
    "query_spans_by_trace",
]
