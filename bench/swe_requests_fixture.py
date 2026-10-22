"""Owned, immutable Requests HTTP dependency with an isolated trust bootstrap.

The frozen task checkout, evaluator, pytest and Requests sources stay intact.
Only the task's existing HTTPBIN_URL and fixture certificate trust are configured.
The same interface provisions reference controls and captured task grading.
"""

from __future__ import annotations

import io
import base64
import re
import subprocess
import tarfile
import time
import uuid
from pathlib import Path
from hashlib import sha256

from bench.coding_submission import private_write
from bench.guard_evidence import file_sha
from bench.process_resources import container_usage
from bench.swebench_cohort import require
from bench.swebench_cohort import CohortError

TASK = "psf__requests-1766"
TASK_IMAGE = "sha256:04d36df979a3cc89ceba67df58abd56e975e3b43db54ef69e94ec1ff5223d020"
HTTP_IMAGE = "sha256:b138b9264903f46a43e1c750e07dc06f5d2a1bd5d51f37fb185bc608f61090dd"
HTTP_MANIFEST = "kennethreitz/httpbin@sha256:599fe5e5073102dbb0ee3dbb65f049dab44fa9fc251f6835c9990f8fb196a72b"
ROOT = "/root/agentc-http-fixture"
COMMAND = (
    "gunicorn -b 0.0.0.0:80 httpbin:app -k gevent --access-logfile - & "
    "exec gunicorn -b 0.0.0.0:443 httpbin:app -k gevent --access-logfile - "
    "--certfile /root/agentc-http-service/cert.pem --keyfile /root/agentc-http-service/key.pem"
)
BOOTSTRAP = r'''"""Certificate configuration for the enrolled Requests task only."""
import importlib.abc
import pathlib
import sys

class FixtureTrust(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "requests.adapters":
            return None
        for finder in tuple(sys.meta_path):
            if finder is self:
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is None:
                continue
            if pathlib.Path(spec.origin).resolve() != pathlib.Path("/testbed/requests/adapters.py"):
                return spec
            original = spec.loader
            class Loader:
                def create_module(self, spec):
                    return original.create_module(spec)
                def exec_module(self, module):
                    original.exec_module(module)
                    source = pathlib.Path(module.DEFAULT_CA_BUNDLE_PATH).read_bytes()
                    certificate = pathlib.Path("/root/agentc-http-fixture/cert.pem").read_bytes()
                    assert 0 < len(source) <= 2097152 and 0 < len(certificate) <= 16384
                    trust = pathlib.Path("/root/agentc-http-fixture/trust.pem")
                    trust.write_bytes(source + b"\n" + certificate)
                    module.DEFAULT_CA_BUNDLE_PATH = str(trust)
                def __getattr__(self, name):
                    return getattr(original, name)
            spec.loader = Loader()
            return spec
        return None

sys.meta_path.insert(0, FixtureTrust())
'''


class FixtureOwnershipError(CohortError):
    """Stop the campaign while fixture creation or cleanup remains uncertain."""


def definition():
    return {
        "schema_version": 1,
        "implementation_sha256": file_sha(Path(__file__)),
        "task_id": TASK,
        "http_image_id": HTTP_IMAGE,
        "http_image_manifest": HTTP_MANIFEST,
        "bootstrap_sha256": sha256(BOOTSTRAP.encode()).hexdigest(),
        "command_sha256": sha256(COMMAND.encode()).hexdigest(),
        "network": "internal; no external egress",
        "httpbin_url": "http://httpbin/",
        "trust": "original candidate roots plus fixture CA; checkout unchanged",
        "pytest_and_evaluator_unchanged": True,
    }


def _upload(container, destination, files):
    with io.BytesIO() as stream:
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name, body in files.items():
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(body), 0o600
                archive.addfile(info, io.BytesIO(body))
        stream.seek(0)
        require(container.put_archive(destination, stream), "fixture upload failed")


class RequestsHTTPFixture:
    def __init__(self, client, output: Path):
        self.client, self.output = client, output
        self.owner = "agentc-http-" + uuid.uuid4().hex
        self.network = self.container = None
        self._network_attempted = self._container_attempted = False
        self._active = False
        self._closed = False
        self._cleanup_failed = False
        self.attachments = []
        self.resources = None

    def __enter__(self):
        require(not self.output.exists(), "HTTP fixture cannot retry or resume")
        self.output.mkdir(mode=0o700)
        private_write(self.output / "definition.json", definition())
        try:
            image = self.client.images.get(HTTP_MANIFEST)
            require(
                image.id == HTTP_IMAGE
                and image.attrs["Os"] == "linux"
                and image.attrs["Architecture"] == "amd64",
                "HTTP fixture image differs from immutable enrollment",
            )
            result = subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(self.output / "key.pem"),
                    "-out",
                    str(self.output / "cert.pem"),
                    "-days",
                    "2",
                    "-subj",
                    "/CN=httpbin",
                    "-addext",
                    "subjectAltName=DNS:httpbin",
                ],
                capture_output=True,
                timeout=15,
                check=False,
            )
            require(result.returncode == 0, "fixture certificate creation failed")
            (self.output / "key.pem").chmod(0o600)
            (self.output / "cert.pem").chmod(0o600)
            private_write(self.output / "network-intent.json", {"owner": self.owner})
            self._network_attempted = True
            self.network = self.client.networks.create(
                self.owner,
                driver="bridge",
                internal=True,
                labels={"agentc.http_fixture.owner": self.owner},
            )
            private_write(self.output / "network-created.json", {"id": self.network.id})
            private_write(
                self.output / "container-intent.json",
                {"owner": self.owner, "image_id": HTTP_IMAGE},
            )
            self._container_attempted = True
            self.container = self.client.containers.create(
                HTTP_IMAGE,
                name=self.owner,
                entrypoint=["/bin/sh", "-c"],
                command=[COMMAND],
                network_mode=self.network.id,
                labels={"agentc.http_fixture.owner": self.owner},
                platform="linux/amd64",
                nano_cpus=1_000_000_000,
                mem_limit="256m",
                pids_limit=128,
                security_opt=["no-new-privileges"],
            )
            private_write(
                self.output / "container-created.json", {"id": self.container.id}
            )
            # Connect the pre-created container with the enrolled service alias.
            self.network.disconnect(self.container)
            self.network.connect(self.container, aliases=["httpbin"])
            _upload(
                self.container,
                "/root",
                {
                    "agentc-http-service/" + p.name: p.read_bytes()
                    for p in (self.output / "cert.pem", self.output / "key.pem")
                },
            )
            self.container.start()
            program = (
                "import ssl,urllib.request; "
                "assert urllib.request.urlopen('http://httpbin/get',timeout=2).status==200; "
                "assert urllib.request.urlopen('https://httpbin/get',timeout=2,"
                "context=ssl.create_default_context(cafile='/root/agentc-http-service/cert.pem')).status==200"
            )
            for _ in range(30):
                reply = self.container.exec_run(["python3", "-c", program])
                if reply.exit_code == 0:
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("isolated HTTP/HTTPS fixture did not become ready")
            self._active = True
            private_write(
                self.output / "enrollment.json",
                {
                    "definition": definition(),
                    "owner": self.owner,
                    "network_id": self.network.id,
                    "container_id": self.container.id,
                    "certificate_sha256": file_sha(self.output / "cert.pem"),
                    "http_https_hostname_verified": True,
                },
            )
            return self
        except BaseException:
            self.close()
            raise

    def container_options(self):
        require(self._active, "HTTP fixture has not become ready")
        return {
            "network_mode": self.network.id,
            "environment": {
                "HTTPBIN_URL": "http://httpbin/",
                "PYTHONPATH": ROOT,
                "REQUESTS_CA_BUNDLE": ROOT + "/trust.pem",
            },
        }

    def configure(self, container):
        require(self._active, "HTTP fixture has not become ready")
        attachment = _configure(
            container, self.network.id, (self.output / "cert.pem").read_bytes()
        )
        self.attachments.append(attachment)
        return dict(attachment)

    def descriptor(self):
        require(self._active, "HTTP fixture has not become ready")
        certificate = (self.output / "cert.pem").read_bytes()
        return {
            "definition": definition(),
            "network_id": self.network.id,
            "container_id": self.container.id,
            "owner": self.owner,
            "certificate_base64": base64.b64encode(certificate).decode(),
            "certificate_sha256": sha256(certificate).hexdigest(),
        }

    def close(self):
        import docker

        if self._closed:
            if self._cleanup_failed:
                raise FixtureOwnershipError(
                    "previous HTTP fixture cleanup remains unverified"
                )
            return
        self._active = False
        uncertain = (self._network_attempted and self.network is None) or (
            self._container_attempted and self.container is None
        )
        try:
            if self.container is not None:
                try:
                    self.resources = container_usage(self.container)
                    self.resources["actor"] = "swe_agent.http_fixture"
                    self.resources["scope"] = (
                        "isolated HTTP/HTTPS environment dependency; bootstrap and task/grader requests"
                    )
                except Exception:
                    self.resources = {
                        "container_id": self.container.id,
                        "cpu_seconds": None,
                        "peak_memory_available": False,
                    }
                self.container.reload()
                require(
                    self.container.attrs["Config"]["Labels"].get(
                        "agentc.http_fixture.owner"
                    )
                    == self.owner
                    and self.container.attrs["Image"] == HTTP_IMAGE,
                    "HTTP fixture container ownership changed",
                )
                self.container.remove(force=True)
                try:
                    self.client.containers.get(self.container.id)
                except docker.errors.NotFound:
                    pass
                else:
                    raise RuntimeError("HTTP fixture container removal is unverified")
            if self.network is not None:
                self.network.reload()
                require(
                    self.network.attrs["Internal"] is True
                    and self.network.attrs["Labels"].get("agentc.http_fixture.owner")
                    == self.owner,
                    "HTTP fixture network ownership changed",
                )
                self.network.remove()
                try:
                    self.client.networks.get(self.network.id)
                except docker.errors.NotFound:
                    pass
                else:
                    raise RuntimeError("HTTP fixture network removal is unverified")
        except BaseException:
            self._closed = self._cleanup_failed = True
            private_write(
                self.output / "cleanup.json",
                {"verified": False, "creation_uncertain": uncertain},
            )
            raise FixtureOwnershipError(
                "HTTP fixture cleanup remains unverified"
            ) from None
        private_write(
            self.output / "cleanup.json",
            {
                "verified": not uncertain,
                "creation_uncertain": uncertain,
                "environment_resources": self.resources,
                "attachments": self.attachments,
            },
        )
        self._closed = True
        if uncertain:
            self._cleanup_failed = True
            raise FixtureOwnershipError(
                "HTTP fixture creation remains uncertain; cannot resume"
            )

    def __exit__(self, *exc):
        self.close()


def _configure(container, network_id, certificate):
    container.reload()
    require(
        container.attrs["Image"] == TASK_IMAGE
        and container.attrs["HostConfig"]["NetworkMode"] == network_id,
        "task did not join its enrolled HTTP fixture",
    )
    before = container.exec_run(
        ["git", "-C", "/testbed", "status", "--porcelain", "--untracked-files=all"]
    )
    require(before.exit_code == 0, "task checkout cannot be inspected")
    _upload(
        container,
        "/root",
        {
            "agentc-http-fixture/sitecustomize.py": BOOTSTRAP.encode(),
            "agentc-http-fixture/cert.pem": certificate,
        },
    )
    probe = (
        "import pathlib,requests.adapters as a; "
        "p=pathlib.Path('/root/agentc-http-fixture/trust.pem'); "
        "s=pathlib.Path('/testbed/requests/cacert.pem').read_bytes(); "
        "c=pathlib.Path('/root/agentc-http-fixture/cert.pem').read_bytes(); "
        "assert a.DEFAULT_CA_BUNDLE_PATH==str(p) and p.read_bytes()==s+b'\\n'+c"
    )
    verified = container.exec_run(
        ["/opt/miniconda3/envs/testbed/bin/python", "-c", probe], workdir="/testbed"
    )
    require(
        verified.exit_code == 0,
        "fixture trust bootstrap did not preserve original roots",
    )
    after = container.exec_run(
        ["git", "-C", "/testbed", "status", "--porcelain", "--untracked-files=all"]
    )
    require(
        after.exit_code == 0 and after.output == before.output,
        "HTTP fixture changed task checkout",
    )
    attachment = {
        "container_id": container.id,
        "checkout_unchanged": True,
        "trust_verified": True,
    }
    return dict(attachment)


class FixtureConnection:
    """Attach a child task to its parent's exact fixture; no resource creation."""

    def __init__(self, client, descriptor):
        require(
            set(descriptor)
            == {
                "definition",
                "network_id",
                "container_id",
                "owner",
                "certificate_base64",
                "certificate_sha256",
            }
            and descriptor["definition"] == definition()
            and all(
                re.fullmatch(r"[0-9a-f]{64}", descriptor[k]) is not None
                for k in ("network_id", "container_id")
            )
            and re.fullmatch(r"agentc-http-[0-9a-f]{32}", descriptor["owner"])
            is not None
            and len(descriptor["certificate_base64"]) <= 22500,
            "HTTP fixture descriptor differs from enrollment",
        )
        self.descriptor = descriptor
        self.certificate = base64.b64decode(
            descriptor["certificate_base64"], validate=True
        )
        require(
            0 < len(self.certificate) <= 16384
            and sha256(self.certificate).hexdigest()
            == descriptor["certificate_sha256"],
            "HTTP fixture certificate differs",
        )
        network = client.networks.get(descriptor["network_id"])
        require(
            network.id == descriptor["network_id"]
            and network.attrs["Internal"] is True
            and network.attrs["Labels"].get("agentc.http_fixture.owner")
            == descriptor["owner"],
            "HTTP fixture network enrollment differs",
        )
        service = client.containers.get(descriptor["container_id"])
        require(
            service.id == descriptor["container_id"]
            and service.attrs["Image"] == HTTP_IMAGE
            and service.attrs["HostConfig"]["NetworkMode"] == descriptor["network_id"]
            and service.attrs["Config"]["Labels"].get("agentc.http_fixture.owner")
            == descriptor["owner"],
            "HTTP fixture service enrollment differs",
        )

    def container_options(self):
        return {
            "network_mode": self.descriptor["network_id"],
            "environment": {
                "HTTPBIN_URL": "http://httpbin/",
                "PYTHONPATH": ROOT,
                "REQUESTS_CA_BUNDLE": ROOT + "/trust.pem",
            },
        }

    def configure(self, container):
        return _configure(container, self.descriptor["network_id"], self.certificate)
