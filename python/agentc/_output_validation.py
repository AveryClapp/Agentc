"""Experimental, versioned validation of candidates before application release.

The application supplies the validator and its pinned identity. This module
does not claim that a validator is accurate or turn its verdict into a paired
reference observation. It retains physical work separately from final-response
usage, including when an optimized result is rejected.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator

VERSION = "pre-release-validation-v2"
CONTEXT_KEY = "output_validation"


class ValidationPersistenceError(BaseException):
    """Fatal stop: a profiled rejection could not be committed to its guard."""


def _persist_rejection(plan: Any, execution_id: str) -> None:
    # Unprofiled hand-written plans cannot provide native guard evidence. Their
    # candidate is still withheld. Real SDK plans carry source_call_json.
    if getattr(plan, "source_call_json", None) is None:
        return
    from agentc import _native

    try:
        recorded = _native.optimize_record_output_validation_rejection(
            plan.raw_json, plan.validation_contract_json or "null", execution_id
        )
    except Exception as exc:
        raise ValidationPersistenceError(
            "output validation rejection could not be persisted"
        ) from exc
    if not recorded:
        raise ValidationPersistenceError(
            "output validation rejection was not persisted"
        )


def reject_failed_candidate(plan: Any, *, cost_complete: bool) -> None:
    """Withhold an unusable candidate; provider failures can have unknown spend."""
    # Billing completeness applies even when no output validator is installed.
    plan.inference_cost_complete = cost_complete
    if (
        _current.get() is None
        and getattr(plan, "validation_contract_sha256", None) is None
    ):
        return
    plan.validation_rejected = True
    plan.validation_status = "candidate_failed"
    _persist_rejection(plan, uuid.uuid4().hex)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class ValidationInput:
    execution_id: str
    contract_sha256: str
    request_json: str
    candidate_text: str


@dataclass(frozen=True)
class ValidationReceipt:
    execution_id: str
    contract_sha256: str
    model_version: str
    accepted: bool | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cost_source: str = "unknown"


@dataclass(frozen=True)
class ValidationFailureUsage:
    input_tokens: int
    output_tokens: int
    cost_usd: float
    uncertain_budget_usd: float
    complete: bool
    cost_source: str = "unknown"


@dataclass(frozen=True)
class ValidationContract:
    validator_id: str
    configuration_sha256: str
    model_version: str
    max_cost_usd: float
    validate: Callable[[ValidationInput], ValidationReceipt]
    validate_async: Callable[[ValidationInput], Awaitable[ValidationReceipt]] | None = (
        None
    )
    failure_accounting: Callable[[ValidationInput], ValidationFailureUsage] | None = (
        None
    )

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.validator_id):
            raise ValueError("validator_id must be a stable identifier")
        if not re.fullmatch(r"[0-9a-f]{64}", self.configuration_sha256):
            raise ValueError("configuration_sha256 must be a lowercase SHA-256")
        if not isinstance(self.model_version, str) or not self.model_version.strip():
            raise ValueError("validator model version must be pinned")
        if (
            isinstance(self.max_cost_usd, bool)
            or not math.isfinite(self.max_cost_usd)
            or self.max_cost_usd < 0
        ):
            raise ValueError("validator cost cap must be finite and nonnegative")
        if not callable(self.validate) or (
            self.validate_async is not None and not callable(self.validate_async)
        ):
            raise TypeError("validator must be callable")
        if self.failure_accounting is not None and not callable(
            self.failure_accounting
        ):
            raise TypeError("validator failure accounting must be callable")

    def declaration(self, *, supported: bool = True) -> dict[str, Any]:
        return {
            "version": VERSION,
            "validator_id": self.validator_id,
            "configuration_sha256": self.configuration_sha256,
            "model_version": self.model_version,
            "max_cost_usd": self.max_cost_usd,
            "supported": supported,
        }


_current: ContextVar[ValidationContract | None] = ContextVar(
    "agentc_output_validation", default=None
)


@contextmanager
def experimental_validation_scope(contract: ValidationContract) -> Iterator[None]:
    if not isinstance(contract, ValidationContract):
        raise TypeError("expected a ValidationContract")
    token = _current.set(contract)
    try:
        yield
    finally:
        _current.reset(token)


def _request_json(kwargs: dict[str, Any]) -> str:
    from agentc._provider_config import _CONTROLS, _STRUCTURAL, _omitted

    # Transport and credentials are deliberately never read or inspected.
    projected = {
        key: value
        for key, value in kwargs.items()
        if key in _CONTROLS | _STRUCTURAL | {"extra_body"} and not _omitted(value)
    }
    return json.dumps(projected, sort_keys=True, separators=(",", ":"), allow_nan=False)


def request_declaration(
    kwargs: dict[str, Any], *, supported: bool
) -> dict[str, Any] | None:
    contract = _current.get()
    if contract is None:
        return None
    try:
        _request_json(kwargs)
    except (TypeError, ValueError):
        supported = False
    if kwargs.get("n", 1) != 1:
        supported = False
    return contract.declaration(supported=supported)


def bind_validation_request(plan: Any, kwargs: dict[str, Any]) -> None:
    """Bind actual SDK inputs locally; only the content-free contract reaches Rust."""
    contract = _current.get()
    if contract is None:
        return
    try:
        request_json = _request_json(kwargs)
        digest = _digest(contract.declaration())
    except (TypeError, ValueError):
        return
    for selected in (plan, getattr(plan, "counterfactual", None)):
        if selected is not None:
            if getattr(selected, "validation_contract_sha256", None) not in (
                None,
                digest,
            ):
                continue
            selected.validation_request_json = request_json
            selected.validation_contract_sha256 = digest
            selected.validation_contract_json = json.dumps(
                contract.declaration(), allow_nan=False
            )


def _cost_source(value: Any) -> str:
    return (
        value
        if type(value) is str
        and value in {"provider_reported", "estimated", "no_inference"}
        else "unknown"
    )


def _add_usage(plan: Any, usage: dict[str, Any]) -> None:
    old = getattr(plan, "additional_inference", None) or {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "cost_source": "no_inference",
    }
    sources = {
        _cost_source(old.get("cost_source")),
        _cost_source(usage.get("cost_source")),
    }
    source = next(
        (
            value
            for value in ("unknown", "estimated", "provider_reported")
            if value in sources
        ),
        "no_inference",
    )
    plan.additional_inference = {
        **{
            key: old[key] + usage[key]
            for key in ("input_tokens", "output_tokens", "cost_usd")
        },
        "cost_source": source,
    }


def _account_failure(
    plan: Any, contract: ValidationContract, request: ValidationInput
) -> None:
    plan.validation_rejected = True
    plan.validation_status = "unmeasured"
    plan.inference_cost_complete = False
    if contract.failure_accounting is None:
        return
    try:
        usage = contract.failure_accounting(request)
        if (
            not isinstance(usage, ValidationFailureUsage)
            or type(usage.complete) is not bool
            or any(
                type(v) is not int or not 0 <= v <= 2**32 - 1
                for v in (usage.input_tokens, usage.output_tokens)
            )
            or any(
                type(v) not in {int, float} or not math.isfinite(v) or v < 0
                for v in (usage.cost_usd, usage.uncertain_budget_usd)
            )
            or (usage.complete and usage.uncertain_budget_usd != 0)
        ):
            return
        _add_usage(
            plan,
            {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": float(usage.cost_usd),
                "cost_source": _cost_source(usage.cost_source),
            },
        )
        plan.validation_uncertain_budget_usd = float(usage.uncertain_budget_usd)
        plan.inference_cost_complete = usage.complete
        plan.validation_status = "failure_accounted"
    except Exception:
        # Failure reporting cannot obscure the original fatal stop/cancellation.
        return


def _account_fatal_failure(
    plan: Any, contract: ValidationContract, request: ValidationInput, response: Any
) -> None:
    try:
        _account_failure(plan, contract, request)
    except BaseException:
        plan.inference_cost_complete = False
    try:
        charge_rejected_candidate(plan, response)
    except BaseException:
        plan.inference_cost_complete = False


def _prepare(
    plan: Any, response: Any
) -> tuple[ValidationContract, ValidationInput] | None:
    contract = _current.get()
    if contract is None and getattr(plan, "validation_contract_sha256", None) is None:
        return None
    from agentc._patches._optimizer_glue import _response_output_text

    text = _response_output_text(response)
    choices = getattr(response, "choices", None)
    if (
        contract is None
        or getattr(plan, "validation_request_json", None) is None
        or getattr(plan, "validation_contract_sha256", None)
        != _digest(contract.declaration())
        or text is None
        or (
            choices is not None
            and (not isinstance(choices, (list, tuple)) or len(choices) != 1)
        )
    ):
        plan.validation_status = "unsupported"
        plan.validation_rejected = True
        _persist_rejection(plan, uuid.uuid4().hex)
        return None
    return contract, ValidationInput(
        uuid.uuid4().hex,
        plan.validation_contract_sha256,
        plan.validation_request_json,
        text,
    )


def _finish(
    plan: Any, contract: ValidationContract, request: ValidationInput, receipt: Any
) -> bool:
    try:
        measured_cost = (
            float(receipt.cost_usd)
            if isinstance(receipt, ValidationReceipt)
            else float("nan")
        )
    except (TypeError, ValueError, OverflowError):
        measured_cost = float("nan")
    valid_usage = (
        isinstance(receipt, ValidationReceipt)
        and type(receipt.input_tokens) is int
        and 0 <= receipt.input_tokens <= 2**32 - 1
        and type(receipt.output_tokens) is int
        and 0 <= receipt.output_tokens <= 2**32 - 1
        and isinstance(receipt.cost_usd, (int, float))
        and not isinstance(receipt.cost_usd, bool)
        and math.isfinite(measured_cost)
        and measured_cost >= 0
    )
    if valid_usage:
        _add_usage(
            plan,
            {
                "input_tokens": receipt.input_tokens,
                "output_tokens": receipt.output_tokens,
                "cost_usd": measured_cost,
                "cost_source": _cost_source(receipt.cost_source),
            },
        )
    else:
        # Unknown physical spend cannot become a zero-cost admitted sample.
        plan.validation_status = "unmeasured"
        plan.validation_rejected = True
        plan.inference_cost_complete = False
        _persist_rejection(plan, request.execution_id)
        return False
    accepted = (
        receipt.execution_id == request.execution_id
        and receipt.contract_sha256 == request.contract_sha256
        and receipt.model_version == contract.model_version
        and measured_cost <= contract.max_cost_usd
        and receipt.accepted is True
    )
    plan.validation_status = "accepted" if accepted else "rejected"
    plan.validation_rejected = not accepted
    if not accepted:
        _persist_rejection(plan, request.execution_id)
    return accepted


def validate_candidate(plan: Any, response: Any) -> bool:
    prepared = _prepare(plan, response)
    if prepared is None:
        return not getattr(plan, "validation_rejected", False)
    contract, request = prepared
    from agentc._dispatch_context import provider_dispatch
    from agentc._optimization_scope import optimization_scope

    try:
        with (
            optimization_scope("output-validation", optimize=False),
            provider_dispatch("validation", primary_plan=plan),
        ):
            receipt = contract.validate(request)
    except Exception:
        _account_failure(plan, contract, request)
        _persist_rejection(plan, request.execution_id)
        return False
    except BaseException:
        _account_fatal_failure(plan, contract, request, response)
        raise
    return _finish(plan, contract, request, receipt)


async def validate_candidate_async(plan: Any, response: Any) -> bool:
    prepared = _prepare(plan, response)
    if prepared is None:
        return not getattr(plan, "validation_rejected", False)
    contract, request = prepared
    from agentc._dispatch_context import provider_dispatch
    from agentc._optimization_scope import optimization_scope

    try:
        with (
            optimization_scope("output-validation", optimize=False),
            provider_dispatch("validation", primary_plan=plan),
        ):
            receipt = (
                await contract.validate_async(request)
                if contract.validate_async is not None
                else await asyncio.to_thread(contract.validate, request)
            )
    except Exception:
        _account_failure(plan, contract, request)
        _persist_rejection(plan, request.execution_id)
        return False
    except BaseException:
        _account_fatal_failure(plan, contract, request, response)
        raise
    return _finish(plan, contract, request, receipt)


def charge_rejected_candidate(plan: Any, response: Any) -> None:
    if plan.kind == "cached":
        return
    from agentc._patches._optimizer_glue import (
        build_outcome_anthropic,
        build_outcome_openai,
        resolve_executed_model_id,
    )

    builder = (
        build_outcome_anthropic
        if plan.provider_protocol == "anthropic.messages.v1"
        else build_outcome_openai
    )
    usage = getattr(response, "usage", None)
    fields = (
        ("input_tokens", "output_tokens")
        if plan.provider_protocol == "anthropic.messages.v1"
        else ("prompt_tokens", "completion_tokens")
    )
    if any(
        type(getattr(usage, key, None)) is not int or getattr(usage, key) < 0
        for key in fields
    ):
        plan.inference_cost_complete = False
        return
    try:
        measured = builder(
            response,
            elapsed_s=0.0,
            model=resolve_executed_model_id(plan, response, ""),
            call_site_id="",
            plan=plan,
        )
    except Exception:
        plan.inference_cost_complete = False
        return
    _add_usage(plan, measured)
