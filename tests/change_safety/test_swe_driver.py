"""SWE study binding: routing, forks, rollouts and accounting without Docker.

``run_task`` and ``grade_task`` are replaced by fakes that drive the binding
exactly as the frozen pipeline does: open ``task()``, let SWE-agent call
``litellm.completion`` repeatedly, then grade.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

pytest.importorskip("litellm")

import agentc  # noqa: E402
import litellm  # noqa: E402
from litellm import ModelResponse  # noqa: E402

from agentc.change_safety.records import ForkRecord, TrajectoryRecord, read_records  # noqa: E402
from bench.change_safety.run import E0_PINS, build_catalog  # noqa: E402
from bench.change_safety.spend import SpendLedger  # noqa: E402
from bench.change_safety.swe_driver import SweStudyBinding  # noqa: E402

MODEL = "mock/swe-strong"


def _response(content: str) -> ModelResponse:
    return ModelResponse(
        model=MODEL,
        choices=[{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": content,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "bash", "arguments": json.dumps({"command": content})}}]}}],
        usage={"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
    )


@pytest.fixture
def binding(tmp_path, monkeypatch, study_env):
    sent: list[dict] = []

    def provider(**request):
        sent.append(request)
        return _response(str(len(request["messages"])))

    monkeypatch.setattr(litellm, "completion", provider)
    agentc.init(storage_path=str(tmp_path / "agentc"))
    b = SweStudyBinding(
        stage="E0", model=MODEL, source=tmp_path, output=tmp_path,
        ledger=SpendLedger(tmp_path / "spend.jsonl", cap_usd=0.0),
        output_cap=256, cohort_sha256="0" * 64, execution_version="1" * 64,
        context_tokens={MODEL: 128_000}, verify=lambda source: {"closure_sha256": "x"}, embed=False,
    )
    with b:
        yield b, sent, tmp_path
    agentc.shutdown()


def _fake_run_task(*, binding, task_id, arm, cell_id, requested_model, **_):
    binding.claim_execution(cell_id)
    with binding.task(task_id=task_id, arm=arm, cell_id=cell_id, requested_model=requested_model, optimize=False):
        messages = [{"role": "system", "content": "You are a coding agent."}, {"role": "user", "content": "Fix the bug."}]
        for step in range(3):
            litellm.completion(model=requested_model, messages=list(messages), max_tokens=256)
            messages.append({"role": "assistant", "content": f"step {step}"})
            messages.append({"role": "tool", "tool_call_id": "c1", "content": "x" * 500})
    return {"upstream_exit_status": "submitted"}


def _fake_grade(**kwargs):
    calls, total, digest = kwargs["accounting"](None, None)
    assert calls == 3 and total == Decimal("0") and len(digest) == 64
    return {"protocol_score": 1.0}


def test_baseline_forks_and_rollout(binding):
    b, sent, out = binding
    catalog = build_catalog(E0_PINS, ["C0", "X2", "X3"])
    base = b.run(inputs=None, environment_report=out, bundle=None, task_id="django__django-1", arm="baseline",
                 repetition=0, catalog={k: catalog[k] for k in ("C0", "X2")}, model=MODEL, output=out / "b",
                 run_task=_fake_run_task, grade_task=_fake_grade)
    assert base.reward == 1.0 and base.eligible_calls == 3 and base.termination == "submitted"
    roll = b.run(inputs=None, environment_report=out, bundle=None, task_id="django__django-1", arm="X3",
                 repetition=0, catalog=catalog, model=MODEL, output=out / "r",
                 run_task=_fake_run_task, grade_task=_fake_grade)
    assert roll.changed_calls == 3
    # X3 removed the system prompt from every rollout call that reached the provider.
    assert all(m["role"] != "system" for req in sent[-3:] for m in req["messages"])
    records = read_records(str(out / "records.jsonl"))
    forks = [r for r in records if isinstance(r, ForkRecord)]
    assert len(forks) == 3 and {f.key.workload for f in forks} == {"swebench-verified"}
    assert [r.arm for r in records if isinstance(r, TrajectoryRecord)] == ["baseline", "X3"]
    # 3 baseline + 3 forks (each fork is a real dispatch) + 3 rollout calls.
    assert len(sent) == 9


def test_refuses_optimize_and_retry(binding):
    b, _, _ = binding
    b.prepare(repetition=0, catalog={})
    with pytest.raises(RuntimeError):
        with b.task(task_id="t", arm="baseline", cell_id="c", requested_model=MODEL, optimize=True):
            pass
    b.claim_execution("cell")
    with pytest.raises(RuntimeError):
        b.claim_execution("cell")
