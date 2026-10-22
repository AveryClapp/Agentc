"""Ownership, admission limits, and real subprocess pull cancellation."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bench import swebench_image_cache as cache
from bench.swebench_cohort import CohortError
from bench.swebench_images import content_digest, resolve_image


@pytest.fixture
def setup(monkeypatch, tmp_path):
    class ImageNotFound(Exception):
        pass

    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(errors=SimpleNamespace(ImageNotFound=ImageNotFound)),
    )
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": "sha256:" + "a" * 64, "size": 100},
            "layers": [{"digest": "sha256:" + "b" * 64, "size": 4096}],
        }
    ).encode()
    index = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": content_digest(manifest),
                    "size": len(manifest),
                    "platform": {"os": "linux", "architecture": "amd64"},
                }
            ],
        }
    ).encode()
    pin = resolve_image(
        "swebench/sweb.eval.x86_64.repo_1776_task-1:latest",
        lambda ref: index if ref == "latest" else manifest,
    )
    pin["task_id"] = "repo__task-1"
    local, removed, containers = {}, [], []

    def get(ref):
        if ref not in local:
            raise ImageNotFound(ref)
        return local[ref]

    def remove(ref, **kwargs):
        removed.append((ref, kwargs))
        image = local.pop(ref)
        local.pop(image.id, None)

    client = SimpleNamespace(
        images=SimpleNamespace(
            get=get,
            list=lambda **kw: list({id(i): i for i in local.values()}.values()),
            remove=remove,
        ),
        containers=SimpleNamespace(list=lambda **kw: containers),
        info=lambda: {"ID": "daemon"},
        version=lambda: {"Version": "27.3.1"},
    )
    capacity = SimpleNamespace(
        daemon_id="daemon",
        endpoint="unix:///test.sock",
        sample=lambda: {
            "host_free_bytes": 40 * cache.GIB,
            "daemon_free_bytes": 40 * cache.GIB,
        },
    )

    def install(size=cache.GIB):
        image = SimpleNamespace(
            id=pin["config_digest"],
            attrs={
                "Os": "linux",
                "Architecture": "amd64",
                "Size": size,
                "RepoTags": [],
                "RepoDigests": [pin["immutable_image"]],
            },
        )
        local[image.id] = local[pin["immutable_image"]] = image
        return image

    def make(**overrides):
        obj = cache.ImmutableImageCache(
            client=client,
            output=tmp_path / "cache",
            limits=replace(cache.CacheLimits(), **overrides),
            capacity=capacity,
        )
        monkeypatch.setattr(obj, "_pull", lambda ref, log: install())
        return obj

    return SimpleNamespace(
        pin=pin,
        client=client,
        capacity=capacity,
        local=local,
        removed=removed,
        containers=containers,
        install=install,
        make=make,
    )


def test_borrowed_image_does_not_require_a_new_digest_alias_or_get_removed(setup):
    image = setup.install()
    image.attrs["RepoDigests"] = ["swebench/task@index"]
    obj = setup.make(max_pulls=0)
    assert obj.acquire(setup.pin)["ownership"] == "borrowed"
    obj.release()
    obj.close()
    assert setup.client.images.get(image.id) is image
    assert setup.removed == [] and obj.pull_count == 0


def test_only_owned_digest_is_evicted_without_force_or_parent_prune(setup):
    obj = setup.make()
    record = obj.acquire(setup.pin)
    assert record["ownership"] == "owned"
    with pytest.raises(CohortError, match="another image lease"):
        obj.acquire(setup.pin)
    obj.release()
    obj.close()
    assert setup.removed == [
        (setup.pin["immutable_image"], {"force": False, "noprune": True})
    ]
    summary = json.loads((obj.output / "summary.json").read_text())
    assert summary["owned_reference_remaining"] is None
    assert summary["physical_provider_calls"] == 0


@pytest.mark.parametrize("foreign", ["tag", "digest", "stopped_container", "daemon"])
def test_foreign_references_or_container_dependencies_halt_without_deletion(
    setup, foreign
):
    obj = setup.make()
    obj.acquire(setup.pin)
    image = setup.client.images.get(setup.pin["config_digest"])
    if foreign == "tag":
        image.attrs["RepoTags"] = ["swebench/task:user"]
    elif foreign == "digest":
        image.attrs["RepoDigests"].append("another/repository@sha256:foreign")
    elif foreign == "stopped_container":
        setup.containers.append(
            SimpleNamespace(attrs={"Image": image.id, "State": "exited"})
        )
    else:
        setup.client.info = lambda: {"ID": "replacement"}
    with pytest.raises(cache.ImageOwnershipError):
        obj.release()
    assert obj.halted and obj.owned and setup.removed == []
    assert not obj.lock.locked()


@pytest.mark.parametrize(
    "limits",
    [
        {"max_pulls": 0},
        {"max_compressed_bytes": 1024},
        {"max_total_compressed_bytes": 1024, "max_compressed_bytes": 1024},
    ],
)
def test_capacity_admission_fails_before_starting_a_pull(setup, limits):
    obj = setup.make(**limits)
    with pytest.raises(cache.ProvisioningError, match="session capacity"):
        obj.acquire(setup.pin)
    assert obj.pull_count == 0 and not obj.lock.locked()
    obj.close()


def test_real_free_space_not_compressed_bytes_is_an_admission_guard(setup):
    setup.capacity.sample = lambda: {
        "host_free_bytes": 30 * cache.GIB,
        "daemon_free_bytes": cache.GIB,
    }
    obj = setup.make()
    with pytest.raises(cache.ProvisioningError, match="headroom"):
        obj.acquire(setup.pin)
    assert obj.pull_count == 0 and setup.local == {}


def test_completed_image_larger_than_cache_cap_is_removed_and_halts(setup, monkeypatch):
    obj = setup.make()
    monkeypatch.setattr(obj, "_pull", lambda ref, log: setup.install(9 * cache.GIB))
    with pytest.raises(CohortError, match="residency"):
        obj.acquire(setup.pin)
    assert obj.halted and obj.owned is None and len(setup.removed) == 1


def test_failed_pull_does_not_retry_or_claim_partial_storage_reclaimed(
    setup, monkeypatch
):
    obj = setup.make(max_pulls=2)

    def failed(ref, log):
        raise OSError("pull disconnected")

    monkeypatch.setattr(obj, "_pull", failed)
    with pytest.raises(OSError):
        obj.acquire(setup.pin)
    with pytest.raises(cache.ProvisioningError, match="halted"):
        obj.acquire(setup.pin)
    assert obj.pull_count == 1 and obj.compressed_bytes == 4096
    assert any(e.get("partial_storage_may_remain") for e in obj.events)
    obj.close()


def test_tampered_pin_cannot_start_a_pull(setup):
    obj = setup.make()
    setup.pin["immutable_image"] = "swebench/task:latest"
    with pytest.raises(CohortError, match="manifest bytes"):
        obj.acquire(setup.pin)
    assert obj.pull_count == 0


@pytest.mark.parametrize("reason", ["deadline", "disk"])
def test_actual_child_process_is_terminated_and_reaped_without_inherited_auth(
    setup, monkeypatch, tmp_path, reason
):
    # Real subprocess; no daemon or network. This proves supervisor teardown,
    # exact CLI arguments, empty config and child environment isolation.
    executable = tmp_path / "docker-fixture"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys,time\n"
        "print(json.dumps({'args':sys.argv[1:],'env':dict(os.environ),'pid':os.getpid()}),flush=True)\n"
        "while True: time.sleep(1)\n"
    )
    executable.chmod(0o700)
    obj = setup.make(pull_timeout_seconds=1, poll_seconds=0.1)
    monkeypatch.setattr(cache.shutil, "which", lambda name: str(executable))
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", "fixture-auth-must-not-propagate")
    monkeypatch.setenv("DOCKER_CONTEXT", "fixture-context-must-not-propagate")
    samples = []

    def space(extra=0):
        samples.append(True)
        if reason == "disk" and len(samples) > 2:
            raise cache.ProvisioningError("disk floor")

    monkeypatch.setattr(obj, "_space", space)
    log = tmp_path / "pull.log"
    with pytest.raises(cache.ProvisioningError):
        cache.ImmutableImageCache._pull(obj, setup.pin["immutable_image"], log)
    child = json.loads(log.read_text())
    assert child["args"][0] == "--config"
    assert child["args"][2:] == [
        "--host",
        "unix:///test.sock",
        "image",
        "pull",
        "--platform",
        "linux/amd64",
        setup.pin["immutable_image"],
    ]
    # macOS injects its text-encoding setting into a spawned Python process.
    assert set(child["env"]) <= {
        "PATH",
        "HOME",
        "LANG",
        "LC_CTYPE",
        "__CF_USER_TEXT_ENCODING",
    }
    with pytest.raises(ProcessLookupError):
        cache.os.kill(child["pid"], 0)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_pulls": True},
        {"poll_seconds": float("nan")},
        {"poll_seconds": 0},
        {"pull_timeout_seconds": 0},
        {"min_free_bytes": 0},
    ],
)
def test_invalid_limits_fail_before_creating_output(setup, limits):
    with pytest.raises(CohortError):
        setup.make(**limits)
