"""TOML bootstrap, real rule semantics and exact persisted admission."""

import json
import os
from unittest.mock import patch

import pytest

import agentc
from agentc import _native
from agentc._config import resolve_config
from agentc._optimizer import Plan, observe_outcome, plan_call
from agentc._patches._optimizer_glue import _shadow_sample_rules, maybe_explore_record
from test_cached_exploration_native import (
    PROTOCOLS,
    response,
    restart,
    runtime as runtime,
    seeded_call,
)
from test_provider_config_native import request


@pytest.fixture
def configured(tmp_path, monkeypatch):
    agentc.shutdown()
    _native.optimize_reset()
    for name in list(os.environ):
        if name.startswith("AGENTC_OPTIMIZE") or name in {
            "AGENTC_ENABLED_RULES",
            "AGENTC_COMPOSE",
            "AGENTC_CONFIG_PATH",
            "AGENTC_SHADOW_DIVERGENCE_BUDGET",
        }:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTC_MODEL_CATALOG_OFFLINE", "1")
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "current_greedy")
    yield tmp_path
    agentc.shutdown()
    _native.optimize_reset()


def write_config(storage, text):
    storage.mkdir(exist_ok=True)
    path = storage / "config.toml"
    path.write_text(text)
    _native.optimize_reset()
    _native.optimize_configure(str(storage))
    return json.loads(_native.optimize_configuration())


def warm(call, count=3):
    for _ in range(count):
        plan = _native.optimize_plan(json.dumps(call))
        _native.optimize_observe(
            plan,
            json.dumps(
                {
                    "call_site_id": call["call_site_id"],
                    "model": call["model"],
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "latency_ms": 100,
                    "cost_usd": 0.01,
                }
            ),
        )


@pytest.mark.parametrize("enabled", [False, True])
def test_toml_enable_flag_changes_actual_rewrite(configured, enabled):
    state = write_config(
        configured,
        "[optimizer]\ncompose=false\nmax_overhead_ms=100\n[optimizer.rules.OutputBudget]\nenabled="
        + str(enabled).lower()
        + "\n",
    )
    assert state["configuration_error"] is None
    call = request(PROTOCOLS[0])
    warm(call)
    selected = json.loads(_native.optimize_plan(json.dumps(call)))
    assert (selected["kind"] == "rewritten") == enabled
    if enabled:
        assert selected["rule"] == "OutputBudget"
        assert selected["call"]["parameters"]["max_output_tokens"] < 512


def test_environment_whitelist_overrides_file_flags_without_losing_tuning(
    configured, monkeypatch
):
    monkeypatch.setenv("AGENTC_ENABLED_RULES", "ContextCompress")
    state = write_config(
        configured,
        "[optimizer.rules.ContextCompress]\nenabled=false\nmin_prompt_bytes=1234\n[optimizer.rules.OutputBudget]\nenabled=true\n",
    )
    rules = state["configuration"]["rules"]
    assert (
        rules["ContextCompress"]["enabled"]
        and rules["ContextCompress"]["min_prompt_bytes"] == 1234
    )
    assert not rules["OutputBudget"]["enabled"]


@pytest.mark.parametrize(
    "bad",
    [
        "[optimizer]\nhot_thresold=2",
        "[optimizer.rules.ModelDowngrade]\nroute=[]",
        "[optimizer.rules.OutputBudget]\nmin_prompt_bytes=4",
        "[optimizer.accuracy_budget]\nUnknown=0.1",
        "[optimizer.accuracy_budget]\nCacheHit=nan",
        "[optimizer]\nshadow_rate=inf",
        "[optimizer.rules.ParallelBranch]\nmax_fanout=1",
        "[optimizer.rules.ContextCompress]\nmin_dead_fraction=-0.1",
        "[optimizer]\nhot_threshold=0",
        '[optimizer.rules.CacheHit]\nenabled="true"',
    ],
)
def test_invalid_declared_file_disables_optimizer_and_probes(configured, bad):
    state = write_config(configured, bad)
    assert state["configuration_error"]
    assert not state["configuration"]["enabled"]
    assert not state["configuration"]["exploration"]["enabled"]
    call = request(PROTOCOLS[0])
    warm(call, 5)
    assert json.loads(_native.optimize_plan(json.dumps(call)))["kind"] == "pass_through"


@pytest.mark.parametrize(
    "variable,value",
    [
        ("AGENTC_OPTIMIZE_SHADOW", "bad"),
        ("AGENTC_ENABLED_RULES", "Unknown"),
        ("AGENTC_OPTIMIZE_HOT_THRESHOLD", "-1"),
    ],
)
def test_invalid_environment_cannot_resolve_to_an_ignored_default(
    configured, monkeypatch, variable, value
):
    monkeypatch.setenv(variable, value)
    state = write_config(configured, "[optimizer]\nenabled=true\n")
    assert state["configuration_error"] and not state["configuration"]["enabled"]
    assert value not in state["configuration_error"]


def test_invalid_optional_allocator_disables_only_probe_acquisition(
    configured, monkeypatch
):
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION", "invalid")
    state = write_config(configured, "[optimizer]\nenabled=true\n")["configuration"]
    assert state["enabled"] and not state["exploration"]["enabled"]


def test_bootstrap_is_shared_across_relocated_storage_and_environment_restores(
    configured, monkeypatch
):

    bootstrap = configured / "bootstrap"
    destination = configured / "data"
    bootstrap.mkdir()
    path = bootstrap / "config.toml"
    path.write_text(
        "storage_path="
        + json.dumps(str(destination))
        + "\n[optimizer]\nhot_threshold=7\n"
    )
    monkeypatch.setenv("AGENTC_CONFIG_PATH", str(path))
    resolved = resolve_config()
    assert resolved.config_path == path and resolved.storage_path == destination
    prior = os.environ.get("AGENTC_STORAGE_PATH")
    with patch("agentc._lifecycle._apply_patches"):
        agentc.init(capture_embeddings=False)
    assert _native.optimize_storage_path() == str(destination)
    assert (
        json.loads(_native.optimize_configuration())["configuration"]["hot_threshold"]
        == 7
    )
    assert os.environ["AGENTC_CONFIG_PATH"] == str(path)
    agentc.shutdown()
    assert os.environ.get("AGENTC_STORAGE_PATH") == prior
    assert os.environ["AGENTC_CONFIG_PATH"] == str(path)


@pytest.mark.parametrize("control", ["min_prompt_bytes=50000", "min_dead_fraction=1.0"])
def test_compression_tuning_changes_real_native_mutations(
    configured, monkeypatch, control
):
    from test_context_compress_native import call_for, request as compression_request

    monkeypatch.setenv("AGENTC_ENABLED_RULES", "ContextCompress")
    call = call_for(
        compression_request(
            [
                {"role": "system", "content": "instructions"},
                {"role": "user", "content": "original task"},
                {"role": "assistant", "content": "old command"},
                {"role": "user", "content": "obsolete output " * 1000},
                {"role": "assistant", "content": "current command"},
                {"role": "user", "content": "current result"},
            ]
        ),
        [1.0, 1.0, 0.0, 0.0, 1.0, 1.0],
    )
    base = "[optimizer]\nmax_overhead_ms=100\n[optimizer.rules.ContextCompress]\n"
    write_config(configured, base + "min_prompt_bytes=1024\nmin_dead_fraction=0.0\n")
    warm(call)
    assert (
        json.loads(_native.optimize_plan(json.dumps(call)))["rule"] == "ContextCompress"
    )
    write_config(configured, base + control + "\n")
    assert json.loads(_native.optimize_plan(json.dumps(call)))["kind"] == "pass_through"


def test_selection_and_exploration_controls_resolve_once(configured, monkeypatch):
    text = '[optimizer.selection]\nobjective="latency"\nmin_plan_evidence=23\nplan_profile_freshness_hours=12\nmax_rewrite_depth=2\ndivergence_exposure_budget=0.5\n[optimizer.exploration]\ncalls_per_site_24h=30\nmax_concurrent_counterfactuals=2\nallocation="least_evidence"\n'
    state = write_config(configured, text)["configuration"]
    assert state["selection"]["objective"] == "latency"
    assert state["selection"]["min_plan_evidence"] == 23
    assert state["selection"]["divergence_exposure_budget"] == 0.5
    assert state["exploration"]["calls_per_site_24h"] == 30
    monkeypatch.setenv("AGENTC_OPTIMIZE_OBJECTIVE", "cost")
    assert json.loads(_native.optimize_configuration())["configuration"] == state
    _native.optimize_reset()
    _native.optimize_configure(str(configured))
    assert (
        json.loads(_native.optimize_configuration())["configuration"]["selection"][
            "objective"
        ]
        == "cost"
    )


def test_sampling_rate_is_issued_with_the_plan_and_does_not_follow_later_environment(
    configured, monkeypatch
):
    state = write_config(
        configured, "[optimizer]\nshadow_rate=0\ncompose=false\nmax_overhead_ms=100\n"
    )
    assert state["configuration"]["shadow_rate"] == 0
    call = request(PROTOCOLS[0])
    warm(call)
    raw = _native.optimize_plan(json.dumps(call))
    assert json.loads(raw)["kind"] == "rewritten"
    assert json.loads(raw)["agentc_observation_context"]["sampling_rate"] == 0
    plan = Plan(kind="rewritten", rule="OutputBudget", raw_json=raw)
    plan.observation_token = "issued"
    monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "1")
    assert _shadow_sample_rules(plan, call["call_site_id"]) == []


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_rule_budget_changes_cold_start_only_the_affected_persisted_plan(
    runtime, protocol
):
    path = runtime / "config.toml"
    path.write_text("[optimizer.accuracy_budget]\nCacheHit=0.01\n")
    restart(runtime)
    call, builder = seeded_call(runtime, protocol)
    for _ in range(20):
        plan = plan_call(call)
        assert plan.kind == "pass_through" and plan.counterfactual.kind == "cached"
        reference = response(call["model"])
        assert observe_outcome(
            plan,
            builder(
                reference,
                elapsed_s=0.1,
                model=reference.model,
                call_site_id=call["call_site_id"],
                plan=plan,
            ),
        )

        def extract(candidate, cached, elapsed):
            return builder(
                cached,
                elapsed_s=elapsed,
                model=cached.model,
                call_site_id=call["call_site_id"],
                plan=candidate,
            )

        worker = maybe_explore_record(
            plan,
            reference,
            lambda _: pytest.fail("cache probe dispatched provider"),
            extract,
        )
        assert worker is not None
        worker.join(5)
        assert not worker.is_alive()
    restart(runtime)
    admitted = plan_call(call)
    assert admitted.kind == "cached"
    key = json.loads(admitted.raw_json)["agentc_observation_context"]["key"]
    path.write_text("[optimizer.accuracy_budget]\nCacheHit=0.02\n")
    restart(runtime)
    changed = plan_call(call)
    assert changed.kind == "pass_through"
    assert (
        json.loads(changed.raw_json)["agentc_observation_context"]["key"][
            "call_site_version"
        ]
        == key["call_site_version"]
    )
    path.write_text("[optimizer.accuracy_budget]\nCacheHit=0.01\nOutputBudget=0.09\n")
    restart(runtime)
    assert plan_call(call).kind == "cached"
