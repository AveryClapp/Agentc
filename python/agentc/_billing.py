"""Verify provider bills without replaying inference or changing quality evidence.

Only the OpenRouter generation metadata endpoint is supported. Callers keep
raw generation IDs locally; durable receipts and invoices contain hashes.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

from agentc._provider_receipts import (
    _active_http,
    _billing_scope_fingerprint,
    _provider_identity_fingerprint,
)

_BASE_URL = "https://openrouter.ai/api/v1"
_ISSUER = "openrouter-generation-metadata-v1"


@dataclass(frozen=True)
class ProviderBillingReceipt:
    batch_id: str
    invocation_id: str
    receipt_sha256: str
    provider_protocol: str
    requested_model_id: str
    model_version: str | None
    billing_scope_sha256: str | None
    generation_sha256: str | None
    request_id_sha256: str | None
    input_tokens: int | None
    output_tokens: int | None
    settled_cost_usd: float | None


@dataclass(frozen=True)
class BillingReconciliation:
    status: str
    cost_usd: float | None = None
    reason: str | None = None


def _record(operation: str, **data: Any) -> dict[str, Any]:
    from agentc import _native

    result: dict[str, Any] = json.loads(
        _native.optimize_record_provider_receipt(
            json.dumps({"operation": operation, **data}, allow_nan=False)
        )
    )
    return result


def provider_billing_receipts(batch_id: str) -> tuple[ProviderBillingReceipt, ...]:
    """Return terminal, content-free billing identities for one physical batch."""
    result = _record("billing_bindings", batch_id=batch_id)
    if result.get("status") != "recorded":
        raise RuntimeError("provider billing receipt store is unavailable")
    return tuple(ProviderBillingReceipt(**row) for row in result["bindings"])


def _invoice(
    data: Any, receipt: ProviderBillingReceipt, generation_id: str
) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("id") != generation_id:
        raise ValueError("generation identity mismatch")
    if data.get("is_byok") is not False:
        raise ValueError("BYOK billing is unsupported")
    cancelled = data.get("cancelled")
    finish_reason = data.get("finish_reason")
    if type(cancelled) is not bool or (
        cancelled is False and (not isinstance(finish_reason, str) or not finish_reason)
    ):
        raise ValueError("provider generation is not terminal")
    cost = data.get("total_cost")
    if (
        not isinstance(cost, (int, float))
        or isinstance(cost, bool)
        or not math.isfinite(cost)
        or cost < 0
    ):
        raise ValueError("provider bill is invalid")
    counts = [data.get("native_tokens_prompt"), data.get("native_tokens_completion")]
    if any(type(value) is not int or not 0 <= value < 2**32 for value in counts):
        raise ValueError("provider token counts are invalid")
    request_hash = _provider_identity_fingerprint(data.get("request_id"))
    model = data.get("model")
    if not isinstance(model, str) or not model or len(model) > 256:
        raise ValueError("provider model identity is invalid")
    return {
        "issuer": _ISSUER,
        "receipt_sha256": receipt.receipt_sha256,
        "billing_scope_sha256": receipt.billing_scope_sha256,
        "generation_sha256": _provider_identity_fingerprint(generation_id),
        "request_id_sha256": request_hash,
        "model_id": model,
        "input_tokens": counts[0],
        "output_tokens": counts[1],
        "cost_usd": float(cost),
        "cancelled": cancelled,
        "finish_reason": finish_reason,
    }


def reconcile_openrouter_generation(
    receipt: ProviderBillingReceipt, *, generation_id: str, client: Any
) -> BillingReconciliation:
    """Read authenticated generation metadata and atomically settle its cost.

    Use the same standard synchronous OpenAI client configured for OpenRouter.
    A missing identity, unsupported transport, or failed lookup preserves the
    original uncertainty. Pending lookups after storage failure remain held.
    """
    import httpx
    import openai

    # Re-read durable identity so a stale or fabricated dataclass cannot return
    # a cached success, or redirect the request to a different receipt.
    try:
        current = next(
            row
            for row in provider_billing_receipts(receipt.batch_id)
            if row.invocation_id == receipt.invocation_id
        )
    except (RuntimeError, StopIteration):
        return BillingReconciliation(
            "abstained", reason="billing receipt is unavailable"
        )
    if replace(current, settled_cost_usd=None) != replace(
        receipt, settled_cost_usd=None
    ):
        return BillingReconciliation(
            "abstained", reason="billing receipt identity changed"
        )
    if current.settled_cost_usd is not None:
        return BillingReconciliation("already_recorded", current.settled_cost_usd)
    try:
        transport = client._client
        valid_client = (
            type(client) is openai.OpenAI
            and str(client.base_url).rstrip("/") == _BASE_URL
            and type(client.max_retries) is int
            and client.max_retries == 0
            and not client._custom_headers
            and not client._custom_query
            and type(transport) in {httpx.Client, openai.DefaultHttpxClient}
            and not any(transport.event_hooks.values())
            and transport.auth is None
            and all(
                item is None
                or type(item) is httpx.MockTransport
                or (type(item) is httpx.HTTPTransport and item._pool._retries == 0)
                for item in [transport._transport, *transport._mounts.values()]
            )
        )
    except (AttributeError, TypeError):
        valid_client = False
    if (
        not valid_client
        or not isinstance(generation_id, str)
        or len(generation_id) > 128
        or re.fullmatch(r"gen-[0-9A-Za-z-]+", generation_id) is None
        or current.generation_sha256 != _provider_identity_fingerprint(generation_id)
        or current.billing_scope_sha256
        != _billing_scope_fingerprint(client.chat.completions)
    ):
        return BillingReconciliation(
            "abstained", reason="billing client or generation is unsupported"
        )

    started = time.perf_counter()
    attempt = uuid.uuid4().hex
    begun = _record(
        "begin_billing_lookup",
        batch_id=current.batch_id,
        invocation_id=current.invocation_id,
        attempt_id=attempt,
        receipt_sha256=current.receipt_sha256,
    )
    if begun.get("status") != "recorded":
        return BillingReconciliation(
            "abstained", reason="billing lookup could not be reserved"
        )

    token = _active_http.set(None)
    invoice = None
    try:
        response = transport.get(
            _BASE_URL + "/generation",
            params={"id": generation_id},
            headers={"Authorization": "Bearer " + client.api_key},
            follow_redirects=False,
        )
        response.raise_for_status()
        invoice = _invoice(response.json().get("data"), current, generation_id)
    except Exception:
        # Do not retain API response bodies, URLs with raw IDs, or credentials
        # in the durable error reason.
        pass
    finally:
        _active_http.reset(token)
    latency = (time.perf_counter() - started) * 1000
    finished = _record(
        "finish_billing_lookup", attempt_id=attempt, latency_ms=latency, invoice=invoice
    )
    if finished.get("status") == "recorded" and invoice is not None:
        return BillingReconciliation("recorded", invoice["cost_usd"])
    if invoice is not None:
        # A rejected bill still consumed lookup time. If persistence itself is
        # broken this also fails, leaving the durable pending hold intact.
        _record(
            "finish_billing_lookup",
            attempt_id=attempt,
            latency_ms=latency,
            invoice=None,
        )
    return BillingReconciliation("abstained", reason="provider bill was not verified")
