"""Offline, conditional replay of captured live coding request histories.

No credentials, ledger, provider calls, graders, or recorded shell commands are
used. The candidate sees observed prefixes, not trajectories it generated.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bench import live_coding_workload as live


def retention(messages, indices):
    """Count partial assistant exchanges and missing current-exchange messages."""
    if (
        not isinstance(indices, list)
        or any(type(i) is not int for i in indices)
        or indices != sorted(set(indices))
        or any(i < 0 or i >= len(messages) for i in indices)
        or not {0, 1}.issubset(indices)
    ):
        raise ValueError("invalid retained indices")
    kept = set(indices)
    starts = [i for i, m in enumerate(messages) if m["role"] == "assistant"]
    ends = starts[1:] + [len(messages)] if starts else []
    partial = sum(
        0 < sum(i in kept for i in range(start, end)) < end - start
        for start, end in zip(starts, ends)
    )
    return {
        "partial_exchanges": partial,
        "latest_action_missing": bool(starts and starts[-1] not in kept),
        "current_exchange_missing_messages": sum(
            i not in kept for i in range(starts[-1], len(messages))
        )
        if starts
        else 0,
        "retained_content_bytes": sum(
            len(messages[i]["content"].encode("utf-8")) for i in indices
        ),
    }


def validate_request(row):
    messages = row["original_messages"]
    meta = row["metadata"]
    rewrite = meta["rewrite"]
    if live.digest(messages) != meta["original_messages_sha256"]:
        raise ValueError("original history digest mismatch")
    metrics = retention(messages, rewrite["retained_indices"])
    expected = [messages[i] for i in rewrite["retained_indices"]]
    if row["payload"]["messages"] != expected:
        raise ValueError("recorded payload differs from retained messages")
    applied = len(expected) < len(messages)
    if rewrite["applied"] is not applied or (meta["arm"] == "source" and applied):
        raise ValueError("recorded rewrite status mismatch")
    return metrics


def audit(cache: Path, manifest_sha: str, candidate: Path):
    manifest_path = cache / "manifest.json"
    if live.sha(manifest_path) != manifest_sha:
        raise ValueError("manifest digest mismatch")
    manifest = json.loads(manifest_path.read_text())
    baseline = Path(manifest["binary_path"])
    if live.sha(baseline) != manifest["binary_sha256"]:
        raise ValueError("baseline binary digest mismatch")
    for name in ["bench/live_coding_workload.py", "python/agentc/_attention.py"]:
        if live.sha(live.ROOT / name) != manifest["source_sha256"][name]:
            raise ValueError("replay input construction changed")
    source_names = (
        *live.OWN_SOURCE,
        "bench/compression_history_audit.py",
        "crates/agentc-optimizer/src/ffi.rs",
        "Cargo.toml",
        "Cargo.lock",
    )
    report = {
        "version": "compression-history-audit-v1",
        "scope": "conditional replay of observed development histories; no new trajectories or efficacy estimate",
        "provider_calls": 0,
        "manifest_sha256": manifest_sha,
        "baseline_binary_sha256": live.sha(baseline),
        "candidate_binary_sha256": live.sha(candidate),
        "source_sha256": {name: live.sha(live.ROOT / name) for name in source_names},
        "traces": [],
    }
    for task, arm in manifest["arm_order"]:
        path = cache / "run" / f"{task}-{arm}.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        requests = [r for r in rows if r["event"] == "request"]
        response_ids = [r["id"] for r in rows if r["event"] == "response"]
        request_ids = [r["id"] for r in requests]
        if request_ids != response_ids or len(set(request_ids)) != len(request_ids):
            raise ValueError("request/response closure mismatch")
        receipts = []
        for turn, row in enumerate(requests):
            meta = row["metadata"]
            if (meta["task"], meta["arm"], meta["turn"]) != (task, arm, turn):
                raise ValueError("trace identity mismatch")
            observed = validate_request(row)
            messages = row["original_messages"]
            _, old = live.compress(messages, baseline, task)
            _, new = live.compress(messages, candidate, task)
            if arm == "compress" and old != meta["rewrite"]:
                raise ValueError("baseline does not reproduce actual dispatch")
            candidate_metrics = retention(messages, new["retained_indices"])
            if (
                candidate_metrics["partial_exchanges"]
                or candidate_metrics["current_exchange_missing_messages"]
            ):
                raise ValueError("candidate broke assistant exchange retention")
            receipts.append(
                {
                    "turn": turn,
                    "original_messages_sha256": live.digest(messages),
                    "original_content_bytes": sum(
                        len(m["content"].encode("utf-8")) for m in messages
                    ),
                    "observed_dispatch": observed,
                    "baseline_replay": {
                        **old,
                        **retention(messages, old["retained_indices"]),
                    },
                    "candidate_replay": {**new, **candidate_metrics},
                }
            )
        report["traces"].append(
            {
                "task": task,
                "arm": arm,
                "trace_sha256": live.sha(path),
                "receipts": receipts,
            }
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.cache, args.manifest_sha256, args.candidate)
    live.save(args.output, result)


if __name__ == "__main__":
    main()
