"""Local opportunity census over unchanged mini-SWE-agent v1 request histories.

No models or archived shell commands are executed. Each historical prefix is an
independent diagnostic; a rewritten prefix never receives an archived answer.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from copy import deepcopy
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "bench/repro/realistic-workloads-2026-09-09"
VERSION = "mini-swe-v1-public-history-census-v1"
EXPERIMENTS_COMMIT = "40f164d5b8f1d249bf95a6df8b74b577fd8e519d"
AGENT_COMMIT = "a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d"
SEED = 2026090902
N_TASKS = 12
MAX_BYTES = 16 * 1024 * 1024
BUCKET = "https://swe-bench-submissions.s3.amazonaws.com/"
RUNS = {
    "sonnet": "20250726_mini-v1.0.0_claude-sonnet-4-20250514",
    "gpt": "20250726_mini-v1.0.0_gpt-4.1-2025-04-14",
}
MODELS = {
    "sonnet": "claude-sonnet-4-20250514",
    "gpt": "gpt-4.1-2025-04-14",
}
SOURCE_FILES = (
    "bench/realistic_workloads.py",
    "python/agentc/_attention.py",
    "python/agentc/_provenance.py",
    "python/agentc/_patches/_optimizer_glue.py",
    "crates/agentc-optimizer/examples/typed_rule_materializer.rs",
    "crates/agentc-optimizer/src/rules/context_compress.rs",
    "crates/agentc-optimizer/src/rules/state_drop.rs",
)


class ContractError(ValueError):
    pass


def canonical(value) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def decode(raw: bytes):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ContractError("duplicate JSON key")
            value[key] = item
        return value

    def invalid(_):
        raise ContractError("nonfinite JSON value")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def save(path: Path, value) -> None:
    with path.open("xb") as stream:
        stream.write(canonical(value))
    path.chmod(0o600)


def fetch(url: str, limit: int = MAX_BYTES) -> bytes:
    with urlopen(url, timeout=30) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ContractError("source exceeds declared size limit")
    return raw


def inventory(run: str) -> list[dict]:
    """List all objects, including pagination; selection uses only task IDs."""
    rows = []
    token = None
    seen_tokens = set()
    ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
    while True:
        params = {"list-type": "2", "prefix": f"bash-only/{RUNS[run]}/trajs/"}
        if token:
            params["continuation-token"] = token
        root = ET.fromstring(fetch(BUCKET + "?" + urlencode(params)))
        for obj in root.findall("s:Contents", ns):
            rows.append(
                {key: obj.find(f"s:{key}", ns).text for key in ("Key", "Size", "ETag")}
            )
        if root.find("s:IsTruncated", ns).text == "false":
            return rows
        token = root.find("s:NextContinuationToken", ns).text
        if not token or token in seen_tokens:
            raise ContractError("invalid inventory pagination")
        seen_tokens.add(token)


def select(inventories: dict) -> dict:
    by_id = {}
    for run, rows in inventories.items():
        if run not in RUNS:
            raise ContractError("unknown run")
        found = {}
        for row in rows:
            key = row["Key"]
            if not key.endswith(".traj.json"):
                continue
            path = PurePosixPath(key)
            task = path.name.removesuffix(".traj.json")
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]+", task)
                or key != f"bash-only/{RUNS[run]}/trajs/{task}/{task}.traj.json"
                or task in found
            ):
                raise ContractError("invalid or duplicate trajectory ID")
            found[task] = row
        by_id[run] = found
    if set(by_id) != set(RUNS):
        raise ContractError("missing source run")
    common = set.intersection(*(set(rows) for rows in by_id.values()))
    if len(common) < N_TASKS:
        raise ContractError("insufficient matched tasks; no shrinking")
    ids = sorted(common, key=lambda task: (sha(f"{SEED}\n{task}".encode()), task))[
        :N_TASKS
    ]
    return {
        "seed": SEED,
        "experiments_commit": EXPERIMENTS_COMMIT,
        "inventory_counts": {run: len(rows) for run, rows in by_id.items()},
        "ids": ids,
        "files": {run: [rows[task] for task in ids] for run, rows in by_id.items()},
    }


def frozen_selection(artifact: Path) -> tuple[dict, dict]:
    inventories = {
        run: decode((artifact / f"inventory-{run}.json").read_bytes()) for run in RUNS
    }
    selection = decode((artifact / "selection.json").read_bytes())
    if select(inventories) != selection:
        raise ContractError("selection differs from frozen ID-only ranking")
    return selection, inventories


def private_directory(path: Path) -> None:
    path = path.resolve()
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ContractError("source cache must be outside Git")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def acquire(artifact: Path, cache: Path) -> dict:
    selection, _ = frozen_selection(artifact)
    private_directory(cache)
    files = []
    for run, rows in selection["files"].items():
        for task, row in zip(selection["ids"], rows, strict=True):
            size = int(row["Size"])
            if not 0 < size <= MAX_BYTES:
                raise ContractError("invalid declared source size")
            name = f"{run}-{task}.json"
            path = cache / name
            if path.exists():
                raise ContractError("refusing to overwrite cached source")
            raw = fetch(BUCKET + row["Key"], size)
            etag = row["ETag"].strip('"')
            # These public single-part S3 objects have MD5 ETags. A multipart
            # ETag is unsupported, never silently treated as a content hash.
            if (
                len(raw) != size
                or not re.fullmatch(r"[0-9a-f]{32}", etag)
                or hashlib.md5(raw, usedforsecurity=False).hexdigest() != etag
            ):
                raise ContractError("source size or ETag mismatch")
            with path.open("xb") as stream:
                stream.write(raw)
            path.chmod(0o600)
            files.append(
                {
                    "run": run,
                    "id": task,
                    "name": name,
                    "url": BUCKET + row["Key"],
                    "bytes": size,
                    "etag": etag,
                    "sha256": sha(raw),
                }
            )
    return {
        "selection_sha256": sha((artifact / "selection.json").read_bytes()),
        "files": files,
        "provider_calls": 0,
    }


def prefixes(document: dict, expected_id: str):
    """Successful-query inputs from v1's append-only DefaultAgent.messages.

    Do not consult info/submission/exit status, or count the final observation
    as a call. Unsupported schemas fail as a whole, never flatten tool objects.
    """
    if document.get("instance_id") != expected_id:
        raise ContractError("trajectory instance mismatch")
    messages = document.get("messages")
    if not isinstance(messages, list) or len(messages) < 2:
        raise ContractError("missing trajectory messages")
    for i, message in enumerate(messages):
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or not isinstance(message["content"], (str, list))
            or message["role"] not in {"system", "user", "assistant"}
        ):
            raise ContractError("unsupported message shape")
        expected_role = "system" if i == 0 else "user" if i % 2 else "assistant"
        terminal_limit = (
            i == len(messages) - 1
            and i > 1
            and messages[i - 1]["role"] == "user"
            and message["role"] == "user"
        )
        if message["role"] != expected_role and not terminal_limit:
            raise ContractError("unsupported v1 message sequence")
    for i, message in enumerate(messages):
        if message["role"] == "assistant":
            yield deepcopy(messages[:i])


def attention_module():
    spec = importlib.util.spec_from_file_location(
        "realistic_attention", ROOT / "python/agentc/_attention.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_call(
    messages: list, run: str, task: str, turn: int, attention, profile: str = "raw_sdk"
) -> dict:
    if profile not in {"raw_sdk", "task_tagged"}:
        raise ContractError("unknown provenance profile")
    if any(not isinstance(m["content"], str) for m in messages):
        raise ContractError("native bridge cannot preserve content blocks")
    scores, follow = attention.compute_attention_scores(messages, None)
    # Match the raw-SDK fallback and current proxy epsilon. Archived strings
    # have no runtime object tags; age is not evidence that a state key is dead.
    deps = [{"kind": "literal"} for _ in messages]
    if profile == "task_tagged":
        # DefaultAgent.run constructs exactly this message from the task.
        # This is an explicit adapter/instrumentation control, not inferred
        # runtime provenance and not a claim that raw SDK use protects it.
        deps[1] = {
            "kind": "user_input",
            "span_id": sha(f"{run}/{task}/task".encode())[:16],
        }
    return {
        "call_site_id": VERSION,
        "trace_id": sha(f"{run}/{task}".encode())[:32],
        "span_id": sha(f"{run}/{task}/{turn}".encode())[:16],
        "model": MODELS[run],
        "messages": deepcopy(messages),
        "tools": [],
        "input_deps": deps,
        "occurrence_ix": turn,
        "parameters": {
            "extra": {
                "message_deps": deepcopy(deps),
                "window_state_reads": [],
                "attention_scores": scores,
                "follow_on_tokens": follow,
                "dead_attention_epsilon": 0.10,
            }
        },
    }


def materialize(binary: Path, call: dict, rule: str) -> dict:
    result = subprocess.run(
        [str(binary)],
        input=canonical({"call": call, "rule": rule}),
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise ContractError("native materializer rejected request")
    value = decode(result.stdout)
    retained = value.get("retained_indices")
    if (
        not isinstance(retained, list)
        or any(type(i) is not int for i in retained)
        or retained != sorted(set(retained))
        or any(i < 0 or i >= len(call["messages"]) for i in retained)
    ):
        raise ContractError("invalid native retained indices")
    expected = deepcopy(call)
    expected["messages"] = [call["messages"][i] for i in retained]
    if (
        value.get("rule") != rule
        or value.get("materialization_only") is not True
        or value.get("call") != expected
        or type(value.get("applied")) is not bool
        or value["applied"] != (len(retained) < len(call["messages"]))
        or (value["applied"] and value.get("abstention_reason") is not None)
        or (
            not value["applied"] and not isinstance(value.get("abstention_reason"), str)
        )
    ):
        raise ContractError("invalid native materialization receipt")
    return value


def census(artifact: Path, cache: Path, binary: Path, expected_sha: str) -> dict:
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
        or sha(binary.read_bytes()) != expected_sha
    ):
        raise ContractError("native binary hash mismatch")
    selection, inventories = frozen_selection(artifact)
    acquisition = decode((artifact / "acquisition.json").read_bytes())
    if acquisition["selection_sha256"] != sha(
        (artifact / "selection.json").read_bytes()
    ):
        raise ContractError("acquisition selection mismatch")
    expected = [(run, task) for run in selection["files"] for task in selection["ids"]]
    if [(row["run"], row["id"]) for row in acquisition["files"]] != expected:
        raise ContractError("acquisition inventory mismatch")
    attention = attention_module()
    records, tasks = [], []
    for row in acquisition["files"]:
        run, task = row["run"], row["id"]
        if row["name"] != f"{run}-{task}.json":
            raise ContractError("invalid local source name")
        raw = (cache / row["name"]).read_bytes()
        source = selection["files"][run][selection["ids"].index(task)]
        if (
            row["url"] != BUCKET + source["Key"]
            or row["etag"] != source["ETag"].strip('"')
            or row["bytes"] != int(source["Size"])
            or hashlib.md5(raw, usedforsecurity=False).hexdigest() != row["etag"]
            or sha(raw) != row["sha256"]
            or len(raw) != row["bytes"]
        ):
            raise ContractError("cached source hash mismatch")
        try:
            histories = list(prefixes(decode(raw), task))
        except ContractError as exc:
            tasks.append({"run": run, "id": task, "status": str(exc), "requests": 0})
            continue
        tasks.append(
            {"run": run, "id": task, "status": "parsed", "requests": len(histories)}
        )
        for turn, messages in enumerate(histories):
            # Sonnet's cache helper mutates earlier message representations;
            # the final archive cannot establish exact historical requests.
            supported = run == "gpt" and all(
                isinstance(m["content"], str) for m in messages
            )
            original_bytes = sum(content_bytes(m["content"]) for m in messages)
            entry = {
                "run": run,
                "id": task,
                "turn": turn,
                "messages": len(messages),
                "content_bytes": original_bytes,
                "history_sha256": sha(canonical(messages)),
                "native_supported": supported,
                "profiles": {},
            }
            for profile in ("raw_sdk", "task_tagged"):
                entry["profiles"][profile] = {}
                request = (
                    make_call(messages, run, task, turn, attention, profile)
                    if supported
                    else None
                )
                for rule in ("ContextCompress", "StateDrop"):
                    value = materialize(binary, request, rule) if supported else None
                    retained = (
                        value["retained_indices"]
                        if value
                        else list(range(len(messages)))
                    )
                    remaining = sum(
                        content_bytes(messages[i]["content"]) for i in retained
                    )
                    entry["profiles"][profile][rule] = {
                        "status": (
                            "applied"
                            if value["applied"]
                            else value["abstention_reason"]
                        )
                        if value
                        else "unsupported_archived_content_blocks",
                        "retained_indices": retained,
                        "removed_bytes": original_bytes - remaining,
                        "removed_initial_instruction": 1 not in retained,
                        "removed_latest_observation": len(messages) - 1 not in retained,
                    }
            records.append(entry)
    summaries = {}
    for run in RUNS:
        calls = [r for r in records if r["run"] == run]
        summaries[run] = {
            "selected_trajectories": N_TASKS,
            "parsed_trajectories": sum(
                t["run"] == run and t["status"] == "parsed" for t in tasks
            ),
            "recorded_requests": len(calls),
            "native_supported_requests": sum(r["native_supported"] for r in calls),
            "requests_over_8192_bytes": sum(r["content_bytes"] > 8192 for r in calls),
            "content_bytes_min": min((r["content_bytes"] for r in calls), default=None),
            "content_bytes_max": max((r["content_bytes"] for r in calls), default=None),
            "profiles": {
                profile: {
                    rule: {
                        "statuses": dict(
                            Counter(
                                r["profiles"][profile][rule]["status"] for r in calls
                            )
                        ),
                        "trajectories_with_proposals": len(
                            {
                                r["id"]
                                for r in calls
                                if r["profiles"][profile][rule]["status"] == "applied"
                            }
                        ),
                        "original_content_bytes": sum(
                            r["content_bytes"] for r in calls
                        ),
                        "proposed_removed_bytes": sum(
                            r["profiles"][profile][rule]["removed_bytes"] for r in calls
                        ),
                        "proposals_removing_initial_instruction": sum(
                            r["profiles"][profile][rule]["removed_initial_instruction"]
                            for r in calls
                        ),
                    }
                    for rule in ("ContextCompress", "StateDrop")
                }
                for profile in ("raw_sdk", "task_tagged")
            },
        }
    all_ids = {
        run: {
            PurePosixPath(r["Key"]).name.removesuffix(".traj.json")
            for r in rows
            if r["Key"].endswith(".traj.json")
        }
        for run, rows in inventories.items()
    }
    union = set.union(*all_ids.values())
    return {
        "version": VERSION,
        "agent_commit": AGENT_COMMIT,
        "native_sha256": expected_sha,
        "source_sha256": {p: sha((ROOT / p).read_bytes()) for p in SOURCE_FILES},
        "artifact_sha256": {
            p: sha((artifact / p).read_bytes())
            for p in (
                "selection.json",
                "acquisition.json",
                "inventory-sonnet.json",
                "inventory-gpt.json",
            )
        },
        "missing_inventory_ids": {
            run: sorted(union - ids) for run, ids in all_ids.items()
        },
        "summaries": summaries,
        "tasks": tasks,
        "records": records,
        "state_liveness": "unobserved; raw-SDK literal fallback, no inferred dead state",
        "request_scope": "GPT saved successful-query message prefixes; hidden retries/provider transforms unavailable. Sonnet final archive has in-place cache mutations: unsupported, text sizes only.",
        "provenance_profiles": {
            "raw_sdk": "current untagged-string fallback",
            "task_tagged": "explicit source-derived task tag at index1; additional adapter control after bd-kmeg discovery, not automatic SDK behavior",
        },
        "admission_or_quality_or_cost_evidence": False,
        "provider_calls": 0,
        "archived_commands_executed": 0,
    }


def content_bytes(content) -> int:
    """Text volume only; do not serialize block metadata as if it were text."""
    if isinstance(content, str):
        return len(content.encode())
    if isinstance(content, list) and all(
        isinstance(b, dict)
        and b.get("type") == "text"
        and isinstance(b.get("text"), str)
        for b in content
    ):
        return sum(len(b["text"].encode()) for b in content)
    raise ContractError("unsupported content for text-volume census")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("select", "acquire", "census"))
    parser.add_argument("--artifact", type=Path, default=ARTIFACT)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--binary-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "select":
        args.artifact.mkdir(parents=True, exist_ok=True)
        inventories = {run: inventory(run) for run in RUNS}
        for run, rows in inventories.items():
            save(args.artifact / f"inventory-{run}.json", rows)
        result = select(inventories)
        save(args.artifact / "selection.json", result)
    elif args.command == "acquire":
        if args.cache is None:
            parser.error("acquire requires --cache")
        result = acquire(args.artifact, args.cache)
        save(args.artifact / "acquisition.json", result)
    else:
        if None in (args.cache, args.binary, args.binary_sha256, args.output):
            parser.error("census requires --cache, --binary, --binary-sha256, --output")
        result = census(args.artifact, args.cache, args.binary, args.binary_sha256)
        save(args.output, result)
    print(
        json.dumps(
            {
                "command": args.command,
                "sha256": sha(canonical(result)),
                "provider_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
