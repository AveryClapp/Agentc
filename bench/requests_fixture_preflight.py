"""Validate the original Requests development task with an isolated HTTP fixture.

Run in the pinned SWE-bench grader environment, with its images already pulled:

    python -m bench.requests_fixture_preflight --dataset verified.parquet \
        --pytest-wheels /path/to/locked-wheels --output /tmp/requests-fixture-control

The unchanged and official-gold controls use separate clean task containers,
the unchanged official evaluation script/parser, and an internal Docker network.
This is environment admission, not an agent quality or savings experiment.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from bench.coding_submission import load_submission

TASK = "psf__requests-1766"
BASE_COMMIT = "847735553aeda6e6633f2b32e14ba14ba86887a4"
GRADER_REVISION = "b524f150d5d76f188c741d75669025f718c89c2e"
DATASET_REVISION = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"
DATASET_SHA256 = "a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd"
HARNESS_SHA256 = "e5974d8363002b3a1881591688af4774e4e174a63542fe2764b2041c9e64c008"
EVAL_SCRIPT_SHA256 = "801b90345134ba3cb1feaee673705c55a65dd693b387c10eb580a1e2738306d9"
TASK_IMAGE = (
    "swebench/sweb.eval.x86_64.psf_1776_requests-1766@"
    "sha256:0618949b48408350dfae74d27e47ff9df8192fbf03248aa4f10650e0a36f52eb"
)
TASK_IMAGE_ID = (
    "sha256:04d36df979a3cc89ceba67df58abd56e975e3b43db54ef69e94ec1ff5223d020"
)
HTTPBIN_IMAGE = (
    "kennethreitz/httpbin@"
    "sha256:599fe5e5073102dbb0ee3dbb65f049dab44fa9fc251f6835c9990f8fb196a72b"
)
HTTPBIN_IMAGE_ID = (
    "sha256:b138b9264903f46a43e1c750e07dc06f5d2a1bd5d51f37fb185bc608f61090dd"
)
# Isolated grader packages retain pytest.raises(..., "expression") on Python3.9.
# They do not change Agentc's runtime or development dependencies.
PYTEST_WHEELS = {
    "atomicwrites-1.4.0-py2.py3-none-any.whl": "6d1784dea7c0c8d4a5172b6c620f40b6e4cbfdf96d783691f2e1302a7b88e197",
    "attrs-18.2.0-py2.py3-none-any.whl": "ca4be454458f9dec299268d472aaa5a11f67a4ff70093396e1ceae9c76cf4bbb",
    "more_itertools-8.14.0-py3-none-any.whl": "1bc4f91ee5b1b31ac7ceacc17c09befe6a40a503907baf9c839c229b5095cfd2",
    "pluggy-0.13.1-py2.py3-none-any.whl": "966c145cd83c96502c3c3868f50408687b38434af77734af1e9ca461a4081d2d",
    "py-1.11.0-py2.py3-none-any.whl": "607c53218732647dff4acdfcd50cb62615cedf612e72d1724fb1a0cc6405b378",
    "pytest-4.0.2-py2.py3-none-any.whl": "f812ea39a0153566be53d88f8de94839db1e8a05352ed8a49525d7d7f37861e9",
    "setuptools-65.5.1-py3-none-any.whl": "d0b9a8433464d5800cbe05094acf5c6d52a91bfac9b52bcfc4d41382be5d5d31",
    "six-1.16.0-py2.py3-none-any.whl": "8abb2f1d86890a2dfb989f9a77cfcfd3e47c2a354b01111771326f8aa26e0254",
}


class FixtureError(RuntimeError):
    """The environment has not established a valid grading control."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _save(path: Path, value: Any) -> None:
    with path.open("x") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    path.chmod(0o600)


def reference_verdicts(
    observed: Mapping[str, str],
    fail_to_pass: Sequence[str],
    pass_to_pass: Sequence[str],
    found: bool,
    official_resolved: bool,
) -> dict[str, bool]:
    """Require every reference test; missing/skipped tests cannot admit grading."""
    required = set(fail_to_pass) | set(pass_to_pass)
    complete = bool(
        found
        and fail_to_pass
        and pass_to_pass
        and all(
            observed.get(test) in {"PASSED", "XFAIL", "FAILED", "ERROR"}
            for test in required
        )
    )
    passed = {"PASSED", "XFAIL"}
    return {
        "complete": complete,
        "resolved": bool(
            complete
            and official_resolved
            and all(observed[test] in passed for test in required)
        ),
        "negative_control_valid": bool(
            complete
            and not official_resolved
            and all(observed[test] in passed for test in pass_to_pass)
            and any(observed[test] in {"FAILED", "ERROR"} for test in fail_to_pass)
        ),
    }


def _command(argv: Sequence[str], *, input: bytes | None = None) -> bytes:
    result = subprocess.run(
        argv, input=input, capture_output=True, timeout=60, check=False
    )
    if result.returncode:
        raise FixtureError(
            f"local command failed: {argv[0]} (exit {result.returncode})"
        )
    return result.stdout


def _image_identity(reference: str, expected: str) -> dict[str, str]:
    rows = json.loads(_command(["docker", "image", "inspect", reference]))
    if len(rows) != 1 or rows[0]["Id"] != expected:
        raise FixtureError("pinned image identity differs")
    if rows[0]["Architecture"] != "amd64":
        raise FixtureError("fixture requires the original amd64 image")
    return {"reference": reference, "id": expected}


def _verified_wheels(directory: Path) -> dict[str, str]:
    observed = {path.name: _sha(path) for path in sorted(directory.glob("*.whl"))}
    if observed != PYTEST_WHEELS:
        raise FixtureError("pytest wheel inventory or digest differs from fixture lock")
    return observed


@contextmanager
def _container(arguments: Sequence[str]) -> Iterator[str]:
    identity = _command(["docker", "run", "-d", *arguments]).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise FixtureError("invalid owned container identity")
    try:
        yield identity
    finally:
        _command(["docker", "rm", "-f", identity])


def _ready(identity: str) -> None:
    # Only local fixture boot is retried. No provider or task execution retries.
    program = (
        "import ssl, urllib.request; "
        "urllib.request.urlopen('http://httpbin/get', timeout=2).read(); "
        "urllib.request.urlopen('https://httpbin/get', timeout=2, "
        "context=ssl.create_default_context(cafile='/fixture/cert.pem')).read()"
    )
    for _ in range(30):
        result = subprocess.run(
            ["docker", "exec", identity, "python3", "-c", program],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(0.2)
    raise FixtureError("local HTTP and HTTPS fixture did not become ready")


def _initialize_task(identity: str) -> None:
    prefix = ["docker", "exec", "-w", "/testbed", identity, "git", "--no-pager"]
    if _command([*prefix, "status", "--porcelain", "--untracked-files=all"]).strip():
        raise FixtureError("new task container is not clean")
    _command([*prefix, "checkout", "--detach", BASE_COMMIT])
    if _command([*prefix, "rev-parse", "HEAD"]).decode().strip() != BASE_COMMIT:
        raise FixtureError("task base commit differs")
    if _command([*prefix, "status", "--porcelain", "--untracked-files=all"]).strip():
        raise FixtureError("selected task checkout is not clean")


def _grade_patch(
    *,
    output: Path,
    network: str,
    certificates: Path,
    pytest_wheels: Path,
    spec: Any,
    name: str,
    patch: str,
    submission: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from swebench.harness.grading import get_eval_report, get_logs_eval

    job = output / name
    job.mkdir(mode=0o700)
    script = job / "eval.sh"
    script.write_text(spec.eval_script)
    script.chmod(0o600)
    with _container(
        [
            "--platform",
            "linux/amd64",
            "--network",
            network,
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
            "--mount",
            f"type=bind,src={certificates / 'cert.pem'},dst=/fixture/cert.pem,readonly",
            "--mount",
            f"type=bind,src={pytest_wheels},dst=/fixture-wheels,readonly",
            "--mount",
            f"type=bind,src={Path(__file__).with_name('_legacy_pytest_reporter.py')},dst=/fixture-reporter/agentc_reference_reporter.py,readonly",
            "--env",
            "HTTPBIN_URL=http://httpbin/",
            "--env",
            "REQUESTS_CA_BUNDLE=/fixture/cert.pem",
            "--env",
            "PYTEST_ADDOPTS=--assert=plain",
            "--env",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "--env",
            "PYTEST_PLUGINS=agentc_reference_reporter",
            "--env",
            "PYTHONPATH=/fixture-reporter",
            TASK_IMAGE,
            "sleep",
            "infinity",
        ]
    ) as identity:
        _initialize_task(identity)
        dependency_log = _command(
            [
                "docker",
                "exec",
                identity,
                "/opt/miniconda3/envs/testbed/bin/python",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--force-reinstall",
                *(f"/fixture-wheels/{name}" for name in sorted(PYTEST_WHEELS)),
            ]
        )
        (job / "dependency-install.log").write_bytes(dependency_log)
        # Session.send() bypasses REQUESTS_CA_BUNDLE in this Requests revision.
        # Extend the existing public trust bundle with the local fixture CA;
        # certificate verification stays enabled and all original roots remain.
        original_bundle = _command(
            ["docker", "exec", identity, "cat", "/testbed/requests/cacert.pem"]
        )
        if patch:
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
                check=False,
            )
            apply_log = job / "patch-apply.log"
            apply_log.write_bytes(applied.stdout + applied.stderr)
            apply_log.chmod(0o600)
            if applied.returncode:
                report = {
                    "task": TASK,
                    "control": name,
                    "status": "patch_apply_failed",
                    "complete": False,
                    "resolved": False,
                    "negative_control_valid": False,
                    "observed": {},
                    "official": None,
                    "patch_apply_exit_code": applied.returncode,
                    "patch_apply_log_sha256": _sha(apply_log),
                    "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
                    "eval_script_sha256": _sha(script),
                    "submission": dict(submission) if submission is not None else None,
                }
                _save(job / "report.json", report)
                return report
        # Apply the submitted diff to the exact clean task base before fixture
        # trust configuration can change a file's patch context.
        candidate_bundle = _command(
            ["docker", "exec", identity, "cat", "/testbed/requests/cacert.pem"]
        )
        trust_bundle = job / "trust-bundle.pem"
        trust_bundle.write_bytes(
            candidate_bundle + b"\n" + (certificates / "cert.pem").read_bytes()
        )
        trust_bundle.chmod(0o600)
        _command(
            [
                "docker",
                "cp",
                str(trust_bundle),
                f"{identity}:/testbed/requests/cacert.pem",
            ]
        )
        _command(["docker", "cp", str(script), f"{identity}:/eval.sh"])
        log = job / "test_output.txt"
        with log.open("xb") as stream:
            log.chmod(0o600)
            execution = subprocess.run(
                ["docker", "exec", identity, "bash", "/eval.sh"],
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=600,
                check=False,
            )
        prediction = {
            "instance_id": TASK,
            "model_name_or_path": name,
            "model_patch": patch,
        }
        official = get_eval_report(spec, prediction, str(log), True)[TASK]
        observed, found = get_logs_eval(spec, str(log))
        verdicts = reference_verdicts(
            observed, spec.FAIL_TO_PASS, spec.PASS_TO_PASS, found, official["resolved"]
        )
        report = {
            "task": TASK,
            "control": name,
            "status": "graded"
            if verdicts["complete"]
            else "incomplete_reference_tests",
            **verdicts,
            "eval_exit_code": execution.returncode,
            "f2p_count": len(spec.FAIL_TO_PASS),
            "p2p_count": len(spec.PASS_TO_PASS),
            "observed": observed,
            "official": official,
            "test_log_sha256": _sha(log),
            "eval_script_sha256": _sha(script),
            "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            "original_trust_bundle_sha256": hashlib.sha256(original_bundle).hexdigest(),
            "candidate_trust_bundle_sha256": hashlib.sha256(
                candidate_bundle
            ).hexdigest(),
            "fixture_trust_bundle_sha256": _sha(trust_bundle),
            "submission": dict(submission) if submission is not None else None,
        }
        _save(job / "report.json", report)
        return report


def _control(*, gold_patch: str, positive: bool, **kwargs: Any) -> dict[str, Any]:
    return _grade_patch(
        **kwargs,
        name="official_gold_control" if positive else "unchanged_control",
        patch=gold_patch if positive else "",
    )


def _load_candidates(submissions: Sequence[Path]) -> list[tuple[dict, str]]:
    candidates = []
    for path in submissions:
        record = load_submission(path)
        provenance = record["provenance"]
        if (
            record["task_id"] != TASK
            or record["base_commit"] != BASE_COMMIT
            or provenance.get("task_image") != TASK_IMAGE
            or provenance.get("task_image_id") != TASK_IMAGE_ID
            or provenance.get("dataset_sha256") != DATASET_SHA256
        ):
            raise FixtureError("submission differs from the pinned task environment")
        candidates.append((record, _sha(path)))
    if len({sha for _, sha in candidates}) != len(candidates):
        raise FixtureError("duplicate submission artifact")
    return candidates


class RequestsGradingSession:
    """One admitted HTTP fixture, with fresh official containers for each patch.

    This adapter is explicitly restricted to the original pinned Requests task.
    It cannot turn this engineering cohort into held-out SWE-bench evidence.
    """

    def __init__(self, dataset: Path, output: Path, pytest_wheels: Path):
        self.dataset, self.output, self.pytest_wheels = dataset, output, pytest_wheels
        self.admitted, self._active, self._entered = False, False, False
        self._receipts = set()
        self.candidate_reports, self.submitted_count = [], 0
        self.summary = None

    def __enter__(self):
        if self._entered:
            raise FixtureError("grading session cannot be resumed or reentered")
        self._entered = True
        self._context = self._open()
        return self._context.__enter__()

    def __exit__(self, *exc):
        return self._context.__exit__(*exc)

    def grade(self, path: Path) -> dict[str, Any]:
        if not self._active or not self.admitted:
            raise FixtureError("grading fixture has not established admission")
        record, receipt_sha = _load_candidates((path,))[0]
        if receipt_sha in self._receipts:
            raise FixtureError("duplicate submission artifact")
        self._receipts.add(receipt_sha)
        self.submitted_count += 1
        report = _grade_patch(
            **self._grading,
            name=f"candidate-{self.submitted_count - 1:03d}-{receipt_sha[:12]}",
            patch=record["model_patch"],
            submission={
                "artifact_sha256": receipt_sha,
                "upstream_status": record["upstream_status"],
                "model_name_or_path": record["model_name_or_path"],
                "serialization_changed": record["serialization_changed"],
                "patch_sha256": record["patch_sha256"],
            },
        )
        self.candidate_reports.append(report)
        return report

    @contextmanager
    def _open(self):
        dataset, output, pytest_wheels = self.dataset, self.output, self.pytest_wheels
        os.environ["PYTHON_DOTENV_DISABLED"] = "1"
        if output.exists():
            raise FixtureError(
                "output already exists; choose a new diagnostic directory"
            )
        if _sha(dataset) != DATASET_SHA256:
            raise FixtureError("pinned dataset digest differs")
        wheel_lock = _verified_wheels(pytest_wheels)
        import pyarrow.parquet as parquet
        import swebench
        from swebench.harness.test_spec.test_spec import make_test_spec

        harness = Path(swebench.__file__).parent / "harness"
        closure = {
            str(path.relative_to(harness)): _sha(path)
            for path in sorted(harness.rglob("*.py"))
        }
        if _digest(closure) != HARNESS_SHA256:
            raise FixtureError("installed official harness differs from pinned source")
        rows = [
            row
            for row in parquet.read_table(dataset).to_pylist()
            if row["instance_id"] == TASK
        ]
        if len(rows) != 1 or rows[0]["base_commit"] != BASE_COMMIT:
            raise FixtureError("original task or base commit differs")
        row = rows[0]
        spec = make_test_spec(row, namespace="swebench")
        if hashlib.sha256(spec.eval_script.encode()).hexdigest() != EVAL_SCRIPT_SHA256:
            raise FixtureError("official evaluation script differs")
        images = {
            "task": _image_identity(TASK_IMAGE, TASK_IMAGE_ID),
            "httpbin": _image_identity(HTTPBIN_IMAGE, HTTPBIN_IMAGE_ID),
        }
        output.mkdir(parents=True, mode=0o700)
        certificates = output / "certificates"
        certificates.mkdir(mode=0o700)
        _command(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(certificates / "key.pem"),
                "-out",
                str(certificates / "cert.pem"),
                "-days",
                "2",
                "-subj",
                "/CN=httpbin",
                "-addext",
                "subjectAltName=DNS:httpbin",
            ],
        )
        (certificates / "key.pem").chmod(0o600)
        fixture_command = (
            "gunicorn -b 0.0.0.0:80 httpbin:app -k gevent & "
            "exec gunicorn -b 0.0.0.0:443 httpbin:app -k gevent "
            "--certfile /fixture/cert.pem --keyfile /fixture/key.pem"
        )
        network = "agentc-requests-fixture-" + uuid.uuid4().hex
        _command(["docker", "network", "create", "--internal", network])
        reports: list[dict[str, Any]] = []
        try:
            with _container(
                [
                    "--platform",
                    "linux/amd64",
                    "--network",
                    network,
                    "--network-alias",
                    "httpbin",
                    "--security-opt",
                    "no-new-privileges",
                    "--mount",
                    f"type=bind,src={certificates.resolve()},dst=/fixture,readonly",
                    HTTPBIN_IMAGE,
                    "/bin/sh",
                    "-c",
                    fixture_command,
                ]
            ) as fixture:
                _ready(fixture)
                for positive in (False, True):
                    report = _control(
                        output=output,
                        network=network,
                        certificates=certificates.resolve(),
                        pytest_wheels=pytest_wheels.resolve(),
                        spec=spec,
                        gold_patch=row["patch"],
                        positive=positive,
                    )
                    reports.append(report)
                    print(
                        json.dumps(
                            {
                                key: report[key]
                                for key in (
                                    "control",
                                    "status",
                                    "resolved",
                                    "negative_control_valid",
                                )
                            }
                        ),
                        flush=True,
                    )
                self.admitted = bool(
                    reports[0]["negative_control_valid"] and reports[1]["resolved"]
                )
                self.provenance = {
                    "dataset_revision": DATASET_REVISION,
                    "dataset_sha256": DATASET_SHA256,
                    "grader_revision": GRADER_REVISION,
                    "harness_sha256": HARNESS_SHA256,
                    "eval_script_sha256": EVAL_SCRIPT_SHA256,
                    "images": images,
                    "pytest_wheels": wheel_lock,
                    "certificate_sha256": _sha(certificates / "cert.pem"),
                    "controls": reports,
                }
                self._grading = dict(
                    output=output,
                    network=network,
                    certificates=certificates.resolve(),
                    pytest_wheels=pytest_wheels.resolve(),
                    spec=spec,
                )
                self._active = True
                try:
                    yield self
                finally:
                    self._active = False
        finally:
            _command(["docker", "network", "rm", network])
        admitted = bool(
            len(reports) == 2
            and reports[0]["negative_control_valid"]
            and reports[1]["resolved"]
        )
        result = {
            "schema_version": 1,
            "task": TASK,
            "fixture_admitted": admitted,
            "provider_calls": 0,
            "provider_call_scope": "grading_only",
            "paper_evidence": False,
            "purpose": "fresh environment and submission diagnostics; original paid pilot stays unscored",
            "dataset_revision": DATASET_REVISION,
            "dataset_sha256": DATASET_SHA256,
            "grader_revision": GRADER_REVISION,
            "harness_sha256": HARNESS_SHA256,
            "harness_files": closure,
            "adapter_sha256": _sha(Path(__file__)),
            "legacy_reporter_sha256": _sha(
                Path(__file__).with_name("_legacy_pytest_reporter.py")
            ),
            "images": images,
            "pytest_wheels": wheel_lock,
            "environment": {
                "network": "internal; no external egress",
                "httpbin_url": "http://httpbin/",
                "https_verified": True,
                "pytest_version": "4.0.2",
                "pytest_addopts": "--assert=plain",
                "pytest_plugin_autoload": False,
                "pytest_reporting_plugin": "actual run outcomes emitted for the unchanged official parser",
                "trust_configuration": "original requests/cacert.pem plus local fixture CA; source code and official eval script unchanged",
                "certificate_sha256": _sha(certificates / "cert.pem"),
            },
            "controls": reports,
            "candidate_grades": self.candidate_reports,
            "submitted_candidate_count": self.submitted_count,
            "candidate_grades_blocked_by_environment": bool(
                self.submitted_count and not admitted
            ),
            "distributions": {
                distribution.metadata["Name"]: distribution.version
                for distribution in importlib.metadata.distributions()
            },
        }
        _save(output / "summary.json", result)
        self.summary = result


def run_preflight(
    dataset: Path,
    output: Path,
    pytest_wheels: Path,
    *,
    submissions: Sequence[Path] = (),
) -> dict[str, Any]:
    """Admit the environment before grading new captured submission artifacts."""
    if output.exists():
        raise FixtureError("output already exists; choose a new diagnostic directory")
    if _sha(dataset) != DATASET_SHA256:
        raise FixtureError("pinned dataset digest differs")
    candidates = _load_candidates(submissions)
    with RequestsGradingSession(dataset, output, pytest_wheels) as session:
        if session.admitted:
            for path in submissions:
                session.grade(path)
        else:
            session.submitted_count = len(candidates)
    return session.summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pytest-wheels", type=Path, required=True)
    parser.add_argument("--submission", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    result = run_preflight(
        args.dataset.resolve(),
        args.output.resolve(),
        args.pytest_wheels.resolve(),
        submissions=[path.resolve() for path in args.submission],
    )
    print(json.dumps({"fixture_admitted": result["fixture_admitted"]}), flush=True)
    return 0 if result["fixture_admitted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
