"""Source-visible native exploration bridge; no CLI, retries, or paid protocol.

The original ledger owns every allowance and every cross-store callback claim.
Only fully completed stages replay. An interrupted claim can lose evidence but
never authorizes another provider dispatch or native observation. No releases.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from bench import openrouter_groups as groups
from bench import openrouter_pilot as pilot
from bench import openrouter_rules_live as live
from bench import typed_relational_workload as typed
from bench.openrouter_attempts import accounting
from bench.openrouter_joint_study import restrict_call
from bench.openrouter_matrix import ROOT, file_hash, load_module
from bench.openrouter_typed_factorial import sources as native_sources

KIND = "protected-native-source-first-v1"
BRIDGE_SOURCE = "crates/agentc-optimizer/examples/protected_pair_bridge.rs"
RULES = ["ContextCompress", "StateDrop", "ModelDowngrade"]
EVENT = "protected_pair"


def source_files():
    return {
        **native_sources(),
        **{
            name: file_hash(ROOT / name)
            for name in (
                "bench/openrouter_protected_pair.py",
                BRIDGE_SOURCE,
            )
        },
    }


def enrollment(catalog, endpoints, native_sha256):
    """Local preimage, not a self-asserted adapter label or capacity certificate."""
    if set(endpoints) != {live.SOURCE_MODEL, live.TARGET_MODEL}:
        raise pilot.PilotError(
            "protected experiment needs exact source/target endpoints"
        )
    for model, endpoint in endpoints.items():
        pilot.validate_schema_endpoint(model, endpoint)
    return json.loads(
        pilot.canonical(
            {
                "kind": KIND,
                "workload": typed.WORKLOAD,
                "typed_contract_sha256": typed.contract_hash(),
                "feedback_contract": typed.FEEDBACK_CONTRACT,
                "prompts": typed.prompts(),
                "schemas": {stage: typed.schema_for(stage) for stage in typed.STAGES},
                "catalog": catalog,
                "endpoints": endpoints,
                "native_sha256": native_sha256,
                "source_files": source_files(),
                "rules": RULES,
                "numeric_policy": "OptimizerConfig::default",
                "source_model": live.SOURCE_MODEL,
            }
        )
    )


class NativeBridge:
    def __init__(self, binary, store, expected_sha256):
        self.binary, self.store = Path(binary).resolve(), Path(store).resolve()
        self.sha256 = expected_sha256
        self.validate()

    def validate(self):
        if file_hash(self.binary) != self.sha256:
            raise pilot.PilotError("native bridge differs from frozen executable")

    def call(
        self,
        binding,
        catalog,
        command,
        callback_id=None,
        ledger_prefix=None,
        previous_prefix=None,
    ):
        self.validate()
        request = pilot.canonical(
            {
                "binding": binding,
                "catalog": catalog,
                "command": command,
                "callback_id": callback_id,
                "ledger_prefix": ledger_prefix,
                "previous_prefix": previous_prefix,
            }
        )
        if command["op"] not in {"verify", "inspect", "checkpoint"}:
            self.store.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.run(
            [str(self.binary), str(self.store)],
            input=request,
            capture_output=True,
            timeout=30,
            check=False,
            env={"PATH": os.defpath, "LC_ALL": "C"},
        )
        if process.returncode or len(process.stdout) > 2_000_000:
            raise pilot.PilotError(
                "native callback failed; its claim cannot be retried"
            )
        reply = json.loads(process.stdout)
        if (
            set(reply) != {"request_sha256", "result"}
            or reply["request_sha256"] != hashlib.sha256(request).hexdigest()
        ):
            raise pilot.PilotError("native acknowledgement has the wrong exact request")
        reply["wire_json"] = process.stdout.strip().decode("utf-8")
        reply["ledger_prefix"], reply["previous_prefix"] = (
            ledger_prefix,
            previous_prefix,
        )
        return reply

    def verify(self, binding, catalog, callback_id, reply, command, events):
        for prefix in (reply["ledger_prefix"], reply["previous_prefix"]):
            _check_prefix(prefix, events)
        expected = {
            "binding": binding,
            "catalog": catalog,
            "callback_id": callback_id,
            "command": command,
            "ledger_prefix": reply["ledger_prefix"],
            "previous_prefix": reply["previous_prefix"],
        }
        if pilot.digest(expected) != reply["request_sha256"]:
            raise pilot.PilotError(
                "native callback differs from reconstructed source/receipt command"
            )
        self._verify_ack(binding, catalog, callback_id, reply)

    def _verify_ack(self, binding, catalog, callback_id, reply):
        wire = reply["wire_json"]
        if pilot.canonical(json.loads(wire)) != pilot.canonical(
            {k: reply[k] for k in ("request_sha256", "result")}
        ):
            raise pilot.PilotError(
                "native reply changed from its exact wire acknowledgement"
            )
        self.call(
            binding,
            catalog,
            {
                "op": "verify",
                "callback_id": callback_id,
                "request_sha256": reply["request_sha256"],
                "reply_sha256": hashlib.sha256(wire.encode()).hexdigest(),
            },
        )


def _check_prefix(prefix, events):
    if prefix is not None and (
        not isinstance(prefix, dict)
        or set(prefix) != {"count", "sha256"}
        or type(prefix.get("count")) is not int
        or not 0 < prefix["count"] <= len(events)
        or pilot.digest(events[: prefix["count"]]) != prefix.get("sha256")
    ):
        raise pilot.PilotError(
            "original journal was truncated, replaced, or changed below native checkpoint"
        )


def _pairs(events):
    """Validate only coordinator transitions; money remains in accounting()."""
    result = {}
    successors = {
        "start": {"planned"},
        "planned": {"observe_claim", "failure_claim", "failed"},
        "observe_claim": {"observed"},
        "observed": {"feedback_claim", "complete", "failure_claim", "failed"},
        "feedback_claim": {"feedback"},
        "feedback": {"complete"},
        "failure_claim": {"failed"},
        "failed": set(),
        "complete": set(),
    }
    for event in events:
        if event["event"] != EVENT:
            continue
        pair_id, action = event.get("pair_id"), event.get("action")
        if not isinstance(pair_id, str) or not pair_id or action not in successors:
            raise pilot.PilotError("invalid protected coordinator event")
        rows = result.setdefault(pair_id, [])
        if (not rows and action != "start") or (
            rows and action not in successors[rows[-1]["action"]]
        ):
            raise pilot.PilotError("protected callback already claimed or out of order")
        if event.get("previous_sha256") != (pilot.digest(rows[-1]) if rows else None):
            raise pilot.PilotError("protected callback journal prefix changed")
        if rows and pilot.canonical(event.get("binding")) != pilot.canonical(
            rows[0].get("binding")
        ):
            raise pilot.PilotError("protected callback changed its store/ledger cohort")
        result[pair_id].append(event)
    return result


def _primary(plan):
    return {k: v for k, v in plan.items() if k != "agentc_exploration_context"}


def _candidate(call, plan):
    """Validate the actual issued single-call plan; never synthesize a rewrite."""
    if (
        plan.get("kind") != "pass_through"
        or "call" in plan
        or "agentc_observation_context" not in plan
    ):
        raise pilot.PilotError("protected native primary must be exact source")
    exploration = plan.get("agentc_exploration_context")
    if exploration is None:
        return None
    candidate = exploration["candidate_plan"]
    rules = (
        [candidate["rule"]]
        if candidate["kind"] == "rewritten"
        else [r["rule"] for r in candidate["rules"]]
        if candidate["kind"] == "composed"
        else []
    )
    selected = candidate.get("call")
    if (
        not rules
        or set(rules) - set(RULES)
        or not isinstance(exploration.get("lease_token"), str)
        or not selected
    ):
        raise pilot.PilotError(
            "native candidate is outside the protected rule contract"
        )
    expected, actual = deepcopy(call), deepcopy(selected)
    routed = actual["parameters"]["extra"].pop("agentc_routed_target", None)
    actual["model"] = expected["model"]
    actual["messages"] = expected["messages"]
    if pilot.canonical(actual) != pilot.canonical(expected):
        raise pilot.PilotError(
            "native candidate changed a non-message request contract"
        )
    remaining = iter(call["messages"])
    if not selected["messages"] or any(
        not any(
            pilot.canonical(m) == pilot.canonical(original) for original in remaining
        )
        for m in selected["messages"]
    ):
        raise pilot.PilotError("native candidate is not an exact ordered deletion")
    changed_model = selected["model"] != call["model"]
    if changed_model != ("ModelDowngrade" in rules) or (
        changed_model and routed is None
    ):
        raise pilot.PilotError("native routing attribution differs from actual call")
    return candidate


def _outcome(receipt, call):
    try:
        json.loads(receipt["answer"])
        structured = True
    except ValueError:
        structured = False
    return {
        "call_site_id": call["call_site_id"],
        "executed_model_id": receipt["model"],
        "input_tokens": receipt["usage"]["prompt_tokens"],
        "output_tokens": receipt["usage"]["completion_tokens"],
        "cost_usd": float(pilot.money(receipt["cost_usd"])),
        "latency_ms": receipt["latency_ms"],
        "dispatch_fallback": False,
        "output_is_structured": structured,
        "output_is_short": receipt["usage"]["completion_tokens"] <= 128,
    }


class Coordinator:
    """Execute one typed stage; history comes only from this run's source rows."""

    def __init__(self, ledger, key, bridge, frozen, *, run_id, budget_stage, stage_cap):
        self.ledger, self.key, self.bridge = ledger, key, bridge
        self.frozen = json.loads(pilot.canonical(frozen))
        self.run_id, self.budget_stage = run_id, budget_stage
        self.stage_cap = pilot.money(stage_cap)
        self.attention = load_module(
            "protected_pair_attention", ROOT / "python/agentc/_attention.py"
        )
        self.binding = {
            "kind": KIND,
            "ledger": groups.ledger_binding(ledger),
            "key_id": ledger.key_id,
            "store": str(bridge.store),
            "enrollment_sha256": pilot.digest(self.frozen),
            "run_id": run_id,
            "budget_stage": budget_stage,
            "stage_cap_usd": str(self.stage_cap),
        }
        self._validate()

    def _validate(self):
        self.bridge.validate()
        if hashlib.sha256(self.key.encode()).hexdigest() != self.ledger.key_id:
            raise pilot.PilotError("protected key differs from ledger key")
        if (
            not self.run_id
            or not self.budget_stage
            or not 0 < self.stage_cap <= pilot.HARD_CAP
        ):
            raise pilot.PilotError("invalid protected run identity or ceiling")
        actual = enrollment(
            self.frozen["catalog"], self.frozen["endpoints"], self.bridge.sha256
        )
        if pilot.canonical(actual) != pilot.canonical(self.frozen):
            raise pilot.PilotError("protected typed/native source contract changed")

    def _read(self):
        with self.ledger.locked() as handle:
            events = pilot.Ledger.read(self.ledger, handle)
        accounting(events)
        pairs = _pairs(events)
        checkpoint = self.bridge.call(
            self.binding, self.frozen["catalog"], {"op": "checkpoint"}
        )["result"]
        _check_prefix(checkpoint, events)
        # Both stores must agree: a valid older prefix alone cannot authorize
        # resetting source/probe counts or guards through native-store rollback.
        # Ahead-of-journal native effects are ambiguous, never recoverable here.
        latest = next(
            (
                row
                for row in reversed(events)
                if row["event"] == EVENT
                and row.get("reply") is not None
                and row["binding"]["store"] == str(self.bridge.store)
            ),
            None,
        )
        expected = latest["reply"]["ledger_prefix"] if latest else None
        if pilot.canonical(checkpoint) != pilot.canonical(expected):
            raise pilot.PilotError(
                "native state differs from latest journal acknowledgement"
            )
        if latest:
            if pilot.canonical(latest["binding"]) != pilot.canonical(self.binding):
                raise pilot.PilotError(
                    "journal acknowledgement belongs to a different native cohort"
                )
            operation = {
                "planned": "plan",
                "observed": "observe",
                "feedback": "complete",
                "failed": "fail",
            }[latest["action"]]
            self.bridge._verify_ack(
                self.binding,
                self.frozen["catalog"],
                latest["pair_id"] + "/" + operation,
                latest["reply"],
            )
        return events, pairs

    def _append(self, pair_id, action, **data):
        with self.ledger.locked() as handle:
            events = pilot.Ledger.read(self.ledger, handle)
            rows = _pairs(events).get(pair_id, [])
            if action == "start":
                state = accounting(events)
                if (
                    any(r[-1]["action"] != "complete" for r in _pairs(events).values())
                    or state["pending"]
                    or state["failed_calls"]
                    or state["unsafe_ids"]
                ):
                    raise pilot.PilotError(
                        "unfinished attempt or callback blocks protected work"
                    )
            event = {
                "event": EVENT,
                "pair_id": pair_id,
                "action": action,
                "binding": self.binding,
                "previous_sha256": pilot.digest(rows[-1]) if rows else None,
                **data,
            }
            _pairs(events + [event])
            return self.ledger.append(handle, event)

    def _native(self, pair_id, command):
        previous = self.bridge.call(
            self.binding, self.frozen["catalog"], {"op": "checkpoint"}
        )["result"]
        events = self._read()[0]
        _check_prefix(previous, events)
        prefix = {"count": len(events), "sha256": pilot.digest(events)}
        return self.bridge.call(
            self.binding,
            self.frozen["catalog"],
            command,
            pair_id + "/" + command["op"],
            prefix,
            previous,
        )

    def _receipt(self, spec, events):
        state = accounting(events)
        result = state["results"].get(spec["id"])
        if result is None:
            raise pilot.PilotError("protected stage lacks durable successful receipt")
        claims = [r for r in state["reserves"].values() if r["id"] == spec["id"]]
        bodies = [
            e["response"]
            for e in events
            if e["event"] == "response" and e.get("id") == spec["id"]
        ]
        if (
            len(claims) != 1
            or len(bodies) != 1
            or any(
                pilot.canonical(claims[0][key]) != pilot.canonical(spec[field])
                for key, field in (("request", "payload"), ("metadata", "metadata"))
            )
        ):
            raise pilot.PilotError(
                "protected receipt differs from exact frozen attempt"
            )
        groups.validate_completion(claims[0], bodies[0], result)
        return result

    def _specs(self, start, plan):
        call = start["source_call"]
        candidate = _candidate(call, plan)
        context = {
            "kind": KIND,
            "start_sha256": pilot.digest(start),
            "plan_sha256": pilot.digest(plan),
            "enrollment_sha256": pilot.digest(self.frozen),
            "native_store": str(self.bridge.store),
        }
        result = {}
        for slot, selected in (
            ("source", call),
            ("candidate", candidate["call"] if candidate else None),
        ):
            if selected is None:
                continue
            payload = live.payload_for(selected, self.frozen)
            endpoint = self.frozen["endpoints"][selected["model"]]
            result[slot] = {
                "id": start["pair_id"] + "/" + slot,
                "payload": payload,
                "metadata": {
                    "protected_pair": {
                        **context,
                        "slot": slot,
                        "call_sha256": pilot.digest(selected),
                    },
                    "dispatch_contract": {
                        "provider_name": endpoint["provider_name"],
                        "endpoint_model": endpoint["name"].split(" | ", 1)[1],
                    },
                },
            }
        return result

    def _completed(self, rows, events):
        if rows[-1]["action"] != "complete" or pilot.canonical(
            rows[0]["binding"]
        ) != pilot.canonical(self.binding):
            raise pilot.PilotError("interrupted protected stage cannot resume")
        start, plan = rows[0], rows[1]["reply"]["result"]
        specs = self._specs(start, plan)
        source = self._receipt(specs["source"], events)
        actions = {row["action"]: row for row in rows}
        _check_prefix(
            self.bridge.call(
                self.binding, self.frozen["catalog"], {"op": "checkpoint"}
            )["result"],
            events,
        )
        commands = {
            "planned": {
                "op": "plan",
                "call": start["source_call"],
                "now_us": start["now_us"],
            },
            "observed": {
                "op": "observe",
                "primary": _primary(plan),
                "outcome": _outcome(source, start["source_call"]),
            },
        }
        if actions["observe_claim"]["source_receipt_sha256"] != pilot.digest(source):
            raise pilot.PilotError("source observation receipt changed")
        if "candidate" in specs:
            candidate = self._receipt(specs["candidate"], events)
            claim = actions["feedback_claim"]
            divergence = typed.application_disagreement(
                start["workflow_stage"],
                source["answer"],
                candidate["answer"],
                upstream_valid=start["source_call"]["parameters"]["extra"][
                    "typed_relational_contract"
                ]["upstream_valid"],
                reference_finish_reason=source["finish_reason"],
                candidate_finish_reason=candidate["finish_reason"],
            )
            if (
                claim["candidate_receipt_sha256"] != pilot.digest(candidate)
                or claim["source_receipt_sha256"] != pilot.digest(source)
                or claim["divergence"] != divergence
            ):
                raise pilot.PilotError("paired observation receipt changed")
            commands["feedback"] = {
                "op": "complete",
                "lease_token": plan["agentc_exploration_context"]["lease_token"],
                "outcome": _outcome(
                    candidate,
                    plan["agentc_exploration_context"]["candidate_plan"]["call"],
                ),
                "divergence": divergence,
                "now_us": claim["now_us"],
            }
        for action, command in commands.items():
            self.bridge.verify(
                self.binding,
                self.frozen["catalog"],
                start["pair_id"] + "/" + command["op"],
                actions[action]["reply"],
                command,
                events,
            )
        value = typed.application_text(
            source["answer"], source["finish_reason"], stage=start["workflow_stage"]
        )
        if rows[-1]["application_text"] != value:
            raise pilot.PilotError(
                "application history differs from exact source receipt"
            )
        return value

    def execute(self, task, workflow_stage):
        self._validate()
        # The renderer strips labels; neither native state nor pair metadata gets gold.
        public = typed._public(task)
        pair_id = pilot.digest([KIND, self.run_id, public["task_id"], workflow_stage])
        events, pairs = self._read()
        history = {}
        for prior in typed.STAGES[: typed.STAGES.index(workflow_stage)]:
            prior_id = pilot.digest([KIND, self.run_id, public["task_id"], prior])
            if prior_id not in pairs or pairs[prior_id][0][
                "task_sha256"
            ] != pilot.digest(public):
                raise pilot.PilotError("stage requires exact own-source history")
            history[prior] = self._completed(pairs[prior_id], events)
        call = restrict_call(
            typed.call(
                public,
                workflow_stage,
                history,
                self.attention,
                model=self.frozen["source_model"],
                run_identity=self.run_id,
            ),
            workflow_stage,
        )
        call["parameters"]["extra"]["agentc_route_context"]["protected_attempt"] = {
            "policy": "opaque-provider-attempt-v1",
            "mode": groups.POLICY,
            "catalog_version": self.frozen["catalog"]["catalog_version"],
            "endpoint_cohort_sha256": pilot.digest(self.frozen["endpoints"]),
            "application_contract_sha256": self.frozen["typed_contract_sha256"],
            "rule_contract_sha256": pilot.digest(
                {"rules": RULES, "sources": self.frozen["source_files"]}
            ),
            "max_request_json_bytes": 65536,
        }
        if pair_id in pairs:
            if pilot.canonical(call) != pilot.canonical(
                pairs[pair_id][0]["source_call"]
            ):
                raise pilot.PilotError("protected replay changed its exact source")
            return self._completed(pairs[pair_id], events)
        state = accounting(events)
        if (
            any(rows[-1]["action"] != "complete" for rows in pairs.values())
            or state["pending"]
            or state["failed_calls"]
            or state["unsafe_ids"]
        ):
            raise pilot.PilotError(
                "unfinished attempt or callback blocks protected work"
            )
        start = self._append(
            pair_id,
            "start",
            source_call=call,
            workflow_stage=workflow_stage,
            task_id=public["task_id"],
            task_sha256=pilot.digest(public),
            now_us=time.time_ns() // 1000,
        )
        reply = self._native(
            pair_id, {"op": "plan", "call": call, "now_us": start["now_us"]}
        )
        plan = reply["result"]
        specs = self._specs(start, plan)
        self._append(pair_id, "planned", reply=reply)
        group = (
            groups.reserve_pair(
                self.ledger,
                self.key,
                pair_id + "/group",
                self.budget_stage,
                self.stage_cap,
                **specs,
            )
            if "candidate" in specs
            else None
        )

        def dispatch(slot):
            try:
                if group:
                    groups.call_slot(self.ledger, self.key, group, slot)
                else:
                    spec = specs[slot]
                    self.ledger.call(
                        self.key,
                        spec["id"],
                        self.budget_stage,
                        self.stage_cap,
                        spec["payload"],
                        spec["metadata"],
                    )
            except pilot.PilotError:
                if group:
                    self._append(pair_id, "failure_claim", slot=slot)
                    failed = self._native(
                        pair_id,
                        {
                            "op": "fail",
                            "lease_token": plan["agentc_exploration_context"][
                                "lease_token"
                            ],
                            "now_us": time.time_ns() // 1000,
                        },
                    )
                else:
                    failed = None
                self._append(pair_id, "failed", slot=slot, reply=failed)
                raise
            return self._receipt(specs[slot], self._read()[0])

        source = dispatch("source")
        self._append(
            pair_id, "observe_claim", source_receipt_sha256=pilot.digest(source)
        )
        observed = self._native(
            pair_id,
            {
                "op": "observe",
                "primary": _primary(plan),
                "outcome": _outcome(source, call),
            },
        )
        self._append(pair_id, "observed", reply=observed)
        if group:
            candidate = dispatch("candidate")
            divergence = typed.application_disagreement(
                workflow_stage,
                source["answer"],
                candidate["answer"],
                upstream_valid=call["parameters"]["extra"]["typed_relational_contract"][
                    "upstream_valid"
                ],
                reference_finish_reason=source["finish_reason"],
                candidate_finish_reason=candidate["finish_reason"],
            )
            claim = self._append(
                pair_id,
                "feedback_claim",
                candidate_receipt_sha256=pilot.digest(candidate),
                source_receipt_sha256=pilot.digest(source),
                divergence=divergence,
                now_us=time.time_ns() // 1000,
            )
            feedback = self._native(
                pair_id,
                {
                    "op": "complete",
                    "lease_token": plan["agentc_exploration_context"]["lease_token"],
                    "outcome": _outcome(
                        candidate,
                        plan["agentc_exploration_context"]["candidate_plan"]["call"],
                    ),
                    "divergence": divergence,
                    "now_us": claim["now_us"],
                },
            )
            if feedback["result"].get("newly_recorded") is not True:
                raise pilot.PilotError(
                    "native pair completion did not create its unique observation"
                )
            self._append(pair_id, "feedback", reply=feedback)
        value = typed.application_text(
            source["answer"], source["finish_reason"], stage=workflow_stage
        )
        self._append(pair_id, "complete", application_text=value)
        return value
