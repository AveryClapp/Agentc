"""Trust outside the checkout, exact ownership and terminal uncertainty."""

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench import swe_requests_fixture as http
from bench.swebench_cohort import CohortError


@pytest.mark.parametrize("targeted", [True, False])
def test_bootstrap_is_lazy_preserves_candidate_roots_and_ignores_other_packages(
    tmp_path, targeted
):
    source = tmp_path / "application"
    package = source / "requests"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    roots = source / "roots.pem"
    roots.write_bytes(b"original roots")
    (package / "adapters.py").write_text(f"DEFAULT_CA_BUNDLE_PATH = {str(roots)!r}\n")
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "cert.pem").write_bytes(b"fixture CA")
    bootstrap = http.BOOTSTRAP.replace(
        "/root/agentc-http-fixture", str(fixture)
    ).replace(
        "/testbed/requests/adapters.py",
        str(package / "adapters.py") if targeted else str(tmp_path / "foreign.py"),
    )
    script = """
import importlib,pathlib,sys
source,bootstrap,fixture,roots,targeted=sys.argv[1:]
sys.path.insert(0,source)
exec(bootstrap)
assert 'requests' not in sys.modules
import requests.adapters as a
if targeted == 'True':
 assert a.DEFAULT_CA_BUNDLE_PATH == fixture+'/trust.pem'
 assert pathlib.Path(a.DEFAULT_CA_BUNDLE_PATH).read_bytes() == b'original roots\\nfixture CA'
 pathlib.Path(roots).write_bytes(b'candidate changed roots')
 importlib.reload(a)
 assert pathlib.Path(a.DEFAULT_CA_BUNDLE_PATH).read_bytes() == b'candidate changed roots\\nfixture CA'
else:
 assert a.DEFAULT_CA_BUNDLE_PATH == roots
 assert not pathlib.Path(fixture+'/trust.pem').exists()
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(source),
            bootstrap,
            str(fixture),
            str(roots),
            str(targeted),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (
        package / "adapters.py"
    ).read_text() == f"DEFAULT_CA_BUNDLE_PATH = {str(roots)!r}\n"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    class NotFound(Exception):
        pass

    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(errors=SimpleNamespace(NotFound=NotFound)),
    )
    containers, networks, events = {}, {}, []

    def get(registry, identity):
        if identity not in registry:
            raise NotFound()
        return registry[identity]

    def network(name, **kwargs):
        identity = "a" * 64
        obj = SimpleNamespace(
            id=identity,
            attrs={"Internal": kwargs["internal"], "Labels": kwargs["labels"]},
            disconnect=lambda container: events.append("disconnect"),
            connect=lambda container, **kw: events.append("alias"),
            reload=lambda: None,
            remove=lambda: (events.append("remove_network"), networks.pop(identity)),
        )
        networks[identity] = obj
        return obj

    def create(image, **kwargs):
        identity = "b" * 64
        obj = SimpleNamespace(
            id=identity,
            attrs={
                "Image": image,
                "Config": {"Labels": kwargs["labels"]},
                "HostConfig": {"NetworkMode": kwargs["network_mode"]},
            },
            start=lambda: events.append("start"),
            reload=lambda: None,
            exec_run=lambda *a, **kw: SimpleNamespace(exit_code=0, output=b""),
            put_archive=lambda *a: True,
            remove=lambda **kw: (
                events.append("remove_container"),
                containers.pop(identity),
            ),
        )
        containers[identity] = obj
        return obj

    client = SimpleNamespace(
        images=SimpleNamespace(
            get=lambda ref: SimpleNamespace(
                id=http.HTTP_IMAGE, attrs={"Os": "linux", "Architecture": "amd64"}
            )
        ),
        containers=SimpleNamespace(
            create=create, get=lambda identity: get(containers, identity)
        ),
        networks=SimpleNamespace(
            create=network, get=lambda identity: get(networks, identity)
        ),
    )

    def openssl(argv, **kwargs):
        Path(argv[argv.index("-keyout") + 1]).write_bytes(b"private test key")
        Path(argv[argv.index("-out") + 1]).write_bytes(b"public test certificate")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(http.subprocess, "run", openssl)
    monkeypatch.setattr(
        http,
        "container_usage",
        lambda obj: {
            "container_id": obj.id,
            "cpu_seconds": 1,
            "peak_memory_available": True,
            "peak_memory_bytes": 64,
        },
    )
    return client, containers, networks, events


def test_fixture_descriptor_has_no_private_key_and_cleanup_is_exact_and_idempotent(
    engine, tmp_path
):
    client, containers, networks, events = engine
    fixture = http.RequestsHTTPFixture(client, tmp_path / "fixture")
    with fixture:
        descriptor = fixture.descriptor()
        assert set(descriptor) == {
            "definition",
            "owner",
            "network_id",
            "container_id",
            "certificate_sha256",
            "certificate_base64",
        }
        assert "private" not in json.dumps(descriptor)
        connection = http.FixtureConnection(client, descriptor)
        assert connection.container_options() == fixture.container_options()
    fixture.close()
    assert not containers and not networks
    assert events[-2:] == ["remove_container", "remove_network"]
    assert json.loads((fixture.output / "cleanup.json").read_text())["verified"] is True
    with pytest.raises(CohortError, match="cannot retry or resume"):
        fixture.__enter__()


@pytest.mark.parametrize("changed", ["certificate", "recipe", "network", "service"])
def test_connection_rejects_changed_certificate_recipe_or_foreign_ownership(
    engine, tmp_path, changed
):
    client, containers, networks, events = engine
    with http.RequestsHTTPFixture(client, tmp_path / "fixture") as fixture:
        descriptor = fixture.descriptor()
        if changed == "certificate":
            descriptor["certificate_sha256"] = "0" * 64
        elif changed == "recipe":
            descriptor["definition"] = {"foreign": True}
        elif changed == "network":
            descriptor["network_id"] = "c" * 64
            networks["c" * 64] = SimpleNamespace(
                id="c" * 64, attrs={"Internal": False, "Labels": {}}
            )
        else:
            descriptor["container_id"] = "c" * 64
            containers["c" * 64] = SimpleNamespace(
                id="c" * 64, attrs={"Image": "foreign"}
            )
        with pytest.raises(CohortError):
            http.FixtureConnection(client, descriptor)
    assert (
        "c" * 64 in networks
        or "c" * 64 in containers
        or changed in {"certificate", "recipe"}
    )


def test_lost_creation_reply_stays_uncertain_and_cannot_be_retried(engine, tmp_path):
    client, containers, networks, events = engine
    client.containers.create = lambda *a, **kw: (_ for _ in ()).throw(
        OSError("lost reply")
    )
    fixture = http.RequestsHTTPFixture(client, tmp_path / "fixture")
    with pytest.raises(http.FixtureOwnershipError, match="creation remains uncertain"):
        fixture.__enter__()
    receipt = json.loads((fixture.output / "cleanup.json").read_text())
    assert receipt["verified"] is False and receipt["creation_uncertain"] is True
    assert (fixture.output / "container-intent.json").exists()
    assert not (fixture.output / "container-created.json").exists()
    assert not networks
    with pytest.raises(http.FixtureOwnershipError, match="previous.*unverified"):
        fixture.close()


def test_unverified_container_removal_keeps_network_and_terminal_evidence(
    engine, tmp_path
):
    client, containers, networks, events = engine
    fixture = http.RequestsHTTPFixture(client, tmp_path / "fixture").__enter__()
    fixture.container.remove = lambda **kwargs: None
    with pytest.raises(http.FixtureOwnershipError, match="cleanup remains unverified"):
        fixture.close()
    assert containers and networks and "remove_network" not in events
    assert (
        json.loads((fixture.output / "cleanup.json").read_text())["verified"] is False
    )


def test_recipe_hashes_actual_bootstrap_bytes():
    assert (
        http.definition()["bootstrap_sha256"]
        == sha256(http.BOOTSTRAP.encode()).hexdigest()
    )
