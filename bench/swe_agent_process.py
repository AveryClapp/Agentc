"""Fresh task processes with pre-import storage, parent billing and OS counters.

Persistent profiles live only in the enrolled arm/repetition/partition store.
Each task gets a new interpreter; initialization and shutdown are measured.
The parent grades the sealed submission after the child and its probes exit.
"""

from __future__ import annotations

import json
import os
import resource
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.campaign_accounting import canonical, digest
from bench.campaign_rpc import MAX_PACKET, ChildTransport, TransportBridge
from bench.campaign_transport import CampaignStopped
from bench.coding_submission import private_write
from bench.process_resources import process_usage, usage_record
from bench.swebench_cohort import require
from bench.swe_agent_runtime import create_task_container, verify_task_container
from bench.swe_requests_fixture import (
    RequestsHTTPFixture,
    definition as http_fixture_definition,
)

_SERIAL_LAUNCH = threading.Lock()


def runtime_environment(config, arm, storage):
    rules = list(config.rewrite_rules) if arm != "routing_only" else []
    if arm in {"routing_only", "current_greedy", "joint_guarded"}:
        rules.insert(0, "ModelDowngrade")
    return {
        "AGENTC_STORAGE_PATH": str(storage),
        "AGENTC_OPTIMIZE": "0"
        if arm in {"unmodified_fixed_strong", "fixed_cheap", "trace_only_fixed_strong"}
        else "1",
        "AGENTC_COMPOSE": "1",
        "AGENTC_MODEL_CATALOG_OFFLINE": "1",
        "AGENTC_EVAL_PLANNER_MODE": arm
        if arm in {"best_static_joint", "route_then_rewrite", "rewrite_then_route"}
        else "current_greedy"
        if arm == "current_greedy"
        else "joint_guarded",
        "AGENTC_ENABLED_RULES": ",".join(rules),
        "AGENTC_OPTIMIZE_EXPLORATION": "1",
        "AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION": "completion_first",
        "AGENTC_OPTIMIZE_SHADOW": "0.02",
        "AGENTC_OPTIMIZE_SELECTION_AUDIT": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": str(config.max_overhead_ms),
    }


def _owned_containers(
    client, owner, image_id, *, remove, container_id=None, network_id="none"
):
    import docker

    objects = client.containers.list(
        all=True, filters={"label": "agentc.swe.owner=" + owner}
    )
    if container_id is not None and not any(c.id == container_id for c in objects):
        try:
            objects.append(client.containers.get(container_id))
        except docker.errors.NotFound:
            pass
    for container in objects:
        verify_task_container(container, owner, image_id, network_id=network_id)
        if remove:
            identity = container.id
            container.remove(force=True)
            try:
                client.containers.get(identity)
            except docker.errors.NotFound:
                pass
            else:
                raise CampaignStopped("task child container removal is unverified")
    if not remove:
        require(not objects, "task child left an owned container")


def _stop(process):
    # Keep the leader unreaped until the whole group has been killed. Its PID
    # cannot be reused while a descendant ignores TERM or retains stdout.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    time.sleep(2)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # An unreaped, already-terminal leader can produce EPERM on Darwin.
        # Accept it only after proving terminal status and an absent group.
        pid, status, usage = os.wait4(process.pid, os.WNOHANG)
        if not pid:
            raise
        process.returncode = os.waitstatus_to_exitcode(status)
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return usage
        raise CampaignStopped("owned task process group termination is unverified")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        pid, status, usage = os.wait4(process.pid, os.WNOHANG)
        if pid:
            process.returncode = os.waitstatus_to_exitcode(status)
            return usage
        time.sleep(0.02)
    raise CampaignStopped("owned task process termination is unverified")


def launch_task(
    *,
    transport,
    inputs,
    environment_report,
    bundle,
    output,
    cell,
    storage,
    config,
    policy=None,
    calibration_metadata=None,
    timeout_seconds=1200,
):
    import docker

    require(
        type(timeout_seconds) is int and 0 < timeout_seconds <= 1800,
        "invalid task process deadline",
    )
    require(
        not (output / "process-enrollment.json").exists()
        and not (output / "container-create-intent.json").exists(),
        "task process cannot retry or resume",
    )
    with _SERIAL_LAUNCH:
        owner = "agentc-swe-" + uuid.uuid4().hex
        pin = next(p for p in inputs.pins["checks"] if p["task_id"] == cell.task_id)
        client = docker.from_env(timeout=10)
        adapter = client.api.get_adapter(client.api.base_url)
        endpoint = getattr(adapter, "socket_path", None)
        require(
            client.api.base_url == "http+docker://localhost"
            and isinstance(endpoint, str)
            and Path(endpoint).is_socket(),
            "task process requires the enrolled local Docker socket",
        )
        started = time.monotonic()
        before_parent = resource.getrusage(resource.RUSAGE_SELF)
        before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
        bridge = TransportBridge(
            transport,
            cell={
                "actor": "agent",
                "task_id": cell.task_id,
                "arm": cell.arm,
                "cell_id": digest(cell.__dict__),
            },
            deadline=started + timeout_seconds,
        )
        request = {
            "schema_version": 1,
            "inputs": {
                name: str(getattr(inputs, name))
                for name in (
                    "dataset",
                    "agent",
                    "evaluator",
                    "task_assets",
                    "image_pins",
                )
            },
            "environment_report": str(environment_report),
            "bundle": str(bundle.root),
            "output": str(output),
            "cell": cell.__dict__,
            "storage": str(storage),
            "configuration": config.__dict__,
            "policy": policy,
            "calibration_metadata": calibration_metadata,
            "resource_owner": owner,
        }
        environment = {
            k: os.environ[k]
            for k in ("PATH", "LANG", "LC_ALL", "TZ")
            if k in os.environ
        }
        environment.update(runtime_environment(config, cell.arm, storage))
        environment["DOCKER_HOST"] = "unix://" + endpoint
        environment["PYTHONUNBUFFERED"] = "1"
        process = log_thread = None
        container_id = None
        fixture = None
        network_id = "none"
        creation_attempted = False
        wait_usage = None
        log_error = []
        try:
            report = json.loads((environment_report / "summary.json").read_bytes())
            recipe = report.get("http_fixtures", {}).get(cell.task_id)
            if recipe is not None:
                require(
                    recipe == http_fixture_definition(),
                    "task HTTP fixture recipe changed",
                )
                fixture = RequestsHTTPFixture(client, output / "http-fixture")
                fixture.__enter__()
                network_id = fixture.network.id
            request["http_fixture"] = fixture.descriptor() if fixture else None
            # The child never sends a create request. A durable intent records
            # uncertainty if the parent loses the daemon's creation response.
            private_write(
                output / "container-create-intent.json",
                {"owner": owner, "image_id": pin["config_digest"]},
            )
            creation_attempted = True
            container = (
                create_task_container(client, owner, pin["config_digest"], fixture)
                if fixture
                else create_task_container(client, owner, pin["config_digest"])
            )
            container_id = container.id
            private_write(
                output / "container-created.json",
                {
                    "owner": owner,
                    "image_id": pin["config_digest"],
                    "container_id": container_id,
                },
            )
            verify_task_container(
                container,
                owner,
                pin["config_digest"],
                fresh=True,
                network_id=network_id,
            )
            request["resource_container_id"] = container_id
            private_write(output / "process-enrollment.json", request)
            packet = canonical({**request, "transport": bridge.descriptor()})
            require(
                len(packet) <= MAX_PACKET, "task bootstrap packet exceeds its bound"
            )
            require(
                time.monotonic() < bridge.deadline, "task startup exceeded its deadline"
            )
            # -I excludes caller PYTHONPATH/user-site/startup environment hooks.
            process = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).resolve())],
                cwd=Path(__file__).resolve().parent.parent,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            def log():
                try:
                    with (output / "process.log").open("xb") as stream:
                        retained, discarded = 0, 0
                        while chunk := process.stdout.read(65536):
                            keep = min(len(chunk), max(0, 4 * 1024**2 - retained))
                            stream.write(chunk[:keep])
                            retained += keep
                            discarded += len(chunk) - keep
                    private_write(
                        output / "process-log.json",
                        {
                            "retained_bytes": retained,
                            "discarded_bytes": discarded,
                        },
                    )
                except BaseException:
                    log_error.append(True)

            log_thread = threading.Thread(target=log, daemon=True)
            log_thread.start()
            process.stdin.write(packet)
            process.stdin.close()
            while True:
                pid, status, wait_usage = os.wait4(process.pid, os.WNOHANG)
                if pid:
                    process.returncode = os.waitstatus_to_exitcode(status)
                    break
                if bridge.failed.is_set() or time.monotonic() >= bridge.deadline:
                    raise CampaignStopped(
                        "task process stopped or exceeded its deadline"
                    )
                time.sleep(0.02)
            log_thread.join(timeout=3)
            require(
                not log_thread.is_alive() and not log_error,
                "task process log did not drain",
            )
            require(
                process.returncode == 0 and not bridge.failed.is_set(),
                "task process did not complete",
            )
            _owned_containers(
                client,
                owner,
                pin["config_digest"],
                remove=False,
                container_id=container_id,
                network_id=network_id,
            )
            receipt = json.loads((output / "process-result.json").read_bytes())
            require(
                receipt["pid"] == process.pid
                and receipt["request_sha256"] == digest(request)
                and receipt["pre_import_storage"] == str(storage)
                and receipt["agentc_loaded_at_entry"] is False,
                "task process receipt differs from enrollment",
            )
            transport.assert_complete()
            return receipt
        finally:
            try:
                try:
                    if process is not None and process.returncode is None:
                        bridge.cancelled.set()
                        wait_usage = _stop(process)
                finally:
                    try:
                        bridge.close()
                    finally:
                        try:
                            _owned_containers(
                                client,
                                owner,
                                pin["config_digest"],
                                remove=True,
                                container_id=container_id,
                                network_id=network_id,
                            )
                        except BaseException:
                            private_write(
                                output / "container-cleanup.json",
                                {
                                    "verified": False,
                                    "creation_uncertain": creation_attempted
                                    and container_id is None,
                                    "container_id": container_id,
                                },
                            )
                            raise
                        uncertain = creation_attempted and container_id is None
                        private_write(
                            output / "container-cleanup.json",
                            {
                                "verified": not uncertain,
                                "creation_uncertain": uncertain,
                                "container_id": container_id,
                            },
                        )
                        require(
                            not uncertain,
                            "task container creation remains uncertain; cannot resume",
                        )
            finally:
                try:
                    if fixture is not None:
                        fixture.close()
                finally:
                    try:
                        client.close()
                    finally:
                        if process is not None:
                            if process.stdin is not None and not process.stdin.closed:
                                process.stdin.close()
                            if log_thread is not None:
                                log_thread.join(timeout=3)
                            if process.stdout is not None:
                                process.stdout.close()
                        after_parent = resource.getrusage(resource.RUSAGE_SELF)
                        after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
                        private_write(
                            output / "process-resources.json",
                            {
                                "wall_seconds": time.monotonic() - started,
                                "task_process": usage_record(wait_usage)
                                if wait_usage is not None
                                else None,
                                "parent_user_cpu_seconds": after_parent.ru_utime
                                - before_parent.ru_utime,
                                "parent_system_cpu_seconds": after_parent.ru_stime
                                - before_parent.ru_stime,
                                "all_reaped_child_user_cpu_seconds": after_children.ru_utime
                                - before_children.ru_utime,
                                "all_reaped_child_system_cpu_seconds": after_children.ru_stime
                                - before_children.ru_stime,
                                "scope": "serialized launch including provider bridge, task process and reaped provider HTTP children; grading separate",
                                "rss_semantics": "task-process lifetime peak; parent/children RSS are not summed",
                                "exit_code": process.returncode
                                if process is not None
                                else None,
                                "environment_fixture": fixture.resources
                                if fixture
                                else None,
                            },
                        )


def _child(request):
    from contextlib import nullcontext
    from bench.swe_agent_binding import SweAgentCampaignBinding
    from bench.swe_agent_campaign_worker import (
        SweAgentTaskConfig,
        cell_identity,
        runtime_manifest,
    )
    from bench.swe_agent_runtime import RuntimeBundle
    from bench.swe_agent_task_worker import execution_version, run_task
    from bench.swebench_environment import load_inputs
    from bench.joint_campaign import ScheduledRun

    entered_with_sdk = any(
        n == "agentc" or n.startswith("agentc.") for n in sys.modules
    )
    require(not entered_with_sdk, "task imported Agentc before launch storage")
    storage = Path(request["storage"]).resolve()
    require(
        os.environ.get("AGENTC_STORAGE_PATH") == str(storage),
        "task process storage differs before SDK import",
    )
    output = Path(request["output"])
    transport = ChildTransport(request.pop("transport"))
    request_sha = digest(request)
    inputs = load_inputs(**{k: Path(v) for k, v in request["inputs"].items()})
    bundle = RuntimeBundle.load(Path(request["bundle"]))
    cell = ScheduledRun(**request["cell"])
    values = dict(request["configuration"])
    values["rewrite_rules"] = tuple(values["rewrite_rules"])
    config = SweAgentTaskConfig(**values)
    config.validate()
    instrument = cell.arm not in {"unmodified_fixed_strong", "fixed_cheap"}
    initialized = False
    scope_report = python_store = native_store = None
    with SweAgentCampaignBinding(
        transport=transport,
        source=inputs.agent,
        models=frozenset({config.strong_model, config.cheap_model}),
        output_cap=config.output_cap,
        cohort_sha256=digest(inputs.cohort),
        execution_version=execution_version(bundle),
        instrument=instrument,
    ) as binding:
        policy_scope = nullcontext()
        if request["policy"] is not None:
            require(instrument, "an uninstrumented baseline cannot use a policy")
            from agentc._evaluation_policy import (
                FrozenEvaluationPolicy,
                evaluation_policy_scope,
            )

            policy_scope = evaluation_policy_scope(
                FrozenEvaluationPolicy(**request["policy"])
            )
        try:
            if instrument:
                import agentc
                from agentc import _native
                from agentc._lifecycle import get_config

                agentc.init(storage_path=str(storage), capture_content=False)
                initialized = True
                python_store = str(Path(get_config().storage_path).resolve())
                native_store = str(Path(_native.optimize_storage_path()).resolve())
                require(
                    python_store == native_store == str(storage),
                    "task process Python/native storage differs",
                )
            import random

            random.seed(cell.run_seed)
            with policy_scope:
                submission = run_task(
                    inputs=inputs,
                    environment_report=Path(request["environment_report"]),
                    bundle=bundle,
                    binding=binding,
                    output=output / "task",
                    task_id=cell.task_id,
                    arm=cell.arm,
                    cell_id=cell_identity(cell),
                    requested_model=config.cheap_model
                    if cell.arm == "fixed_cheap"
                    else config.strong_model,
                    optimize=instrument and cell.arm != "trace_only_fixed_strong",
                    run_seed=cell.run_seed,
                    calibration_metadata=request["calibration_metadata"],
                    resource_owner=request["resource_owner"],
                    resource_container_id=request["resource_container_id"],
                    http_fixture=request.get("http_fixture"),
                    measure_resources=True,
                )
            binding.assert_complete()
            if instrument:
                scope_report = agentc.optimization_scope_report()
        finally:
            if initialized:
                agentc.shutdown()
    if not instrument:
        require(
            not any(n == "agentc" or n.startswith("agentc.") for n in sys.modules),
            "uninstrumented task imported Agentc",
        )
        require(not storage.exists(), "uninstrumented task created an Agentc store")
    private_write(
        output / "process-result.json",
        {
            "schema_version": 1,
            "pid": os.getpid(),
            "request_sha256": request_sha,
            "agentc_loaded_at_entry": entered_with_sdk,
            "pre_import_storage": str(storage),
            "instrumented": instrument,
            "python_store": python_store,
            "native_store": native_store,
            "optimization_scope": scope_report,
            "logical_agent_queries": binding._turn,
            "submission": submission,
            "plans": binding.plans,
            "cell_accounting": binding.cell_accounting(cell_identity(cell)),
            "binding_settings_version": binding.settings_version,
            "resources": process_usage(),
            "runtime": runtime_manifest(),
        },
    )


if __name__ == "__main__":
    request = None
    try:
        packet = sys.stdin.buffer.read(MAX_PACKET + 1)
        require(0 < len(packet) <= MAX_PACKET, "invalid task bootstrap size")
        request = json.loads(packet)
        _child(request)
    except BaseException as error:
        # Raw task failures are saved by run_task. No exception/body/key logging.
        if isinstance(request, dict) and isinstance(request.get("output"), str):
            import traceback

            private_write(
                Path(request["output"]) / "process-failure.json",
                {
                    "failure_type": type(error).__name__,
                    "frames": [
                        {
                            "file": Path(f.filename).name,
                            "line": f.lineno,
                            "function": f.name,
                        }
                        for f in traceback.extract_tb(error.__traceback__)
                    ],
                },
            )
        sys.stderr.write("owned task process failed\n")
        raise SystemExit(1)
