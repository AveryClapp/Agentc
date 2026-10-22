"""Own function calls, exact cell liabilities and fatal failures at the SWE seam."""

import hashlib
import sys
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from agentc._dispatch_context import provider_dispatch
from agentc._optimizer import Plan
from bench import swe_agent_binding as swe
from bench.campaign_chat import ChatContract
from bench.campaign_transport import CampaignStopped

MODEL = "openai/fixture"
KEY = "literal-fixture-key"
CELL = "a" * 64
TOOL = {
    "type": "function",
    "function": {"name": "bash", "parameters": {"type": "object"}},
}


@pytest.fixture
def binding(tmp_path, monkeypatch):
    calls = []

    class Response:
        def __init__(self, **body):
            self.body = body
            self._hidden_params = {}

    def call(**kwargs):
        calls.append(kwargs)
        return {
            "usage": {"cost": "0.0001"},
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "literal-id",
                                "type": "function",
                                "function": {
                                    "name": "bash",
                                    "arguments": "INVALID JSON",
                                },
                            }
                        ]
                    }
                }
            ],
        }

    contract = ChatContract(
        MODEL, "fixture", "Fixture", MODEL, 128, 16, cached_text_blocks=True
    )
    transport = SimpleNamespace(
        contracts={MODEL: contract},
        key_id=hashlib.sha256(KEY.encode()).hexdigest(),
        http=SimpleNamespace(base_url="http://127.0.0.1:1/api/v1"),
        call=call,
    )
    monkeypatch.setattr(
        swe, "verify_agent", lambda source: {"closure_sha256": "f" * 64}
    )
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(ModelResponse=Response))
    instance = swe.SweAgentCampaignBinding(
        transport=transport,
        source=tmp_path,
        models=frozenset({MODEL}),
        output_cap=16,
        cohort_sha256="f" * 64,
    )
    instance._installed = True
    monkeypatch.setattr(
        instance,
        "_wire_request",
        lambda values: {
            k: deepcopy(v)
            for k, v in values.items()
            if k in {"model", "messages", "tools", "max_tokens"}
        },
    )
    return instance, calls


def invoke(instance, **extra):
    model = SimpleNamespace(
        config=SimpleNamespace(name=MODEL, per_instance_cost_limit=2.0),
        tools=SimpleNamespace(use_function_calling=True),
    )
    wrapped = instance._query(
        lambda model: instance._completion(
            model=MODEL,
            messages=[{"role": "user", "content": "fixture"}],
            tools=[TOOL],
            max_tokens=16,
            **extra,
        )
    )
    with instance.task(
        task_id="task",
        arm="strong",
        cell_id=CELL,
        requested_model=MODEL,
        optimize=False,
    ):
        return wrapped(model)


def test_actual_function_envelope_is_not_parsed_repaired_or_executed(binding):
    instance, calls = binding
    response = invoke(instance)
    assert len(calls) == 1
    assert (
        response.body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        == "INVALID JSON"
    )
    assert response._hidden_params["response_cost"] == 0.0001
    assert calls[0]["request"]["tools"] == [TOOL]
    assert calls[0]["metadata"] == {
        "actor": "agent",
        "kind": "primary",
        "task_id": "task",
        "arm": "strong",
        "cell_id": CELL,
        "turn_id": "swe-agent-1",
        "requested_model": MODEL,
    }
    state = instance.cell_accounting(CELL)
    assert Decimal(state["committed_usd"]) == Decimal("0.0001")
    assert Decimal(state["pending_upper_usd"]) == 0
    assert state["budget_exhausted"] is False


@pytest.mark.parametrize(
    "extra",
    [
        {"api_key": "other"},
        {"api_base": "http://other.invalid"},
        {"fallbacks": [{"model": "other"}]},
        {"n": 2},
        {"api_version": "future"},
        {"num_retries": 1},
        {"caching": True},
        {"plugins": [{"id": "web"}]},
    ],
)
def test_unfrozen_provider_features_stop_before_dispatch(binding, extra):
    instance, calls = binding
    with pytest.raises(CampaignStopped):
        invoke(instance, **extra)
    assert not calls and instance._failed


def test_unknown_scope_cannot_dispatch(binding):
    instance, calls = binding
    with pytest.raises(CampaignStopped):
        instance._completion(model=MODEL, messages=[])
    assert not calls


def test_calibration_attribution_survives_upstream_query_and_cannot_be_rebound(binding):
    instance, calls = binding
    metadata = {
        "calibration_trial_id": "trial-1",
        "calibration_authorization_sha256": "a" * 64,
    }
    model = SimpleNamespace(
        config=SimpleNamespace(name=MODEL, per_instance_cost_limit=2.0),
        tools=SimpleNamespace(use_function_calling=True),
    )
    query = instance._query(
        lambda model: instance._completion(
            model=MODEL,
            messages=[{"role": "user", "content": "fixture"}],
            tools=[TOOL],
            max_tokens=16,
        )
    )
    scope = dict(
        task_id="task",
        arm="strong",
        cell_id=CELL,
        requested_model=MODEL,
        optimize=False,
    )
    with instance.task(**scope, calibration_metadata=metadata):
        metadata["calibration_trial_id"] = "mutated-after-enrollment"
        query(model)
    assert calls[0]["metadata"]["calibration_trial_id"] == "trial-1"
    assert calls[0]["metadata"]["calibration_authorization_sha256"] == "a" * 64
    with pytest.raises(CampaignStopped):
        with instance.task(**scope):
            pytest.fail("missing trial attribution cannot reuse the cell")


def test_invalid_calibration_attribution_stops_before_dispatch(binding):
    instance, calls = binding
    with pytest.raises(CampaignStopped):
        with instance.task(
            task_id="task",
            arm="strong",
            cell_id=CELL,
            requested_model=MODEL,
            optimize=False,
            calibration_metadata={"calibration_trial_id": "unbound"},
        ):
            pytest.fail("unbound metadata cannot enter a query")
    assert not calls


def test_full_execution_cell_is_fresh_and_cannot_retry(binding):
    instance, calls = binding
    instance.claim_execution(CELL)
    with pytest.raises(CampaignStopped):
        instance.claim_execution(CELL)
    assert not calls


def test_full_execution_cannot_reuse_prior_model_queries(binding):
    instance, calls = binding
    invoke(instance)
    with pytest.raises(CampaignStopped):
        instance.claim_execution(CELL)
    assert len(calls) == 1


def test_native_probe_tokens_keep_parent_attribution(binding):
    instance, calls = binding
    parent = Plan(
        kind="pass_through", observation_token="parent", exploration_lease_token="lease"
    )
    leaf = Plan(kind="rewritten", observation_token="leaf")
    with provider_dispatch("exploration", plan=leaf, primary_plan=parent):
        invoke(instance)
    metadata = calls[0]["metadata"]
    assert metadata["kind"] == "exploration" and metadata["observation_token"] == "leaf"
    assert (
        metadata["parent_observation_token"] == "parent"
        and metadata["exploration_lease_token"] == "lease"
    )
    assert (
        metadata["native_plan_json"] == leaf.raw_json
        and metadata["parent_native_plan_json"] == parent.raw_json
    )


def test_cell_ceiling_checks_full_next_call_before_dispatch(binding):
    instance, calls = binding
    instance._spent[CELL] = Decimal("1.9999")
    with pytest.raises(swe.CellBudgetExhausted):
        invoke(instance)
    assert not calls and not instance._failed
    assert Decimal(instance.cell_accounting(CELL)["pending_upper_usd"]) == 0
    assert instance.cell_accounting(CELL)["budget_exhausted"] is True
    with pytest.raises(CampaignStopped):
        invoke(instance)


@pytest.mark.parametrize("exact", [True, False])
def test_failed_call_retains_measured_cost_and_uncertain_liability(binding, exact):
    instance, calls = binding

    def fail(**kwargs):
        calls.append(kwargs)
        raise CampaignStopped("provider failure")

    def accounting(call_id):
        if not exact:
            raise OSError("unavailable")
        return {"known_cost_usd": "0.0001", "uncertain_budget_usd": "0.0005"}

    instance.transport.call = fail
    instance.transport.call_accounting = accounting
    with pytest.raises(CampaignStopped):
        invoke(instance)
    assert len(calls) == 1 and instance._failed
    state = instance.cell_accounting(CELL)
    expected = (
        Decimal("0.0006")
        if exact
        else instance.transport.contracts[MODEL].bound({"max_tokens": 16})
    )
    assert Decimal(state["committed_usd"]) == expected
    assert Decimal(state["pending_upper_usd"]) == 0


def test_planning_audit_failure_blocks_fail_open_provider_dispatch(binding):
    instance, calls = binding
    instance.plan_observer = lambda event: (_ for _ in ()).throw(
        OSError("audit failed")
    )
    model = SimpleNamespace(
        config=SimpleNamespace(name=MODEL, per_instance_cost_limit=2),
        tools=SimpleNamespace(use_function_calling=True),
    )
    wrapped = instance._query(
        lambda model: instance._planner(lambda call: Plan(kind="pass_through"))(
            {"call_site_id": "site"}
        )
    )
    with instance.task(
        task_id="task", arm="strong", cell_id=CELL, requested_model=MODEL, optimize=True
    ):
        with pytest.raises(OSError):
            wrapped(model)
        with pytest.raises(CampaignStopped):
            instance._completion(model=MODEL, messages=[])
    assert instance._failed and not calls


@pytest.mark.parametrize("field", ["task_id", "arm", "optimize"])
def test_cell_identity_cannot_be_rebound_after_a_query(binding, field):
    instance, calls = binding
    invoke(instance)
    scope = dict(
        task_id="task",
        arm="strong",
        cell_id=CELL,
        requested_model=MODEL,
        optimize=False,
    )
    scope[field] = True if field == "optimize" else "other"
    with pytest.raises(CampaignStopped):
        with instance.task(**scope):
            pytest.fail("changed cell must not enter")
    assert len(calls) == 1 and instance._failed


def test_concurrent_calls_reserve_pending_liability_before_dispatch(binding):
    instance, calls = binding
    original = instance.transport.call
    entered, release = Event(), Event()
    bound = instance.transport.contracts[MODEL].bound({"max_tokens": 16})
    instance._spent[CELL] = Decimal("2") - bound

    def held(**kwargs):
        entered.set()
        assert release.wait(10)
        return original(**kwargs)

    instance.transport.call = held
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(invoke, instance)
        try:
            assert entered.wait(10)
            assert Decimal(instance.cell_accounting(CELL)["pending_upper_usd"]) == bound
            with pytest.raises(swe.CellBudgetExhausted):
                invoke(instance)
        finally:
            release.set()
        pending.result(timeout=10)
    assert len(calls) == 1 and not instance._failed
    state = instance.cell_accounting(CELL)
    assert Decimal(state["committed_usd"]) <= 2
    assert Decimal(state["pending_upper_usd"]) == 0
    assert state["budget_exhausted"] is True
