"""Binding policy gates; real pinned orchestration is tested by the E0 preflight."""

import hashlib
from copy import deepcopy
import sys
from types import SimpleNamespace

import pytest

from agentc._dispatch_context import provider_dispatch
from agentc._optimizer import Plan
from bench.campaign_transport import CampaignStopped
from bench.tau_campaign_binding import TauCampaignBinding

KEY = "literal-fixture-key"
MODEL = "openai/fixture"


@pytest.fixture
def binding(tmp_path, monkeypatch):
    calls = []

    class Response:
        def __init__(self, **kwargs):
            self._hidden_params = {}

    def call(**kwargs):
        calls.append(kwargs)
        return {"usage": {"cost": "0.02"}}

    transport = SimpleNamespace(
        contracts={MODEL: object()},
        key_id=hashlib.sha256(KEY.encode()).hexdigest(),
        http=SimpleNamespace(base_url="http://127.0.0.1:1/api/v1", deadline_seconds=10),
        call=call,
    )
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(ModelResponse=Response))
    instance = TauCampaignBinding(
        transport=transport,
        source=tmp_path,
        model_aliases={MODEL: MODEL, "fixture": MODEL},
        actor_output_caps={"agent": 16, "user": 16, "judge": 16},
    )
    instance._installed = True
    instance.guard = SimpleNamespace(assert_valid=lambda: None)
    return instance, calls


def invoke(instance, actor="agent", **extra):
    generate = instance._actor_generate(instance._completion, actor)
    with instance.task(task_id="task", arm="control", optimize_agent=False):
        return generate(
            model="fixture", messages=[{"role": "user", "content": "fixture"}], **extra
        )


@pytest.mark.parametrize("actor", ["agent", "user", "judge"])
def test_calibration_scope_is_frozen_and_captured_for_every_actor(binding, actor):
    instance, calls = binding
    metadata = {
        "calibration_trial_id": "registered-trial",
        "calibration_authorization_sha256": "a" * 64,
    }
    generate = instance._actor_generate(instance._completion, actor)
    with instance.task(
        task_id="task",
        arm="control",
        optimize_agent=False,
        calibration_metadata=metadata,
    ):
        metadata["calibration_trial_id"] = "changed-after-enrollment"
        generate(model="fixture", messages=[{"role": "user", "content": "fixture"}])
    assert calls[0]["metadata"]["calibration_trial_id"] == "registered-trial"
    assert calls[0]["metadata"]["calibration_authorization_sha256"] == "a" * 64


def test_safety_review_uses_judge_actor_and_binds_its_evidence(binding):
    instance, calls = binding
    generate = instance._actor_generate(
        instance._completion,
        "judge",
        {"judge_purpose": "tau-retail-safety-v1", "judge_evidence_sha256": "a" * 64},
    )
    with instance.task(task_id="task", arm="control", optimize_agent=True):
        generate(
            model="fixture", messages=[{"role": "user", "content": "review evidence"}]
        )
    assert calls[0]["metadata"]["actor"] == "judge"
    assert calls[0]["metadata"]["judge_purpose"] == "tau-retail-safety-v1"
    assert calls[0]["metadata"]["judge_evidence_sha256"] == "a" * 64


def test_review_metadata_cannot_overwrite_reserved_actor_attribution(binding):
    instance, calls = binding
    with pytest.raises(CampaignStopped):
        instance._actor_generate(
            instance._completion,
            "judge",
            {
                "judge_purpose": "tau-retail-safety-v1",
                "judge_evidence_sha256": "a" * 64,
                "actor": "agent",
            },
        )
    assert not calls


@pytest.mark.parametrize("actor", ["agent", "user", "judge"])
def test_all_actor_identity_and_explicit_caps_are_captured(binding, actor):
    instance, calls = binding
    response = invoke(instance, actor)
    assert response._hidden_params["response_cost"] == 0.02
    assert len(calls) == 1
    assert calls[0]["request"]["model"] == MODEL
    assert calls[0]["request"]["max_tokens"] == 16
    assert calls[0]["metadata"] == {
        "actor": actor,
        "kind": "primary",
        "task_id": "task",
        "arm": "control",
        "turn_id": f"{actor}-1",
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"num_retries": 1},
        {"caching": True},
        {"api_key": "different"},
        {"api_base": "http://other.invalid"},
        {"timeout": 20},
        {"max_tokens": 17},
        {"max_tokens": True},
        {"plugins": [{"id": "web"}]},
    ],
)
def test_unfrozen_client_features_fail_before_the_budget_backend(binding, extra):
    instance, calls = binding
    with pytest.raises(CampaignStopped):
        invoke(instance, **extra)
    assert calls == []
    with pytest.raises(CampaignStopped):
        invoke(instance)


def test_unknown_actor_scope_cannot_reach_a_provider(binding):
    instance, calls = binding
    with pytest.raises(CampaignStopped):
        instance._completion(model=MODEL, messages=[], num_retries=0)
    assert calls == []


def test_stock_wire_null_omission_preserves_history_and_argument_strings(binding):
    instance, calls = binding
    action = {
        "id": "tool-id",
        "name": "fixture",
        "type": "function",
        "function": {"name": "fixture", "arguments": '{"value":null}'},
    }
    history = [
        {"role": "user", "content": "fixture"},
        {"role": "assistant", "content": None, "tool_calls": [action]},
        {"role": "tool", "content": "result", "tool_call_id": "tool-id"},
        {"role": "assistant", "content": "answer", "tool_calls": None},
    ]
    before = deepcopy(history)
    generate = instance._actor_generate(instance._completion, "agent")
    with instance.task(task_id="task", arm="control", optimize_agent=False):
        generate(model=MODEL, messages=history)
    assert calls[0]["request"]["messages"] == [
        before[0],
        {"role": "assistant", "tool_calls": [action]},
        before[2],
        {"role": "assistant", "content": "answer"},
    ]
    assert history == before


def test_native_probe_tokens_are_forwarded_without_reconstruction(binding):
    instance, calls = binding
    parent = Plan(
        kind="pass_through", observation_token="parent", exploration_lease_token="lease"
    )
    leaf = Plan(kind="rewritten", observation_token="leaf")
    with provider_dispatch("exploration", plan=leaf, primary_plan=parent):
        invoke(instance)
    metadata = calls[0]["metadata"]
    assert metadata["kind"] == "exploration"
    assert metadata["observation_token"] == "leaf"
    assert metadata["parent_observation_token"] == "parent"
    assert metadata["exploration_lease_token"] == "lease"
    assert metadata["native_plan_json"] == leaf.raw_json
    assert metadata["parent_native_plan_json"] == parent.raw_json


def test_frozen_configuration_cannot_be_mutated_after_enrollment(binding):
    instance, _ = binding
    with pytest.raises(TypeError):
        instance.aliases["fixture"] = "other"
    with pytest.raises(TypeError):
        instance.caps["judge"] = 2048


def test_cell_attribution_captures_requested_model_before_native_mutation(binding):
    previous, calls = binding
    target = "openai/cheaper-snapshot"
    previous.transport.contracts[target] = object()
    instance = TauCampaignBinding(
        transport=previous.transport,
        source=previous.source,
        model_aliases={"fixture": MODEL, MODEL: MODEL, target: target},
        actor_output_caps={"agent": 16, "user": 16, "judge": 16},
        agent_model_allowlist=frozenset({MODEL, target}),
    )
    instance._installed = True
    instance.guard = previous.guard

    def mutated_generate(**kwargs):
        return instance._completion(
            model=target, messages=kwargs["messages"], num_retries=0
        )

    generate = instance._actor_generate(mutated_generate, "agent")
    with instance.task(
        task_id="task", arm="joint_guarded", optimize_agent=True, cell_id="a" * 64
    ):
        generate(model="fixture", messages=[{"role": "user", "content": "fixture"}])
    assert calls[0]["metadata"]["cell_id"] == "a" * 64
    assert calls[0]["metadata"]["requested_model"] == MODEL
    assert calls[0]["request"]["model"] == target


def test_helper_model_is_billed_for_user_but_cannot_become_an_agent_route(binding):
    previous, calls = binding
    helper = "openai/helper-only"
    previous.transport.contracts[helper] = object()
    instance = TauCampaignBinding(
        transport=previous.transport,
        source=previous.source,
        model_aliases={MODEL: MODEL, helper: helper},
        actor_output_caps={"agent": 16, "user": 16, "judge": 16},
        agent_model_allowlist=frozenset({MODEL}),
    )
    instance._installed = True
    instance.guard = previous.guard
    with instance.task(task_id="task", arm="control", optimize_agent=False):
        instance._actor_generate(instance._completion, "user")(
            model=helper, messages=[]
        )
        with pytest.raises(CampaignStopped):
            instance._actor_generate(instance._completion, "agent")(
                model=helper, messages=[]
            )
    assert len(calls) == 1 and calls[0]["metadata"]["actor"] == "user"


@pytest.mark.parametrize("cell_id", [True, 7, "short", "A" * 64, "g" * 64])
def test_invalid_cell_identity_rejects_scope_before_dispatch(binding, cell_id):
    instance, calls = binding
    with pytest.raises(CampaignStopped):
        with instance.task(
            task_id="task", arm="control", optimize_agent=False, cell_id=cell_id
        ):
            invoke(instance)
    assert calls == []


def test_actual_planning_wall_time_and_context_are_observed(binding):
    instance, _ = binding
    events = []
    instance.plan_observer = events.append
    plan = Plan(kind="pass_through")
    planner = instance._observed_planner(lambda call: plan)

    def generate(**kwargs):
        assert planner({"call_site_id": "real.call.site", "model": MODEL}) is plan

    with instance.task(
        task_id="task", arm="joint_guarded", optimize_agent=True, cell_id="b" * 64
    ):
        instance._actor_generate(generate, "agent")(model=MODEL)
    assert events[0]["turn_id"] == "agent-1"
    assert events[0]["cell_id"] == "b" * 64
    assert events[0]["native_plan_json"] == plan.raw_json
    assert events[0]["planning_overhead_us"] >= 0


def test_swallowed_observer_error_is_sticky_before_provider_dispatch(binding):
    instance, calls = binding

    def failed_observer(event):
        raise RuntimeError("fixture audit failed")

    instance.plan_observer = failed_observer
    planner = instance._observed_planner(lambda call: Plan(kind="pass_through"))

    def generate(**kwargs):
        # Reproduce the SDK's planning fail-open boundary.
        try:
            planner({"call_site_id": "site", "model": MODEL})
        except BaseException:
            pass
        return instance._completion(model=MODEL, messages=[], num_retries=0)

    with instance.task(task_id="task", arm="joint_guarded", optimize_agent=True):
        with pytest.raises(CampaignStopped):
            instance._actor_generate(generate, "agent")(model=MODEL)
    assert calls == []


def test_alias_configuration_cannot_silently_route_to_another_model(binding, tmp_path):
    instance, _ = binding
    with pytest.raises(CampaignStopped):
        TauCampaignBinding(
            transport=instance.transport,
            source=tmp_path,
            model_aliases={"strong": MODEL},
            actor_output_caps=dict(instance.caps),
        )
