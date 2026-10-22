"""Run the actual frozen DefaultAgent and capture its reported submission.

Environment admission precedes acquisition. The agent receives only its public
problem statement and repository. Grading remains a separate operation; raw
submission and trajectory evidence survive failures and budget exhaustion.
"""

from __future__ import annotations

import os
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from bench.campaign_accounting import canonical, digest, json_object
from bench.coding_submission import private_write
from bench.guard_evidence import file_sha
from bench.swe_agent_binding import CellBudgetExhausted, verify_agent
from bench.swe_agent_runtime import (
    RuntimeBundle,
    make_deployment,
    make_environment,
    verify_base,
)
from bench.swebench_cohort import require
from bench.swebench_environment import verify_environment_report


def execution_version(bundle: RuntimeBundle) -> str:
    return digest(
        {
            "runtime_bundle": bundle.sha256,
            "runtime_harness": file_sha(
                Path(__file__).with_name("swe_agent_runtime.py")
            ),
            "task_harness": file_sha(Path(__file__)),
            "process_harness": file_sha(
                Path(__file__).with_name("swe_agent_process.py")
            ),
            "resource_harness": file_sha(
                Path(__file__).with_name("process_resources.py")
            ),
            "rpc_harness": file_sha(Path(__file__).with_name("campaign_rpc.py")),
            "http_fixture_harness": file_sha(
                Path(__file__).with_name("swe_requests_fixture.py")
            ),
        }
    )


def capture_submission(*, task, model, info, events, provenance, budget_exhausted):
    """Use the file text read by upstream handle_submission, without repairs."""
    returned = info.get("submission")
    status = info.get("exit_status")
    require(type(budget_exhausted) is bool, "invalid task budget state")
    require(
        returned is None or isinstance(returned, str), "invalid upstream submission"
    )
    require(
        status is None or isinstance(status, (int, str)), "invalid upstream exit status"
    )
    patch, index = "", None
    terminal = [(i, e) for i, e in enumerate(events) if e.get("event") == "step_done"]
    stopped = bool(events and events[-1].get("event") == "budget_exhausted")
    if stopped:
        require(
            budget_exhausted and status == "cell_budget_exhausted" and returned is None,
            "budget termination differs from its recorded stop",
        )
    else:
        require(
            bool(terminal)
            and terminal[-1][1]["step"].get("done") is True
            and terminal[-1][1]["step"].get("exit_status") == status
            and terminal[-1][1]["step"].get("submission") == returned,
            "reported result differs from terminal upstream step",
        )
        terminal_info = terminal[-1][1].get("info", {})
        require(
            terminal_info.get("exit_status") == status
            and terminal_info.get("submission") == returned,
            "reported result differs from captured upstream info",
        )
    if returned is not None:
        candidates = [
            (i, e)
            for i, e in enumerate(events)
            if e.get("event") == "file_read" and e.get("path") == "/root/model.patch"
        ]
        require(bool(candidates), "reported submission lacks an upstream patch read")
        index, receipt = candidates[-1]
        require(
            receipt["content"] == returned,
            "reported submission differs from actual patch read",
        )
        require(
            bool(terminal)
            and terminal[-1][0] > index
            and terminal[-1][1]["step"].get("submission") == returned
            and terminal[-1][1]["step"].get("done") is True,
            "patch read is not supported by the terminal upstream step",
        )
        starts = [
            i
            for i, e in enumerate(events[: terminal[-1][0]])
            if e.get("event") == "step_start"
        ]
        require(
            bool(starts) and starts[-1] < index, "submission used a stale patch read"
        )
        patch = returned
    record = {
        "schema_version": 1,
        "record_type": "frozen_swe_agent_submission",
        "task_id": task["instance_id"],
        "base_commit": task["base_commit"],
        "model_name_or_path": model,
        "upstream_exit_status": status,
        "upstream_submission": returned,
        "model_patch": patch,
        "patch_sha256": sha256(patch.encode()).hexdigest(),
        "terminal_patch_read_event_index": index,
        "budget_exhausted": budget_exhausted,
        "task_status": "failed"
        if budget_exhausted
        or not (isinstance(status, str) and status.startswith("submitted"))
        else "completed",
        "events_sha256": digest(events),
        "provenance": deepcopy(provenance),
    }
    canonical(record)
    return record


class EventSink:
    def __init__(self, output: Path):
        self.events = []
        fd = os.open(
            output / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        self.stream = os.fdopen(fd, "wb")

    def append(self, event):
        frozen = deepcopy(event)
        self.stream.write(canonical(frozen) + b"\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.events.append(frozen)

    def close(self):
        self.stream.close()


def load_submission(path: Path) -> dict:
    record = json_object(path.read_bytes())
    root = path.parent
    enrollment = json_object((root / "enrollment.json").read_bytes())
    accounting = json_object((root / "cell-accounting.json").read_bytes())
    events = [
        json_object(line) for line in (root / "events.jsonl").read_bytes().splitlines()
    ]
    expected = capture_submission(
        task=enrollment["public_task"],
        model=enrollment["agent_configuration"]["model"]["name"],
        info={
            "submission": record["upstream_submission"],
            "exit_status": record["upstream_exit_status"],
        },
        events=events,
        provenance=enrollment["provenance"],
        budget_exhausted=accounting["budget_exhausted"],
    )
    require(
        record == expected, "submission artifact differs from its execution receipts"
    )
    return record


def run_task(
    *,
    inputs,
    environment_report: Path,
    task_id: str,
    binding,
    bundle: RuntimeBundle,
    output: Path,
    requested_model: str,
    arm: str,
    cell_id: str,
    optimize: bool,
    run_seed: int,
    client=None,
    calibration_metadata=None,
    resource_owner=None,
    resource_container_id=None,
    http_fixture=None,
    measure_resources=False,
):
    import docker
    import yaml
    from sweagent.agent.agents import DefaultAgent, DefaultAgentConfig
    from sweagent.agent.models import GenericAPIModelConfig
    from sweagent.agent.problem_statement import TextProblemStatement
    from sweagent.agent.hooks.abstract import AbstractAgentHook

    inputs.verify()
    bundle.verify()
    report = verify_environment_report(inputs, environment_report)
    require(
        (http_fixture["definition"] if http_fixture else None)
        == report.get("http_fixtures", {}).get(task_id),
        "task HTTP fixture differs from admitted environment",
    )
    tasks = {t["task_id"]: t for t in inputs.cohort["tasks"]}
    require(
        task_id in tasks
        and any(
            c["task_id"] == task_id and c["environment_admitted"]
            for c in report["checks"]
        ),
        "task environment has not passed frozen controls",
    )
    require(
        binding.cohort_sha256 == digest(inputs.cohort)
        and verify_agent(inputs.agent) == binding.upstream
        and binding.execution_version == execution_version(bundle)
        and binding.source == inputs.agent.resolve()
        and type(run_seed) is int
        and 0 <= run_seed < 2**32,
        "task binding differs from its frozen execution enrollment",
    )
    require(not output.exists(), "task output already exists; no resume")
    binding.claim_execution(cell_id)
    task = deepcopy(tasks[task_id]["public_task"])
    pin = next(p for p in inputs.pins["checks"] if p["task_id"] == task_id)
    raw = yaml.safe_load((binding.source / "config/default.yaml").read_bytes())
    raw["agent"]["model"] = GenericAPIModelConfig(
        name=requested_model,
        per_instance_cost_limit=2.0,
        max_input_tokens=binding.transport.contracts[requested_model].context_tokens,
        max_output_tokens=binding.output_cap,
        completion_kwargs={"max_tokens": binding.output_cap, "seed": run_seed},
    )
    configuration = DefaultAgentConfig.model_validate(raw["agent"])
    output.mkdir(mode=0o700)
    own_client = client is None
    sink = EventSink(output)
    env, agent, deployment = None, None, None
    patches = []
    budget_exhausted = False
    provenance = {
        "public_task_sha256": digest(task),
        "cohort_sha256": digest(inputs.cohort),
        "source_closure_sha256": binding.upstream["closure_sha256"],
        "execution_version": binding.execution_version,
        "environment_report_sha256": file_sha(environment_report / "summary.json"),
        "immutable_image": pin["immutable_image"],
        "image_id": pin["config_digest"],
        "cell_id": cell_id,
        "arm": arm,
        "run_seed": run_seed,
        "transport_session_id": binding.transport.session_id,
    }

    class Capture(AbstractAgentHook):
        def on_step_start(self):
            sink.append({"event": "step_start"})

        def on_model_query(self, *, messages, agent):
            sink.append({"event": "model_query", "messages": messages, "agent": agent})

        def on_actions_generated(self, *, step):
            sink.append({"event": "actions_generated", "step": step.model_dump()})

        def on_action_executed(self, *, step):
            sink.append({"event": "action_executed", "step": step.model_dump()})

        def on_step_done(self, *, step, info):
            sink.append(
                {"event": "step_done", "step": step.model_dump(), "info": dict(info)}
            )

    try:
        if own_client:
            client = docker.from_env(timeout=60)
        private_write(
            output / "enrollment.json",
            {
                "public_task": task,
                "provenance": provenance,
                "agent_configuration": configuration.model_dump(mode="json"),
            },
        )
        deployment = make_deployment(
            client=client,
            bundle=bundle,
            image_id=pin["config_digest"],
            base_commit=task["base_commit"],
            output=output,
            tool_source=binding.source / "tools",
            resource_owner=resource_owner,
            resource_container_id=resource_container_id,
            http_fixture=http_fixture,
        )
        env = make_environment(deployment=deployment, base_commit=task["base_commit"])
        env.start()
        identity = verify_base(
            deployment.resources.container, task["base_commit"], exact_head=True
        )
        private_write(output / "task-base.json", identity)
        original_read = env.read_file

        def read_file(path, *args, **kwargs):
            content = original_read(path, *args, **kwargs)
            sink.append({"event": "file_read", "path": str(path), "content": content})
            return content

        patches.append((env, "read_file", original_read))
        env.read_file = read_file
        agent = DefaultAgent.from_config(configuration)
        agent.add_hook(Capture())
        with binding.task(
            task_id=task_id,
            arm=arm,
            cell_id=cell_id,
            requested_model=requested_model,
            optimize=optimize,
            calibration_metadata=calibration_metadata,
        ):
            try:
                result = agent.run(
                    env=env,
                    problem_statement=TextProblemStatement(
                        id=task_id,
                        text=task["problem_statement"],
                    ),
                    output_dir=output / "upstream",
                )
                info = dict(result.info)
            except CellBudgetExhausted:
                budget_exhausted = True
                info = {"submission": None, "exit_status": "cell_budget_exhausted"}
                sink.append({"event": "budget_exhausted"})
                agent.save_trajectory()
        binding.assert_complete()
        accounting = binding.cell_accounting(cell_id)
        budget_exhausted |= accounting["budget_exhausted"]
        private_write(output / "cell-accounting.json", accounting)
        private_write(output / "history.json", agent.history)
        private_write(output / "trajectory.json", agent.get_trajectory_data())
        submission = capture_submission(
            task=task,
            model=requested_model,
            info=info,
            events=sink.events,
            provenance=provenance,
            budget_exhausted=budget_exhausted,
        )
        # Close the environment and verify ownership before sealing success.
        if measure_resources:
            from bench.process_resources import container_usage

            private_write(
                output / "container-resources.json",
                container_usage(deployment.resources.container),
            )
        env.close()
        require(deployment.resources.container is None, "task container remains owned")
        inputs.verify()
        bundle.verify()
        private_write(output / "submission.json", submission)
        return submission
    except BaseException as error:
        if agent is not None and agent._problem_statement is not None:
            private_write(
                output / "partial-trajectory.json", agent.get_trajectory_data()
            )
        private_write(
            output / "failure.json",
            {"failure_type": type(error).__name__, "official_score": None},
        )
        raise
    finally:
        try:
            for target, name, original in reversed(patches):
                setattr(target, name, original)
            if env is not None:
                env.close()
            elif deployment is not None:
                deployment.resources.drain()
        finally:
            sink.close()
            if own_client and client is not None:
                client.close()
