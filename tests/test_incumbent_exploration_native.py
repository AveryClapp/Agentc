"""Exercise incumbent exploration through the native runtime and OpenAI adapter.

Synthetic profiles and a local fake provider test the wiring and call bounds.
They provide no workload quality or savings evidence.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agentc
from agentc import _native
from agentc._patches._openai import _wrap_create
from agentc._patches._optimizer_glue import (
    build_call_dict_openai,
    drain_exploration,
    estimate_cost_usd,
)

MODEL = "gpt-4o-2024-11-20"
TARGET = "gpt-4o-mini-2024-07-18"
SITE = "native.incumbent.exploration"


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    agentc.shutdown()
    _native.optimize_reset()
    for key, value in {
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_COMPOSE": "1",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "50",
        "AGENTC_OPTIMIZE_SHADOW": "0",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION", raising=False)
    monkeypatch.delenv("AGENTC_SHADOW_DIVERGENCE_BUDGET", raising=False)
    yield
    drain_exploration(5000)
    agentc.shutdown()
    _native.optimize_reset()


def identity(plan: dict) -> str:
    return plan["agentc_observation_context"]["key"]["execution_plan_id"]


def seed_incumbent(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict, str, str, str]:
    request = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "original user instruction"}],
        "max_tokens": 1024,
        "temperature": 0,
        "stop": ["STOP"],
        "extra_headers": {"X-Request-ID": "incumbent-test"},
    }
    call = build_call_dict_openai(
        request,
        call_site_id=SITE,
        trace_id_hex="00" * 16,
        span_id_hex="01" * 8,
    )

    def observe(plan_json: str, output_tokens: int, paired: bool = False) -> None:
        plan = json.loads(plan_json)
        model = plan.get("call", {}).get("model", MODEL)
        token = _native.optimize_observe(
            plan_json,
            json.dumps(
                {
                    "call_site_id": SITE,
                    "model": model,
                    "input_tokens": 100,
                    "output_tokens": output_tokens,
                    "latency_ms": 100.0,
                    "cost_usd": estimate_cost_usd(model, 100, output_tokens),
                    "cost_source": "provider_reported",
                    "output_is_short": True,
                }
            ),
        )
        assert token
        if paired:
            _native.optimize_record_divergence(token, 0.0)

    # Use the explicit greedy fixture mode to obtain native-issued executable
    # plans. Only production observe/shadow endpoints populate evidence; no
    # database rows, clocks, admission floors or exploration caps are altered.
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "current_greedy")
    monkeypatch.setenv("AGENTC_ENABLED_RULES", "OutputBudget")
    _native.optimize_configure(str(storage))
    reference = None
    for _ in range(3):
        plan_json = _native.optimize_plan(json.dumps(call))
        reference = json.loads(plan_json)
        assert reference["kind"] == "pass_through"
        observe(plan_json, 100)
    incumbent_json = _native.optimize_plan(json.dumps(call))
    incumbent = json.loads(incumbent_json)
    assert incumbent["kind"] == "rewritten"
    assert incumbent["rule"] == "OutputBudget"
    for _ in range(20):
        observe(incumbent_json, 40, paired=True)

    monkeypatch.setenv("AGENTC_ENABLED_RULES", "OutputBudget,ModelDowngrade")
    _native.optimize_configure(str(storage))
    # The greedy route's probation sampler may abstain. Planning these trials
    # makes no provider calls and changes no evidence or spend counters.
    for _ in range(1000):
        joint_json = _native.optimize_plan(json.dumps(call))
        joint = json.loads(joint_json)
        if joint["kind"] == "composed" and joint["call"]["model"] == TARGET:
            break
    else:
        pytest.fail("native greedy planner never supplied a joint fixture plan")
    for _ in range(19):
        observe(joint_json, 40, paired=True)

    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "joint_guarded")
    _native.optimize_configure(str(storage))
    selected = json.loads(_native.optimize_plan(json.dumps(call)))
    assert identity(selected) == identity(incumbent)
    assert selected["kind"] == "rewritten"
    _native.optimize_reset()
    assert reference is not None
    return request, identity(reference), identity(incumbent), identity(joint)


class FakeProvider:
    def __init__(
        self,
        *,
        fail_candidate: bool = False,
        fail_reference: bool = False,
        candidate_started: threading.Event | None = None,
        candidate_release: threading.Event | None = None,
    ) -> None:
        self.fail_candidate = fail_candidate
        self.fail_reference = fail_reference
        self.candidate_started = candidate_started
        self.candidate_release = candidate_release
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, **kwargs: object) -> SimpleNamespace:
        model = kwargs["model"]
        cap = kwargs["max_tokens"]
        origin = "reference" if model == MODEL and cap == 1024 else "candidate"
        with self.lock:
            self.calls.append(
                dict(
                    kwargs=copy.deepcopy(kwargs), thread=threading.current_thread().name
                )
            )
        if self.fail_reference and origin == "reference":
            raise RuntimeError("synthetic source provider failure")
        if model == TARGET and self.candidate_release is not None:
            assert self.candidate_started is not None
            self.candidate_started.set()
            assert self.candidate_release.wait(5), "test did not release the candidate"
        if self.fail_candidate and model == TARGET:
            raise RuntimeError("synthetic counterfactual provider failure")
        return SimpleNamespace(
            id="fake-response",
            model=model,
            origin=origin,
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=100 if origin == "reference" else 40,
                cost=estimate_cost_usd(
                    model, 100, 100 if origin == "reference" else 40
                ),
                is_byok=False,
            ),
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(role="assistant", content="stable answer"),
                )
            ],
        )


@pytest.mark.parametrize("fail_candidate", [False, True])
def test_incumbent_probe_uses_original_request_and_one_candidate_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_candidate: bool
) -> None:
    storage = (tmp_path / ".agentc").resolve()
    request, reference_id, incumbent_id, joint_id = seed_incumbent(storage, monkeypatch)
    original = copy.deepcopy(request)
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "1")
    provider = FakeProvider(fail_candidate=fail_candidate)
    with (
        patch("agentc._lifecycle._apply_patches"),
        patch("agentc._patches._openai._write_root_span"),
        patch("agentc._patches._optimizer_glue.derive_call_site_id", return_value=SITE),
    ):
        agentc.init(storage_path=str(storage))
        try:
            result = _wrap_create(provider, None, (), request)
            drain_exploration(5000)
        finally:
            agentc.shutdown()
    assert result.origin == "reference"
    assert request == original
    assert len(provider.calls) == 2, "a probe must issue only source plus candidate"
    assert provider.calls[0]["kwargs"] == original
    assert provider.calls[1]["thread"] == "agentc-counterfactual"
    candidate_kwargs = provider.calls[1]["kwargs"]
    assert candidate_kwargs["model"] == TARGET
    assert candidate_kwargs["max_tokens"] < original["max_tokens"]
    for key in ("messages", "temperature", "stop", "extra_headers"):
        assert candidate_kwargs[key] == original[key]
    with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
        attempts = connection.execute(
            "SELECT execution_plan_id, status, COUNT(*) FROM execution_plan_exploration "
            "GROUP BY execution_plan_id, status"
        ).fetchall()
        counts = dict(
            connection.execute(
                "SELECT execution_plan_id, paired_observations FROM execution_plan_profile"
            )
        )
        source_observations = connection.execute(
            "SELECT n_observations FROM execution_plan_profile WHERE execution_plan_id = ?",
            (reference_id,),
        ).fetchone()[0]
    assert attempts == [(joint_id, "failed" if fail_candidate else "completed", 1)]
    assert counts[incumbent_id] == 20, "source feedback must not update the incumbent"
    assert counts[joint_id] == (19 if fail_candidate else 20)
    assert counts[reference_id] == 0
    assert source_observations == 4

    # A full SDK restart with exploration disabled must dispatch the newly
    # admitted joint plan, or retain the incumbent after candidate failure.
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "0")
    provider.fail_candidate = False
    with (
        patch("agentc._lifecycle._apply_patches"),
        patch("agentc._patches._openai._write_root_span"),
        patch("agentc._patches._optimizer_glue.derive_call_site_id", return_value=SITE),
    ):
        agentc.init(storage_path=str(storage))
        try:
            _wrap_create(provider, None, (), request)
        finally:
            agentc.shutdown()
    assert len(provider.calls) == 3
    assert provider.calls[-1]["kwargs"]["model"] == (
        MODEL if fail_candidate else TARGET
    )
    assert provider.calls[-1]["kwargs"]["max_tokens"] < original["max_tokens"]


@pytest.mark.parametrize("policy", ["disabled", "invalid_allocation"])
def test_disabled_exploration_preserves_the_admitted_sdk_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    storage = (tmp_path / ".agentc").resolve()
    request, _, _, joint_id = seed_incumbent(storage, monkeypatch)
    if policy == "invalid_allocation":
        monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "1")
        monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION", "invalid")
    provider = FakeProvider()
    with (
        patch("agentc._lifecycle._apply_patches"),
        patch("agentc._patches._openai._write_root_span"),
        patch("agentc._patches._optimizer_glue.derive_call_site_id", return_value=SITE),
    ):
        agentc.init(storage_path=str(storage))
        try:
            _wrap_create(provider, None, (), request)
            drain_exploration(5000)
        finally:
            agentc.shutdown()
    assert len(provider.calls) == 1
    assert provider.calls[0]["kwargs"]["model"] == MODEL
    assert provider.calls[0]["kwargs"]["max_tokens"] < request["max_tokens"]
    with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM execution_plan_exploration"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT paired_observations FROM execution_plan_profile WHERE execution_plan_id = ?",
                (joint_id,),
            ).fetchone()[0]
            == 19
        )


def test_active_lease_preserves_incumbent_on_a_concurrent_sdk_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = (tmp_path / ".agentc").resolve()
    request, _, incumbent_id, joint_id = seed_incumbent(storage, monkeypatch)
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "1")
    started, release = threading.Event(), threading.Event()
    provider = FakeProvider(candidate_started=started, candidate_release=release)
    with (
        patch("agentc._lifecycle._apply_patches"),
        patch("agentc._patches._openai._write_root_span"),
        patch("agentc._patches._optimizer_glue.derive_call_site_id", return_value=SITE),
    ):
        agentc.init(storage_path=str(storage))
        try:
            reference = _wrap_create(provider, None, (), request)
            assert reference.origin == "reference"
            assert started.wait(2), "counterfactual did not start"
            incumbent = _wrap_create(provider, None, (), request)
            assert incumbent.origin == "candidate"
        finally:
            release.set()
            drain_exploration(5000)
            agentc.shutdown()
    assert len(provider.calls) == 3
    assert provider.calls[0]["kwargs"] == request
    assert provider.calls[1]["kwargs"]["model"] == TARGET
    assert provider.calls[1]["thread"] == "agentc-counterfactual"
    assert provider.calls[2]["kwargs"]["model"] == MODEL
    assert provider.calls[2]["kwargs"]["max_tokens"] < request["max_tokens"]
    with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
        assert connection.execute(
            "SELECT execution_plan_id, status, COUNT(*) FROM execution_plan_exploration "
            "GROUP BY execution_plan_id, status"
        ).fetchall() == [(joint_id, "completed", 1)]
        assert connection.execute(
            "SELECT paired_observations, n_observations FROM execution_plan_profile "
            "WHERE execution_plan_id = ?",
            (incumbent_id,),
        ).fetchone() == (20, 21)


def test_source_failure_closes_the_lease_without_dispatching_or_retrying_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = (tmp_path / ".agentc").resolve()
    request, _, _, joint_id = seed_incumbent(storage, monkeypatch)
    monkeypatch.setenv("AGENTC_OPTIMIZE_EXPLORATION", "1")
    provider = FakeProvider(fail_reference=True)
    with (
        patch("agentc._lifecycle._apply_patches"),
        patch("agentc._patches._openai._write_root_span"),
        patch("agentc._patches._optimizer_glue.derive_call_site_id", return_value=SITE),
    ):
        agentc.init(storage_path=str(storage))
        try:
            with pytest.raises(RuntimeError, match="synthetic source provider failure"):
                _wrap_create(provider, None, (), request)
            drain_exploration(5000)
        finally:
            agentc.shutdown()
    assert len(provider.calls) == 1
    assert provider.calls[0]["kwargs"] == request
    with closing(sqlite3.connect(storage / "cost_model.db")) as connection:
        assert connection.execute(
            "SELECT execution_plan_id, status, COUNT(*) FROM execution_plan_exploration "
            "GROUP BY execution_plan_id, status"
        ).fetchall() == [(joint_id, "failed", 1)]
        assert (
            connection.execute(
                "SELECT paired_observations FROM execution_plan_profile WHERE execution_plan_id = ?",
                (joint_id,),
            ).fetchone()[0]
            == 19
        )
