"""Fresh, concurrent all-actor dispatch against the original campaign journal.

Opening a transport requires an explicit original-prefix enrollment and a fresh
stage. There is no cached-call replay, automatic retry, stage resume, journal
creation or allowance-release operation. The short file transactions never own
the lock while waiting for provider HTTP. All actual calls, including probes,
simulator and judge calls, must use this same dispatcher before SDK integration.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bench.campaign_accounting import (
    HARD_CAP_USD,
    AccountingError,
    canonical,
    digest,
    json_object,
    money,
    read_locked_snapshot,
    replay_history,
)
from bench.campaign_chat import (
    POLICY,
    ChatContract,
    check_funding,
    key_balance_liabilities,
    completion_record,
    observed_usage_floor,
    prepare_request,
    validate_endpoint,
    validate_metadata,
)
from bench.campaign_http import CampaignHTTP, HttpOperation


class CampaignStopped(BaseException):
    """Fatal dispatch stop; SDK fallback handlers must not turn it into a retry."""


class CampaignTransport:
    def __init__(
        self,
        *,
        ledger: Path,
        expected_sha256: str,
        key: str,
        stage: str,
        stage_cap_usd: str,
        contracts: list[ChatContract],
        http: CampaignHTTP | None = None,
    ):
        self._mutex = threading.RLock()
        self._active: dict[str, tuple[threading.Thread, HttpOperation]] = {}
        self._stopped = False
        self._key = key
        self.key_id = hashlib.sha256(key.encode()).hexdigest()
        self.ledger = ledger.resolve()
        self.stage = stage
        self.cap = money(stage_cap_usd)
        self.session_id = uuid.uuid4().hex
        self.contracts = {contract.model: contract for contract in contracts}
        self.http = http if http is not None else CampaignHTTP()
        self._identity = None
        self._tip = None
        if (
            not key
            or not isinstance(stage, str)
            or not stage
            or not 0 < self.cap <= HARD_CAP_USD
            or not self.contracts
            or len(self.contracts) != len(contracts)
        ):
            raise CampaignStopped("invalid fresh campaign enrollment")
        try:
            with self._transaction() as (stream, snapshot):
                if snapshot.sha256 != expected_sha256:
                    raise AccountingError(
                        "original journal differs from explicit enrollment"
                    )
                self._require_resolved(snapshot.state)
                if any(event.get("stage") == stage for event in snapshot.events):
                    raise AccountingError("campaign stage cannot be reused or resumed")
                origins = [
                    event for event in snapshot.events if event["event"] == "origin"
                ]
                if len(origins) != 1:
                    raise AccountingError("original account origin is required")
                self.baseline = money(origins[0]["usage_usd"])
                enrollment = self._event(
                    "campaign_session",
                    policy=POLICY,
                    session_id=self.session_id,
                    stage=stage,
                    stage_cap_usd=str(self.cap),
                    ledger_binding=digest(str(self.ledger)),
                    prefix_sha256=snapshot.sha256,
                    prefix_bytes=snapshot.byte_count,
                    contracts={
                        model: value.record() for model, value in self.contracts.items()
                    },
                )
                self._append(stream, snapshot.events, enrollment)
                self._enrollment_sha256 = digest(enrollment)
        except Exception:
            raise CampaignStopped(
                "cannot enroll against the complete original campaign"
            ) from None

    @contextmanager
    def _transaction(self):
        # rb+ never creates a missing file. Read and append through the same
        # locked descriptor; replacement of the enrolled inode is fatal.
        with self.ledger.open("r+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stat = os.fstat(stream.fileno())
            identity = stat.st_dev, stat.st_ino
            if self._identity is not None and identity != self._identity:
                raise AccountingError("original enrolled journal was replaced")
            snapshot = read_locked_snapshot(
                stream,
                self.ledger,
                expected_key_id=self.key_id,
                expected_prefix=self._tip,
            )
            self._identity = identity
            self._tip = snapshot.sha256, snapshot.byte_count
            self._checksum = snapshot.checksum_state.copy()
            self._mtime_ns = os.fstat(stream.fileno()).st_mtime_ns
            yield stream, snapshot

    def _event(self, kind: str, **fields) -> dict[str, Any]:
        return {
            "event": kind,
            "key_id": self.key_id,
            "at": datetime.now(timezone.utc).isoformat(),
            **fields,
        }

    def assert_complete(self) -> dict[str, Any]:
        """Worker completion gate, including errors swallowed by probe workers.

        Call after the framework has drained its background work. Pending work
        remains a liability; this method neither cancels it nor releases funds.
        """
        try:
            with self._mutex, self._transaction() as (_, snapshot):
                self._require_resolved(snapshot.state)
                if self._active or snapshot.state["pending"]:
                    raise AccountingError("campaign still has in-flight work")
                return snapshot.summary()
        except Exception:
            raise CampaignStopped(
                "campaign worker cannot certify complete accounting"
            ) from None

    def _append(
        self, stream, events: list[dict[str, Any]], event: dict[str, Any]
    ) -> None:
        replay_history(
            [*events, event], ledger_path=self.ledger, expected_key_id=self.key_id
        )
        self._check_descriptor(stream)
        stream.seek(0, os.SEEK_END)
        data = canonical(event) + b"\n"
        if stream.write(data) != len(data):
            raise AccountingError("journal append was incomplete")
        stream.flush()
        os.fsync(stream.fileno())
        self._checksum.update(data)
        self._tip = self._checksum.hexdigest(), self._tip[1] + len(data)
        self._mtime_ns = os.fstat(stream.fileno()).st_mtime_ns
        self._check_descriptor(stream)
        events.append(event)

    def call_accounting(self, call_id: str) -> dict[str, Any]:
        """Read exact owned-call liabilities without dispatch, replay or release."""
        with self._mutex, self._transaction() as (_, snapshot):
            claims = [e for e in snapshot.events if e["event"] == "reserve" and e["id"] == call_id]
            if not claims:
                return {"status": "not_dispatched", "known_cost_usd": "0",
                        "uncertain_budget_usd": "0", "input_tokens": 0, "output_tokens": 0,
                        "complete": True}
            if len(claims) != 1 or claims[0].get("session_id") != self.session_id:
                raise CampaignStopped("call accounting does not belong to this exact session")
            claim = claims[0]
            result = snapshot.state["results"].get(call_id)
            failure = next((e for e in reversed(snapshot.events)
                            if e["event"] in {"attempt_failure", "attempt_abandoned"}
                            and e.get("attempt_id") == claim["attempt_id"]), None)
            known = money(result["cost_usd"]) if result is not None else money(failure.get("reported_cost_usd") or "0") if failure else money("0")
            bodies = [e["response"] for e in snapshot.events if e["event"] == "response"
                      and e.get("attempt_id") == claim["attempt_id"]]
            usage = bodies[0].get("usage", {}) if len(bodies) == 1 else {}
            contract = self.contracts[claim["request"]["model"]]
            counts = []
            for name, bound in (("prompt_tokens", contract.context_tokens),
                                ("completion_tokens", claim["request"]["max_tokens"])):
                value = usage.get(name) if isinstance(usage, dict) else None
                counts.append(value if type(value) is int and 0 <= value <= bound else 0)
            return {"status": "completed" if result is not None else "failed" if failure else "pending",
                    "known_cost_usd": str(known),
                    "uncertain_budget_usd": "0" if result is not None else str(max(money("0"), money(claim["upper_cost_usd"]) - known)),
                    "input_tokens": counts[0], "output_tokens": counts[1],
                    "complete": result is not None}

    def budget_snapshot(self) -> dict[str, Any]:
        """Read all-actor journal capacity for forecasts; never authorize HTTP.

        The same locked original descriptor owns the session, monetary totals
        and journal head. Failed or lost work remains charged and blocks new
        forecasts. Stored account observations can tighten the journal bound;
        fresh live reconciliation still belongs to each physical call.
        """
        try:
            with self._mutex, self._transaction() as (stream, snapshot):
                sessions = [e for e in snapshot.events if e["event"] == "campaign_session"]
                if not sessions or digest(sessions[-1]) != self._enrollment_sha256:
                    raise AccountingError("original campaign session changed")
                original = sessions[-1]
                baseline = money(next(e["usage_usd"] for e in snapshot.events if e["event"] == "origin"))
                if (
                    original["session_id"] != self.session_id
                    or original["stage"] != self.stage
                    or money(original["stage_cap_usd"]) != self.cap
                    or baseline != self.baseline
                    or original["contracts"] != {m: c.record() for m, c in self.contracts.items()}
                ):
                    raise AccountingError("original campaign forecast binding changed")
                state = snapshot.state
                known, holds, pending = (state[n] for n in ("known_usd", "holds_usd", "pending_usd"))
                committed = max(known + holds + pending,
                                observed_usage_floor(snapshot.events, baseline) - baseline + holds + pending)
                stage_committed = (state["known_by_stage"].get(self.stage, money("0"))
                                   + state["holds_by_stage"].get(self.stage, money("0"))
                                   + sum((money(e["upper_cost_usd"]) for e in state["pending"]
                                          if e["stage"] == self.stage), money("0")))
                available = min(max(money("0"), HARD_CAP_USD - committed),
                                max(money("0"), self.cap - stage_committed))
                observations = [e["account_observation"] for e in snapshot.events
                                if e["event"] == "reserve" and "account_observation" in e]
                if observations and observations[-1].get("limit_remaining") is not None:
                    account = observations[-1]
                    available = min(available, max(money("0"), money(account["limit_remaining"])
                                                    - key_balance_liabilities(state)))
                blocked = False
                try:
                    self._require_resolved(state)
                except AccountingError:
                    blocked = True
                    available = money("0")
                value = {
                    "version": "external-graph-budget-v1",
                    "binding": {
                        "ledger_binding_sha256": original["ledger_binding"],
                        "key_id": self.key_id, "session_id": self.session_id,
                        "stage": self.stage, "session_sha256": self._enrollment_sha256,
                        "contracts_sha256": digest(original["contracts"]),
                        "prefix_sha256": original["prefix_sha256"], "prefix_bytes": original["prefix_bytes"],
                        "stage_cap_usd": original["stage_cap_usd"], "hard_cap_usd": str(HARD_CAP_USD),
                    },
                    "journal_sha256": snapshot.sha256, "journal_bytes": snapshot.byte_count,
                    "known_cost_usd": str(known), "retained_uncertainty_usd": str(holds),
                    "pending_upper_bound_usd": str(pending), "conservative_committed_usd": str(committed),
                    "stage_committed_usd": str(stage_committed), "remaining_upper_usd": str(available),
                    "blocked": blocked, "live_account_reconciled": False, "dispatch_authorized": False,
                }
                result = {**value, "snapshot_sha256": digest(value)}
                self._check_descriptor(stream)
                return result
        except Exception:
            raise CampaignStopped("original campaign budget snapshot cannot be verified") from None

    def call_binding(self, call_id: str) -> dict[str, Any]:
        """Read an exact call's original custody hashes without copying bodies.

        A missing claim cannot become evidence of zero-cost SDK work. Terminal
        bindings remain identical when unrelated actors append later calls.
        """
        try:
            with self._mutex, self._transaction() as (stream, snapshot):
                sessions = [e for e in snapshot.events if e["event"] == "campaign_session"
                            and e["session_id"] == self.session_id]
                if len(sessions) != 1 or digest(sessions[0]) != self._enrollment_sha256:
                    raise AccountingError("original call session changed")
                claims = [e for e in snapshot.events if e["event"] == "reserve" and e["id"] == call_id]
                if len(claims) != 1 or claims[0].get("session_id") != self.session_id:
                    raise AccountingError("call has no unique original owned reservation")
                claim = claims[0]
                def receipt_hash(kind: str) -> str | None:
                    records = [e for e in snapshot.events if e["event"] == kind
                               and e.get("attempt_id") == claim["attempt_id"]]
                    if len(records) > 1:
                        raise AccountingError("call has ambiguous original receipts")
                    return digest(records[0]) if records else None
                result_hash, failure_hash = receipt_hash("result"), receipt_hash("attempt_failure")
                value = {
                    "version": "external-call-binding-v1",
                    "ledger_binding_sha256": sessions[0]["ledger_binding"],
                    "session_sha256": self._enrollment_sha256,
                    "call_id": call_id, "attempt_id": claim["attempt_id"],
                    "reservation_sha256": digest(claim), "request_sha256": digest(claim["request"]),
                    "metadata_sha256": digest(claim["metadata"]),
                    "response_sha256": receipt_hash("response"),
                    "result_sha256": result_hash, "failure_sha256": failure_hash,
                    "status": "completed" if result_hash else "failed" if failure_hash else "pending",
                }
                result = {**value, "binding_sha256": digest(value)}
                self._check_descriptor(stream)
                return result
        except Exception:
            raise CampaignStopped("original owned call binding cannot be verified") from None

    def _check_descriptor(self, stream) -> None:
        descriptor, current = os.fstat(stream.fileno()), self.ledger.stat()
        for stat in (descriptor, current):
            if (
                (stat.st_dev, stat.st_ino) != self._identity
                or stat.st_size != self._tip[1]
                or stat.st_mtime_ns != self._mtime_ns
            ):
                raise AccountingError(
                    "original journal changed outside its transaction"
                )

    def _require_resolved(self, state: dict[str, Any]) -> None:
        if self._stopped or state["failed_calls"] or state["unsafe_ids"]:
            raise AccountingError("campaign has a fatal or unreconciled failed call")
        for reservation in state["pending"]:
            owner = self._active.get(reservation["id"])
            if (
                owner is None
                or reservation.get("session_id") != self.session_id
                or not owner[0].is_alive()
                or not owner[1].alive()
            ):
                raise AccountingError(
                    "campaign has a foreign or lost in-flight reservation"
                )

    def _preflight(self, contract: ChatContract, payload: dict[str, Any], cancelled: threading.Event | None = None):
        operation = self.http.start(
            self._key,
            [("/key", None), ("/models/" + contract.model + "/endpoints", None)],
        )
        try:
            replies = operation.receive(cancelled) if cancelled is not None else operation.receive()
            if len(replies) != 2 or any(
                reply["http_status"] != 200 for reply in replies
            ):
                raise AccountingError("live account or endpoint preflight failed")
            account = json_object(replies[0]["raw"])["data"]
            available = json_object(replies[1]["raw"])["data"]["endpoints"]
            selected = [
                endpoint
                for endpoint in available
                if endpoint.get("tag") == contract.provider_tag
            ]
            if len(selected) != 1:
                raise AccountingError("frozen endpoint is not uniquely available")
            validate_endpoint(contract, payload, selected[0])
            # Do not record account labels or any credential-bearing headers.
            observation = {
                name: account.get(name)
                for name in ("usage", "byok_usage", "limit_remaining", "limit_reset")
            }
            return observation, selected[0]
        finally:
            operation.close()

    def call(
        self, *, call_id: str, request: dict[str, Any], metadata: dict[str, str], cancelled: threading.Event | None = None
    ) -> dict[str, Any]:
        """Dispatch one fresh provider request; return its unmodified JSON envelope."""
        claim, operation, started = None, None, time.monotonic()
        dispatched = None
        received = None
        try:
            if cancelled is not None and cancelled.is_set():
                raise AccountingError("campaign call was cancelled before preflight")
            with self._mutex:
                if self._stopped:
                    raise AccountingError("campaign dispatcher is stopped")
            if not isinstance(call_id, str) or not call_id:
                raise AccountingError("fresh call identity is required")
            contract = self.contracts[request["model"]]
            payload = prepare_request(contract, request)
            validate_metadata(metadata)
            metadata = json.loads(canonical(metadata))
            observation, endpoint = self._preflight(contract, payload, cancelled)
            with self._mutex, self._transaction() as (stream, snapshot):
                if cancelled is not None and cancelled.is_set():
                    raise AccountingError("campaign call was cancelled before reservation")
                self._require_resolved(snapshot.state)
                sessions = [
                    event
                    for event in snapshot.events
                    if event["event"] == "campaign_session"
                ]
                if sessions[-1]["session_id"] != self.session_id or any(
                    event.get("id") == call_id for event in snapshot.events
                ):
                    raise AccountingError(
                        "call identity or campaign session cannot be replayed"
                    )
                bound = contract.bound(payload)
                if money(observation.get("usage")) < observed_usage_floor(
                    snapshot.events, self.baseline
                ):
                    raise AccountingError("cumulative account usage regressed")
                check_funding(
                    snapshot.state,
                    self.stage,
                    self.cap,
                    bound,
                    observation,
                    self.baseline,
                )
                claim = self._event(
                    "reserve",
                    id=call_id,
                    stage=self.stage,
                    session_id=self.session_id,
                    attempt_id=uuid.uuid4().hex,
                    request=payload,
                    metadata=metadata,
                    fingerprint=digest(
                        {"payload": payload, "metadata": metadata, "stage": self.stage}
                    ),
                    upper_cost_usd=str(bound),
                    account_observation=observation,
                    endpoint_observation=endpoint,
                )
                self._append(stream, snapshot.events, claim)
                if cancelled is not None and cancelled.is_set():
                    raise AccountingError("campaign call was cancelled after reservation")
                dispatched = time.monotonic()
                operation = self.http.start(self._key, [("/chat/completions", payload)])
                self._active[call_id] = (threading.current_thread(), operation)
            replies = operation.receive(cancelled) if cancelled is not None else operation.receive()
            received = time.monotonic()
            if len(replies) != 1:
                raise AccountingError("provider omitted its HTTP receipt")
            receipt = replies[0]
            body = {}
            try:
                body = json_object(receipt["raw"])
            except AccountingError:
                pass
            with self._mutex, self._transaction() as (stream, snapshot):
                response = self._event(
                    "response",
                    id=call_id,
                    stage=self.stage,
                    session_id=self.session_id,
                    attempt_id=claim["attempt_id"],
                    reserve_sha256=digest(claim),
                    response=body,
                    raw_base64=base64.b64encode(receipt["raw"]).decode("ascii"),
                    http_status=receipt["http_status"],
                    cache_status=receipt["cache_status"],
                )
                self._append(stream, snapshot.events, response)
                if receipt["http_status"] != 200 or receipt["cache_status"] not in {
                    None,
                    "MISS",
                }:
                    raise AccountingError(
                        "provider returned an unsuccessful or cached HTTP reply"
                    )
                result = completion_record(claim, body, contract)
                generations = {
                    event.get("generation_id")
                    for event in snapshot.events
                    if event.get("generation_id")
                }
                generations.update(
                    event["response"].get("id")
                    for event in snapshot.events
                    if event["event"] == "response"
                    and event.get("attempt_id") != claim["attempt_id"]
                    and isinstance(event.get("response"), dict)
                    and event["response"].get("id")
                )
                if result["generation_id"] in generations:
                    raise AccountingError(
                        "provider reused a previous generation identity"
                    )
                self._append(
                    stream,
                    snapshot.events,
                    self._event(
                        "result",
                        **{
                            name: value
                            for name, value in result.items()
                            if name != "event"
                        },
                        latency_ms=(received - dispatched) * 1000,
                        pre_dispatch_ms=(dispatched - started) * 1000,
                    ),
                )
                self._active.pop(call_id)
            return body
        except BaseException as exc:
            with self._mutex:
                self._stopped = True
                if claim is not None:
                    try:
                        self._retain_failure(claim)
                    except Exception:
                        # A torn/missing/unwritable journal remains unresolved.
                        # Never recreate it or manufacture zero-cost completion.
                        pass
                self._active.pop(call_id, None)
            if not isinstance(exc, Exception):
                raise
            raise CampaignStopped(
                "campaign dispatch stopped; reservation retained if issued"
            ) from None
        finally:
            if operation is not None:
                pending_error = sys.exception()
                try:
                    operation.close()
                except BaseException as cleanup_error:
                    with self._mutex:
                        self._stopped = True
                    if pending_error is None:
                        if not isinstance(cleanup_error, Exception):
                            raise
                        raise CampaignStopped(
                            "owned provider process cleanup failed"
                        ) from None

    def _retain_failure(self, claim: dict[str, Any]) -> None:
        with self._transaction() as (stream, snapshot):
            if claim["id"] in snapshot.state["results"]:
                return
            bodies = [
                event["response"]
                for event in snapshot.events
                if event["event"] == "response"
                and event.get("attempt_id") == claim["attempt_id"]
            ]
            cost = None
            if len(bodies) == 1:
                try:
                    cost = str(money(bodies[0].get("usage", {}).get("cost")))
                except (AccountingError, AttributeError):
                    pass
            failure = self._event(
                "attempt_failure",
                **{
                    name: claim[name]
                    for name in ("id", "stage", "attempt_id", "session_id")
                },
                reserve_sha256=digest(claim),
                budget_hold_usd=claim["upper_cost_usd"],
                reported_cost_usd=cost,
                reason="dispatch_failed",
                retryable=False,
            )
            self._append(stream, snapshot.events, failure)
