"""Append-only spend ledger with a hard cap checked before every dispatch.

Before a call is sent, the ledger reserves its worst-case cost: estimated
input tokens at the input price plus the full output cap at the output
price. If committed spend plus that reservation would exceed the cap, the
call is refused with :class:`SpendCapReached`, which derives from
``BaseException`` so that no SDK retry or fallback handler can swallow it.
After the call, the ledger records the provider-reported cost (LiteLLM's
``response_cost``) or, failing that, the cost from token usage.

Prices come from LiteLLM's model table unless overridden. A live model with
no known price is refused, never assumed free.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping


class SpendCapReached(BaseException):
    """Stop the stage: the next call could exceed the declared cap."""


def _litellm_prices(model: str) -> tuple[float, float] | None:
    try:
        import litellm

        info = litellm.get_model_info(model)
    except Exception:
        return None
    price_in = info.get("input_cost_per_token")
    price_out = info.get("output_cost_per_token")
    if price_in is None or price_out is None:
        return None
    return float(price_in), float(price_out)


def estimate_input_tokens(request: Mapping[str, Any]) -> int:
    """Conservative upper estimate: one token per three characters of JSON."""
    payload = json.dumps(
        {k: request.get(k) for k in ("messages", "tools", "system")}, default=str
    )
    return len(payload) // 3 + 16


@dataclass
class SpendLedger:
    path: Path
    cap_usd: float
    #: Per-model (input, output) price per token; overrides LiteLLM's table.
    prices: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: Models that cost nothing (the E0 mock).
    free_prefixes: tuple[str, ...] = ("mock/",)
    committed_usd: float = 0.0
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not self.cap_usd >= 0:
            raise ValueError("cap must be non-negative")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _price(self, model: str) -> tuple[float, float]:
        if model.startswith(self.free_prefixes):
            return 0.0, 0.0
        price = self.prices.get(model) or _litellm_prices(model)
        if price is None:
            raise SpendCapReached(f"no known price for {model}; refusing to dispatch")
        return price

    def _reserve(self, request: Mapping[str, Any]) -> float:
        price_in, price_out = self._price(str(request.get("model", "")))
        output_cap = request.get("max_tokens") or request.get("max_completion_tokens")
        if not isinstance(output_cap, int) or output_cap <= 0:
            raise SpendCapReached("every dispatched call needs a positive output cap")
        return estimate_input_tokens(request) * price_in + output_cap * price_out

    def _actual(self, request: Mapping[str, Any], response: Any) -> float:
        if str(request.get("model", "")).startswith(self.free_prefixes):
            return 0.0
        hidden = getattr(response, "_hidden_params", None) or {}
        reported = hidden.get("response_cost")
        if isinstance(reported, (int, float)) and reported >= 0:
            return float(reported)
        price_in, price_out = self._price(str(request.get("model", "")))
        usage = getattr(response, "usage", None)
        prompt = getattr(usage, "prompt_tokens", None) or 0
        completion = getattr(usage, "completion_tokens", None) or 0
        return prompt * price_in + completion * price_out

    def call(
        self,
        *,
        role: str,
        request: Mapping[str, Any],
        dispatch: Callable[[Mapping[str, Any]], Any],
    ) -> Any:
        reservation = self._reserve(request)
        with self._lock:
            if self.committed_usd + reservation > self.cap_usd:
                raise SpendCapReached(
                    f"cap ${self.cap_usd:.2f} reached: committed ${self.committed_usd:.4f}, "
                    f"next call reserves ${reservation:.4f}"
                )
            # Hold the reservation while the call is in flight.
            self.committed_usd += reservation
        started = time.time()
        response = None
        error: str | None = None
        try:
            response = dispatch(request)
            return response
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            # A failed call may still be billed; keep the full reservation.
            cost = self._actual(request, response) if response is not None else reservation
            with self._lock:
                self.committed_usd += cost - reservation
                self.calls += 1
                entry = {
                    "time": started,
                    "role": role,
                    "model": request.get("model"),
                    "cost_usd": cost,
                    "reserved_usd": reservation,
                    "committed_usd": self.committed_usd,
                    "error": error,
                }
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(entry, sort_keys=True) + "\n")


__all__ = ["SpendCapReached", "SpendLedger", "estimate_input_tokens"]
