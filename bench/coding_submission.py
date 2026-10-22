"""Preserve mini-SWE-agent submissions from actual terminal tool receipts.

The pinned v1 agent joins split lines when reporting Submitted. Recover the
captured body only when that same transformation proves the reported text.
Never manufacture a newline or grade a post-run worktree as the submission.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from bench.campaign_accounting import canonical, digest


MINI_REVISION = "a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d"
CAPTURE_POLICY = "mini-v1-terminal-submission-v1"
MARKER = "MICRO_SWE_AGENT_FINAL_OUTPUT"


class SubmissionError(RuntimeError):
    """The reported submission is not supported by its captured execution."""


def capture_submission(
    *,
    task_id: str,
    base_commit: str,
    model_name: str,
    status: str,
    returned_text: str,
    events: Sequence[Mapping[str, Any]],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(task_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task_id)
        or not isinstance(base_commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", base_commit)
        or not all(isinstance(s, str) and s for s in (model_name, status))
        or not isinstance(returned_text, str)
        or not isinstance(provenance, Mapping)
        or provenance.get("upstream_revision") != MINI_REVISION
        or not isinstance(events, (list, tuple))
        or any(not isinstance(event, Mapping) for event in events)
    ):
        raise SubmissionError("invalid submission identity or upstream contract")
    captured = deepcopy(list(events))
    patch, receipt_index = "", None
    if status == "Submitted":
        tool_indices = [
            i
            for i, event in enumerate(captured)
            if event.get("event") in {"tool", "tool_failure"}
        ]
        if not tool_indices:
            raise SubmissionError("Submitted has no captured terminal tool receipt")
        receipt_index = tool_indices[-1]
        event = captured[receipt_index]
        result = event.get("result")
        if (
            event["event"] != "tool"
            or not isinstance(result, dict)
            or type(result.get("returncode")) is not int
            or result["returncode"] != 0
            or not isinstance(result.get("output"), str)
        ):
            raise SubmissionError("terminal submission command did not succeed")
        output = result["output"].lstrip()
        lines = output.splitlines(keepends=True)
        if not lines or lines[0].strip() != MARKER:
            raise SubmissionError("latest tool output is not the submitted marker")
        if "\n".join(output.splitlines()[1:]) != returned_text:
            raise SubmissionError("reported patch differs from captured submission")
        patch = output[len(lines[0]) :]
    try:
        record = {
            "schema_version": 1,
            "record_type": "coding_submission",
            "capture_policy": CAPTURE_POLICY,
            "task_id": task_id,
            "base_commit": base_commit,
            "model_name_or_path": model_name,
            "upstream_status": status,
            "upstream_returned_text": returned_text,
            "upstream_returned_text_sha256": digest(returned_text),
            "model_patch": patch,
            "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            "serialization_changed": status == "Submitted" and patch != returned_text,
            "terminal_tool_receipt_index": receipt_index,
            "tool_events": captured,
            "tool_events_sha256": digest(captured),
            "provenance": deepcopy(dict(provenance)),
        }
        canonical(record)
    except (TypeError, ValueError, UnicodeError) as error:
        raise SubmissionError("submission is not finite UTF-8 JSON") from error
    return record


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SubmissionError("duplicate submission JSON key")
        result[key] = value
    return result


def load_submission(path: Path) -> dict[str, Any]:
    try:
        record = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
        if not isinstance(record, dict):
            raise SubmissionError("submission must be an object")
        expected = capture_submission(
            task_id=record["task_id"],
            base_commit=record["base_commit"],
            model_name=record["model_name_or_path"],
            status=record["upstream_status"],
            returned_text=record["upstream_returned_text"],
            events=record["tool_events"],
            provenance=record["provenance"],
        )
        if digest(record) != digest(expected):
            raise SubmissionError("submission fields or receipt digests differ")
        return record
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise SubmissionError("malformed submission artifact") from error


def private_write(path: Path, record: Any) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical(record) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
