"""Pinned SDK paired-window acquisition; forced-sampling E0, never efficacy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

from bench.campaign_accounting import digest, read_snapshot
from bench.campaign_chat import ChatContract
from bench.campaign_records import cell_calls
from bench.campaign_transport import CampaignTransport
from bench.frozen_policy_preflight import (
    CHEAP,
    MODES,
    STRONG,
    FrozenProvider,
    configuration,
)
from bench.guard_evidence import collect_checkpoint, file_sha
from bench.tau_campaign_binding import TauCampaignBinding
from bench.tau_campaign_preflight import FIXTURE_KEY, source_manifest
from bench.tau_task_worker import TauTaskWorker, private_write


class ExposureProvider(FrozenProvider):
    def message(self, body):
        response = super().message(body)
        if body["max_tokens"] < 1024:
            response["choices"][0]["message"]["content"] = "different candidate output"
        return response


def run(source: Path, output: Path, *, observations=20, control="paired"):
    import agentc
    from agentc._evaluation_policy import (
        FrozenEvaluationPolicy,
        evaluation_policy_scope,
    )
    from agentc._patches._optimizer_glue import drain_exploration

    if type(observations) is not int or not 1 <= observations <= 60:
        raise ValueError("engineering fixture requires 1--60 observations per policy")
    if control not in {"paired", "disable-restart"} or (
        control == "disable-restart" and observations != 2
    ):
        raise ValueError(
            "disable-restart control requires exactly two paired observations"
        )
    upstream = source_manifest(source)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    ledger = output / "fixture-budget.jsonl"
    private_write(
        ledger,
        {
            "event": "origin",
            "at": "fixture",
            "key_id": hashlib.sha256(FIXTURE_KEY.encode()).hexdigest(),
            "usage_usd": "7",
        },
    )
    configurations = {mode: configuration(mode) for mode in MODES}
    declaration = {
        "source_stage": "E0",
        "evidence_kind": "engineering_fixture",
        "configurations": configurations,
    }
    lock_sha = digest(declaration)
    runtime = TauTaskWorker._runtime_manifest()
    measurement_files = {
        name: file_sha(Path(__file__).parent / name)
        for name in (
            "guard_evidence.py",
            "guard_evidence_preflight.py",
            "frozen_policy_preflight.py",
        )
    }
    private_write(
        output / "enrollment.json",
        {
            "record_type": "guard_evidence_engineering_enrollment",
            "source": upstream,
            "runtime": runtime,
            "measurement_files": measurement_files,
            "control": control,
            "declaration": declaration,
            "calibration_lock_sha256": lock_sha,
            "sampling_control": "reset_rng_before_each_primary",
            "configured_shadow_rate": 0.02,
            "observations_per_policy": observations,
        },
    )
    provider = (
        ExposureProvider() if control == "disable-restart" else FrozenProvider()
    ).start()
    plans, executions, reports = [], [], []
    original_env = {k: v for k, v in os.environ.items() if k.startswith("AGENTC_")}
    original_rng = random.getstate()
    try:
        transport = CampaignTransport(
            ledger=ledger,
            expected_sha256=read_snapshot(ledger).sha256,
            key=FIXTURE_KEY,
            stage="local-guard-paired-evidence",
            stage_cap_usd="2",
            contracts=[
                ChatContract(m, "openai", "OpenAI", m, 16384, 1024)
                for m in (STRONG, CHEAP)
            ],
            http=provider.http,
        )
        with TauCampaignBinding(
            transport=transport,
            source=source,
            model_aliases={m: m for m in (STRONG, CHEAP)},
            actor_output_caps={actor: 1024 for actor in ("agent", "user", "judge")},
            agent_model_allowlist=frozenset({STRONG, CHEAP}),
            plan_observer=plans.append,
        ) as binding:
            from tau2.agent import llm_agent
            from tau2.data_model.message import (
                AssistantMessage,
                SystemMessage,
                UserMessage,
            )

            messages = [
                SystemMessage(role="system", content="Preserve current instructions."),
                UserMessage(role="user", content="Answer the current task."),
                AssistantMessage(
                    role="assistant", content="obsolete unrelated archive " * 100
                ),
                UserMessage(role="user", content="obsolete unrelated archive " * 100),
                AssistantMessage(role="assistant", content="Current answer context."),
                UserMessage(role="user", content="Give the current answer."),
            ]
            for mode in MODES:
                for name in tuple(os.environ):
                    if name.startswith("AGENTC_"):
                        os.environ.pop(name)
                os.environ.update(
                    AGENTC_OPTIMIZE="1",
                    AGENTC_COMPOSE="1",
                    AGENTC_EVAL_PLANNER_MODE=mode,
                    AGENTC_MODEL_CATALOG_OFFLINE="1",
                    AGENTC_OPTIMIZE_SELECTION_AUDIT="1",
                    AGENTC_OPTIMIZE_EXPLORATION="1",
                    AGENTC_OPTIMIZE_SHADOW="0.02",
                    AGENTC_OPTIMIZE_MAX_OVERHEAD_MS="50",
                )
                frozen = FrozenEvaluationPolicy.from_configuration(
                    mode=mode,
                    calibration_lock_sha256=lock_sha,
                    configuration=configurations[mode],
                )
                agentc.init(storage_path=str(output / mode))
                mode_calls = []
                try:

                    def generate_one(cell):
                        with (
                            evaluation_policy_scope(frozen),
                            binding.task(
                                task_id="synthetic-frozen-policy-history",
                                arm=mode,
                                optimize_agent=True,
                                cell_id=cell,
                            ),
                        ):
                            return llm_agent.generate(
                                model=STRONG,
                                messages=messages,
                                tools=None,
                                temperature=0,
                                max_tokens=1024,
                            )

                    for ordinal in range(observations):
                        cell = digest({"mechanism": mode, "ordinal": ordinal})
                        before = len(plans)
                        # Deliberate dense fixture sampling; never infer 2% performance.
                        random.seed(31)
                        answer = generate_one(cell)
                        assert answer.content == (
                            "different candidate output"
                            if control == "disable-restart"
                            else "stable answer"
                        )
                        drain_exploration(120000)
                        binding.assert_complete()
                        selected = plans[before:]
                        assert len(selected) == 1
                        calls = cell_calls(
                            read_snapshot(ledger),
                            session_id=transport.session_id,
                            cell_id=cell,
                            plans=selected,
                        )
                        assert [c["metadata"]["kind"] for c in calls] == [
                            "primary",
                            "shadow",
                        ]
                        mode_calls.extend(calls)
                        executions.append(
                            {
                                "mode": mode,
                                "cell_id": cell,
                                "plan": selected[0],
                                "calls": calls,
                            }
                        )
                    if control == "disable-restart":
                        agentc.shutdown()
                        agentc.init(storage_path=str(output / mode))
                        cell = digest({"mechanism": mode, "restart": True})
                        before = len(plans)
                        answer = generate_one(cell)
                        assert answer.content == "stable answer"
                        drain_exploration(120000)
                        binding.assert_complete()
                        selected = plans[before:]
                        assert len(selected) == 1
                        audit = json.loads(selected[0]["native_plan_json"])[
                            "agentc_selection_audit"
                        ]
                        assert audit["abstention_reason"] == "complete_plan_guard"
                        calls = cell_calls(
                            read_snapshot(ledger),
                            session_id=transport.session_id,
                            cell_id=cell,
                            plans=selected,
                        )
                        assert (
                            len(calls) == 1
                            and calls[0]["metadata"]["kind"] == "primary"
                        )
                        mode_calls.extend(calls)
                        executions.append(
                            {
                                "mode": mode,
                                "cell_id": cell,
                                "plan": selected[0],
                                "calls": calls,
                                "restart": True,
                            }
                        )
                finally:
                    agentc.shutdown()
                measured = collect_checkpoint(
                    output / mode / "cost_model.db",
                    mode_calls,
                    ledger=ledger,
                    python=Path(sys.executable),
                    expected_sdk_files=runtime["sdk_files"],
                    mode=mode,
                    configuration=configurations[mode],
                    calibration_lock_sha256=lock_sha,
                    session_id=transport.session_id,
                    now_us=time.time_ns() // 1000,
                )
                report = measured["audit"]
                paired = [
                    p for p in report["plans"] if p["retained_paired_observations"]
                ]
                assert len(paired) == 1 and paired[0][
                    "retained_paired_observations"
                ] == min(observations, 50)
                assert paired[0]["paired_window_qualifies"] is (
                    observations >= 20 and control == "paired"
                )
                assert (paired[0]["durable_disable"] is not None) is (
                    control == "disable-restart"
                )
                private_write(output / mode / "checkpoint.json", measured["checkpoint"])
                private_write(
                    output / mode / "comparisons.json", measured["comparison_values"]
                )
                private_write(output / mode / "audit.json", report)
                reports.append(report)
        snapshot = read_snapshot(ledger)
        assert snapshot.state["pending_usd"] == snapshot.state["holds_usd"] == 0
        assert len(provider.posts) == (
            observations * 2 + (control == "disable-restart")
        ) * len(MODES)
        assert source_manifest(source) == upstream
        assert TauTaskWorker._runtime_manifest() == runtime
        assert all(
            file_sha(Path(__file__).parent / name) == expected
            for name, expected in measurement_files.items()
        )
        private_write(output / "executions.json", executions)
        summary = {
            "schema_version": 1,
            "record_type": "guard_paired_evidence_engineering_control",
            "source_stage": "E0",
            "control": control,
            "paper_evidence": False,
            "synthetic_billing": True,
            "paid_provider_calls": 0,
            "calibration_admission": False,
            "configured_shadow_rate": 0.02,
            "observed_sampling_fraction": observations
            / (observations + (control == "disable-restart")),
            "sampling_control": "reset_rng_before_each_primary",
            "policies": len(reports),
            "retained_pairs": sum(
                p["retained_paired_observations"] for r in reports for p in r["plans"]
            ),
            "local_http_post_count": len(provider.posts),
            "known_fixture_charge_usd": str(snapshot.state["known_usd"]),
            "journal_sha256": snapshot.sha256,
            "executions_sha256": digest(executions),
            "reports_sha256": digest(reports),
            "transport_session_id": transport.session_id,
        }
        private_write(output / "summary.json", summary)
        return summary
    finally:
        provider.close()
        random.setstate(original_rng)
        for name in tuple(os.environ):
            if name.startswith("AGENTC_"):
                os.environ.pop(name)
        os.environ.update(original_env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observations", type=int, default=20)
    parser.add_argument(
        "--control", choices=("paired", "disable-restart"), default="paired"
    )
    args = parser.parse_args(argv)
    os.environ.update(PYTHON_DOTENV_DISABLED="1", LITELLM_LOCAL_MODEL_COST_MAP="True")

    def network_guard(event, arguments):
        if event == "socket.connect" and (
            not isinstance(arguments[1], tuple) or arguments[1][0] != "127.0.0.1"
        ):
            raise RuntimeError("guard evidence E0 forbids external network")

    sys.addaudithook(network_guard)
    print(
        json.dumps(
            run(
                args.source.resolve(),
                args.output.resolve(),
                observations=args.observations,
                control=args.control,
            )
        )
    )


if __name__ == "__main__":
    main()
