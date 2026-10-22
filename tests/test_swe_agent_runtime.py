"""Frozen bundle integrity and ownership through provisioning failures."""

import sys
from types import SimpleNamespace

import pytest

from bench import swe_agent_runtime as runtime
from bench.swebench_cohort import CohortError


@pytest.fixture
def package_tree(tmp_path):
    for name, version in runtime.REQUIRED.items():
        directory = tmp_path / (name.replace("-", "_") + "-" + version + ".dist-info")
        directory.mkdir()
        (directory / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        )
    (tmp_path / "module.py").write_text("literal frozen fixture\n")
    return tmp_path


def test_bundle_rejects_changed_extra_or_missing_bytes(package_tree):
    runtime.freeze_bundle(package_tree)
    bundle = runtime.RuntimeBundle.load(package_tree)
    (package_tree / "module.py").write_text("changed\n")
    with pytest.raises(CohortError):
        bundle.verify()


@pytest.mark.parametrize("change", ["extra", "missing", "manifest", "version"])
def test_bundle_integrity_covers_complete_package_and_metadata_closure(
    package_tree, change
):
    runtime.freeze_bundle(package_tree)
    bundle = runtime.RuntimeBundle.load(package_tree)
    if change == "extra":
        (package_tree / "extra.py").write_text("unexpected")
    elif change == "missing":
        (package_tree / "module.py").unlink()
    elif change == "manifest":
        bundle.manifest["platform"] = "host"
    else:
        (package_tree / "swe_rex-1.2.1.dist-info/METADATA").write_text(
            "Name: swe-rex\nVersion: 2\n"
        )
    with pytest.raises(CohortError):
        bundle.verify()


def test_bundle_rejects_symlinks_and_host_native_libraries(package_tree):
    (package_tree / "alias").symlink_to(package_tree / "module.py")
    with pytest.raises(CohortError):
        runtime.freeze_bundle(package_tree)
    (package_tree / "alias").unlink()
    (package_tree / "host.so").write_bytes(b"not a Linux extension")
    with pytest.raises(CohortError):
        runtime.freeze_bundle(package_tree)


def test_archive_contains_only_verified_relative_files(package_tree):
    import tarfile

    runtime.freeze_bundle(package_tree)
    bundle = runtime.RuntimeBundle.load(package_tree)
    cached = package_tree / "__pycache__"
    cached.mkdir()
    (cached / "untrusted.pyc").write_bytes(b"ignored")
    archive_names = []

    def put_archive(destination, stream):
        assert destination == "/root"
        with tarfile.open(fileobj=stream) as archive:
            archive_names.extend(archive.getnames())
        return True

    bundle.upload(SimpleNamespace(put_archive=put_archive))
    assert set(archive_names) == {
        "agentc-runtime-packages/" + n
        for n in [*bundle.manifest["files"], runtime.MANIFEST]
    }


def test_repository_requires_clean_identical_base_tree_and_fresh_checkout():
    base, tree = "a" * 40, "b" * 40
    values = {"HEAD": "c" * 40, "HEAD^{tree}": tree, base + "^{tree}": tree}
    state = {"status": ""}

    def execute(command):
        assert command[:3] == ["git", "-C", "/testbed"]
        output = state["status"] if command[3] == "status" else values[command[-1]]
        return SimpleNamespace(exit_code=0, output=output.encode())

    container = SimpleNamespace(exec_run=execute)
    assert runtime.verify_base(container, base, exact_head=False)["head"] == "c" * 40
    with pytest.raises(CohortError):
        runtime.verify_base(container, base, exact_head=True)
    values["HEAD"] = base
    runtime.verify_base(container, base, exact_head=True)
    state["status"] = " M modified.py"
    with pytest.raises(CohortError):
        runtime.verify_base(container, base, exact_head=True)


@pytest.fixture
def resources(monkeypatch):
    class NotFound(Exception):
        pass

    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(errors=SimpleNamespace(NotFound=NotFound)),
    )
    containers, networks, removed = {}, {}, []

    def registry(objects):
        def get(identity):
            if identity not in objects:
                raise NotFound()
            return objects[identity]

        return SimpleNamespace(get=get)

    def enroll(objects, identity, kind):
        def remove(**kwargs):
            removed.append((kind, identity))
            objects.pop(identity)

        objects[identity] = SimpleNamespace(id=identity, remove=remove)
        return objects[identity]

    client = SimpleNamespace(
        containers=registry(containers), networks=registry(networks)
    )
    owned = runtime.OwnedResources(client)
    owned.container = enroll(containers, "owned-container", "container")
    owned.network = enroll(networks, "owned-network", "network")
    enroll(containers, "unrelated-container", "container")
    return owned, containers, networks, removed


def test_cleanup_only_removes_owned_ids_and_is_idempotent(resources):
    owned, containers, networks, removed = resources
    owned.drain()
    owned.drain()
    assert removed == [("container", "owned-container"), ("network", "owned-network")]
    assert list(containers) == ["unrelated-container"] and not networks


def test_failed_container_cleanup_preserves_network_and_uncertain_ownership(resources):
    owned, containers, networks, removed = resources
    owned.container.remove = lambda **kwargs: None
    with pytest.raises(RuntimeError, match="termination is unverified"):
        owned.drain()
    assert owned.container is not None and owned.network is not None
    assert networks and not removed


def test_unknown_container_state_is_fatal_before_network_cleanup(resources):
    owned, containers, networks, removed = resources
    owned.client.containers.get = lambda identity: (_ for _ in ()).throw(
        OSError("daemon unavailable")
    )
    with pytest.raises(OSError):
        owned.drain()
    assert owned.container is not None and networks and not removed


def test_fixture_configuration_survives_minimal_swerex_shell_environment():
    import subprocess

    fixture = SimpleNamespace(
        container_options=lambda: {
            "environment": {
                "PYTHONPATH": "/root/fixture",
                "HTTPBIN_URL": "http://httpbin/",
                "REQUESTS_CA_BUNDLE": "/root/fixture/trust.pem",
            }
        }
    )
    script = runtime.shell_startup(fixture) + "/usr/bin/env"
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", script],
        env={"PS1": "fixture", "PS2": "", "PS0": ""},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    assert values["HTTPBIN_URL"] == "http://httpbin/"
    assert values["REQUESTS_CA_BUNDLE"] == "/root/fixture/trust.pem"
    assert values["PYTHONPATH"].split(":")[:2] == ["/root/fixture", runtime.TARGET]
    assert values["PIP_NO_INDEX"] == "1"


@pytest.mark.parametrize(
    "changed", ["state", "command", "cpu", "memory", "pids", "security"]
)
def test_preallocated_container_rejects_used_or_changed_resources(changed):
    from copy import deepcopy

    owner, image = "agentc-swe-" + "a" * 32, "sha256:" + "b" * 64
    attrs = {
        "Name": "/" + owner,
        "Image": image,
        "Mounts": [],
        "Config": {
            "Labels": {"agentc.swe.owner": owner},
            "Entrypoint": ["/bin/sleep"],
            "Cmd": ["infinity"],
        },
        "State": {"Status": "created"},
        "HostConfig": {
            "NetworkMode": "none",
            "NanoCpus": 2_000_000_000,
            "Memory": 4 * 1024**3,
            "PidsLimit": 512,
            "SecurityOpt": ["no-new-privileges"],
        },
    }
    container = SimpleNamespace(attrs=attrs, reload=lambda: None)
    runtime.verify_task_container(container, owner, image, fresh=True)
    container.attrs = deepcopy(attrs)
    if changed == "state":
        container.attrs["State"]["Status"] = "exited"
    elif changed == "command":
        container.attrs["Config"]["Cmd"] = ["foreign"]
    else:
        field = {
            "cpu": "NanoCpus",
            "memory": "Memory",
            "pids": "PidsLimit",
            "security": "SecurityOpt",
        }[changed]
        container.attrs["HostConfig"][field] = [] if changed == "security" else 0
    with pytest.raises(CohortError, match="fresh resource enrollment"):
        runtime.verify_task_container(container, owner, image, fresh=True)
