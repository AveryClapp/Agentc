"""Fresh pinned mini-SWE-agent execution and durable submission acquisition.

The caller supplies the model and public task fields. This layer owns only the
task container and raw transcript; provider accounting belongs to the model's
owned campaign transport. It does not expose grader or dataset oracle fields.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from bench.campaign_accounting import canonical, digest
from bench.coding_submission import MINI_REVISION, capture_submission, private_write


PUBLIC_FIELDS = {"instance_id", "repo", "base_commit", "problem_statement", "version"}


class CodingTaskError(RuntimeError):
    """The task cannot run with the frozen upstream or environment."""


def source_manifest(source: Path) -> dict[str, Any]:
    from hashlib import sha256

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(source), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise CodingTaskError("cannot verify pinned mini-SWE-agent source")
        return result.stdout.strip()

    if git("rev-parse", "HEAD") != MINI_REVISION or git(
        "status", "--porcelain", "--untracked-files=all"
    ):
        raise CodingTaskError("mini-SWE-agent source differs from its pristine pin")
    files = {
        p.relative_to(source).as_posix(): sha256(p.read_bytes()).hexdigest()
        for p in sorted((source / "src/microsweagent").rglob("*"))
        if p.is_file() and p.suffix in {".py", ".yaml"}
    }
    if not files:
        raise CodingTaskError("mini-SWE-agent source is missing")
    return {"revision": MINI_REVISION, "files": files, "closure_sha256": digest(files)}


def _load_upstream(source: Path, config_dir: Path):
    import yaml

    source = source.resolve()
    manifest = source_manifest(source)
    package = source / "src/microsweagent"
    for name, module in tuple(sys.modules.items()):
        if name == "microsweagent" or name.startswith("microsweagent."):
            path = getattr(module, "__file__", None)
            if path is None or not Path(path).resolve().is_relative_to(package):
                raise CodingTaskError("foreign mini-SWE-agent module is already loaded")
    config_dir.mkdir(mode=0o700, exist_ok=False)
    previous = {
        k: os.environ.get(k)
        for k in (
            "MICRO_SWE_AGENT_GLOBAL_CONFIG_DIR",
            "MICRO_SWE_AGENT_SILENT_STARTUP",
            "PYTHON_DOTENV_DISABLED",
        )
    }
    os.environ.update(
        MICRO_SWE_AGENT_GLOBAL_CONFIG_DIR=str(config_dir),
        MICRO_SWE_AGENT_SILENT_STARTUP="1",
        PYTHON_DOTENV_DISABLED="1",
    )
    sys.path.insert(0, str(source / "src"))
    try:
        agent_module = importlib.import_module("microsweagent.agents.default")
        env_module = importlib.import_module("microsweagent.environments.docker")
        for module in (agent_module, env_module):
            if not Path(module.__file__).resolve().is_relative_to(package):
                raise CodingTaskError("upstream import escaped the pinned source")
        config = yaml.safe_load((package / "config/extra/swebench.yaml").read_bytes())
        return agent_module.DefaultAgent, env_module.DockerEnvironment, config, manifest
    finally:
        sys.path.remove(str(source / "src"))
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def initialize_task(environment: Any, base_commit: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
        raise CodingTaskError("invalid task base commit")
    result = environment.execute("git status --porcelain --untracked-files=all")
    if result["returncode"] or result["output"].strip():
        raise CodingTaskError("new task container has inherited worktree changes")
    result = environment.execute("git checkout --detach " + base_commit)
    head = environment.execute("git rev-parse HEAD")
    status = environment.execute("git status --porcelain --untracked-files=all")
    if (
        result["returncode"]
        or head["returncode"]
        or status["returncode"]
        or head["output"].strip() != base_commit
        or status["output"].strip()
    ):
        raise CodingTaskError("task container is not clean at its frozen base")


def run_task(
    *,
    source: Path,
    public_task: Mapping[str, str],
    model: Any,
    output: Path,
    image: str,
    image_id: str,
    dataset_sha256: str,
    step_limit: int,
    acquisition_kind: str,
    acquisition_context: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if (
        set(public_task) != PUBLIC_FIELDS
        or any(not isinstance(value, str) for value in public_task.values())
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", public_task["instance_id"]
        )
        or not re.fullmatch(r"[0-9a-f]{40}", public_task["base_commit"])
        or not re.fullmatch(r"[0-9a-f]{64}", dataset_sha256)
        or type(step_limit) is not int
        or step_limit < 1
        or model.n_calls != 0
        or model.cost != 0
        or (
            acquisition_context is not None
            and (
                set(acquisition_context) != {"cell_id", "transport_session_id"}
                or any(
                    not isinstance(v, str) or not v
                    for v in acquisition_context.values()
                )
            )
        )
    ):
        raise CodingTaskError("task must contain only public fields and a fresh model")
    if output.exists():
        raise CodingTaskError("task output already exists; no retry or resume")
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
        raise CodingTaskError("task image must have an immutable digest")
    inspected = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if inspected.returncode:
        raise CodingTaskError("pinned task image is unavailable")
    rows = json.loads(inspected.stdout)
    if (
        len(rows) != 1
        or rows[0]["Id"] != image_id
        or rows[0]["Architecture"] != "amd64"
    ):
        raise CodingTaskError("task image differs from its frozen identity")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        Agent, DockerEnvironment, config, upstream = _load_upstream(
            source, output / "config"
        )
    except BaseException as error:
        private_write(
            output / "failure.json",
            {
                "record_type": "unscored_coding_acquisition_failure",
                "phase": "upstream_initialization",
                "failure_type": type(error).__name__,
                "official_score": None,
            },
        )
        raise
    events = []
    stream = (output / "events.jsonl").open("x", encoding="utf-8")
    os.chmod(output / "events.jsonl", 0o600)

    def append(event):
        events.append(deepcopy(event))
        stream.write(canonical(event).decode() + "\n")
        stream.flush()
        os.fsync(stream.fileno())

    class CapturedModel:
        @property
        def config(self):
            return model.config

        @property
        def cost(self):
            return model.cost

        @property
        def n_calls(self):
            return model.n_calls

        def query(self, messages, **kwargs):
            append(
                {
                    "event": "model_request",
                    "messages": deepcopy(messages),
                    "requested_model": model.config.model_name,
                }
            )
            response = model.query(deepcopy(messages), **kwargs)
            append({"event": "model_response", "response": deepcopy(response)})
            return response

    class Environment(DockerEnvironment):
        def execute(self, command, cwd=""):
            started = time.monotonic()
            try:
                result = super().execute(command, cwd)
            except BaseException as error:
                append(
                    {
                        "event": "tool_failure",
                        "command": command,
                        "failure_type": type(error).__name__,
                    }
                )
                raise
            append(
                {
                    "event": "tool",
                    "command": command,
                    "result": result,
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
            return result

        def cleanup(self):
            identity = getattr(self, "container_id", None)
            if identity is None:
                return
            if not re.fullmatch(r"[0-9a-f]{64}", identity):
                raise CodingTaskError("invalid owned task container identity")
            result = subprocess.run(
                ["docker", "rm", "-f", identity],
                capture_output=True,
                timeout=60,
                check=False,
            )
            if result.returncode:
                raise CodingTaskError("owned task container cleanup failed")
            self.container_id = None

    environment, agent = None, None
    try:
        environment_config = config["environment"] | {
            "image": image,
            "forward_env": [],
            "run_args": [
                "--platform",
                "linux/amd64",
                "--pull",
                "never",
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
        agent_config = config["agent"] | {"step_limit": step_limit}
        enrollment = {
            "upstream_revision": MINI_REVISION,
            "upstream_source": upstream,
            "task_image": image,
            "task_image_id": image_id,
            "dataset_sha256": dataset_sha256,
            "environment": environment_config,
            "agent_configuration": agent_config,
            "public_task_sha256": digest(dict(public_task)),
            "acquisition_kind": acquisition_kind,
            "acquisition_context": dict(acquisition_context)
            if acquisition_context
            else None,
        }
        import importlib.metadata
        import platform
        from hashlib import sha256

        enrollment["runtime"] = {
            "python_version": platform.python_version(),
            "distributions": sorted(
                (dist.metadata["Name"], dist.version)
                for dist in importlib.metadata.distributions()
            ),
            "harness_files": {
                name: sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                for name in ("coding_task_worker.py", "coding_submission.py")
            },
        }
        private_write(output / "enrollment.json", enrollment)
        prepare = getattr(model, "prepare_execution", None)
        if callable(prepare):
            prepare(enrollment)
        environment = Environment(**environment_config)
        initialize_task(environment, public_task["base_commit"])
        agent = Agent(CapturedModel(), environment, **agent_config)
        status, returned = agent.run(public_task["problem_statement"])
        private_write(output / "trajectory.json", agent.messages)
        submission = capture_submission(
            task_id=public_task["instance_id"],
            base_commit=public_task["base_commit"],
            model_name=model.config.model_name,
            status=status,
            returned_text=returned,
            events=events,
            provenance=enrollment,
        )
        environment.cleanup()
        private_write(output / "submission.json", submission)
        return submission
    except BaseException as error:
        append({"event": "task_failure", "failure_type": type(error).__name__})
        private_write(
            output / "failure.json",
            {
                "record_type": "unscored_coding_acquisition_failure",
                "failure_type": type(error).__name__,
                "official_score": None,
            },
        )
        raise
    finally:
        try:
            if environment is not None:
                environment.cleanup()
        finally:
            stream.close()
