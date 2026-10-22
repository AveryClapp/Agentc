from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import sys

import pytest

from agentc._dispatch_context import provider_dispatch
from agentc._optimizer import Plan
from bench.campaign_transport import CampaignStopped
from bench import coding_campaign_binding as module

MODEL = "openai/gpt-4o-2024-11-20"
CELL = "a" * 64


@pytest.fixture
def active(tmp_path, monkeypatch):
    calls = []
    upstream = {
        "revision": module.MINI_REVISION,
        "files": {},
        "closure_sha256": "b" * 64,
    }
    monkeypatch.setattr(module, "source_manifest", lambda _: upstream)

    class Response:
        def __init__(self, **body):
            self.choices = [
                SimpleNamespace(
                    message=SimpleNamespace(**body["choices"][0]["message"])
                )
            ]
            self._hidden_params = {}

    def call(**kwargs):
        calls.append(deepcopy(kwargs))
        return {
            "usage": {"cost": "0.0004"},
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "```bash\nprintf reference\n```"},
                }
            ],
        }

    vendor = SimpleNamespace(
        completion=lambda: None, acompletion=lambda: None, ModelResponse=Response
    )
    monkeypatch.setitem(sys.modules, "litellm", vendor)
    transport = SimpleNamespace(contracts={MODEL: object()}, call=call)
    binding = module.CodingCampaignBinding(
        transport=transport,
        source=tmp_path,
        model_pair=frozenset({MODEL}),
        output_cap=1024,
        max_steps=3,
        cohort_sha256="c" * 64,
    )
    binding._installed = True
    vendor.completion = binding._completion
    model = binding.model(
        task_id="task",
        arm="control",
        cell_id=CELL,
        requested_model=MODEL,
        optimize=False,
    )
    enrollment = {
        "upstream_revision": module.MINI_REVISION,
        "upstream_source": upstream,
        "agent_configuration": {"step_limit": 3},
        "environment": {"forward_env": [], "cwd": "/testbed", "image": "image"},
    }
    model.prepare_execution(enrollment)
    return binding, model, calls, enrollment


def test_actual_history_models_and_bills_are_preserved(active):
    binding, model, calls, _ = active
    messages = [{"role": "user", "content": "public issue"}]
    before = deepcopy(messages)
    assert model.query(messages) == {"content": "```bash\nprintf reference\n```"}
    assert messages == before and calls[0]["request"]["messages"] == before
    assert calls[0]["request"]["model"] == MODEL
    assert calls[0]["metadata"]["cell_id"] == CELL
    assert calls[0]["metadata"]["requested_model"] == MODEL
    assert model.n_calls == 1 and model.cost == 0.0004
    assert binding._frame.get() is None


def test_calibration_model_freezes_trial_provenance_before_queries(active):
    binding, _, calls, enrollment = active
    metadata = {
        "calibration_trial_id": "registered-trial",
        "calibration_authorization_sha256": "a" * 64,
    }
    model = binding.model(
        task_id="task",
        arm="control",
        cell_id=CELL,
        requested_model=MODEL,
        optimize=False,
        calibration_metadata=metadata,
    )
    model.prepare_execution(enrollment)
    metadata["calibration_trial_id"] = "changed"
    model.query([{"role": "user", "content": "public issue"}])
    assert calls[0]["metadata"]["calibration_trial_id"] == "registered-trial"
    assert calls[0]["metadata"]["calibration_authorization_sha256"] == "a" * 64


def test_counterfactuals_retain_actual_opaque_tokens_and_contribute_to_cell_cost(
    active,
):
    _, model, calls, _ = active
    parent = Plan(
        kind="pass_through",
        raw_json="opaque parent",
        observation_token="opaque observation",
        exploration_lease_token="opaque lease",
    )
    child = Plan(kind="pass_through", raw_json="opaque candidate")
    with provider_dispatch("exploration", plan=child, primary_plan=parent):
        model.query([{"role": "user", "content": "fixture"}])
    metadata = calls[0]["metadata"]
    assert metadata["kind"] == "exploration"
    assert metadata["native_plan_json"] == "opaque candidate"
    assert metadata["parent_native_plan_json"] == "opaque parent"
    assert metadata["exploration_lease_token"] == "opaque lease"
    model.query([{"role": "user", "content": "fixture"}])
    assert model.cost == 0.0008


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_tokens", 2049),
        ("num_retries", 1),
        ("caching", True),
        ("model", "unenrolled"),
        ("api_key", "not-a-live-key"),
        ("tools", [{}]),
    ],
)
def test_unfrozen_dispatch_cannot_charge_and_failure_is_sticky(active, field, value):
    binding, model, calls, _ = active
    original = sys.modules["litellm"].completion

    def invalid(**kwargs):
        kwargs[field] = value
        return original(**kwargs)

    sys.modules["litellm"].completion = invalid
    with pytest.raises(CampaignStopped):
        model.query([{"role": "user", "content": "fixture"}])
    assert not calls and binding._failed
    with pytest.raises(CampaignStopped):
        model.query([{"role": "user", "content": "fixture"}])


def test_bad_billed_response_is_retained_and_never_drives_an_action(active):
    binding, model, calls, _ = active
    original = binding.transport.call

    def truncated(**kwargs):
        body = original(**kwargs)
        body["choices"][0]["finish_reason"] = "length"
        return body

    binding.transport.call = truncated
    with pytest.raises(CampaignStopped):
        model.query([{"role": "user", "content": "fixture"}])
    assert len(calls) == 1 and model.cost == 0.0004 and model.n_calls == 0
    assert binding._failed


def test_execution_contract_cannot_be_reprepared_or_change_its_step_bound(active):
    binding, model, _, enrollment = active
    with pytest.raises(CampaignStopped):
        model.prepare_execution(enrollment)
    fresh = binding.model(
        task_id="other",
        arm="control",
        cell_id="d" * 64,
        requested_model=MODEL,
        optimize=False,
    )
    enrollment["agent_configuration"]["step_limit"] = 4
    with pytest.raises(CampaignStopped):
        fresh.prepare_execution(enrollment)


def test_unscoped_dispatch_cannot_reach_the_backend(active):
    binding, _, calls, _ = active
    with pytest.raises(CampaignStopped):
        binding._completion(
            model=MODEL, messages=[], max_tokens=1024, num_retries=0, caching=False
        )
    assert not calls
