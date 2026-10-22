"""Before-import isolation and exact owned-container cleanup."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from bench.swe_agent_process import _owned_containers, runtime_environment
from bench.swe_agent_campaign_worker import SweAgentTaskConfig
from bench.swebench_cohort import CohortError


def test_importing_the_launcher_and_runtime_manifest_does_not_import_agentc():
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from bench.swe_agent_campaign_worker import runtime_manifest
from bench.swe_agent_process import launch_task
assert not any(n == 'agentc' or n.startswith('agentc.') for n in sys.modules)
record = runtime_manifest()
assert record['sdk_files'] and record['harness_files']
assert not any(n == 'agentc' or n.startswith('agentc.') for n in sys.modules)
"""
    from pathlib import Path

    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(Path.cwd())],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_arm_environment_is_explicit_and_contains_no_provider_credentials(tmp_path):
    config = SweAgentTaskConfig("strong", "cheap")
    for arm in (
        "unmodified_fixed_strong",
        "trace_only_fixed_strong",
        "joint_guarded",
        "routing_only",
    ):
        value = runtime_environment(config, arm, tmp_path)
        assert value["AGENTC_STORAGE_PATH"] == str(tmp_path)
        assert value["AGENTC_OPTIMIZE"] == (
            "1" if arm in ("joint_guarded", "routing_only") else "0"
        )
        assert all(k.startswith("AGENTC_") for k in value)
    assert (
        runtime_environment(config, "routing_only", tmp_path)["AGENTC_ENABLED_RULES"]
        == "ModelDowngrade"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"Image": "foreign"},
        {"Name": "/foreign"},
        {"Mounts": [{}]},
        {"HostConfig": {"NetworkMode": "host"}},
    ],
)
def test_parent_will_not_delete_a_foreign_container(change, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(errors=SimpleNamespace(NotFound=KeyError)),
    )
    owner = "agentc-swe-" + "a" * 32
    attrs = {
        "Config": {"Labels": {"agentc.swe.owner": owner}},
        "Name": "/" + owner,
        "Image": "immutable",
        "HostConfig": {"NetworkMode": "none"},
        "Mounts": [],
    }
    removed = []
    container = SimpleNamespace(
        attrs=attrs | change,
        reload=lambda: None,
        id="owned",
        remove=lambda **kw: removed.append(kw),
    )
    client = SimpleNamespace(containers=SimpleNamespace(list=lambda **kw: [container]))
    with pytest.raises(CohortError):
        _owned_containers(client, owner, "immutable", remove=True)
    assert removed == []


def test_uninstrumented_binding_never_imports_sdk_even_when_entering_or_closing(
    tmp_path,
):
    script = """
import hashlib, importlib.abc, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
class RejectSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname == 'agentc' or fullname.startswith('agentc.'):
            raise AssertionError('baseline attempted an SDK import')
sys.meta_path.insert(0, RejectSdk())
from bench import swe_agent_binding as swe
from bench.campaign_chat import ChatContract
source = Path(sys.argv[2])
swe.verify_agent = lambda p: {'closure_sha256': 'f'*64}
class Response:
    def __init__(self, **body):
        self._hidden_params = {}
litellm = SimpleNamespace(completion=None, acompletion=None, ModelResponse=Response,
                         cost_calculator=SimpleNamespace(completion_cost=None))
class Model:
    config = SimpleNamespace(name='fixture', per_instance_cost_limit=2.0)
    tools = SimpleNamespace(use_function_calling=True)
    def query(self):
        return litellm.completion(model='fixture', messages=[{'role':'user','content':'fixture'}], tools=[{'type':'function','function':{'name':'bash','parameters':{'type':'object'}}}], max_tokens=16)
models = SimpleNamespace(__file__=str(source/'sweagent/models.py'), LiteLLMModel=Model)
package = SimpleNamespace(__file__=str(source/'sweagent/__init__.py'), CONFIG_DIR=source/'config', TOOLS_DIR=source/'tools')
serializer = SimpleNamespace(__file__=str(Path(sys.argv[1])/'bench/campaign_chat.py'))
sys.modules['litellm'] = litellm
sys.modules['sweagent'] = package
sys.modules['sweagent.agent.models'] = models
sys.modules['litellm.llms.openrouter.chat.transformation'] = serializer
import importlib.metadata
importlib.metadata.version = lambda name: 'fixture-version'
calls = []
transport = SimpleNamespace(contracts={'fixture': ChatContract('fixture','fixture','Fixture','fixture',128,16,cached_text_blocks=True)},
    key_id='a'*64, http=SimpleNamespace(base_url='http://127.0.0.1:1'),
    call=lambda **kw: calls.append(kw) or {'usage':{'cost':'0.0001'}}, assert_complete=lambda: {})
with swe.SweAgentCampaignBinding(transport=transport, source=source, models=frozenset({'fixture'}),output_cap=16,cohort_sha256='f'*64,instrument=False) as binding:
    binding._wire_request = lambda values: values
    with binding.task(task_id='task',arm='unmodified_fixed_strong',cell_id='a'*64,requested_model='fixture',optimize=False):
        Model().query()
assert len(calls) == 1
assert not any(n == 'agentc' or n.startswith('agentc.') for n in sys.modules)
"""
    from pathlib import Path

    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(Path.cwd()), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_parent_cleanup_uses_exact_id_even_if_label_search_loses_it(monkeypatch):
    class NotFound(Exception):
        pass

    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(errors=SimpleNamespace(NotFound=NotFound)),
    )
    owner, removed = "agentc-swe-" + "a" * 32, []
    container = SimpleNamespace(
        id="owned",
        attrs={
            "Name": "/" + owner,
            "Image": "immutable",
            "Mounts": [],
            "Config": {"Labels": {"agentc.swe.owner": owner}},
            "HostConfig": {"NetworkMode": "none"},
        },
        reload=lambda: None,
        remove=lambda **kwargs: removed.append(kwargs),
    )

    def get(identity):
        assert identity == "owned"
        if removed:
            raise NotFound()
        return container

    client = SimpleNamespace(
        containers=SimpleNamespace(list=lambda **kwargs: [], get=get)
    )
    _owned_containers(client, owner, "immutable", remove=True, container_id="owned")
    assert removed == [{"force": True}]


def test_lost_creation_response_remains_uncertain_and_cannot_resume(
    tmp_path, monkeypatch
):
    import json
    from pathlib import Path
    from bench import swe_agent_process as process

    closed = []
    (tmp_path / "summary.json").write_text("{}")
    socket_path = tmp_path / "docker.sock"
    monkeypatch.setattr(Path, "is_socket", lambda p: p == socket_path)
    client = SimpleNamespace(
        api=SimpleNamespace(
            base_url="http+docker://localhost",
            get_adapter=lambda url: SimpleNamespace(socket_path=str(socket_path)),
        ),
        close=lambda: closed.append("client"),
    )
    monkeypatch.setitem(
        sys.modules, "docker", SimpleNamespace(from_env=lambda **kw: client)
    )
    bridge = SimpleNamespace(
        deadline=float("inf"), close=lambda: closed.append("bridge")
    )
    monkeypatch.setattr(process, "TransportBridge", lambda *a, **kw: bridge)
    monkeypatch.setattr(
        process,
        "create_task_container",
        lambda *a: (_ for _ in ()).throw(OSError("lost daemon reply")),
    )
    monkeypatch.setattr(
        process,
        "_owned_containers",
        lambda *a, **kw: closed.append("discovered cleanup"),
    )
    monkeypatch.setattr(
        process.subprocess,
        "Popen",
        lambda *a, **kw: pytest.fail("child started without a known container"),
    )
    inputs = SimpleNamespace(
        pins={"checks": [{"task_id": "task", "config_digest": "sha256:" + "a" * 64}]},
        **{
            k: tmp_path
            for k in ("dataset", "agent", "evaluator", "task_assets", "image_pins")
        },
    )
    kwargs = dict(
        transport=object(),
        inputs=inputs,
        environment_report=tmp_path,
        bundle=SimpleNamespace(root=tmp_path),
        output=tmp_path,
        cell=SimpleNamespace(task_id="task", arm="unmodified_fixed_strong"),
        storage=tmp_path / "state",
        config=SweAgentTaskConfig("strong", "cheap"),
    )
    with pytest.raises(CohortError, match="creation remains uncertain"):
        process.launch_task(**kwargs)
    receipt = json.loads((tmp_path / "container-cleanup.json").read_text())
    assert receipt == {
        "verified": False,
        "creation_uncertain": True,
        "container_id": None,
    }
    assert closed == ["bridge", "discovered cleanup", "client"]
    assert not (tmp_path / "process-enrollment.json").exists()
    with pytest.raises(CohortError, match="cannot retry or resume"):
        process.launch_task(**kwargs)
