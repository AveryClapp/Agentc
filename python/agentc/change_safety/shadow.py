"""Off-path forks: send a changed copy of a call and compare, never deliver.

:class:`ShadowRunner` is the measurement primitive. Given an eligible call's
request and the response the application already received, it applies the
assigned change, dispatches the changed request, normalizes both responses,
computes the signals, and emits one :class:`ForkRecord`. The fork response is
never returned to the caller, so the application's history is unchanged.

:class:`ChangeMonitor` is the deployment-facing wrapper around a completion
function. In ``shadow`` mode the application always receives the original
response and a sampled share of calls is forked with the change. In ``live``
mode the change is applied to every call until the detector alarms; after an
alarm the monitor stops applying the change and the application receives the
original request's response again.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agentc.change_safety.normalize import error_response, normalize_response
from agentc.change_safety.records import (
    CallKey,
    ChangeSpec,
    ForkRecord,
    NormalizedResponse,
    SignalValues,
)
from agentc.change_safety.signals import TerminationMarkers, compute_signals

Dispatch = Callable[[Mapping[str, Any]], Any]
Sink = Callable[[ForkRecord], None]


def _apply(spec: ChangeSpec, request: Mapping[str, Any], call_site_id: str | None = None) -> Any:
    from agentc.change_safety.changes import apply_change

    return apply_change(spec, request, call_site_id=call_site_id)


def _applied_edits(response: Any) -> tuple[Mapping[str, Any], ...] | None:
    from agentc.change_safety.changes import extract_applied_edits

    edits: tuple[Mapping[str, Any], ...] | None = extract_applied_edits(response)
    return edits


def timed_dispatch(
    dispatch: Dispatch, request: Mapping[str, Any]
) -> tuple[NormalizedResponse, Any]:
    """Dispatch once and normalize; provider errors become error responses."""
    start = time.perf_counter()
    try:
        raw = dispatch(request)
    except Exception as exc:  # BaseException (e.g. a spend stop) must propagate.
        return error_response(exc, latency_s=time.perf_counter() - start), None
    latency = time.perf_counter() - start
    try:
        return normalize_response(raw, latency_s=latency, applied_edits=_applied_edits(raw)), raw
    except ValueError as exc:
        return error_response(exc, latency_s=latency), raw


@dataclass
class ShadowRunner:
    dispatch: Dispatch
    sink: Sink
    markers: TerminationMarkers = TerminationMarkers()
    embed: bool = True

    def fork(
        self,
        *,
        key: CallKey,
        spec: ChangeSpec,
        request: Mapping[str, Any],
        reference: NormalizedResponse,
        reference_request_tokens: int | None = None,
        call_site_id: str | None = None,
    ) -> ForkRecord:
        """Fork one call. ``call_site_id`` is needed only by C5 and C6."""
        applied = _apply(spec, request, call_site_id)
        if not applied.applied:
            fork: NormalizedResponse | None = None
            signals = SignalValues(components={"reason": "change_not_applied"})
        else:
            fork, _ = timed_dispatch(self.dispatch, applied.request)
            signals = compute_signals(reference, fork, markers=self.markers, embed=self.embed)
        record = ForkRecord(
            key=key,
            arm=spec.change_id,
            change_identity=spec.identity,
            change_applied=applied.applied,
            change_evidence=dict(applied.evidence),
            baseline=reference,
            fork=fork,
            signals=signals,
            baseline_request_tokens=reference_request_tokens,
            ineligible_reason=applied.ineligible_reason,
        )
        self.sink(record)
        return record


class Detector:
    """Structural type of the online detectors in :mod:`detectors`."""

    alarmed: bool

    def update(self, x: float) -> bool:  # pragma: no cover - protocol only
        raise NotImplementedError


def sampled(rate: float, *parts: object) -> bool:
    """Deterministic Bernoulli(rate) draw keyed by ``parts``."""
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    digest = hashlib.sha256("\x00".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < rate


class ChangeMonitor:
    """Guard one change on a live completion function.

    ``signal`` names the signal fed to the detector (S1 to S5). Forks whose
    signal is ``None`` are recorded but not fed to the detector.
    """

    def __init__(
        self,
        *,
        spec: ChangeSpec,
        detector: Detector,
        sink: Sink,
        mode: str = "shadow",
        sample_rate: float = 0.02,
        signal: str = "S4",
        seed: str = "agentc-change-monitor-v1",
        markers: TerminationMarkers = TerminationMarkers(),
        embed: bool = True,
    ):
        if mode not in {"shadow", "live"} or not 0.0 <= sample_rate <= 1.0:
            raise ValueError("mode must be shadow or live and sample_rate in [0, 1]")
        self.spec, self.detector, self.mode = spec, detector, mode
        self.sample_rate, self.signal, self.seed = sample_rate, signal, seed
        self._sink, self._markers, self._embed = sink, markers, embed
        self._lock = threading.Lock()
        self._calls = 0
        self.forks = 0

    @property
    def disabled(self) -> bool:
        return bool(self.detector.alarmed)

    def _observe(self, record: ForkRecord) -> None:
        self._sink(record)
        value = record.signals.get(self.signal)
        if value is not None:
            with self._lock:
                self.detector.update(value)

    def wrap(self, completion: Dispatch, *, key_for: Callable[[int], CallKey]) -> Dispatch:
        """Return a completion function guarded by this monitor.

        ``key_for(call_index)`` names each call for its record.
        """
        runner = ShadowRunner(
            dispatch=completion, sink=self._observe, markers=self._markers, embed=self._embed
        )

        def guarded(request: Mapping[str, Any]) -> Any:
            with self._lock:
                index = self._calls
                self._calls += 1
            changed = None
            if self.mode == "live" and not self.disabled:
                applied = _apply(self.spec, request)
                changed = applied if applied.applied else None
            live_request = changed.request if changed is not None else request
            start = time.perf_counter()
            raw = completion(live_request)  # provider errors reach the caller unchanged
            latency = time.perf_counter() - start
            if not sampled(self.sample_rate, self.seed, index):
                return raw
            try:
                delivered = normalize_response(
                    raw, latency_s=latency, applied_edits=_applied_edits(raw)
                )
            except ValueError:
                return raw
            with self._lock:
                self.forks += 1
            if changed is None:
                # Shadow: the delivered response is the reference.
                runner.fork(key=key_for(index), spec=self.spec, request=request, reference=delivered)
            else:
                # Live: the original request is the off-path counterfactual.
                original, _ = timed_dispatch(completion, request)
                signals = compute_signals(
                    original, delivered, markers=self._markers, embed=self._embed
                )
                self._observe(
                    ForkRecord(
                        key=key_for(index),
                        arm=self.spec.change_id,
                        change_identity=self.spec.identity,
                        change_applied=True,
                        change_evidence=dict(changed.evidence),
                        baseline=original,
                        fork=delivered,
                        signals=signals,
                    )
                )
            return raw

        return guarded


__all__ = ["ChangeMonitor", "Detector", "ShadowRunner", "sampled", "timed_dispatch"]
