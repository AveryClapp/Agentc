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


def enrollment(
    catalog, endpoints, native_sha256, *, max_calls_per_site=20, max_output_tokens=512
):
    """Local preimage, not a self-asserted adapter label or capacity certificate."""
    if (
        type(max_calls_per_site) is not int
        or max_calls_per_site not in {20, 60}
        or type(max_output_tokens) is not int
        or max_output_tokens not in {256, 512}
    ):
        raise pilot.PilotError("unsupported frozen exploration allowance/output cap")
    experiment_policy = {
        "kind": "protected-pair-experiment-policy-v1",
        "max_calls_per_site": max_calls_per_site,
        "max_output_tokens": max_output_tokens,
    }
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
                "numeric_policy": "OptimizerConfig::default+frozen-exploration-allowance-v1",
                "experiment_policy": experiment_policy,
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
        _verify_request(binding, catalog, callback_id, reply, command, events)
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


def _verify_request(binding, catalog, callback_id, reply, command, events, replay=None):
    for prefix in (reply["ledger_prefix"], reply["previous_prefix"]):
        _check_prefix(prefix, events, replay)
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


def _check_prefix(prefix, events, replay=None):
    if prefix is not None and (
        not isinstance(prefix, dict)
        or set(prefix) != {"count", "sha256"}
        or type(prefix.get("count")) is not int
        or not 0 < prefix["count"] <= len(events)
        or (
            replay.prefix_digest(events, prefix["count"])
            if replay is not None
            else pilot.digest(events[: prefix["count"]])
        )
        != prefix.get("sha256")
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


def _validate_contract(bridge, frozen, binding):
    bridge.validate()
    actual = enrollment(
        frozen["catalog"],
        frozen["endpoints"],
        bridge.sha256,
        max_calls_per_site=frozen["experiment_policy"]["max_calls_per_site"],
        max_output_tokens=frozen["experiment_policy"]["max_output_tokens"],
    )
    if pilot.canonical(actual) != pilot.canonical(frozen):
        raise pilot.PilotError("protected typed/native source contract changed")
    if (
        binding["kind"] != KIND
        or binding["store"] != str(bridge.store)
        or binding["enrollment_sha256"] != pilot.digest(frozen)
        or pilot.canonical(binding["experiment_policy"])
        != pilot.canonical(frozen["experiment_policy"])
    ):
        raise pilot.PilotError("audit binding differs from frozen native enrollment")


def audit_completed(bridge, frozen, binding, rows, events):
    """Verify completed source text using only readonly native callbacks."""
    _validate_contract(bridge, frozen, binding)
    pairs = _native_state(bridge, frozen, binding, events)
    if not rows or pilot.canonical(rows) != pilot.canonical(
        pairs.get(rows[0]["pair_id"])
    ):
        raise pilot.PilotError("audited pair differs from original journal")
    return _audit_completed(bridge, frozen, binding, rows, events)


def audit_native_state(bridge, frozen, binding, events):
    """Verify available frozen attempt records; missing receipts stay incomplete."""
    _validate_contract(bridge, frozen, binding)
    return _audit_native_state(bridge, frozen, binding, events)


def _audit_native_state(bridge, frozen, binding, events, replay=None):
    pairs = _native_state(bridge, frozen, binding, events, replay)
    state = replay.state if replay is not None else accounting(events)
    positions = {id(event): index for index, event in enumerate(events)}
    for rows in pairs.values():
        start = rows[0]
        if start["binding"]["store"] != binding["store"]:
            continue
        if pilot.canonical(start["binding"]) != pilot.canonical(binding):
            raise pilot.PilotError("audited pair changed its native cohort")
        planned = next((row for row in rows if row["action"] == "planned"), None)
        if planned is None:
            continue
        bridge.verify(
            binding,
            frozen["catalog"],
            start["pair_id"] + "/plan",
            planned["reply"],
            {"op": "plan", "call": start["source_call"], "now_us": start["now_us"]},
            events,
        )
        specs = _specs(bridge, frozen, start, planned["reply"]["result"])
        group = next(
            (
                event
                for event in events
                if event["event"] == "budget_group"
                and event["id"] == start["pair_id"] + "/group"
            ),
            None,
        )
        if group is not None:
            if (
                set(group["slots"]) != set(specs)
                or group["stage"] != binding["budget_stage"]
                or group["stage_cap_usd"] != binding["stage_cap_usd"]
            ):
                raise pilot.PilotError("audited group differs from frozen pair")
            for slot, spec in specs.items():
                expected = {
                    "id": spec["id"],
                    "request": spec["payload"],
                    "metadata": spec["metadata"],
                }
                if pilot.canonical(
                    {key: group["slots"][slot][key] for key in expected}
                ) != pilot.canonical(expected):
                    raise pilot.PilotError(
                        "audited group slot changed its exact request"
                    )
        for slot, spec in specs.items():
            claims = [
                claim
                for claim in state["reserves"].values()
                if claim["id"] == spec["id"]
            ]
            if claims:
                expected = {
                    "id": spec["id"],
                    "request": spec["payload"],
                    "metadata": spec["metadata"],
                    "stage": binding["budget_stage"],
                }
                if len(claims) != 1 or pilot.canonical(
                    {key: claims[0][key] for key in expected}
                ) != pilot.canonical(expected):
                    raise pilot.PilotError("audited attempt changed its exact request")
                if slot == "candidate":
                    observed = next(
                        (row for row in rows if row["action"] == "observed"), None
                    )
                    if (
                        observed is None
                        or positions[id(observed)] >= positions[id(claims[0])]
                    ):
                        raise pilot.PilotError(
                            "audited probe preceded durable source observation"
                        )
            if spec["id"] in state["results"]:
                _receipt(spec, events, replay)
    return pairs


def source_call(task, workflow_stage, history, frozen, run_id):
    """Pure source rendering for execution and key-free exact replay."""
    public = typed._public(task)
    attention = load_module(
        "protected_pair_attention", ROOT / "python/agentc/_attention.py"
    )
    call = restrict_call(
        typed.call(
            public,
            workflow_stage,
            history,
            attention,
            model=frozen["source_model"],
            run_identity=run_id,
        ),
        workflow_stage,
    )
    call["parameters"]["max_output_tokens"] = frozen["experiment_policy"][
        "max_output_tokens"
    ]
    call["parameters"]["extra"]["agentc_route_context"]["protected_attempt"] = {
        "policy": "opaque-provider-attempt-v1",
        "mode": groups.POLICY,
        "catalog_version": frozen["catalog"]["catalog_version"],
        "endpoint_cohort_sha256": pilot.digest(frozen["endpoints"]),
        "application_contract_sha256": frozen["typed_contract_sha256"],
        "rule_contract_sha256": pilot.digest(
            {
                "rules": RULES,
                "sources": frozen["source_files"],
                "experiment_policy": frozen["experiment_policy"],
            }
        ),
        "max_request_json_bytes": 65536,
    }
    return call


def _native_state(bridge, frozen, binding, events, replay=None):
    if any(event.get("key_id") != binding["key_id"] for event in events):
        raise pilot.PilotError("audit journal belongs to another key")
    if any(
        event["event"] == "budget_group"
        and event["ledger_binding"] != binding["ledger"]
        for event in events
    ):
        raise pilot.PilotError("audit group belongs to another original ledger")
    if replay is None:
        accounting(events)
    pairs = _pairs(events)
    checkpoint = bridge.call(binding, frozen["catalog"], {"op": "checkpoint"})["result"]
    _check_prefix(checkpoint, events, replay)
    # Both stores must agree: a valid older prefix alone cannot authorize
    # resetting source/probe counts or guards through native-store rollback.
    # Ahead-of-journal native effects are ambiguous, never recoverable here.
    latest = next(
        (
            row
            for row in reversed(events)
            if row["event"] == EVENT
            and row.get("reply") is not None
            and row["binding"]["store"] == str(bridge.store)
        ),
        None,
    )
    expected = latest["reply"]["ledger_prefix"] if latest else None
    if pilot.canonical(checkpoint) != pilot.canonical(expected):
        raise pilot.PilotError(
            "native state differs from latest journal acknowledgement"
        )
    if latest:
        if pilot.canonical(latest["binding"]) != pilot.canonical(binding):
            raise pilot.PilotError(
                "journal acknowledgement belongs to a different native cohort"
            )
        operation = {
            "planned": "plan",
            "observed": "observe",
            "feedback": "complete",
            "failed": "fail",
        }[latest["action"]]
        bridge._verify_ack(
            binding,
            frozen["catalog"],
            latest["pair_id"] + "/" + operation,
            latest["reply"],
        )
    return pairs


def _receipt(spec, events, replay=None):
    state = replay.state if replay is not None else accounting(events)
    result = state["results"].get(spec["id"])
    if result is None:
        raise pilot.PilotError("protected stage lacks durable successful receipt")
    if replay is not None:
        claims = replay.claims.get(spec["id"], [])
        bodies = replay.bodies.get(spec["id"], [])
    else:
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
        raise pilot.PilotError("protected receipt differs from exact frozen attempt")
    groups.validate_completion(claims[0], bodies[0], result)
    return result


def _specs(bridge, frozen, start, plan):
    call = start["source_call"]
    cap = call["parameters"]["max_output_tokens"]
    if type(cap) is not int or cap != frozen["experiment_policy"]["max_output_tokens"]:
        raise pilot.PilotError("source cap differs from frozen experiment policy")
    candidate = _candidate(call, plan)
    context = {
        "kind": KIND,
        "start_sha256": pilot.digest(start),
        "plan_sha256": pilot.digest(plan),
        "enrollment_sha256": pilot.digest(frozen),
        "native_store": str(bridge.store),
    }
    result = {}
    for slot, selected in (
        ("source", call),
        ("candidate", candidate["call"] if candidate else None),
    ):
        if selected is None:
            continue
        payload = live.payload_for(selected, frozen)
        endpoint = frozen["endpoints"][selected["model"]]
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


def _audit_completed(bridge, frozen, binding, rows, events, replay=None):
    if rows[-1]["action"] != "complete" or pilot.canonical(
        rows[0]["binding"]
    ) != pilot.canonical(binding):
        raise pilot.PilotError("interrupted protected stage cannot resume")
    start, plan = rows[0], rows[1]["reply"]["result"]
    specs = _specs(bridge, frozen, start, plan)
    source = _receipt(specs["source"], events, replay)
    actions = {row["action"]: row for row in rows}
    _check_prefix(
        bridge.call(binding, frozen["catalog"], {"op": "checkpoint"})["result"],
        events,
        replay,
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
        candidate = _receipt(specs["candidate"], events, replay)
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
        bridge.verify(
            binding,
            frozen["catalog"],
            start["pair_id"] + "/" + command["op"],
            actions[action]["reply"],
            command,
            events,
        )
    value = typed.application_text(
        source["answer"], source["finish_reason"], stage=start["workflow_stage"]
    )
    if rows[-1]["application_text"] != value:
        raise pilot.PilotError("application history differs from exact source receipt")
    return value


def _immutable(*args, **kwargs):
    raise pilot.PilotError("read-only replay snapshot cannot be mutated")


class _FrozenDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable
    __ior__ = _immutable

    def __deepcopy__(self, memo):
        # Existing proposal checks deliberately transform their private copies.
        return {key: deepcopy(value, memo) for key, value in self.items()}


class _FrozenList(list):
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = _immutable
    remove = reverse = sort = __iadd__ = __imul__ = _immutable

    def __deepcopy__(self, memo):
        return [deepcopy(value, memo) for value in self]


def _freeze(value):
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    return value


class _ReplaySnapshot:
    """Single invocation, private ownership; never a caller-supplied cache."""

    def __init__(self, frozen, binding, events):
        if not isinstance(events, list):
            raise pilot.PilotError("read-only replay requires an ordered journal array")
        raw = pilot.canonical([frozen, binding, events])
        self.input_sha256 = hashlib.sha256(raw).hexdigest()
        self.frozen, self.binding, self.events = _freeze(json.loads(raw))
        self.state = accounting(self.events)
        self.claims, self.bodies = {}, {}
        for row in self.state["reserves"].values():
            self.claims.setdefault(row["id"], []).append(row)
        # One streaming canonical array preimage; hash copies add only the ']'.
        # This is exactly digest(events[:count]), not a different chain format.
        self.prefixes = {}
        running = hashlib.sha256(b"[")
        for index, row in enumerate(self.events):
            if index:
                running.update(b",")
            running.update(pilot.canonical(row))
            prefix = running.copy()
            prefix.update(b"]")
            self.prefixes[index + 1] = prefix.hexdigest()
            if row["event"] == "response":
                self.bodies.setdefault(row.get("id"), []).append(row["response"])

    def prefix_digest(self, events, count):
        if events is not self.events:
            raise pilot.PilotError("replay prefix belongs to another snapshot")
        return self.prefixes[count]

    def unchanged(self, frozen, binding, events):
        if pilot.digest([frozen, binding, events]) != self.input_sha256:
            raise pilot.PilotError("caller input changed during read-only replay")


class _ReplayBridge(NativeBridge):
    """No saved native reply is trusted; checkpoint/ack RPCs remain live."""

    def __init__(self, bridge, replay):
        self._bridge, self._replay = bridge, replay
        self.binary, self.store, self.sha256 = (
            bridge.binary,
            bridge.store,
            bridge.sha256,
        )

    def validate(self):
        if (self.binary, self.store, self.sha256) != (
            self._bridge.binary,
            self._bridge.store,
            self._bridge.sha256,
        ):
            raise pilot.PilotError("native replay dependency identity changed")
        self._bridge.validate()

    def call(self, binding, catalog, command, *args, **kwargs):
        if command["op"] not in {"checkpoint", "verify"}:
            raise pilot.PilotError("replay cannot execute a mutating native callback")
        self.validate()
        return self._bridge.call(binding, catalog, command, *args, **kwargs)

    def verify(self, binding, catalog, callback_id, reply, command, events):
        _verify_request(
            binding, catalog, callback_id, reply, command, events, self._replay
        )
        self._verify_ack(binding, catalog, callback_id, reply)


def audit_completed_many(bridge, frozen, binding, events):
    """Opt-in batch equivalent to state audit plus every completed pair replay.

    Returns pair_id -> exact source application text for this native store.
    Any incomplete/failed pair refuses the entire result. No saved audit flags,
    labels, provider calls, native effects or caller-supplied trusted cache.
    Inputs are privately snapshotted and checked again before return; native
    checkpoints, exact reconstructed commands and every ack are still verified.

    This does NOT replace the study's schedule/own-history/historical-budget
    audit. It is a successor-source interface: enrollment validation remains
    exact, so this changed module cannot replay an older frozen source contract.
    Legacy audit/execution entry points never select this path automatically.
    """
    replay = _ReplaySnapshot(frozen, binding, events)
    readonly = _ReplayBridge(bridge, replay)
    _validate_contract(readonly, replay.frozen, replay.binding)
    pairs = _audit_native_state(
        readonly, replay.frozen, replay.binding, replay.events, replay
    )
    result = {}
    for pair_id, rows in pairs.items():
        if rows[0]["binding"]["store"] != replay.binding["store"]:
            continue
        result[pair_id] = _audit_completed(
            readonly, replay.frozen, replay.binding, rows, replay.events, replay
        )
    # A fresh reciprocal checkpoint/ack check rejects native state changing
    # during the batch, including rollback or an effect without a journal ack.
    _native_state(readonly, replay.frozen, replay.binding, replay.events, replay)
    _validate_contract(readonly, replay.frozen, replay.binding)
    replay.unchanged(frozen, binding, events)
    return result


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
            "experiment_policy": self.frozen["experiment_policy"],
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
        _validate_contract(self.bridge, self.frozen, self.binding)

    def _read(self):
        with self.ledger.locked() as handle:
            events = pilot.Ledger.read(self.ledger, handle)
        return events, _native_state(self.bridge, self.frozen, self.binding, events)

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
        return _receipt(spec, events)

    def _specs(self, start, plan):
        return _specs(self.bridge, self.frozen, start, plan)

    def _completed(self, rows, events):
        return _audit_completed(self.bridge, self.frozen, self.binding, rows, events)

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
        call = source_call(public, workflow_stage, history, self.frozen, self.run_id)
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
