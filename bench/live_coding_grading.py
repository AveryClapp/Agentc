"""Grade fresh predictions with pinned official SWE-bench scripts and parsers.

Only container lifecycle and strict model-patch application are local adapters.
Oracle fields are opened after the live run, never exposed to its agent.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
from pathlib import Path

from bench.live_coding_workload import DATA_SHA, PilotError, command, save, sha

REVISION = "b524f150d5d76f188c741d75669025f718c89c2e"


def complete_reference_tests(observed, fail_to_pass, pass_to_pass, found):
    """Missing tests cannot establish either success or a negative control."""
    required = set(fail_to_pass) | set(pass_to_pass)
    return bool(
        found
        and fail_to_pass
        and required.issubset(observed)
        and all(observed[t] in {"PASSED", "XFAIL", "FAILED", "ERROR"} for t in required)
    )


def reference_verdicts(observed, fail_to_pass, pass_to_pass, found, official_resolved):
    complete = complete_reference_tests(observed, fail_to_pass, pass_to_pass, found)
    passed = {"PASSED", "XFAIL"}
    all_pass = all(observed.get(t) in passed for t in fail_to_pass + pass_to_pass)
    stable = all(observed.get(t) in passed for t in pass_to_pass)
    regression_fails = any(observed.get(t) in {"FAILED", "ERROR"} for t in fail_to_pass)
    return {
        "complete": complete,
        "resolved": bool(complete and official_resolved and all_pass),
        "negative_control_valid": bool(complete and stable and regression_fails),
    }


def captured_patch(patch, events):
    """Restore only a final newline proven present in the actual tool receipt."""
    if not patch or patch.endswith("\n"):
        return patch
    for event in reversed(events):
        if event.get("event") != "tool":
            continue
        result = event.get("result", {})
        output = result.get("output", "").lstrip()
        marker, separator, body = output.partition("\n")
        if marker.strip() == "MICRO_SWE_AGENT_FINAL_OUTPUT" and separator:
            if result.get("returncode") == 0 and body == patch + "\n":
                return body
            break
    raise PilotError("missing patch newline is not supported by captured tool output")


def grade(cache: Path, run: Path, output: Path):
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    import pyarrow.parquet as pq
    import swebench
    from swebench.harness.grading import get_eval_report, get_logs_eval, test_failed
    from swebench.harness.test_spec.test_spec import make_test_spec

    if output.exists():
        raise PilotError("grading output exists")
    source = cache / "swe-source"
    if (
        command(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()
        != REVISION
        or sha(cache / "verified.parquet") != DATA_SHA
    ):
        raise PilotError("official grader or dataset revision changed")
    installed = Path(swebench.__file__).parent
    closure = {}
    for path in sorted((source / "swebench/harness").rglob("*.py")):
        relative = path.relative_to(source / "swebench")
        if sha(path) != sha(installed / relative):
            raise PilotError("installed official grader differs from pinned source")
        closure[str(relative)] = sha(path)
    summary = json.loads((run / "summary.json").read_bytes())
    manifest = json.loads((cache / "manifest.json").read_bytes())
    from bench.openrouter_pilot import digest

    if summary["manifest_digest"] != digest(manifest):
        raise PilotError("run does not belong to grading manifest")
    rows = {
        r["instance_id"]: r
        for r in pq.read_table(cache / "verified.parquet").to_pylist()
        if r["instance_id"] in manifest["ids"]
    }
    output.mkdir(mode=0o700)
    specs = {
        task: make_test_spec(row, namespace="swebench") for task, row in rows.items()
    }
    reports = []
    # An unchanged checkout is a negative control for each selected task.
    jobs = [(task, "unchanged_control", None) for task in manifest["ids"]]
    jobs += [(a["task"], a["arm"], a) for a in summary["arms"]]
    for task, arm, receipt in jobs:
        name = f"{task}-{arm}"
        if receipt is not None and receipt["status"] != "Submitted":
            reports.append(
                {"task": task, "arm": arm, "status": "no_submission", "resolved": False}
            )
            continue
        prediction = (
            json.loads((run / f"{name}.prediction.json").read_bytes())
            if receipt is not None
            else {"instance_id": task, "model_name_or_path": arm, "model_patch": ""}
        )
        if prediction["instance_id"] != task:
            raise PilotError("prediction task differs")
        patch = prediction["model_patch"]
        if receipt is not None:
            import hashlib

            if hashlib.sha256(patch.encode()).hexdigest() != receipt["patch_sha256"]:
                raise PilotError("prediction changed after run")
            events = [
                json.loads(line)
                for line in (run / f"{name}.jsonl").read_text().splitlines()
            ]
            patch = captured_patch(patch, events)
        job = output / name
        job.mkdir(mode=0o700)
        eval_file = job / "eval.sh"
        eval_file.write_text(specs[task].eval_script)
        eval_file.chmod(0o600)
        image = manifest["images"][task]
        current = json.loads(
            command(["docker", "image", "inspect", image["reference"]])
        )[0]
        if current["Id"] != image["id"]:
            raise PilotError("grading image changed")
        identity = (
            command(
                [
                    "docker",
                    "run",
                    "-d",
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
                    "-w",
                    "/testbed",
                    image["reference"],
                    "sleep",
                    "infinity",
                ],
                timeout=120,
            )
            .decode()
            .strip()
        )
        result = {
            "task": task,
            "arm": arm,
            "resolved": False,
            "restored_captured_terminal_newline": patch != prediction["model_patch"],
        }
        try:
            from bench.live_coding_workload import initialize_task

            class Environment:
                def execute(self, cmd):
                    val = subprocess.run(
                        [
                            "docker",
                            "exec",
                            "-w",
                            "/testbed",
                            identity,
                            "bash",
                            "-lc",
                            cmd,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    return {"output": val.stdout, "returncode": val.returncode}

            initialize_task(Environment(), rows[task]["base_commit"])
            if patch:
                # No fuzzy application: an inapplicable submission stays a failure.
                applied = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        "-w",
                        "/testbed",
                        identity,
                        "git",
                        "apply",
                        "--verbose",
                        "-",
                    ],
                    input=patch.encode(),
                    capture_output=True,
                    timeout=60,
                )
                (job / "patch-apply.log").write_bytes(applied.stdout + applied.stderr)
                if applied.returncode:
                    result["status"] = "patch_apply_failed"
                    reports.append(result)
                    continue
            command(["docker", "cp", str(eval_file), identity + ":/eval.sh"])
            with (job / "test_output.txt").open("xb") as stream:
                execution = subprocess.run(
                    ["docker", "exec", identity, "bash", "/eval.sh"],
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=600,
                )
            official = get_eval_report(
                specs[task], prediction, str(job / "test_output.txt"), True
            )[task]
            observed, found = get_logs_eval(specs[task], str(job / "test_output.txt"))
            verdicts = reference_verdicts(
                observed,
                specs[task].FAIL_TO_PASS,
                specs[task].PASS_TO_PASS,
                found,
                official["resolved"],
            )
            result.update(
                status="graded"
                if verdicts["complete"]
                else "incomplete_reference_tests",
                resolved=verdicts["resolved"],
                official=official,
                complete_reference_tests=verdicts["complete"],
                negative_control_valid=verdicts["negative_control_valid"],
                observed_failing_f2p=sum(
                    test in observed and test_failed(test, observed)
                    for test in specs[task].FAIL_TO_PASS
                ),
                eval_exit_code=execution.returncode,
                test_log_sha256=sha(job / "test_output.txt"),
                eval_script_sha256=sha(eval_file),
            )
            save(job / "report.json", result)
        except (Exception, KeyboardInterrupt) as exc:
            result.update(status="grading_error", error_type=type(exc).__name__)
        finally:
            command(["docker", "rm", "-f", identity], timeout=60)
        reports.append(result)
        print(
            json.dumps({k: result[k] for k in ("task", "arm", "status", "resolved")}),
            flush=True,
        )
    report = {
        "grader_revision": REVISION,
        "grader_source_sha256": closure,
        "adapter_sha256": sha(Path(__file__)),
        "manifest_sha256": sha(cache / "manifest.json"),
        "distributions": {
            d.metadata["Name"]: d.version for d in importlib.metadata.distributions()
        },
        "reports": reports,
        "adapter": "Official eval script and get_eval_report; isolated pinned containers; strict git apply; no fuzzy fallback",
        "negative_controls_pass": all(
            r["status"] == "graded"
            and not r["resolved"]
            and r["negative_control_valid"]
            for r in reports
            if r["arm"] == "unchanged_control"
        ),
    }
    save(output / "summary.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    grade(args.cache, args.run, args.output)
