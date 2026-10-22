"""Join retained complete-plan safety samples to actual owned provider receipts.

This audits native paired evidence, not task damage or guard efficacy. A warm
paired window alone never issues a calibration admission. SQLite inspection and
SDK output comparison run in separate processes to avoid mixed native libraries.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

from bench.campaign_accounting import digest, money, read_snapshot
from bench.campaign_records import normalize_owned_calls
from bench.policy_calibration import configuration_sha256, policy_identity_sha256

VERSION = "complete-plan-paired-evidence-v1"
MAX_AGE_US = 24 * 60 * 60 * 1_000_000
RUNTIME_FIELDS = (
    "provider_protocol",
    "target_model_id",
    "target_model_version",
    "price_table_version",
)
TABLES = {
    "execution_plan_profile": "call_site_version, execution_plan_id",
    "execution_plan_observation": "call_site_version, execution_plan_id, sample_sequence",
    "execution_plan_divergence_observation": "call_site_version, execution_plan_id, sample_sequence",
    "execution_plan_guard": "call_site_version, execution_plan_id",
    "execution_plan_guard_observation": "call_site_version, execution_plan_id, plan_observation_sequence",
    "execution_plan_disabled": "call_site_version, execution_plan_id",
}


class GuardEvidenceError(ValueError):
    pass


def require(value, message):
    if not value:
        raise GuardEvidenceError(message)


def strict_json(text):
    def pairs(items):
        require(len(dict(items)) == len(items), "duplicate evidence key")
        return dict(items)

    def nonfinite(value):
        raise GuardEvidenceError("nonfinite evidence number")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, TypeError) as error:
        raise GuardEvidenceError("invalid evidence JSON") from error


def file_sha(path):
    with path.open("rb") as stream:
        return sha256(stream.read()).hexdigest()


_CHECKPOINT_READER = """
import json, sqlite3, sys
from pathlib import Path
from urllib.parse import quote
path = Path(sys.argv[1]).resolve()
tables = json.loads(sys.argv[2])
with sqlite3.connect('file:' + quote(str(path)) + '?mode=ro&immutable=1', uri=True) as db:
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    result = {name: [dict(row) for row in db.execute('SELECT * FROM ' + name + ' ORDER BY ' + order)]
              for name, order in tables.items()}
print(json.dumps(result, allow_nan=False))
"""


def read_closed_checkpoint(path: Path):
    """Read a checkpoint after shutdown, without creating or touching its DB."""
    path = path.resolve(strict=True)
    require(path.is_file(), "native checkpoint is not a file")
    sidecars = [Path(str(path) + suffix) for suffix in ("-wal", "-journal")]
    require(
        not any(p.exists() and p.stat().st_size for p in sidecars),
        "native checkpoint has unsettled journal state",
    )
    before = file_sha(path)
    result = subprocess.run(
        [sys.executable, "-c", _CHECKPOINT_READER, str(path), json.dumps(TABLES)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    require(
        file_sha(path) == before
        and not any(p.exists() and p.stat().st_size for p in sidecars),
        "native checkpoint changed during observation",
    )
    return {
        "record_type": "closed_native_guard_checkpoint",
        "database_sha256": before,
        "tables": strict_json(result.stdout),
    }


_COMPARATOR = """
import hashlib, json, os, sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import agentc
from agentc import _native
from agentc._patches._optimizer_glue import _response_output_text, _text_divergence
from agentc._text_actions import TextActionContract, text_action_scope
def attributes(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{k: attributes(v) if k in ('choices', 'message') else v
                                 for k, v in value.items()})
    if isinstance(value, list):
        return [attributes(v) for v in value]
    return value
root = Path(agentc.__file__).resolve().parent
def files():
    values = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(root.rglob('*.py'))}
    values[Path(_native.__file__).name] = hashlib.sha256(Path(_native.__file__).read_bytes()).hexdigest()
    return values
before = files()
results = []
for pair in json.load(sys.stdin):
    os.environ['AGENTC_SHADOW_DIVERGENCE_MODE'] = pair['divergence_mode']
    contract = pair['text_action_contract']
    scope = text_action_scope(TextActionContract(**contract)) if contract else nullcontext()
    with scope:
        a, b = [_response_output_text(attributes(pair[n])) for n in ('candidate', 'reference')]
        results.append(None if a is None or b is None else _text_divergence(a, b))
if files() != before:
    raise RuntimeError('SDK changed during comparison')
print(json.dumps({'sdk_files': before, 'values': results}, allow_nan=False))
"""


def compare_receipt_outputs(pairs, *, python: Path, expected_sdk_files=None):
    """Recompute with the enrolled SDK comparator, without executing any action."""
    require(
        all(p["divergence_mode"] in {"lexical", "normalized"} for p in pairs),
        "embedding comparator requires separately enrolled model evidence",
    )
    result = subprocess.run(
        [str(python), "-c", _COMPARATOR],
        input=json.dumps(pairs, allow_nan=False),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    compared = strict_json(result.stdout)
    if expected_sdk_files is not None:
        require(
            compared["sdk_files"] == expected_sdk_files,
            "comparison SDK differs from enrollment",
        )
    require(len(compared["values"]) == len(pairs), "incomplete comparator coverage")
    return compared["values"]


def collect_checkpoint(
    path: Path,
    calls,
    *,
    ledger: Path,
    python: Path,
    expected_sdk_files,
    mode,
    configuration,
    calibration_lock_sha256,
    session_id,
    now_us,
    divergence_mode="lexical",
):
    """Public collector: join the journal, replay outputs, then audit native state."""
    snapshot = read_snapshot(ledger)
    require(
        isinstance(expected_sdk_files, dict) and bool(expected_sdk_files),
        "guard comparator needs an enrolled SDK manifest",
    )
    bills = {
        e["attempt_id"]: e
        for e in snapshot.events
        if e["event"] == "result"
        and e["session_id"] == session_id
        and e["metadata"]["arm"] == mode
    }
    require(
        set(bills) == {c["attempt_id"] for c in calls},
        "incomplete journal receipt coverage",
    )
    claims = {
        e["attempt_id"]: e
        for e in snapshot.events
        if e["event"] == "reserve"
        and e.get("session_id") == session_id
        and e.get("metadata", {}).get("arm") == mode
    }
    require(set(claims) == set(bills), "guard acquisition has unsettled owned claims")
    responses = {
        e["attempt_id"]: e
        for e in snapshot.events
        if e["event"] == "response" and e.get("attempt_id") in claims
    }
    require(
        set(responses) == set(claims), "guard acquisition lacks owned HTTP responses"
    )
    for call in calls:
        require(
            call["billed_result"] == bills[call["attempt_id"]]
            and call["http_receipt"] == responses[call["attempt_id"]]
            and call["request"] == claims[call["attempt_id"]]["request"],
            "owned request/response/bill differs from journal",
        )
    primaries = {
        (c["metadata"]["cell_id"], c["metadata"]["turn_id"]): c
        for c in calls
        if c["metadata"]["kind"] == "primary" and c["metadata"]["actor"] == "agent"
    }
    inputs, attempts = [], []
    for call in calls:
        metadata = call["metadata"]
        if metadata["kind"] != "shadow":
            continue
        primary = primaries.get((metadata["cell_id"], metadata["turn_id"]))
        require(primary is not None, "shadow lacks primary response")
        action_fields = {
            k: metadata.get(k)
            for k in ("text_action_comparator_version", "text_action_settings_version")
        }
        require(
            all(primary["metadata"].get(k) == v for k, v in action_fields.items()),
            "shadow action contract differs from primary",
        )
        contract = None
        if any(v is not None for v in action_fields.values()):
            require(
                all(isinstance(v, str) for v in action_fields.values()),
                "incomplete action comparison contract",
            )
            contract = {
                "comparator_version": action_fields["text_action_comparator_version"],
                "settings_version": action_fields["text_action_settings_version"],
            }
        inputs.append(
            {
                "candidate": primary["http_receipt"]["response"],
                "reference": call["http_receipt"]["response"],
                "divergence_mode": divergence_mode,
                "text_action_contract": contract,
            }
        )
        attempts.append(call["attempt_id"])
    values = compare_receipt_outputs(
        inputs, python=python, expected_sdk_files=expected_sdk_files
    )
    comparison = dict(zip(attempts, values, strict=True))
    checkpoint = read_closed_checkpoint(path)
    audit = audit_checkpoint(
        checkpoint,
        calls,
        mode=mode,
        configuration=configuration,
        calibration_lock_sha256=calibration_lock_sha256,
        session_id=session_id,
        now_us=now_us,
        comparison_values=comparison,
    )
    require(
        read_snapshot(ledger).sha256 == snapshot.sha256,
        "journal changed during guard audit",
    )
    audit.update(
        journal_sha256=snapshot.sha256,
        journal_prefix_bytes=snapshot.byte_count,
        global_retained_uncertainty_usd=str(snapshot.state["holds_usd"]),
        sdk_closure_sha256=digest(expected_sdk_files),
    )
    return {"audit": audit, "checkpoint": checkpoint, "comparison_values": comparison}


def key(row):
    return row["call_site_version"], row["execution_plan_id"]


def runtime(row):
    return {field: row[field] for field in RUNTIME_FIELDS}


def conformal_upper_p95(values):
    require(
        all(
            type(v) in {float, int} and math.isfinite(v) and 0 <= v <= 1 for v in values
        ),
        "invalid native divergence",
    )
    return (
        sorted(values)[min(len(values), math.ceil(0.95 * (len(values) + 1))) - 1]
        if values
        else None
    )


def audit_checkpoint(
    checkpoint,
    calls,
    *,
    mode,
    configuration,
    calibration_lock_sha256,
    session_id,
    now_us,
    comparison_values,
):
    """Audit one policy store; keep opposite execution orders and versions apart.

    comparison_values maps each shadow attempt to a divergence independently
    recomputed from its actual response and the primary response. The public
    collector obtains it with compare_receipt_outputs; it is not a gate flag.
    """
    require(type(now_us) is int and now_us >= 0, "invalid checkpoint audit time")
    require(set(checkpoint["tables"]) == set(TABLES), "incomplete native guard tables")
    tables = checkpoint["tables"]
    for table, order in TABLES.items():
        fields = order.split(", ")
        identities = [tuple(row[field] for field in fields) for row in tables[table]]
        require(len(identities) == len(set(identities)), "duplicate native state row")
    profiles = {key(row): row for row in tables["execution_plan_profile"]}
    require(
        len(profiles) == len(tables["execution_plan_profile"]),
        "duplicate native profile",
    )
    records, primaries, groups, pairs = [], {}, {}, {}
    for call in calls:
        metadata = call["metadata"]
        require(metadata["arm"] == mode, "policy store contains another mode")
        require(
            call["billed_result"]["session_id"] == session_id,
            "foreign provider receipt",
        )
        records.extend(
            normalize_owned_calls(
                [call],
                metadata,
                cell_id=metadata["cell_id"],
                optimized_arms=frozenset({mode}),
                fixed_schema=VERSION,
                call_site_prefix="guard-evidence.",
            )
        )
        if metadata["actor"] != "agent":
            require(
                metadata["actor"] in {"user", "judge"}
                and metadata["kind"] == "primary",
                "non-agent counterfactual entered guard evidence",
            )
            continue
        if metadata["kind"] != "primary":
            continue
        turn = metadata["cell_id"], metadata["turn_id"]
        require(turn not in primaries, "ambiguous primary for shadow comparison")
        plan = strict_json(metadata["native_plan_json"])
        context = plan["agentc_observation_context"]
        policy = plan["agentc_selection_audit"]["policy"]
        require(
            policy["configuration_sha256"] == configuration_sha256(configuration)
            and policy["calibration_lock_sha256"] == calibration_lock_sha256
            and plan["agentc_selection_audit"]["planner_mode"] == mode
            and context["divergence_threshold"]
            == configuration["divergence_threshold"],
            "native receipt has another policy identity",
        )
        identity = key(context["key"])
        groups.setdefault(identity, []).append(call)
        primaries[turn] = call, context
    require(
        set(profiles) == set(groups),
        "native profiles lack complete owned receipt coverage",
    )
    require(
        len({(c["call_id"], c["attempt_id"]) for c in calls}) == len(calls),
        "reused physical receipt",
    )
    for call in calls:
        metadata = call["metadata"]
        if metadata["actor"] != "agent":
            continue
        if metadata["kind"] != "shadow":
            require(
                metadata["kind"] == "primary", "unsupported guard evidence dispatch"
            )
            continue
        turn = metadata["cell_id"], metadata["turn_id"]
        require(turn in primaries, "shadow has no owned primary")
        primary, context = primaries[turn]
        require(
            metadata["parent_native_plan_json"]
            == primary["metadata"]["native_plan_json"],
            "shadow parent plan differs",
        )
        token = strict_json(metadata["parent_observation_token"])
        observation = token["plan_observation"]
        require(
            token["guard_eligible"] is True
            and token["divergence_threshold"] == context["divergence_threshold"]
            and observation["key"] == context["key"]
            and observation["runtime_version"] == context["runtime_version"]
            and type(observation["sequence"]) is int
            and observation["sequence"] > 0,
            "shadow token has another complete-plan identity",
        )
        pair_key = (*key(observation["key"]), observation["sequence"])
        require(
            pair_key not in pairs, "replayed shadow cannot supply another safety sample"
        )
        require(
            call["attempt_id"] in comparison_values,
            "shadow output was not independently compared",
        )
        pairs[pair_key] = (
            call,
            primary,
            observation,
            comparison_values[call["attempt_id"]],
        )
    require(
        set(comparison_values)
        == {c["attempt_id"] for c in calls if c["metadata"]["kind"] == "shadow"},
        "unowned comparator result",
    )
    results = []
    for identity, profile in profiles.items():
        require(
            profile["n_observations"] == len(groups[identity]),
            "native lifetime outcome count lacks complete billed coverage",
        )
        require(
            profile["n_paired_observations"]
            == sum(
                value[3] is not None
                for pair_key, value in pairs.items()
                if pair_key[:2] == identity
            ),
            "native lifetime paired count differs from owned comparisons",
        )
        require(
            all(
                strict_json(call["metadata"]["native_plan_json"])[
                    "agentc_observation_context"
                ]["runtime_version"]
                == runtime(profile)
                for call in groups[identity]
            ),
            "native profile differs from its enrolled receipt runtime",
        )
        retained = [
            r
            for r in tables["execution_plan_divergence_observation"]
            if key(r) == identity
        ]
        require(
            profile["paired_observations"] == len(retained)
            and profile["n_paired_observations"] >= len(retained)
            and profile["window_observations"]
            == len(
                [r for r in tables["execution_plan_observation"] if key(r) == identity]
            ),
            "native retained-window counts disagree",
        )
        seen = set()
        for row in retained:
            pair_key = (*identity, row["plan_observation_sequence"])
            require(
                pair_key in pairs and pair_key not in seen,
                "native safety sample lacks a unique billed pair",
            )
            seen.add(pair_key)
            shadow, primary, observation, compared = pairs[pair_key]
            require(
                compared is not None
                and math.isclose(row["divergence"], compared, rel_tol=0, abs_tol=1e-12)
                and runtime(row) == observation["runtime_version"] == runtime(profile)
                and 0 <= row["observed_at"] <= now_us,
                "native safety sample differs from actual outputs or runtime",
            )
            outcome = next(
                (
                    r
                    for r in tables["execution_plan_observation"]
                    if key(r) == identity
                    and r["sample_sequence"] == observation["sequence"]
                ),
                None,
            )
            if outcome is not None:
                bill = primary["billed_result"]
                require(
                    outcome["input_tokens"] == bill["usage"]["prompt_tokens"]
                    and outcome["output_tokens"] == bill["usage"]["completion_tokens"]
                    and math.isclose(
                        outcome["cost_usd"],
                        float(money(bill["cost_usd"])),
                        rel_tol=0,
                        abs_tol=1e-12,
                    )
                    and runtime(outcome) == runtime(row),
                    "native outcome differs from actual billed invocation",
                )
        upper = conformal_upper_p95([r["divergence"] for r in retained])
        last = max((r["observed_at"] for r in retained), default=None)
        require(
            profile["divergence_upper_p95"] == upper
            and profile["last_paired_at"] == last,
            "native paired summary differs from retained samples",
        )
        guard = next(
            (r for r in tables["execution_plan_guard"] if key(r) == identity), None
        )
        exposure_rows = [
            r for r in tables["execution_plan_guard_observation"] if key(r) == identity
        ]
        if guard is not None:
            require(
                guard["divergence_threshold"] == configuration["divergence_threshold"]
                and runtime(guard) == runtime(profile),
                "guard policy/runtime mismatch",
            )
            for row in exposure_rows:
                pair_key = (*identity, row["plan_observation_sequence"])
                require(pair_key in pairs, "guard exposure lacks an owned shadow")
                compared = pairs[pair_key][3]
                require(
                    compared is not None
                    and row["divergence"] == compared
                    and math.isclose(
                        row["excess"],
                        max(0, compared - guard["divergence_threshold"]),
                        rel_tol=0,
                        abs_tol=1e-12,
                    )
                    and runtime(row) == runtime(profile),
                    "native guard exposure differs from actual pair",
                )
            require(
                guard["window_samples"] == len(exposure_rows)
                and math.isclose(
                    guard["divergence_exposure"],
                    sum(r["excess"] for r in exposure_rows),
                    rel_tol=0,
                    abs_tol=1e-12,
                ),
                "guard exposure summary mismatch",
            )
        else:
            require(not exposure_rows, "orphan guard exposure")
        disabled = next(
            (r for r in tables["execution_plan_disabled"] if key(r) == identity), None
        )
        if disabled is not None:
            require(
                0 <= disabled["disabled_at"] <= now_us
                and disabled["reenable_at"] >= disabled["disabled_at"]
                and disabled["exposure"] >= 1.0,
                "invalid durable plan disable",
            )
        reasons = []
        if len(retained) < 20:
            reasons.append("insufficient_retained_pairs")
        if upper is None or upper > configuration["divergence_threshold"]:
            reasons.append("divergence_bound_not_met")
        if last is None or now_us - last > MAX_AGE_US:
            reasons.append("paired_window_stale_or_missing")
        if disabled is not None:
            reasons.append("durable_plan_disable")
        results.append(
            {
                "call_site_version": identity[0],
                "execution_plan_id": identity[1],
                "runtime_version": runtime(profile),
                "retained_paired_observations": len(retained),
                "lifetime_paired_observations": profile["n_paired_observations"],
                "divergence_upper_p95": upper,
                "last_paired_at_us": last,
                "paired_window_qualifies": not reasons,
                "rejection_reasons": reasons,
                "guard": guard,
                "durable_disable": disabled,
            }
        )
    require(
        all(key(r) in profiles for table in TABLES for r in tables[table]),
        "orphan native state",
    )
    return {
        "schema_version": 1,
        "record_type": "guard_paired_evidence_audit",
        "version": VERSION,
        "mode": mode,
        "policy_identity_sha256": policy_identity_sha256(mode, configuration),
        "configuration_sha256": configuration_sha256(configuration),
        "calibration_lock_sha256": calibration_lock_sha256,
        "checkpoint_sha256": checkpoint["database_sha256"],
        "native_tables_sha256": digest(tables),
        "calls_sha256": digest(calls),
        "comparison_values_sha256": digest(comparison_values),
        "audited_at_us": now_us,
        "plans": results,
        "physical_calls": len(calls),
        "all_call_cost_usd": str(
            sum((money(c["billed_result"]["cost_usd"]) for c in calls), money("0"))
        ),
        "shadow_cost_usd": str(
            sum(
                (
                    money(c["billed_result"]["cost_usd"])
                    for c in calls
                    if c["metadata"]["kind"] == "shadow"
                ),
                money("0"),
            )
        ),
        "calibration_admission": False,
        "unmeasured_requirements": [
            "task_damage_frontier",
            "safety_sensor_accuracy",
            "net_savings_retention",
            "restart_and_drift_outcomes",
        ],
    }
