"""A private concurrent call bridge; only the parent owns provider credentials.

The child may dispatch for one enrolled cell and read its own liabilities.
There is no transport creation, stage enrollment, release, replay or retry RPC.
"""

from __future__ import annotations

import hmac
import json
import secrets
import socket
import socketserver
import struct
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bench.campaign_accounting import canonical
from bench.campaign_chat import ChatContract
from bench.campaign_transport import CampaignStopped

MAX_PACKET = 24 * 1024 * 1024


def receive(sock: socket.socket, deadline: float) -> dict[str, Any]:
    def exact(size: int) -> bytes:
        parts: list[bytes] = []
        while size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("campaign bridge deadline elapsed")
            sock.settimeout(remaining)
            part = sock.recv(min(size, 65536))
            if not part:
                raise EOFError("campaign bridge closed")
            parts.append(part)
            size -= len(part)
        return b"".join(parts)

    size = struct.unpack("!I", exact(4))[0]
    if not 0 < size <= MAX_PACKET:
        raise ValueError("campaign bridge packet exceeds its bound")
    value = json.loads(exact(size))
    if not isinstance(value, dict):
        raise ValueError("campaign bridge requires an object")
    return value


def send(sock: socket.socket, value: dict[str, Any], deadline: float) -> None:
    body = canonical(value)
    if not 0 < len(body) <= MAX_PACKET:
        raise ValueError("campaign bridge packet exceeds its bound")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("campaign bridge deadline elapsed")
    sock.settimeout(remaining)
    sock.sendall(struct.pack("!I", len(body)) + body)


class TransportBridge:
    def __init__(self, transport, *, cell, deadline):
        self.transport, self.cell, self.deadline = transport, dict(cell), deadline
        self.cancelled = threading.Event()
        self.failed = threading.Event()
        self._mutex = threading.Lock()
        self._calls = set()
        self._handlers = set()
        self._sockets = set()
        self._slots = threading.BoundedSemaphore(8)
        self._directory = tempfile.TemporaryDirectory(prefix="agc-rpc-")
        self.path = str(Path(self._directory.name) / "bridge.sock")
        self.token = secrets.token_hex(32)
        bridge = self

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            block_on_close = False

            def process_request(self, request, address):
                if bridge.cancelled.is_set():
                    request.close()
                    return
                if not bridge._slots.acquire(blocking=False):
                    bridge.failed.set()
                    request.close()
                    return
                try:
                    thread = threading.Thread(
                        target=self.process_request_thread,
                        args=(request, address),
                        daemon=True,
                    )
                    with bridge._mutex:
                        bridge._sockets.add(request)
                        bridge._handlers.add(thread)
                    thread.start()
                except BaseException:
                    bridge._slots.release()
                    request.close()
                    raise

            def process_request_thread(self, request, address):
                thread = threading.current_thread()
                with bridge._mutex:
                    bridge._handlers.add(thread)
                try:
                    super().process_request_thread(request, address)
                finally:
                    with bridge._mutex:
                        bridge._handlers.discard(thread)
                        bridge._sockets.discard(request)
                    bridge._slots.release()

            def handle_error(self, request, client_address):
                bridge.failed.set()  # Never print bodies, credentials or exceptions.

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    packet = receive(self.request, bridge.deadline)
                    result = bridge._invoke(packet)
                    send(self.request, {"result": result}, bridge.deadline)
                except BaseException:
                    bridge.failed.set()
                    try:
                        send(
                            self.request,
                            {"failure": "owned_campaign_call_stopped"},
                            bridge.deadline,
                        )
                    except (OSError, TimeoutError):
                        pass

        self.server = Server(self.path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def descriptor(self):
        return {
            "socket": self.path,
            "token": self.token,
            "deadline": self.deadline,
            "contracts": {m: asdict(c) for m, c in self.transport.contracts.items()},
            "key_id": self.transport.key_id,
            "base_url": self.transport.http.base_url,
            "session_id": self.transport.session_id,
            "ledger": str(self.transport.ledger),
        }

    def _invoke(self, packet):
        if (
            set(packet) != {"token", "operation", "arguments"}
            or not isinstance(packet["token"], str)
            or not hmac.compare_digest(packet["token"], self.token)
            or not isinstance(packet["arguments"], dict)
        ):
            raise CampaignStopped("invalid child campaign capability")
        operation, arguments = packet["operation"], packet["arguments"]
        if operation == "call":
            if self.failed.is_set() or self.cancelled.is_set():
                raise CampaignStopped("child campaign stopped")
            if set(arguments) != {"call_id", "request", "metadata"}:
                raise CampaignStopped("invalid child provider call")
            metadata = arguments["metadata"]
            if not isinstance(metadata, dict) or any(
                metadata.get(k) != v for k, v in self.cell.items()
            ):
                raise CampaignStopped("child call left its enrolled cell")
            call_id = arguments["call_id"]
            if not isinstance(call_id, str) or not call_id:
                raise CampaignStopped("missing child call identity")
            with self._mutex:
                if call_id in self._calls:
                    raise CampaignStopped("child call cannot retry")
                self._calls.add(call_id)
            return self.transport.call(**arguments, cancelled=self.cancelled)
        if operation == "call_accounting":
            if set(arguments) != {"call_id"} or arguments["call_id"] not in self._calls:
                raise CampaignStopped("child accounting belongs to another cell")
            return self.transport.call_accounting(arguments["call_id"])
        if operation == "call_binding":
            if set(arguments) != {"call_id"} or arguments["call_id"] not in self._calls:
                raise CampaignStopped("child call binding belongs to another cell")
            return self.transport.call_binding(arguments["call_id"])
        if operation == "budget_snapshot" and arguments == {}:
            if self.failed.is_set() or self.cancelled.is_set():
                raise CampaignStopped("child campaign stopped")
            return self.transport.budget_snapshot()
        if operation == "assert_complete" and arguments == {}:
            return self.transport.assert_complete()
        raise CampaignStopped("unsupported child campaign operation")

    def close(self):
        self.cancelled.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        with self._mutex:
            sockets = list(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        deadline = time.monotonic() + 5
        while True:
            with self._mutex:
                handlers = list(self._handlers)
            if not handlers:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CampaignStopped("child campaign handlers remain alive")
            handlers[0].join(timeout=min(remaining, 0.05))
        self._directory.cleanup()


class ChildTransport:
    def __init__(self, descriptor):
        self._descriptor = descriptor
        self.contracts = {
            m: ChatContract(**v) for m, v in descriptor["contracts"].items()
        }
        self.key_id = descriptor["key_id"]
        self.http = SimpleNamespace(base_url=descriptor["base_url"])
        self.session_id = descriptor["session_id"]
        self.ledger = Path(descriptor["ledger"])

    def _rpc(self, operation: str, **arguments: Any) -> Any:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(
                    max(0.001, self._descriptor["deadline"] - time.monotonic())
                )
                sock.connect(self._descriptor["socket"])
                send(
                    sock,
                    {
                        "token": self._descriptor["token"],
                        "operation": operation,
                        "arguments": arguments,
                    },
                    self._descriptor["deadline"],
                )
                packet = receive(sock, self._descriptor["deadline"])
            if set(packet) != {"result"}:
                raise CampaignStopped("owned campaign call stopped")
            return packet["result"]
        except Exception:
            raise CampaignStopped(
                "child campaign bridge failed; details suppressed"
            ) from None

    def call(self, **arguments):
        return self._rpc("call", **arguments)

    def call_accounting(self, call_id):
        return self._rpc("call_accounting", call_id=call_id)

    def call_binding(self, call_id: str) -> dict[str, Any]:
        result = self._rpc("call_binding", call_id=call_id)
        if not isinstance(result, dict):
            raise CampaignStopped("child campaign returned an invalid call binding")
        return result

    def assert_complete(self):
        return self._rpc("assert_complete")

    def budget_snapshot(self) -> dict[str, Any]:
        result = self._rpc("budget_snapshot")
        if not isinstance(result, dict):
            raise CampaignStopped("child campaign returned an invalid budget snapshot")
        return result
