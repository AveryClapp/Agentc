"""Provision an owned, offline SWE-ReX runtime on an unchanged task image.

The caller supplies a frozen Linux package tree. This module downloads and
builds nothing, forwards no host environment, and only removes resources it
created. SWEEnv and the tool bundles remain the actual frozen upstream code.
"""

from __future__ import annotations

import importlib.metadata
import base64
import json
import logging
import re
import subprocess
import tarfile
import tempfile
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path

from bench.campaign_accounting import digest, json_object
from bench.coding_submission import private_write
from bench.guard_evidence import file_sha
from bench.swebench_cohort import CohortError, require

MANIFEST = "agentc-runtime-manifest.json"
TARGET = "/root/agentc-runtime-packages"
PYTHON = "/opt/miniconda3/bin/python"
REQUIRED = {
    "swe-rex": "1.2.1",
    "tree-sitter": "0.21.3",
    "tree-sitter-languages": "1.10.2",
}


def _closure(root: Path) -> dict:
    files = {}
    size = 0
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "runtime bundle contains a symlink")
        if not path.is_file() or path.name == MANIFEST or "__pycache__" in path.parts:
            continue
        size += path.stat().st_size
        require(
            size <= 512 * 1024 * 1024 and len(files) < 20000,
            "runtime bundle exceeds limits",
        )
        if path.suffix == ".so":
            with path.open("rb") as stream:
                header = stream.read(20)
            require(
                header[:6] == b"\x7fELF\x02\x01" and header[18:20] == b"\x3e\x00",
                "runtime extension is not Linux x86_64",
            )
        files[path.relative_to(root).as_posix()] = file_sha(path)
    require(bool(files), "runtime package tree is empty")
    return files


def _distributions(root: Path) -> dict:
    result = {}
    for distribution in importlib.metadata.distributions(path=[str(root)]):
        name = re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower()
        require(name not in result, "runtime has duplicate distributions")
        result[name] = distribution.version
    require(
        all(result.get(name) == version for name, version in REQUIRED.items()),
        "runtime package versions differ",
    )
    return dict(sorted(result.items()))


def freeze_bundle(root: Path) -> dict:
    """Seal an already-provisioned package tree; never install or resolve here."""
    require(root.is_dir() and not root.is_symlink(), "invalid runtime package tree")
    manifest = {
        "schema_version": 1,
        "platform": "linux/amd64",
        "python_minor": "3.11",
        "distributions": _distributions(root),
        "files": _closure(root),
    }
    private_write(root / MANIFEST, manifest)
    return manifest


@dataclass(frozen=True)
class RuntimeBundle:
    root: Path
    manifest: dict

    @classmethod
    def load(cls, root: Path):
        root = root.resolve()
        record = json_object((root / MANIFEST).read_bytes())
        require(
            set(record)
            == {"schema_version", "platform", "python_minor", "distributions", "files"}
            and record["schema_version"] == 1
            and record["platform"] == "linux/amd64"
            and record["python_minor"] == "3.11",
            "invalid runtime bundle manifest",
        )
        bundle = cls(root, record)
        bundle.verify()
        return bundle

    def verify(self):
        require(
            json_object((self.root / MANIFEST).read_bytes()) == self.manifest
            and _closure(self.root) == self.manifest["files"]
            and _distributions(self.root) == self.manifest["distributions"],
            "runtime bundle changed",
        )

    @property
    def sha256(self):
        return digest(self.manifest)

    def upload(self, container):
        self.verify()
        # Stream an archive rather than duplicating the full runtime in RAM.
        with tempfile.TemporaryFile() as stream:
            with tarfile.open(fileobj=stream, mode="w") as archive:
                for name in [*self.manifest["files"], MANIFEST]:
                    archive.add(
                        self.root / name,
                        arcname="agentc-runtime-packages/" + name,
                        recursive=False,
                    )
            stream.seek(0)
            require(container.put_archive("/root", stream), "runtime upload failed")
        self.verify()


def verify_base(container, base_commit: str, *, exact_head: bool) -> dict:
    require(re.fullmatch(r"[0-9a-f]{40}", base_commit) is not None, "invalid task base")
    values = {}
    for name, args in {
        "head": ["rev-parse", "HEAD"],
        "tree": ["rev-parse", "HEAD^{tree}"],
        "base_tree": ["rev-parse", base_commit + "^{tree}"],
        "status": ["status", "--porcelain", "--untracked-files=all"],
    }.items():
        result = container.exec_run(["git", "-C", "/testbed", *args])
        require(result.exit_code == 0, "task repository inspection failed")
        values[name] = result.output.decode().strip()
    require(
        not values["status"]
        and values["tree"] == values["base_tree"]
        and all(
            re.fullmatch(r"[0-9a-f]{40}", values[n])
            for n in ("head", "tree", "base_tree")
        )
        and (not exact_head or values["head"] == base_commit),
        "task repository differs from clean frozen base",
    )
    return {"base_commit": base_commit, **values}


class OwnedResources:
    """Track exact Docker objects through bootstrap failures and cancellation."""

    def __init__(self, client):
        self.client, self.container, self.network = client, None, None

    def drain(self):
        import docker

        if self.container is not None:
            identity = self.container.id
            try:
                self.client.containers.get(identity).remove(force=True)
            except docker.errors.NotFound:
                pass
            try:
                self.client.containers.get(identity)
            except docker.errors.NotFound:
                self.container = None
            else:
                raise RuntimeError("owned task container termination is unverified")
        if self.network is not None:
            identity = self.network.id
            try:
                self.client.networks.get(identity).remove()
            except docker.errors.NotFound:
                pass
            try:
                self.client.networks.get(identity)
            except docker.errors.NotFound:
                self.network = None
            else:
                raise RuntimeError("owned task network termination is unverified")


def create_task_container(client, owner, image_id, http_fixture=None):
    require(
        re.fullmatch(r"agentc-swe-[0-9a-f]{32}", owner) is not None
        and re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is not None,
        "invalid task container enrollment",
    )
    image = client.images.get(image_id)
    require(
        image.id == image_id
        and image.attrs["Os"] == "linux"
        and image.attrs["Architecture"] == "amd64",
        "task image identity differs",
    )
    options = (
        http_fixture.container_options()
        if http_fixture is not None
        else {"network_mode": "none"}
    )
    return client.containers.create(
        image_id,
        entrypoint=["/bin/sleep"],
        command=["infinity"],
        name=owner,
        **options,
        labels={"agentc.swe.owner": owner},
        platform="linux/amd64",
        nano_cpus=2_000_000_000,
        mem_limit="4g",
        pids_limit=512,
        security_opt=["no-new-privileges"],
    )


def verify_task_container(
    container, owner, image_id, *, fresh=False, network_id="none"
):
    container.reload()
    attrs = container.attrs
    host = attrs["HostConfig"]
    require(
        attrs["Config"].get("Labels", {}).get("agentc.swe.owner") == owner
        and attrs["Name"].lstrip("/") == owner
        and attrs["Image"] == image_id
        and host["NetworkMode"] == network_id
        and not attrs.get("Mounts"),
        "task child container ownership differs",
    )
    if fresh:
        require(
            attrs["State"]["Status"] == "created"
            and attrs["Config"]["Entrypoint"] == ["/bin/sleep"]
            and attrs["Config"]["Cmd"] == ["infinity"]
            and host["NanoCpus"] == 2_000_000_000
            and host["Memory"] == 4 * 1024**3
            and host["PidsLimit"] == 512
            and "no-new-privileges" in host["SecurityOpt"],
            "task container differs from fresh resource enrollment",
        )


def shell_startup(http_fixture=None):
    startup = (
        "export PYTHONPATH=" + TARGET + "${PYTHONPATH:+:$PYTHONPATH}\n"
        "export PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1\n"
    )
    if http_fixture is not None:
        values = http_fixture.container_options()["environment"]
        startup += (
            "export PYTHONPATH=" + shlex.quote(values["PYTHONPATH"]) + ":$PYTHONPATH\n"
        )
        startup += "".join(
            "export " + key + "=" + shlex.quote(values[key]) + "\n"
            for key in ("HTTPBIN_URL", "REQUESTS_CA_BUNDLE")
        )
    return startup


def make_deployment(
    *,
    client,
    bundle: RuntimeBundle,
    image_id: str,
    base_commit: str,
    output: Path,
    tool_source: Path,
    resource_owner: str | None = None,
    resource_container_id: str | None = None,
    http_fixture=None,
):
    """Use the official RemoteRuntime with a privately owned offline container."""
    from swerex.deployment.abstract import AbstractDeployment
    from swerex.runtime.remote import RemoteRuntime
    from swerex.runtime.abstract import IsAliveResponse, UploadResponse

    class Runtime(RemoteRuntime):
        """Preserve the official RPC server/API on a network-none container."""

        def __init__(self, container, token):
            super().__init__(host="http://127.0.0.1", auth_token=token, timeout=30)
            self.container = container

        def _http(self, method, endpoint, body=None):
            import requests

            data = (
                json.dumps(body, ensure_ascii=False).encode()
                if body is not None
                else b""
            )
            envelope = {
                "method": method,
                "endpoint": endpoint,
                "body": base64.b64encode(data).decode(),
                "token": self._config.auth_token,
            }
            script = (
                "import base64,json,sys,urllib.request,urllib.error; e=json.load(sys.stdin); "
                "r=urllib.request.Request('http://127.0.0.1:8000/'+e['endpoint'], "
                "data=base64.b64decode(e['body']) if e['method']=='POST' else None, "
                "headers={'X-API-Key':e['token'],'Content-Type':'application/json'},method=e['method']); "
                "\ntry:\n p=urllib.request.urlopen(r,timeout=900)\n"
                "except urllib.error.HTTPError as error:\n p=error\n"
                "with p:\n b=p.read(16777217); assert len(b)<=16777216; "
                "print(json.dumps({'status':p.status,'body':base64.b64encode(b).decode()}))"
            )
            timeout = 60
            if isinstance(body, dict):
                duration = body.get("timeout")
                if isinstance(duration, (int, float)) and 0 < duration <= 1800:
                    timeout = max(timeout, duration + 30)
            reply = subprocess.run(
                ["docker", "exec", "-i", self.container.id, PYTHON, "-c", script],
                input=json.dumps(envelope).encode(),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            require(reply.returncode == 0, "owned runtime HTTP transport failed")
            record = json_object(reply.stdout)
            response = requests.Response()
            response.status_code = record["status"]
            response._content = base64.b64decode(record["body"], validate=True)
            self._handle_response_errors(response)
            return response.json()

        def _request(self, endpoint, request, output_class):
            return output_class(
                **self._http(
                    "POST", endpoint, request.model_dump() if request else None
                )
            )

        async def is_alive(self, *, timeout=None):
            try:
                return IsAliveResponse(**self._http("GET", "is_alive"))
            except (CohortError, subprocess.TimeoutExpired):
                return IsAliveResponse(
                    is_alive=False, message="owned runtime is not ready"
                )

        async def upload(self, request):
            source = Path(request.source_path).resolve()
            target = Path(request.target_path)
            require(
                source.is_relative_to(tool_source.resolve())
                and source.is_dir()
                and target.is_absolute()
                and ".." not in target.parts
                and target.is_relative_to("/root/tools"),
                "unenrolled tool upload",
            )
            result = self.container.exec_run(["mkdir", "-p", str(target)])
            require(result.exit_code == 0, "tool upload directory could not be created")
            with tempfile.TemporaryFile() as stream:
                with tarfile.open(fileobj=stream, mode="w") as archive:
                    for path in sorted(source.rglob("*")):
                        require(not path.is_symlink(), "tool upload contains symlink")
                        if path.is_file():
                            archive.add(
                                path,
                                arcname=path.relative_to(source).as_posix(),
                                recursive=False,
                            )
                stream.seek(0)
                require(
                    self.container.put_archive(str(target), stream),
                    "tool upload failed",
                )
            return UploadResponse()

    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is not None,
        "invalid immutable image config",
    )
    require(
        resource_owner is None
        or re.fullmatch(r"agentc-swe-[0-9a-f]{32}", resource_owner) is not None,
        "invalid task resource owner",
    )
    require(
        resource_container_id is None
        or (
            resource_owner is not None
            and re.fullmatch(r"[0-9a-f]{64}", resource_container_id) is not None
        ),
        "invalid preallocated task container",
    )
    connection = None
    if http_fixture is not None:
        from bench.swe_requests_fixture import FixtureConnection

        connection = FixtureConnection(client, http_fixture)
    network_id = (
        connection.container_options()["network_mode"] if connection else "none"
    )

    class Deployment(AbstractDeployment):
        def __init__(self):
            self.logger = logging.getLogger("agentc.swe-runtime")
            self.resources = OwnedResources(client)
            self._runtime = None
            self._started = False
            self.enrollment = {}

        def add_hook(self, hook):
            raise RuntimeError("extra deployment hooks are not enrolled")

        def __del__(self):
            # The upstream destructor schedules a coroutine after asyncio.run
            # has closed its loop. Explicit stop remains the checked gate;
            # this fallback only removes resources still owned by this object.
            resources = getattr(self, "resources", None)
            if resources is not None:
                try:
                    resources.drain()
                except BaseException:
                    pass

        @property
        def runtime(self):
            require(self._runtime is not None, "runtime has not started")
            return self._runtime

        async def is_alive(self, *, timeout=None):
            return await self.runtime.is_alive(timeout=timeout)

        async def start(self):
            require(not self._started, "runtime cannot restart or resume")
            self._started = True
            try:
                bundle.verify()
                image = client.images.get(image_id)
                require(
                    image.id == image_id
                    and image.attrs["Os"] == "linux"
                    and image.attrs["Architecture"] == "amd64",
                    "task image identity differs",
                )
                owner = resource_owner or "agentc-swe-" + uuid.uuid4().hex
                if resource_container_id is None:
                    self.resources.container = create_task_container(
                        client, owner, image_id, connection
                    )
                else:
                    container = client.containers.get(resource_container_id)
                    require(
                        container.id == resource_container_id,
                        "task container ID differs",
                    )
                    verify_task_container(
                        container, owner, image_id, fresh=True, network_id=network_id
                    )
                    self.resources.container = container
                container = self.resources.container
                container.start()
                initial = verify_base(container, base_commit, exact_head=False)
                fixture_attachment = None
                if connection is not None:
                    fixture_attachment = connection.configure(container)
                bundle.upload(container)
                # Hash every uploaded byte inside the container before imports.
                verification = (
                    "import hashlib,json,pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                    "m=json.loads((p/sys.argv[2]).read_bytes()); "
                    "assert hashlib.sha256(json.dumps(m,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()==sys.argv[3]; "
                    "assert all(hashlib.sha256((p/k).read_bytes()).hexdigest()==v for k,v in m['files'].items()); "
                    "assert sys.version_info[:2]==(3,11)"
                )
                result = container.exec_run(
                    [PYTHON, "-c", verification, TARGET, MANIFEST, bundle.sha256]
                )
                require(
                    result.exit_code == 0,
                    "uploaded runtime bytes or Python version differ",
                )
                # Frozen SWEEnv invokes git fetch even for pre-existing repos.
                # A local origin avoids a network dependency; worktree bytes stay
                # unchanged and the subsequent checkout still uses the real base.
                result = container.exec_run(
                    [
                        "git",
                        "-C",
                        "/testbed",
                        "config",
                        "remote.origin.url",
                        "/testbed/.git",
                    ]
                )
                require(
                    result.exit_code == 0, "offline repository configuration failed"
                )
                startup = shell_startup(connection)
                result = container.exec_run(
                    [
                        PYTHON,
                        "-c",
                        "import pathlib,sys; p=pathlib.Path('/root/.bashrc'); p.write_text(p.read_text()+'\\n'+sys.argv[1])",
                        startup,
                    ]
                )
                require(result.exit_code == 0, "offline runtime shell setup failed")
                token = uuid.uuid4().hex
                container.exec_run(
                    [PYTHON, "-m", "swerex.server", "--auth-token", token],
                    environment={"PYTHONPATH": TARGET},
                    detach=True,
                )
                self._runtime = Runtime(container, token)
                await self._runtime.wait_until_alive(timeout=45)
                self.enrollment = {
                    "schema_version": 1,
                    "image_id": image_id,
                    "runtime_bundle_sha256": bundle.sha256,
                    "runtime_distributions": bundle.manifest["distributions"],
                    "initial_base": initial,
                    "network": "internal_http_fixture" if connection else "none",
                    "http_fixture": http_fixture["definition"] if connection else None,
                    "http_fixture_attachment": fixture_attachment,
                    "runtime_transport": "docker_exec_loopback_http",
                    "repository_origin": "/testbed/.git",
                    "cpus": 2,
                    "memory_bytes": 4 * 1024**3,
                    "pids_limit": 512,
                }
                private_write(output / "runtime-enrollment.json", self.enrollment)
            except BaseException:
                await self.stop()
                raise

        async def stop(self):
            # Container removal terminates the entire runtime, including shell
            # children. Avoid an unbounded close HTTP call during cancellation.
            self.resources.drain()
            self._runtime = None

    return Deployment()


def make_environment(*, deployment, base_commit: str):
    from sweagent.environment.repo import PreExistingRepoConfig
    from sweagent.environment.swe_env import SWEEnv

    return SWEEnv(
        deployment=deployment,
        repo=PreExistingRepoConfig(repo_name="testbed", base_commit=base_commit),
        post_startup_commands=[],
    )
