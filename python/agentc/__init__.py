"""Agentc: JIT optimization runtime for multi-step LLM agent workloads.

Zero-config profiling via `agentc record -- python my_agent.py`.
Programmatic usage via `agentc.init()` and `@agentc.trace`.
"""

from __future__ import annotations

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

# Import memoization API.
from agentc._memoize import cache_invalidate  # noqa: F401
from agentc._memoize import cache_invalidate_all  # noqa: F401
from agentc._memoize import memoize  # noqa: F401

# Import optimizer API.
from agentc._optimizer import Plan  # noqa: F401
from agentc._optimizer import model_catalog  # noqa: F401
from agentc._optimizer import observe_outcome  # noqa: F401
from agentc._optimizer import plan_call  # noqa: F401
from agentc._executor import dispatch  # noqa: F401
from agentc._intercept import intercept  # noqa: F401
from agentc._optimization_scope import optimization_scope  # noqa: F401
from agentc._optimization_scope import optimization_scope_report  # noqa: F401
from agentc._application_version import application_version  # noqa: F401

# Provenance / state tagging surface used by ParallelBranch and StateDrop.
from agentc._provenance import State  # noqa: F401
from agentc._provenance import state_read  # noqa: F401
from agentc._provenance import state_write  # noqa: F401

# Driver-style fan-out helper. Replaces a serial list comprehension
# with a thread-pool dispatch that exposes the fan-out to
# ``ParallelBranchRule``.
from agentc._parallel import parallel_map  # noqa: F401
from agentc._batch_observation import ParallelObservation  # noqa: F401
from agentc._batch_observation import ParallelComparison  # noqa: F401
from agentc._batch_observation import compare_parallel_observations  # noqa: F401
from agentc._batch_observation import refresh_parallel_observation  # noqa: F401
from agentc._completion_batch import BatchPolicy  # noqa: F401
from agentc._completion_batch import BatchSchedule  # noqa: F401
from agentc._completion_batch import CompletionRequest  # noqa: F401
from agentc._completion_batch import completion_batch  # noqa: F401
from agentc._billing import BillingReconciliation  # noqa: F401
from agentc._billing import ProviderBillingReceipt  # noqa: F401
from agentc._billing import provider_billing_receipts  # noqa: F401
from agentc._billing import reconcile_openrouter_generation  # noqa: F401
from agentc._provider_tariff import TariffAccounting  # noqa: F401
from agentc._provider_tariff import tariff_accounting  # noqa: F401
from agentc._provider_tariff import provider_tariff_report  # noqa: F401
from agentc._provider_tariff import provider_tariff_plan_report  # noqa: F401
from agentc._provider_tariff import provider_original_execution  # noqa: F401
from agentc._provider_tariff import provider_original_quality_report  # noqa: F401
from agentc._provider_tariff import compare_original_provider_executions  # noqa: F401

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
    "memoize",
    "cache_invalidate",
    "cache_invalidate_all",
    "Plan",
    "model_catalog",
    "plan_call",
    "observe_outcome",
    "dispatch",
    "intercept",
    "optimization_scope",
    "optimization_scope_report",
    "application_version",
    "parallel_map",
    "ParallelObservation",
    "refresh_parallel_observation",
    "ParallelComparison",
    "compare_parallel_observations",
    "BatchPolicy",
    "BatchSchedule",
    "CompletionRequest",
    "completion_batch",
    "BillingReconciliation",
    "ProviderBillingReceipt",
    "provider_billing_receipts",
    "reconcile_openrouter_generation",
    "TariffAccounting",
    "tariff_accounting",
    "provider_tariff_report",
    "provider_tariff_plan_report",
    "provider_original_execution",
    "provider_original_quality_report",
    "compare_original_provider_executions",
    "State",
    "state_read",
    "state_write",
]
