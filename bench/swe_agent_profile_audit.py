"""Audit sealed SWE-agent eligibility and spend without importing the SDK.

Run only after worker completion. Native profile counts describe retained
evidence; they do not establish validator accuracy or admission qualification.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from bench.campaign_accounting import digest, money
from bench.coding_submission import private_write
from bench.guard_evidence import file_sha
from bench.swebench_cohort import require


def _read(path):
    return json.loads(path.read_bytes())


def _profiles(storage):
    require(
        not any(n == "agentc" or n.startswith("agentc.") for n in sys.modules),
        "profile audit requires a separate process without the SDK",
    )
    database = Path(storage) / "cost_model.db"
    require(database.is_file(), "sealed native profile database is missing")
    with closing(
        sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    ) as sql:
        sql.execute("PRAGMA query_only = ON")
        sql.execute("BEGIN")
        profiles = [
            {
                "call_site_version": version,
                "execution_plan_id": plan,
                "observations": observations,
                "retained_pairs": pairs,
            }
            for version, plan, observations, pairs in sql.execute(
                "SELECT call_site_version, execution_plan_id, n_observations, "
                "paired_observations FROM execution_plan_profile "
                "ORDER BY call_site_version, execution_plan_id"
            )
        ]
        states = dict(
            sql.execute(
                "SELECT status, count(*) FROM execution_plan_exploration GROUP BY status"
            )
        )
        require(
            not set(states) - {"completed", "failed", "abandoned"},
            "sealed store contains unresolved exploration",
        )
    return {
        "profiles": profiles,
        "exploration_states": states,
        "maximum_retained_pairs": max(
            (p["retained_pairs"] for p in profiles), default=0
        ),
    }


def audit(root: Path) -> dict:
    require(
        not any(n == "agentc" or n.startswith("agentc.") for n in sys.modules),
        "profile audit requires a separate process without the SDK",
    )
    acquisition = root / "acquisition"
    enrollment = _read(acquisition / "enrollment.json")
    completion = _read(acquisition / "completion.json")
    require(
        completion["enrollment_sha256"] == digest(enrollment)
        and completion["visited_cells"] == len(enrollment["schedule"]),
        "profile audit requires a completed enrolled worker",
    )
    results, cells, stores = [], [], {}
    spend = defaultdict(lambda: defaultdict(Decimal))
    counts = defaultdict(Counter)
    seen_calls, seen_ordinals = set(), set()
    for cell in enrollment["schedule"]:
        identity = digest(cell)
        path = acquisition / identity
        result = _read(path / "result.json")
        records = _read(path / "normalized.json")
        calls = _read(path / "calls.json")
        task = records[0]
        require(
            cell["ordinal"] not in seen_ordinals
            and task["cell_id"] == result["cell_id"] == identity
            and all(
                task[k] == cell[k] for k in ("task_id", "arm", "run_seed", "repetition")
            )
            and task["schedule_ordinal"] == cell["ordinal"]
            and task["acquisition_enrollment_sha256"] == digest(enrollment)
            and result["calls_sha256"] == task["calls_sha256"] == digest(calls)
            and result["provider_call_count"] == len(calls)
            and len(records) == len(calls) + 1,
            "cell identity or physical-call seal differs",
        )
        seen_ordinals.add(cell["ordinal"])
        results.append(result)
        counts[cell["arm"]]["assigned_cells"] += 1
        if task["official_score"] is None:
            counts[cell["arm"]]["unknown_score_cells"] += 1
        row = {
            "cell": cell,
            "executed": (path / "process-result.json").exists(),
            "official_score": task["official_score"],
            "native_call_site_versions": [],
            "first_selected_alternative_query": None,
        }
        versions, search, abstention = set(), Counter(), Counter()
        primary_query = 0
        total = Decimal(0)
        for call, normalized in zip(calls, records[1:]):
            metadata = call["metadata"]
            require(
                call["call_id"] not in seen_calls
                and metadata["cell_id"] == identity
                and metadata["arm"] == cell["arm"]
                and metadata["task_id"] == cell["task_id"]
                and call["request"]["seed"] == cell["run_seed"]
                and normalized["attempt_id"] == call["attempt_id"]
                and normalized["request_digest"]
                == call["request_digest"]
                == digest(call["request"])
                and normalized["cost_usd_exact"] == call["billed_result"]["cost_usd"],
                "physical receipt differs from enrolled cell or normalized billing",
            )
            seen_calls.add(call["call_id"])
            kind = metadata["kind"]
            cost = money(call["billed_result"]["cost_usd"])
            total += cost
            spend[cell["arm"]][kind] += cost
            counts[cell["arm"]][kind + "_calls"] += 1
            if kind != "primary":
                continue
            query_index = primary_query
            primary_query += 1
            if normalized["eligible"]:
                counts[cell["arm"]]["eligible_primary_calls"] += 1
            else:
                counts[cell["arm"]]["excluded_primary_calls"] += 1
            native_json = (call["planning"] or {}).get("native_plan_json")
            if native_json:
                native = json.loads(native_json)
                context = native.get("agentc_observation_context")
                if context:
                    versions.add(context["key"]["call_site_version"])
                selection = native.get("agentc_selection_audit")
                if selection:
                    search[selection["search_status"]] += 1
                    abstention[selection["abstention_reason"] or "none"] += 1
                    reference = selection.get("reference")
                    if reference is None:
                        counts[cell["arm"]][
                            "selection_reference_unavailable_primary_calls"
                        ] += 1
                    elif selection["effective_selected_plan_id"] != reference["id"]:
                        counts[cell["arm"]]["selected_alternative_primary_calls"] += 1
                        if row["first_selected_alternative_query"] is None:
                            row["first_selected_alternative_query"] = query_index
        require(
            total == money(result["all_call_cost_usd"]), "cell billed total differs"
        )
        row.update(
            native_call_site_versions=sorted(versions),
            search_status=dict(search),
            abstention_reasons=dict(abstention),
        )
        storage_path = path / "runtime-storage.json"
        if storage_path.exists():
            storage = _read(storage_path)
            expected = enrollment["storage"][str(cell["ordinal"])]
            require(
                storage["python_store"]
                == storage["native_store"]
                == storage["environment_store"]
                == expected,
                "native store differs from enrolled arm/partition",
            )
            if expected not in stores:
                stores[expected] = _profiles(expected)
            row["store"] = expected
        counts[cell["arm"]]["executed_cells"] += int(row["executed"])
        cells.append(row)
    require(
        completion["results_sha256"] == digest(results), "completed result seal differs"
    )
    return {
        "schema_version": 1,
        "record_type": "sealed_swe_agent_profile_and_spend_audit",
        "producer_sha256": file_sha(Path(__file__)),
        "enrollment_sha256": digest(enrollment),
        "completion_file_sha256": file_sha(acquisition / "completion.json"),
        "runtime": enrollment["runtime"],
        "arm_counts": {arm: dict(c) for arm, c in counts.items()},
        "physical_spend_usd_by_arm_and_kind": {
            a: {k: str(v) for k, v in s.items()} for a, s in spend.items()
        },
        "cells": cells,
        "native_stores": stores,
        "paper_evidence": False,
        "interpretation_limits": [
            "Eligibility describes the recorded scope, not qualified safety or benefit.",
            "Retained pairs cannot substitute for independently qualified task quality.",
            "Unknown scores stay in the assigned denominator; first selection may be null.",
            "Physical spend includes all recorded shadow/validation/fallback kinds; accuracy and counterfactual savings are unmeasured.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    private_write(args.output, audit(args.campaign))
    print(json.dumps({"output": str(args.output), "sha256": file_sha(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
