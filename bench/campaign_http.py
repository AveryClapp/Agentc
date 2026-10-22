"""Single-attempt HTTP in owned child processes, with whole-operation deadlines.

No provider keys, HTTP exceptions or request bodies appear in diagnostics. The
parent retains the financial reservation even if it terminates a timed-out child;
termination proves nothing about provider cancellation or eventual billing.
"""

from __future__ import annotations

import base64
import math
import multiprocessing
import os
import select
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from bench.campaign_accounting import AccountingError, canonical, json_object

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PACKET_BYTES = 24 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_worker(connection, key: str, operations: list[dict[str, Any]]) -> None:
    try:
        opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.ProxyHandler({})
        )
        replies = []
        for operation in operations:
            request = urllib.request.Request(
                operation["url"],
                data=canonical(operation["payload"])
                if operation["payload"] is not None
                else None,
                headers={
                    "Authorization": "Bearer " + key,
                    "Content-Type": "application/json",
                    "X-OpenRouter-Metadata": "enabled",
                    "X-OpenRouter-Cache": "false",
                },
                method="POST" if operation["payload"] is not None else "GET",
            )
            try:
                response = opener.open(request, timeout=operation["timeout"])
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    connection.send_bytes(canonical({"failure": "response_size"}))
                    break
                replies.append(
                    {
                        "http_status": response.code,
                        "raw_base64": base64.b64encode(raw).decode("ascii"),
                        "cache_status": response.headers.get(
                            "X-OpenRouter-Cache-Status"
                        ),
                    }
                )
        else:
            connection.send_bytes(canonical({"replies": replies}))
        # Keep the real owned process alive until its parent durably reconciles
        # the response. A completed pipe send is not dispatch permission.
        connection.recv_bytes()
    except (EOFError, BrokenPipeError):
        pass
    except BaseException:
        try:
            connection.send_bytes(canonical({"failure": "transport"}))
            connection.recv_bytes()
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


class HttpOperation:
    def __init__(self, key: str, operations: list[dict[str, Any]], deadline: float):
        self.deadline = time.monotonic() + deadline
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(
            target=_http_worker, args=(child, key, operations), daemon=True
        )
        try:
            self.process.start()
        except BaseException:
            self.connection.close()
            child.close()
            self.process.close()
            raise
        child.close()

    def alive(self) -> bool:
        return self.process.is_alive()

    def receive(self, cancelled: threading.Event | None = None) -> list[dict[str, Any]]:
        # Connection.recv() can block after poll() if the child stops halfway
        # through a large frame. Read its bounded send_bytes frame directly,
        # checking the same total deadline before every nonblocking read.
        descriptor = self.connection.fileno()
        os.set_blocking(descriptor, False)
        try:
            size = struct.unpack("!i", self._read_exact(descriptor, 4, cancelled))[0]
            if not 0 < size <= MAX_PACKET_BYTES:
                raise AccountingError("owned provider process sent an unbounded frame")
            packet = json_object(self._read_exact(descriptor, size, cancelled))
        except OSError:
            raise AccountingError(
                "owned provider process exited without a receipt"
            ) from None
        if time.monotonic() >= self.deadline:
            raise AccountingError("provider operation exceeded its total deadline")
        if not isinstance(packet, dict) or "failure" in packet:
            raise AccountingError("provider transport failed; details suppressed")
        replies = packet.get("replies")
        if not isinstance(replies, list):
            raise AccountingError("owned provider process omitted HTTP receipts")
        for reply in replies:
            reply["raw"] = base64.b64decode(reply.pop("raw_base64"), validate=True)
            if len(reply["raw"]) > MAX_RESPONSE_BYTES:
                raise AccountingError("owned provider response exceeds its byte bound")
        return replies

    def _read_exact(self, descriptor: int, size: int, cancelled: threading.Event | None = None) -> bytes:
        chunks, received = [], 0
        while received < size:
            if cancelled is not None and cancelled.is_set():
                raise AccountingError("owned provider operation was cancelled")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise AccountingError("provider operation exceeded its total deadline")
            interval = min(remaining, 0.05) if cancelled is not None else remaining
            if not select.select([descriptor], [], [], interval)[0]:
                if cancelled is not None:
                    continue
                raise AccountingError("provider operation exceeded its total deadline")
            try:
                chunk = os.read(descriptor, min(size - received, 65536))
            except BlockingIOError:
                continue
            if not chunk:
                raise AccountingError("owned provider process exited without a receipt")
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        try:
            if self.process.is_alive():
                self.connection.send_bytes(b"reconciled")
        except (OSError, EOFError):
            pass
        finally:
            self.connection.close()
            self.process.join(timeout=0.1)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=1)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=1)
            self.process.close()


class CampaignHTTP:
    def __init__(self, *, deadline_seconds: float = 120):
        if (
            type(deadline_seconds) not in {int, float}
            or not math.isfinite(deadline_seconds)
            or not 0 < deadline_seconds <= 120
        ):
            raise AccountingError("provider deadline must be in (0, 120] seconds")
        self.deadline_seconds = deadline_seconds
        self.base_url = "https://openrouter.ai/api/v1"

    @classmethod
    def local_test(cls, base_url: str, *, deadline_seconds: float = 10) -> CampaignHTTP:
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != "/api/v1"
        ):
            raise AccountingError("fixture HTTP must use an explicit loopback endpoint")
        result = cls(deadline_seconds=deadline_seconds)
        result.base_url = base_url
        return result

    def start(
        self, key: str, requests: list[tuple[str, dict[str, Any] | None]]
    ) -> HttpOperation:
        operations = [
            {
                "url": self.base_url + path,
                "payload": payload,
                "timeout": self.deadline_seconds,
            }
            for path, payload in requests
        ]
        return HttpOperation(key, operations, self.deadline_seconds)
