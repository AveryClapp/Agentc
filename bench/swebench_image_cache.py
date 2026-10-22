"""Serial immutable image leases with capacity guards and checked ownership.

Only completed, owned image residency is capped. Sampled free-space guards do
not impose a physical quota or reclaim canceled downloads. No global pruning,
registry credentials, floating tags, retries, or replacement task images.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from bench.campaign_accounting import digest
from bench.coding_submission import private_write
from bench.guard_evidence import file_sha
from bench.swebench_cohort import CohortError, require
from bench.swebench_images import verify_receipt

GIB = 1024**3
MAX_PULL_LOG = 4 * 1024**2


class ProvisioningError(CohortError):
    """An unavailable image remains in the assigned cohort."""


class ImageOwnershipError(CohortError):
    """An owned resource cannot be safely released; stop the session."""


@dataclass(frozen=True)
class CacheLimits:
    max_pulls: int = 1
    max_compressed_bytes: int = 4 * GIB
    max_total_compressed_bytes: int = 4 * GIB
    max_owned_image_bytes: int = 8 * GIB
    min_free_bytes: int = 12 * GIB
    pull_timeout_seconds: int = 900
    poll_seconds: float = 2.0

    def verify(self):
        for name in (
            "max_pulls",
            "max_compressed_bytes",
            "max_total_compressed_bytes",
            "max_owned_image_bytes",
            "min_free_bytes",
            "pull_timeout_seconds",
        ):
            require(type(getattr(self, name)) is int, "invalid cache limit type")
        require(0 <= self.max_pulls <= 150, "invalid image pull count")
        require(
            0 < self.max_compressed_bytes <= self.max_total_compressed_bytes
            and self.max_owned_image_bytes > 0
            and self.min_free_bytes >= GIB
            and 0 < self.pull_timeout_seconds <= 1800
            and type(self.poll_seconds) in (int, float)
            and 0.1 <= self.poll_seconds <= 10,
            "invalid image cache capacity or deadline",
        )


def references(attrs: dict) -> set[str]:
    return {
        reference
        for key in ("RepoTags", "RepoDigests")
        for reference in (attrs.get(key) or [])
        if reference not in {"<none>:<none>", "<none>@<none>"}
    }


def verify_identity(image, pin: dict):
    require(
        image.id == pin["config_digest"]
        and image.attrs["Os"] == "linux"
        and image.attrs["Architecture"] == "amd64"
        and type(image.attrs["Size"]) is int
        and image.attrs["Size"] > 0,
        "local image differs from the frozen manifest",
    )


class DockerCapacity:
    """Measure a local overlay2 daemon's backing filesystem inside a probe."""

    def __init__(self, client, bootstrap_image: str, output: Path):
        self.client = client
        self.output = output
        self.container = None
        info = client.info()
        adapter = client.api.get_adapter(client.api.base_url)
        socket = getattr(adapter, "socket_path", None)
        require(
            client.api.base_url == "http+docker://localhost"
            and isinstance(socket, str)
            and Path(socket).is_socket()
            and info["Driver"] == "overlay2"
            and info["OSType"] == "linux",
            "capacity probe requires a local Unix-socket Linux overlay2 daemon",
        )
        self.endpoint = "unix://" + socket
        self.daemon_id = info["ID"]
        self.root = info["DockerRootDir"].rstrip("/")
        self.owner = uuid.uuid4().hex
        image = client.images.get(bootstrap_image)
        require(
            image.id == bootstrap_image, "capacity probe requires an exact image ID"
        )
        try:
            self.container = client.containers.create(
                image.id,
                name="agentc-image-capacity-" + self.owner,
                labels={"agentc.image_cache.owner": self.owner},
                entrypoint=["/bin/sleep", "infinity"],
                network_mode="none",
                mem_limit=64 * 1024**2,
                nano_cpus=100_000_000,
                pids_limit=32,
                security_opt=["no-new-privileges"],
            )
            self.container.start()
            self.sample()
        except BaseException:
            self.close()
            raise

    def sample(self) -> dict:
        require(self.client.info()["ID"] == self.daemon_id, "Docker daemon changed")
        script = (
            "import json,os; "
            "s=os.statvfs('/'); "
            "m=[x.strip() for x in open('/proc/self/mountinfo') "
            "if x.split()[4]=='/']; "
            "print(json.dumps({'free_bytes':s.f_bavail*s.f_frsize,'root_mount':m}))"
        )
        result = self.container.exec_run(["/opt/miniconda3/bin/python", "-c", script])
        require(result.exit_code == 0, "Docker capacity probe failed")
        raw = json.loads(result.output)
        require(
            len(raw["root_mount"]) == 1
            and " - overlay " in raw["root_mount"][0]
            and "upperdir=" + self.root + "/overlay2/" in raw["root_mount"][0]
            and type(raw["free_bytes"]) is int
            and raw["free_bytes"] > 0,
            "capacity probe cannot establish the daemon backing filesystem",
        )
        host = os.statvfs(self.output)
        return {
            "daemon_free_bytes": raw["free_bytes"],
            "host_free_bytes": host.f_bavail * host.f_frsize,
        }

    def close(self):
        if self.container is None:
            return
        import docker

        container = self.container
        try:
            current = self.client.containers.get(container.id)
            require(
                current.attrs["Config"]["Labels"].get("agentc.image_cache.owner")
                == self.owner,
                "capacity probe ownership changed",
            )
            current.remove(force=True)
            try:
                self.client.containers.get(container.id)
            except docker.errors.NotFound:
                self.container = None
                return
            raise ImageOwnershipError("owned capacity probe remains")
        except docker.errors.NotFound:
            self.container = None
        except Exception as error:
            raise ImageOwnershipError("capacity probe cleanup uncertain") from error


class ImmutableImageCache:
    """One active lease. Baseline images are borrowed and never removed."""

    def __init__(self, *, client, output: Path, limits: CacheLimits, capacity):
        limits.verify()
        require(not output.exists(), "image cache requires fresh output")
        self.client, self.output, self.limits, self.capacity = (
            client,
            output,
            limits,
            capacity,
        )
        self.baseline = {
            image.id: sorted(references(image.attrs))
            for image in client.images.list(all=True)
        }
        self.active = None
        self.owned = None
        self.halted = False
        self.closed = False
        self.pull_count = 0
        self.compressed_bytes = 0
        self.events = []
        self.lock = threading.Lock()
        self.source_sha = file_sha(Path(__file__))
        output.mkdir(mode=0o700)
        self.enrollment = {
            "schema_version": 1,
            "record_type": "immutable_image_cache_enrollment",
            "limits": asdict(limits),
            "baseline_images": self.baseline,
            "daemon_id": capacity.daemon_id,
            "storage_contract": "local_unix_overlay2_sampled_free_space",
            "physical_quota": False,
            "source_sha256": self.source_sha,
            "docker_server_version": client.version()["Version"],
        }
        private_write(output / "enrollment.json", self.enrollment)

    def _record(self, event: dict):
        event = {"ordinal": len(self.events), **event}
        self.events.append(event)
        private_write(self.output / f"event-{event['ordinal']:04d}.json", event)

    def _space(self, extra: int = 0):
        sample = self.capacity.sample()
        self._record({"event": "capacity_sample", **sample})
        if min(sample.values()) < self.limits.min_free_bytes + extra:
            raise ProvisioningError("insufficient measured image storage headroom")

    def _pull(self, reference: str, log_path: Path):
        executable = shutil.which("docker")
        require(executable is not None, "Docker CLI unavailable")
        stop = threading.Event()
        reader_failed = []
        with tempfile.TemporaryDirectory(prefix="agentc-anonymous-docker-") as config:
            # The CLI must not resolve a user context, credential helper, auth
            # config, proxy environment, plugin, or inherited Docker settings.
            child_env = {"PATH": os.defpath, "HOME": config, "LANG": "C"}
            command = [
                executable,
                "--config",
                config,
                "--host",
                self.capacity.endpoint,
                "image",
                "pull",
                "--platform",
                "linux/amd64",
                reference,
            ]
            started = time.monotonic()
            process = subprocess.Popen(
                command,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            deadline_hit = threading.Event()

            def cancel_deadline():
                deadline_hit.set()
                if process.poll() is None:
                    process.terminate()

            deadline = threading.Timer(
                self.limits.pull_timeout_seconds, cancel_deadline
            )
            deadline.daemon = True
            deadline.start()

            def drain():
                try:
                    with log_path.open("xb") as target:
                        log_path.chmod(0o600)
                        total = 0
                        while chunk := process.stdout.read(16384):
                            available = MAX_PULL_LOG - total
                            target.write(chunk[: max(0, available)])
                            total += len(chunk)
                            if total > MAX_PULL_LOG:
                                reader_failed.append("pull_log_limit")
                                stop.set()
                except Exception:
                    reader_failed.append("pull_log_failure")
                    stop.set()

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            try:
                while process.poll() is None:
                    self._space()
                    if reader_failed:
                        raise ProvisioningError(reader_failed[0])
                    if time.monotonic() - started >= self.limits.pull_timeout_seconds:
                        raise ProvisioningError("image pull deadline exceeded")
                    stop.wait(self.limits.poll_seconds)
                if deadline_hit.is_set():
                    raise ProvisioningError("image pull deadline exceeded")
                require(process.returncode == 0, "immutable image pull failed")
            finally:
                deadline.cancel()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                process.wait(timeout=5)
                reader.join(timeout=5)
                process.stdout.close()
                require(not reader.is_alive(), "pull log reader did not terminate")
            require(not reader_failed, "pull log was incomplete")

    def acquire(self, pin: dict) -> dict:
        import docker

        require(self.lock.acquire(blocking=False), "another image lease is active")
        try:
            require(
                not self.closed and self.active is None, "image cache is unavailable"
            )
            verified = verify_receipt(pin["receipt"])
            require(
                all(pin.get(k) == v for k, v in verified.items()),
                "image pin differs from retained manifest bytes",
            )
            try:
                image = self.client.images.get(pin["config_digest"])
            except docker.errors.ImageNotFound:
                image = None
            pulled = False
            if image is None:
                if self.halted:
                    raise ProvisioningError(
                        "image acquisition halted after uncertain pull"
                    )
                size = pin["compressed_layer_bytes"]
                permitted = (
                    self.pull_count < self.limits.max_pulls
                    and size <= self.limits.max_compressed_bytes
                    and self.compressed_bytes + size
                    <= self.limits.max_total_compressed_bytes
                )
                if not permitted:
                    raise ProvisioningError(
                        "image pull exceeds session capacity limits"
                    )
                self._space(extra=size)
                self.pull_count += 1
                self.compressed_bytes += size
                reference = pin["immutable_image"]
                self.owned = (pin["config_digest"], reference)
                log = self.output / f"pull-{self.pull_count:04d}.log"
                self._record(
                    {
                        "event": "pull_started",
                        "reference": reference,
                        "config_digest": pin["config_digest"],
                        "compressed_layer_bytes": size,
                    }
                )
                try:
                    self._pull(reference, log)
                    image = self.client.images.get(reference)
                    verify_identity(image, pin)
                    self._space()
                    require(
                        image.attrs["Size"] <= self.limits.max_owned_image_bytes,
                        "completed image exceeds owned residency limit",
                    )
                except BaseException as error:
                    self.halted = True
                    self._record(
                        {
                            "event": "pull_failed",
                            "error_type": type(error).__name__,
                            "partial_storage_may_remain": True,
                        }
                    )
                    self._evict()
                    raise
                pulled = True
            verify_identity(image, pin)
            # A concurrently introduced image is also foreign, never ours.
            borrowed = not pulled or image.id in self.baseline
            if borrowed:
                self.owned = None
            record = {
                "task_id": pin["task_id"],
                "image_id": image.id,
                "immutable_image": pin["immutable_image"],
                "ownership": "borrowed" if borrowed else "owned",
                "image_size_bytes": image.attrs["Size"],
            }
            self.active = record
            self._record({"event": "image_acquired", **record})
            return record
        except BaseException:
            self.lock.release()
            raise

    def _evict(self):
        if self.owned is None:
            return
        import docker

        image_id, reference = self.owned
        if self.client.info()["ID"] != self.capacity.daemon_id:
            self.halted = True
            raise ImageOwnershipError("Docker daemon changed before image cleanup")
        try:
            image = self.client.images.get(reference)
        except docker.errors.ImageNotFound:
            self._record(
                {
                    "event": "owned_reference_absent",
                    "reference": reference,
                    "partial_storage_may_remain": self.halted,
                }
            )
            self.owned = None
            return
        try:
            require(
                image.id == image_id and image_id not in self.baseline,
                "owned image identity changed",
            )
            require(
                references(image.attrs) == {reference},
                "owned image has foreign references",
            )
            containers = self.client.containers.list(all=True)
            require(
                not any(c.attrs["Image"] == image_id for c in containers),
                "owned image has a container dependency",
            )
            self.client.images.remove(reference, force=False, noprune=True)
            try:
                self.client.images.get(reference)
            except docker.errors.ImageNotFound:
                pass
            else:
                raise ImageOwnershipError(
                    "owned image reference remains after eviction"
                )
            self._record(
                {
                    "event": "image_evicted",
                    "image_id": image_id,
                    "reference": reference,
                    "bytes_reclaimed": None,
                }
            )
            self.owned = None
        except Exception as error:
            self.halted = True
            self._record(
                {
                    "event": "eviction_uncertain",
                    "reference": reference,
                    "error_type": type(error).__name__,
                }
            )
            raise ImageOwnershipError(
                "owned image cleanup cannot be established"
            ) from error

    def release(self):
        require(self.active is not None, "no image lease to release")
        try:
            self._evict()
            self._space()
        finally:
            self.active = None
            self.lock.release()

    def close(self):
        if self.closed:
            return
        try:
            if self.active is not None:
                self.release()
            elif self.owned is not None:
                self._evict()
            require(
                file_sha(Path(__file__)) == self.source_sha,
                "image cache source changed during execution",
            )
        finally:
            self.closed = True
            private_write(
                self.output / "summary.json",
                {
                    "schema_version": 1,
                    "record_type": "immutable_image_cache_summary",
                    "enrollment_sha256": digest(self.enrollment),
                    "events": self.events,
                    "pull_attempts": self.pull_count,
                    "declared_compressed_bytes": self.compressed_bytes,
                    "halted": self.halted,
                    "owned_reference_remaining": self.owned[1] if self.owned else None,
                    "physical_provider_calls": 0,
                    "paper_evidence": False,
                },
            )
