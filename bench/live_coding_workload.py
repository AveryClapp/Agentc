"""Fresh mini-SWE-agent coding runs with the original campaign ledger.

The upstream agent drives actual commands in separate task containers. Fixed
compression changes only the next model input; each arm owns its actual outputs.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from bench.openrouter_pilot import (
    Ledger,
    PilotError,
    account,
    canonical,
    digest,
    load_key,
    make_request,
    money,
    request_json,
)

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = Path("/Users/averyclapp/Documents/Coding/GitProjects/Agentc")
MINI_COMMIT = "a2b0efee5fd9c1d3bc9fdfc6fe88036a2235d74d"
DATA_COMMIT = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"
DATA_SHA = "a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd"
MODEL = "openai/gpt-4.1-mini"
ENDPOINT = "openai/gpt-4.1-mini-2025-04-14"
SEED = 2026090903
STAGE_CAP = Decimal("0.50")
STEPS = 20
MAX_TOKENS = 2048
OWN_SOURCE = (
    "bench/live_coding_workload.py",
    "bench/openrouter_pilot.py",
    "bench/openrouter_attempts.py",
    "bench/openrouter_groups.py",
    "bench/openrouter_transport.py",
    "python/agentc/_attention.py",
    "crates/agentc-optimizer/src/rules/context_compress.rs",
    "crates/agentc-optimizer/examples/typed_rule_materializer.rs",
    "crates/agentc-optimizer/tests/protected_unknown.rs",
)
UPSTREAM_SOURCE = (
    "src/microsweagent/__init__.py",
    "src/microsweagent/agents/default.py",
    "src/microsweagent/environments/docker.py",
    "src/microsweagent/config/extra/swebench.yaml",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, data) -> None:
    with path.open("xb") as stream:
        stream.write(canonical(data) + b"\n")
    path.chmod(0o600)


def command(argv, *, timeout=60, input=None):
    result = subprocess.run(
        argv, input=input, capture_output=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise PilotError("local command failed: " + str(argv[0]))
    return result.stdout


def selected_ids(rows, excluded):
    ids = []
    for repo in ("pytest-dev/pytest", "psf/requests"):
        choices = [
            r["instance_id"]
            for r in rows
            if r["repo"] == repo and r["instance_id"] not in excluded
        ]
        if not choices or len(choices) != len(set(choices)):
            raise PilotError("invalid task inventory")
        ids.append(
            min(
                choices,
                key=lambda task: hashlib.sha256(f"{SEED}\n{task}".encode()).hexdigest(),
            )
        )
    return ids


def verify_endpoint(key):
    rows = request_json(f"/models/{MODEL}/endpoints", key)["data"]["endpoints"]
    matches = [r for r in rows if r.get("tag") == "openai"]
    if len(matches) != 1:
        raise PilotError("endpoint unavailable or ambiguous")
    row = matches[0]
    if (
        row.get("name") != "OpenAI | " + ENDPOINT
        or row.get("provider_name") != "OpenAI"
        or row.get("model_id") != MODEL
        or not {"temperature", "max_tokens"}.issubset(
            row.get("supported_parameters", [])
        )
        or money(row["pricing"]["prompt"]) > Decimal("0.0000004")
        or money(row["pricing"]["completion"]) > Decimal("0.0000016")
    ):
        raise PilotError("pinned coding endpoint changed")
    return row


def prepare(cache: Path, excluded_path: Path, binary: Path, output: Path):
    import pyarrow.parquet as pq

    if sha(cache / "verified.parquet") != DATA_SHA:
        raise PilotError("task dataset hash mismatch")
    excluded = json.loads(excluded_path.read_bytes())["ids"]
    inventory = pq.read_table(
        cache / "verified.parquet", columns=["instance_id", "repo"]
    ).to_pylist()
    ids = selected_ids(inventory, excluded)
    # Public allowlist only; gold patches and test oracle stay in the parquet
    # for the separate evaluator, which is never mounted into agent containers.
    fields = ["instance_id", "repo", "base_commit", "problem_statement", "version"]
    rows = pq.read_table(cache / "verified.parquet", columns=fields).to_pylist()
    tasks = [next(r for r in rows if r["instance_id"] == task) for task in ids]
    public_path = cache / "public-tasks.json"
    mini = cache / "mini-source"
    if (
        command(["git", "-C", str(mini), "rev-parse", "HEAD"]).decode().strip()
        != MINI_COMMIT
    ):
        raise PilotError("upstream agent checkout differs")
    images = {}
    for task in ids:
        tag = (
            "swebench/sweb.eval.x86_64."
            + task.replace("__", "_1776_").lower()
            + ":latest"
        )
        info = json.loads(command(["docker", "image", "inspect", tag]))[0]
        names = info.get("RepoDigests", [])
        exact = [
            n for n in names if n.startswith(tag.removesuffix(":latest") + "@sha256:")
        ]
        if len(exact) != 1 or info.get("Architecture") != "amd64":
            raise PilotError("task image is not a pinned amd64 image")
        images[task] = {"reference": exact[0], "id": info["Id"]}
    key = load_key(CAMPAIGN / ".env")
    endpoint = verify_endpoint(key)
    ledger = Ledger(CAMPAIGN / ".git/openrouter-pilot-budget.jsonl", key)
    manifest = {
        "version": "live-mini-coding-v1",
        "seed": SEED,
        "ids": ids,
        "excluded": excluded,
        "dataset_revision": DATA_COMMIT,
        "dataset_sha256": DATA_SHA,
        "public_sha256": hashlib.sha256(canonical(tasks) + b"\n").hexdigest(),
        "agent_commit": MINI_COMMIT,
        "source_sha256": {p: sha(ROOT / p) for p in OWN_SOURCE},
        "upstream_sha256": {p: sha(mini / p) for p in UPSTREAM_SOURCE},
        "binary_path": str(binary.resolve()),
        "binary_sha256": sha(binary),
        "images": images,
        "model": MODEL,
        "endpoint": endpoint,
        "steps_per_arm": STEPS,
        "max_output_tokens": MAX_TOKENS,
        "stage_cap_usd": str(STAGE_CAP),
        "ledger_path": str(ledger.path),
        "ledger_initial_sha256": sha(ledger.path),
        "ledger_initial": ledger.summary(),
        "account_initial": account(key),
        # Counterbalance order deterministically without selecting task outcomes.
        "arm_order": [
            [ids[0], "source"],
            [ids[0], "compress"],
            [ids[1], "compress"],
            [ids[1], "source"],
        ],
        "purpose": "development pilot; no learned-policy or heldout claim",
    }
    save(public_path, tasks)
    save(output, manifest)
    return {"manifest_sha256": sha(output), "ids": ids}


def load_manifest(path: Path, expected_hash: str, cache: Path):
    if sha(path) != expected_hash:
        raise PilotError("manifest hash mismatch")
    manifest = json.loads(path.read_bytes())
    if (
        manifest["model"] != MODEL
        or manifest["steps_per_arm"] != STEPS
        or manifest["max_output_tokens"] != MAX_TOKENS
        or money(manifest["stage_cap_usd"]) != STAGE_CAP
        or manifest["ledger_path"]
        != str(CAMPAIGN / ".git/openrouter-pilot-budget.jsonl")
    ):
        raise PilotError("manifest changed experiment or budget controls")
    for p, expected in manifest["source_sha256"].items():
        if p not in OWN_SOURCE or sha(ROOT / p) != expected:
            raise PilotError("runner source differs from frozen manifest")
    if set(manifest["source_sha256"]) != set(OWN_SOURCE):
        raise PilotError("incomplete runner source closure")
    for p, expected in manifest["upstream_sha256"].items():
        if p not in UPSTREAM_SOURCE or sha(cache / "mini-source" / p) != expected:
            raise PilotError("upstream source differs from frozen manifest")
    if set(manifest["upstream_sha256"]) != set(UPSTREAM_SOURCE):
        raise PilotError("incomplete upstream source closure")
    if (
        sha(cache / "public-tasks.json") != manifest["public_sha256"]
        or sha(Path(manifest["binary_path"])) != manifest["binary_sha256"]
    ):
        raise PilotError("public tasks or native binary changed")
    return manifest


def load_attention():
    spec = importlib.util.spec_from_file_location(
        "live_attention", ROOT / "python/agentc/_attention.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compress(messages, binary: Path, identity: str):
    if (
        len(messages) < 2
        or messages[0]["role"] != "system"
        or messages[1]["role"] != "user"
        or any(
            set(m) != {"role", "content"} or not isinstance(m["content"], str)
            for m in messages
        )
    ):
        raise PilotError("unsupported upstream request shape")
    scores, follow = load_attention().compute_attention_scores(messages, None)
    deps = [{"kind": "literal"} for _ in messages]
    deps[1] = {"kind": "user_input", "span_id": digest(identity)[:16]}
    call = {
        "call_site_id": "live-mini-coding-v1",
        "trace_id": digest(identity)[:32],
        "span_id": digest([identity, len(messages)])[:16],
        "model": MODEL,
        "messages": deepcopy(messages),
        "tools": [],
        "input_deps": deps,
        "parameters": {
            "extra": {
                "message_deps": deps,
                "window_state_reads": [],
                "attention_scores": scores,
                "follow_on_tokens": follow,
                "dead_attention_epsilon": 0.10,
            }
        },
    }
    raw = command(
        [str(binary)],
        input=canonical({"call": call, "rule": "ContextCompress"}),
        timeout=15,
    )
    result = json.loads(raw)
    indices = result.get("retained_indices")
    if (
        not isinstance(indices, list)
        or any(type(i) is not int for i in indices)
        or indices != sorted(set(indices))
        or not {0, 1}.issubset(indices)
        or any(i < 0 or i >= len(messages) for i in indices)
    ):
        raise PilotError("native proposal did not retain original instructions")
    expected = deepcopy(call)
    expected["messages"] = [messages[i] for i in indices]
    if (
        result.get("call") != expected
        or result.get("rule") != "ContextCompress"
        or result.get("materialization_only") is not True
        or result.get("applied") != (len(indices) < len(messages))
    ):
        raise PilotError("native result is not an exact ordered deletion")
    return deepcopy(expected["messages"]), {
        "retained_indices": indices,
        "applied": result["applied"],
        "abstention_reason": result.get("abstention_reason"),
    }


class Transcript:
    def __init__(self, path):
        self.stream = path.open("x", encoding="utf-8")
        path.chmod(0o600)

    def append(self, event):
        self.stream.write(canonical(event).decode() + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())


@dataclass
class ModelConfig:
    model_name: str = MODEL


class BudgetModel:
    def __init__(self, ledger, key, stage, task, arm, binary, transcript):
        self.config = ModelConfig()
        self.ledger, self.key, self.stage = ledger, key, stage
        self.task, self.arm, self.binary, self.transcript = (
            task,
            arm,
            binary,
            transcript,
        )
        self.n_calls = 0
        self.cost = 0.0
        self.receipts = []

    def query(self, messages):
        original = deepcopy(messages)
        outgoing, rewrite = (
            original,
            {"applied": False, "retained_indices": list(range(len(original)))},
        )
        if self.arm == "compress":
            outgoing, rewrite = compress(original, self.binary, self.task)
        payload = make_request(MODEL, ["openai"], outgoing, max_tokens=MAX_TOKENS)
        call_id = f"{self.stage}-{self.task}-{self.arm}-{self.n_calls:03d}"
        metadata = {
            "task": self.task,
            "arm": self.arm,
            "turn": self.n_calls,
            "original_messages_sha256": digest(original),
            "rewrite": rewrite,
            "dispatch_contract": {
                "provider_name": "OpenAI",
                "endpoint_model": ENDPOINT,
            },
        }
        verify_endpoint(self.key)
        self.transcript.append(
            {
                "event": "request",
                "id": call_id,
                "original_messages": original,
                "payload": payload,
                "metadata": metadata,
            }
        )
        result = self.ledger.call(
            self.key, call_id, self.stage, STAGE_CAP, payload, metadata
        )
        self.transcript.append({"event": "response", "id": call_id, "result": result})
        self.n_calls += 1
        self.cost += float(money(result["cost_usd"]))
        self.receipts.append(
            {
                "id": call_id,
                "cost_usd": result["cost_usd"],
                "usage": result["usage"],
                "finish_reason": result["finish_reason"],
                "rewritten": rewrite["applied"],
            }
        )
        if result["finish_reason"] != "stop":
            raise PilotError("truncated response cannot drive a shell action")
        return {"content": result["answer"]}


def upstream(cache):
    # Keep upstream dotenv bootstrap confined to an empty task-local directory.
    config = cache / "mini-config"
    config.mkdir(exist_ok=True, mode=0o700)
    if (config / ".env").exists():
        raise PilotError("upstream config must not contain provider credentials")
    os.environ["MICRO_SWE_AGENT_GLOBAL_CONFIG_DIR"] = str(config)
    os.environ["MICRO_SWE_AGENT_SILENT_STARTUP"] = "1"
    sys.path.insert(0, str(cache / "mini-source/src"))
    from microsweagent.agents.default import DefaultAgent
    from microsweagent.environments.docker import DockerEnvironment

    return DefaultAgent, DockerEnvironment


def initialize_task(environment, base_commit):
    """Select the task commit only inside a new, clean benchmark container."""
    if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
        raise PilotError("invalid task base commit")
    status = environment.execute("git status --porcelain --untracked-files=all")
    if status["returncode"] or status["output"].strip():
        raise PilotError("new task container has inherited worktree changes")
    selected = environment.execute("git checkout --detach " + base_commit)
    if selected["returncode"]:
        raise PilotError("cannot select task base commit")
    head = environment.execute("git rev-parse HEAD")
    status = environment.execute("git status --porcelain --untracked-files=all")
    if (
        head["returncode"]
        or head["output"].strip() != base_commit
        or status["returncode"]
        or status["output"].strip()
    ):
        raise PilotError("container is not clean at the selected task base commit")


def run(manifest, cache: Path, output: Path):
    import yaml

    if output.exists():
        raise PilotError("run directory exists; no implicit retry or restart")
    output.mkdir(parents=True, mode=0o700)
    save(output / "claim.json", {"manifest_digest": digest(manifest)})
    tasks = {
        r["instance_id"]: r
        for r in json.loads((cache / "public-tasks.json").read_bytes())
    }
    if set(tasks) != set(manifest["ids"]):
        raise PilotError("public task inventory changed")
    key = load_key(CAMPAIGN / ".env")
    ledger = Ledger(Path(manifest["ledger_path"]), key)
    stage = "live-mini-coding-v1-" + digest(manifest)[:16]
    DefaultAgent, DockerEnvironment = upstream(cache)
    config = yaml.safe_load(
        (cache / "mini-source/src/microsweagent/config/extra/swebench.yaml").read_text()
    )

    class Environment(DockerEnvironment):
        def execute(self, command, cwd=""):
            started = time.monotonic()
            try:
                result = super().execute(command, cwd)
            except BaseException:
                transcript.append({"event": "tool_failure", "command": command})
                raise
            transcript.append(
                {
                    "event": "tool",
                    "command": command,
                    "result": result,
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
            return result

        def cleanup(self):
            if self.container_id is not None:
                identity, self.container_id = self.container_id, None
                if not re.fullmatch(r"[0-9a-f]{64}", identity):
                    raise PilotError("invalid owned container identity")
                command([self.config.executable, "rm", "-f", identity], timeout=30)

    summaries = []
    abort = False
    for task_id, arm in manifest["arm_order"]:
        if abort:
            summaries.append(
                {"task": task_id, "arm": arm, "status": "not_run_after_stop"}
            )
            continue
        task = tasks[task_id]
        transcript = Transcript(output / f"{task_id}-{arm}.jsonl")
        model = BudgetModel(
            ledger, key, stage, task_id, arm, Path(manifest["binary_path"]), transcript
        )
        environment = None
        status, patch = "not_started", ""
        try:
            image = manifest["images"][task_id]
            current = json.loads(
                command(["docker", "image", "inspect", image["reference"]])
            )[0]
            if current["Id"] != image["id"]:
                raise PilotError("task image changed")
            environment = Environment(
                **(
                    config["environment"]
                    | {
                        "image": image["reference"],
                        "run_args": [
                            "--platform",
                            "linux/amd64",
                            "--network",
                            "none",
                            "--cpus",
                            "2",
                            "--memory",
                            "4g",
                            "--pids-limit",
                            "512",
                            "--security-opt",
                            "no-new-privileges",
                        ],
                    }
                )
            )
            initialize_task(environment, task["base_commit"])
            agent = DefaultAgent(
                model, environment, **(config["agent"] | {"step_limit": STEPS})
            )
            status, patch = agent.run(task["problem_statement"])
            if status != "Submitted":
                patch = ""
            save(
                output / f"{task_id}-{arm}.prediction.json",
                {
                    "instance_id": task_id,
                    "model_name_or_path": stage + "-" + arm,
                    "model_patch": patch,
                },
            )
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 — durable stop, never retry
            status = type(exc).__name__
            transcript.append({"event": "stop", "kind": status})
            abort = True  # Unknown provider/local failures never trigger a retry.
        finally:
            if environment is not None:
                environment.cleanup()
            transcript.stream.close()
        summaries.append(
            {
                "task": task_id,
                "arm": arm,
                "status": status,
                "calls": model.n_calls,
                "cost_usd": str(
                    sum((money(r["cost_usd"]) for r in model.receipts), Decimal(0))
                ),
                "receipts": model.receipts,
                "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            }
        )
        print(
            json.dumps(
                {"task": task_id, "arm": arm, "status": status, "calls": model.n_calls}
            ),
            flush=True,
        )
    report = {
        "stage": stage,
        "manifest_digest": digest(manifest),
        "arms": summaries,
        "ledger_final": ledger.summary(),
        "account_final": account(key),
        "quality": "ungraded; separate official test evaluator required",
        "learned_policy": False,
    }
    save(output / "summary.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--excluded-selection", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        if args.excluded_selection is None or args.binary is None:
            parser.error("prepare requires excluded selection and native binary")
        print(
            json.dumps(
                prepare(args.cache, args.excluded_selection, args.binary, args.manifest)
            )
        )
    else:
        if args.expected_sha256 is None or args.output is None:
            parser.error("run requires expected manifest hash and fresh output")
        manifest = load_manifest(args.manifest, args.expected_sha256, args.cache)
        run(manifest, args.cache, args.output)


if __name__ == "__main__":
    main()
