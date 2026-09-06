"""Read-only replay of one externally pinned, incomplete public terminal snapshot.

The historical prefix is injected only into the frozen audit in a dedicated
single-threaded process. It is NOT a ledger, budget reset, or dispatch interface.
Complete current-ledger accounting is captured separately before that injection.
No analysis regeneration, labels, credentials, provider calls, or files written.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bench import openrouter_public_factorial as study
from bench.openrouter_attempts import accounting
from bench.openrouter_groups import validate_binding
from bench.openrouter_pilot import HARD_CAP, Ledger, PilotError, canonical, digest

VERSION = "public-terminal-prefix-audit-v1"
_ACTIVE = None
_NATIVE_PIPE_OPEN = False
_LOCK = threading.Lock()


def _require(condition, message):
    if not condition:
        raise PilotError(message)


def _deny(*args, **kwargs):
    raise PilotError("snapshot audit cannot perform inference or analysis")


def _guard(event, args):
    if _ACTIVE is None:
        return
    if event == "open":
        name, mode, flags = args
        if isinstance(name, (str, bytes)):
            path = Path(os.fsdecode(name))
            resolved = path.resolve()
            _require(
                all(
                    p.name != "labels.json" and not p.name.startswith(".env")
                    for p in (path, resolved)
                ),
                "snapshot audit prohibits labels and credentials",
            )
        writing = (isinstance(mode, str) and any(char in mode for char in "wax+")) or (
            isinstance(flags, int)
            and flags
            & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        )
        # Popen opens its stdin pipe before emitting its subprocess audit event.
        # The scoped constructor below has already verified the exact command.
        native_pipe = (
            _NATIVE_PIPE_OPEN
            and type(name) is int
            and stat.S_ISFIFO(os.fstat(name).st_mode)
        )
        _require(
            not writing or native_pipe,
            "snapshot audit prohibits file writes",
        )
    elif event in {
        "socket.connect",
        "socket.getaddrinfo",
        "urllib.Request",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.remove",
        "os.rename",
        "os.mkdir",
        "os.rmdir",
        "os.truncate",
        "os.symlink",
        "os.link",
        "os.chmod",
    }:
        raise PilotError("snapshot audit prohibits network or filesystem mutation")
    elif event == "subprocess.Popen":
        executable, argv = args[:2]
        _require(
            isinstance(executable, (str, bytes))
            and str(Path(os.fsdecode(executable)).resolve()) == _ACTIVE
            and argv == [_ACTIVE],
            "snapshot audit permits only its frozen native materializer",
        )


sys.addaudithook(_guard)


@contextmanager
def _read_only(bridge):
    global _ACTIVE, _NATIVE_PIPE_OPEN
    _require(
        threading.active_count() == 1,
        "snapshot audit requires a dedicated single-threaded process",
    )
    _require(_LOCK.acquire(blocking=False), "snapshot audit already active")
    previous_bytecode = sys.dont_write_bytecode
    original_popen = subprocess.Popen

    def native_popen(command, *args, **kwargs):
        global _NATIVE_PIPE_OPEN
        _require(
            command == [_ACTIVE]
            and not args
            and set(kwargs) == {"stdin", "stdout", "stderr"}
            and all(
                type(value) is int and value == subprocess.PIPE
                for value in kwargs.values()
            ),
            "snapshot audit permits only frozen native pipe IPC",
        )
        _NATIVE_PIPE_OPEN = True
        try:
            return original_popen(command, **kwargs)
        finally:
            _NATIVE_PIPE_OPEN = False

    try:
        _ACTIVE = str(Path(bridge).resolve(strict=True))
        sys.dont_write_bytecode = True
        with (
            patch.object(Ledger, "call", _deny),
            patch.object(study, "analyze", _deny),
            patch.object(subprocess, "Popen", native_popen),
        ):
            yield
    finally:
        _ACTIVE = None
        _NATIVE_PIPE_OPEN = False
        sys.dont_write_bytecode = previous_bytecode
        _LOCK.release()


def _sha(value):
    _require(
        isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value),
        "invalid external snapshot digest",
    )
    return value


def _capture(args, manifest):
    """Hold one shared read lock across raw-byte and frozen-helper reads."""
    path = Path(args.ledger).resolve(strict=True)
    _require(
        str(path) == manifest["campaign_ledger"]["resolved_path"],
        "snapshot requires the original campaign ledger path",
    )
    _require(
        type(args.ledger_event_count) is int
        and args.ledger_event_count > 0
        and type(args.ledger_prefix_bytes) is int
        and args.ledger_prefix_bytes > 0,
        "invalid exact snapshot cutoff",
    )
    with path.open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PilotError(
                "campaign busy; historical audit cannot take a snapshot"
            ) from exc
        try:
            raw = handle.read()
            binding, current, state = study.finance_helpers.typed_ledger_snapshot(path)
            validate_binding(SimpleNamespace(path=path), current)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    _require(
        canonical(binding) == canonical(manifest["campaign_ledger"]),
        "current campaign origin differs from frozen binding",
    )
    prefix = raw[: args.ledger_prefix_bytes]
    _require(
        len(prefix) == args.ledger_prefix_bytes
        and prefix.endswith(b"\n")
        and hashlib.sha256(prefix).hexdigest() == _sha(args.ledger_raw_sha256)
        and len([line for line in prefix.splitlines() if line.strip()])
        == args.ledger_event_count
        and len(current) >= args.ledger_event_count,
        "original ledger prefix bytes or exact event cutoff changed",
    )
    historical = current[: args.ledger_event_count]
    _require(
        digest(historical) == _sha(args.ledger_canonical_sha256),
        "original ledger prefix events changed",
    )
    _require(
        historical[-1]["event"] == "attempt_failure"
        and historical[-1]["stage"] == study.stage_id(manifest),
        "cutoff is not this study original failure boundary",
    )
    return (
        binding,
        historical,
        {
            "captured_at": datetime.now(UTC).isoformat(),
            "event_count": len(current),
            "bytes": len(raw),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "canonical_sha256": digest(current),
            "accounting": {
                "known_usd": str(state["known_usd"]),
                "retained_uncertainty_usd": str(state["holds_usd"]),
                "pending_upper_bound_usd": str(state["pending_usd"]),
                "conservative_commitment_usd": str(
                    state["known_usd"] + state["holds_usd"] + state["pending_usd"]
                ),
                "original_cap_usd": str(HARD_CAP),
                "remaining_after_all_commitments_usd": str(
                    HARD_CAP
                    - state["known_usd"]
                    - state["holds_usd"]
                    - state["pending_usd"]
                ),
                "known_by_stage": {
                    key: str(value) for key, value in state["known_by_stage"].items()
                },
                "holds_by_stage": {
                    key: str(value) for key, value in state["holds_by_stage"].items()
                },
                "completed_calls": len(state["results"]),
                "reserved_attempts": len(state["reserves"]),
                "unresolved_calls": sorted(
                    {event["id"] for event in state["pending"]}
                    | state["failed_calls"]
                    | state["unsafe_ids"]
                ),
                "abandoned_calls": sorted(state["closed_ids"]),
            },
            "not_dispatch_authorization": True,
        },
    )


def audit_snapshot(args):
    """Verify external pins and replay old evidence; return sanitized data only."""
    try:
        with _read_only(args.bridge):
            manifest = study.read(Path(args.output) / "manifest.json")
            _require(
                digest(manifest) == _sha(args.manifest_sha256),
                "external manifest digest changed",
            )
            _require(
                manifest["kind"] == study.KIND
                and manifest["phase"] == "primary"
                and manifest["native_execution_allowed"] is False
                and manifest["paper_evidence"] is False,
                "only incomplete fixed public primary snapshots are supported",
            )
            binding, historical, current = _capture(args, manifest)
            analysis_path = Path(args.output) / "analysis.json"
            _require(
                study.file_hash(analysis_path) == _sha(args.analysis_sha256),
                "archived diagnostic analysis differs from external digest",
            )
            report = study.read(analysis_path)
            _require(
                report["kind"] == study.KIND
                and report["manifest_sha256"] == digest(manifest)
                and report["schedule_complete"] is False
                and report["labels_read"] is False
                and report["paired_tasks"] == []
                and report["descriptive_task_cluster_intervals"] is None
                and all(
                    report[field] is False
                    for field in (
                        "paper_evidence",
                        "efficacy_claim",
                        "adaptive_policy_tested",
                        "native_admission_tested",
                    )
                ),
                "snapshot wrapper prohibits efficacy or label-bearing analysis",
            )
            historical_state = accounting(historical)

            def original_path_prefix(path, key=None):
                _require(
                    key is None
                    and str(Path(path).resolve(strict=True))
                    == binding["resolved_path"],
                    "historical read cannot change campaign identity or use credentials",
                )
                return binding, historical, historical_state

            with patch.object(
                study.finance_helpers, "typed_ledger_snapshot", original_path_prefix
            ):
                checked = study.audit(args, manifest)
            _require(
                checked["schedule_complete"] is False
                and canonical(checked["finance"]) == canonical(report["finance"])
                and canonical(checked["artifact_hashes"])
                == canonical(report["artifact_hashes"]),
                "archived diagnostics differ from reconstructed terminal snapshot",
            )
            _require(
                digest(historical) == args.ledger_canonical_sha256,
                "audit mutated its historical input",
            )
            return {
                "kind": VERSION,
                "manifest_sha256": digest(manifest),
                "analysis_raw_sha256": args.analysis_sha256,
                "wrapper_source_sha256": study.file_hash(Path(__file__)),
                "source_files": manifest["source_files"],
                "campaign_ledger": binding,
                "historical_snapshot": {
                    "event_count": args.ledger_event_count,
                    "bytes": args.ledger_prefix_bytes,
                    "raw_sha256": args.ledger_raw_sha256,
                    "canonical_sha256": args.ledger_canonical_sha256,
                    "finance": checked["finance"],
                    "artifact_hashes": checked["artifact_hashes"],
                    "calls": len(checked["calls"]),
                    "intents": len(checked["intents"]),
                    "application_valid_calls": sum(checked["valid_by_call"].values()),
                    "schedule_complete": False,
                },
                "current_ledger": current,
                "labels_read": False,
                "efficacy_claim": False,
                "analysis_regenerated": False,
                "provider_calls": 0,
                "scope": "Exact terminal-tip reproducibility only; full current accounting retained, never budget reset or resume authority.",
            }
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise PilotError("malformed historical snapshot evidence") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "bundle", "pilot", "bridge", "ledger"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in (
        "manifest-sha256",
        "pilot-manifest-sha256",
        "analysis-sha256",
        "ledger-raw-sha256",
        "ledger-canonical-sha256",
    ):
        parser.add_argument("--" + name, required=True)
    for name in ("ledger-event-count", "ledger-prefix-bytes"):
        parser.add_argument("--" + name, type=int, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(audit_snapshot(args), sort_keys=True))
        return 0
    except (PilotError, OSError):
        print(
            "Historical snapshot audit stopped: changed evidence or forbidden access; no raw diagnostics."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
