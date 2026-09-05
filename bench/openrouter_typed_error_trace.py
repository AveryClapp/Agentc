"""Post-hoc public-fact audit of a complete typed factorial; never runtime feedback.

Reads committed artifacts only, without credentials, network, or ledger access.
Rechecks artifact consistency and own-history inputs, not provider billing.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import re

from bench import openrouter_typed_factorial as study
from bench import typed_relational_workload as typed
from bench.openrouter_matrix import file_hash, write_json
from bench.openrouter_pilot import PilotError, digest

FILES = (
    "manifest.json",
    "fixture.json",
    "calls.json",
    "intents.json",
    "summary.json",
    "analysis.json",
)
TIERS = ("low", "middle", "high")


def _pairs(text):
    pairs = [part.split("=", 1) for part in text.split("|")]
    if any(len(pair) != 2 for pair in pairs) or len({p[0] for p in pairs}) != len(
        pairs
    ):
        raise PilotError("ambiguous public record")
    return dict(pairs)


def _join(public, tenant, requisition, selected):
    tables = {}
    for paragraph in public["meta"]["paragraphs"]:
        header = _pairs(paragraph["sentences"][0])
        if paragraph["title"] in selected and header["tenant"] == tenant:
            if header["table"] in tables:
                raise PilotError("ambiguous tenant table")
            tables[header["table"]] = paragraph
    if set(tables) != {"REQUESTS", "CATALOG", "OFFERS"}:
        return {
            "value": None,
            "witness": [],
            "reason": "selected records lack the target tenant's complete triple",
        }
    witness = []

    def row(kind, key, value):
        paragraph = tables[kind]
        matches = [
            (index, text, _pairs(text))
            for index, text in enumerate(paragraph["sentences"][1:], 1)
            if _pairs(text).get(key) == value
        ]
        if len(matches) != 1:
            raise PilotError("public join is not unique")
        index, text, record = matches[0]
        witness.append(
            {
                "document_id": paragraph["title"],
                "table": kind,
                "sentence_index": index,
                "text": text,
            }
        )
        return record

    request = row("REQUESTS", "req", requisition)
    catalog = row("CATALOG", "sku", request["sku"])
    offer = row("OFFERS", "vendor", catalog["vendor"])
    qty, unit = int(request["qty"]), int(offer["unit"])
    return {
        "value": {
            "sku": request["sku"],
            "vendor": catalog["vendor"],
            "qty": qty,
            "unit": unit,
            "total": qty * unit,
            "region": request["region"],
        },
        "witness": witness,
        "reason": None,
    }


def _policy(policies, synthesis):
    if synthesis is None:
        return None
    rule = policies[synthesis["region"]]
    total = synthesis["total"]
    index = 0 if total <= rule["low"] else 1 if total <= rule["high"] else 2
    return {
        "region": synthesis["region"],
        "total": total,
        "tier": TIERS[index],
        "approver": rule["approvers"][index],
    }


def public_facts(task):
    """Solve only public records/policy; task.expected is deliberately inaccessible."""
    public = typed._public(task)
    lines = public["prompt"].splitlines()
    match = re.fullmatch(r"Tenant (T[A-Z0-9]+); requisition (R[0-9]{3})\.", lines[0])
    if (
        not match
        or len(lines) != 4
        or lines[1] != "Approval table (inclusive upper bounds):"
    ):
        raise PilotError("unexpected public policy grammar")
    tenant, requisition = match.groups()
    policies = {}
    for line in lines[2:]:
        match = re.fullmatch(
            r"(EU|US): total<=(\d+)->(M\d{2}); (\d+)<total<=(\d+)->(M\d{2}); total>(\d+)->(M\d{2})",
            line,
        )
        if not match:
            raise PilotError("unexpected public policy bounds")
        region, low, first, low2, high, second, high2, third = match.groups()
        if low != low2 or high != high2 or int(low) >= int(high) or region in policies:
            raise PilotError("public policy is not disjoint/exhaustive")
        policies[region] = {
            "low": int(low),
            "high": int(high),
            "approvers": [first, second, third],
            "text": line,
        }
    if set(policies) != {"EU", "US"}:
        raise PilotError("public policy lacks a region")
    selected = sorted(
        p["title"]
        for p in public["meta"]["paragraphs"]
        if _pairs(p["sentences"][0])["tenant"] == tenant
    )
    if len(selected) != 3:
        raise PilotError("target tenant lacks exactly three documents")
    joined = _join(public, tenant, requisition, selected)
    if joined["value"] is None:
        raise PilotError("public fixture cannot be solved")
    return {
        "tenant": tenant,
        "requisition": requisition,
        "filter": selected,
        "synthesis": joined["value"],
        "join_witness": joined["witness"],
        "policy": policies,
        "approval": _policy(policies, joined["value"]),
    }


def trace_cell(task, facts, rows):
    """Compare each actual value with full-fixture and own-input requirements."""
    values = {
        stage: typed.parse_stage(
            stage, rows[stage]["answer"], finish_reason=rows[stage]["finish_reason"]
        )
        for stage in typed.STAGES
    }
    if values["filter"] is not None:
        values["filter"] = list(values["filter"])
    selected_join = _join(
        typed._public(task),
        facts["tenant"],
        facts["requisition"],
        values["filter"] or [],
    )
    policy = _policy(facts["policy"], values["synthesize"])
    valid = {stage: values[stage] is not None for stage in typed.STAGES}
    correct = {
        "filter": values["filter"] == facts["filter"],
        "synthesize": values["synthesize"] == facts["synthesis"],
        "answer": all(valid.values())
        and values["answer"] == facts["approval"]["approver"],
    }
    policy_correct = policy is not None and values["answer"] == policy["approver"]
    if not all(valid.values()):
        category = "invalid_application_path"
    elif all(correct.values()) and policy_correct:
        category = "all_stages_correct"
    elif correct["answer"]:
        category = "final_correct_with_stage_inconsistency"
    elif policy_correct:
        category = "propagated_upstream_error"
    else:
        category = (
            "final_policy_error"
            if correct["filter"] and correct["synthesize"]
            else "upstream_and_policy_error"
        )
    chosen = [
        {"region": region, "tier": TIERS[index]}
        for region, rule in facts["policy"].items()
        for index, approver in enumerate(rule["approvers"])
        if approver == values["answer"]
    ]
    matching_offer_rows = []
    if values["synthesize"] is not None and values["filter"]:
        for paragraph in task["meta"]["paragraphs"]:
            header = _pairs(paragraph["sentences"][0])
            if paragraph["title"] in values["filter"] and header == {
                "tenant": facts["tenant"],
                "table": "OFFERS",
            }:
                matching_offer_rows.extend(
                    {
                        "document_id": paragraph["title"],
                        "sentence_index": index,
                        "text": text,
                    }
                    for index, text in enumerate(paragraph["sentences"][1:], 1)
                    if int(_pairs(text)["unit"]) == values["synthesize"]["unit"]
                )
    return {
        "values": values,
        "valid_by_stage": valid,
        "correct_against_full_fixture": correct,
        "synthesis_required_by_actual_selection": selected_join,
        "synthesis_matches_actual_selection": selected_join["value"] is not None
        and values["synthesize"] == selected_join["value"],
        "synthesis_differing_fields": [
            key
            for key in facts["synthesis"]
            if values["synthesize"] is None
            or values["synthesize"].get(key) != facts["synthesis"][key]
        ],
        "policy_required_by_actual_synthesis": policy,
        "answer_matches_actual_synthesis_policy": policy_correct,
        "chosen_policy_rows": chosen,
        "actual_unit_matches_selected_offer_rows": matching_offer_rows,
        "classification": category,
        "receipts": {
            stage: {
                "call_id": row["id"],
                "generation_id": row["generation_id"],
                "raw_answer_sha256": digest(row["answer"]),
            }
            for stage, row in rows.items()
        },
    }


def audit(directory, manifest_sha256):
    data = {name: json.loads((directory / name).read_text()) for name in FILES}
    manifest, tasks, calls, intents, summary, analysis = (data[name] for name in FILES)
    if (
        digest(manifest) != manifest_sha256
        or summary["manifest_sha256"] != manifest_sha256
        or analysis["manifest_sha256"] != manifest_sha256
    ):
        raise PilotError("artifact manifest mismatch")
    if (
        len(tasks) != study.COUNT
        or tasks != typed.make_tasks(study.SEED, study.COUNT)
        or manifest["schedule"] != study.schedule(tasks)
    ):
        raise PilotError("unexpected frozen fixture/schedule")
    if (
        not summary["schedule_complete"]
        or not analysis["schedule_complete"]
        or summary["completed_calls"] != study.COUNT * 12
        or len(calls) != len(intents)
        or len(calls) != study.COUNT * 12
    ):
        raise PilotError("audit requires all32x4x3 saved calls")
    if (
        summary["calls_sha256"] != digest(calls)
        or summary["intents_sha256"] != digest(intents)
        or file_hash(directory / "fixture.json") != manifest["fixture_sha256"]
    ):
        raise PilotError("raw artifact digests changed")
    if study.sources() != manifest["source_files"]:
        raise PilotError("frozen acquisition/parser source changed")
    attention = study.load_module(
        "posthoc_trace_attention", study.ROOT / "python/agentc/_attention.py"
    )
    indexed = {t["task_id"]: t for t in tasks}
    by_cell = {}
    generations = set()
    for index, workflow in enumerate(manifest["schedule"]):
        history, stage_rows = {}, {}
        task = indexed[workflow["task_id"]]
        for offset, stage in enumerate(typed.STAGES):
            row, intent = calls[index * 3 + offset], intents[index * 3 + offset]
            item = {**workflow, "workflow_stage": stage}
            if (
                any(row[k] != v or intent[k] != v for k, v in item.items())
                or row["id"] != intent["id"]
                or row["generation_id"] in generations
            ):
                raise PilotError("duplicate or misaligned stage evidence")
            generations.add(row["generation_id"])
            original = study.restrict_call(
                typed.call(
                    typed._public(task),
                    stage,
                    history,
                    attention,
                    model=study.ARMS[workflow["arm"]][0],
                    run_identity=study.stage_id(manifest) + "/" + workflow["arm"],
                    prompts=manifest["prompts"],
                ),
                stage,
            )
            if digest(original) != digest(intent["original_call"]):
                raise PilotError("saved request does not use its own preceding outputs")
            selected = deepcopy(original)
            indices = intent["materialization"]["retained_indices"]
            if (
                not indices
                or any(
                    type(i) is not int or not 0 <= i < len(original["messages"])
                    for i in indices
                )
                or indices != sorted(set(indices))
            ):
                raise PilotError("invalid saved native deletion indices")
            for field in ("messages", "input_deps"):
                selected[field] = [selected[field][i] for i in indices]
            for field in ("message_deps", "attention_scores"):
                aligned = selected["parameters"]["extra"].get(field)
                if aligned is not None:
                    selected["parameters"]["extra"][field] = [
                        aligned[i] for i in indices
                    ]
            if digest(selected) != digest(intent["selected_call"]) or digest(
                study.live.payload_for(selected, manifest)
            ) != digest(intent["payload"]):
                raise PilotError(
                    "saved transmitted payload differs from its exact deletion"
                )
            study.analysis.validate_row(
                row, intent["payload"], intent["metadata"], manifest
            )
            history[stage] = typed.application_text(
                row["answer"], row["finish_reason"], stage=stage
            )
            stage_rows[stage] = row
        by_cell[(task["task_id"], workflow["arm"])] = stage_rows
    traces = []
    reported = {r["task_id"]: r for r in analysis["paired_tasks"]}
    if len(reported) != len(tasks):
        raise PilotError("analysis does not cover each task")
    for index, task in enumerate(tasks, 1):
        facts = public_facts(task)
        if facts["approval"]["approver"] != task["expected"]:
            raise PilotError("public-fact result disagrees with evaluator label")
        cells = {
            arm: trace_cell(task, facts, by_cell[(task["task_id"], arm)])
            for arm in study.ARMS
        }
        pair_inputs = {}
        for model, original_arm, rewritten_arm in (
            ("Sonnet", "S0", "SR"),
            ("Haiku", "H0", "HR"),
        ):
            payloads = {
                (intent["arm"], intent["workflow_stage"]): intent["payload"]
                for intent in intents
                if intent["task_id"] == task["task_id"]
            }
            original_answer = deepcopy(payloads[(original_arm, "answer")])
            original_answer["messages"] = [
                original_answer["messages"][i] for i in (0, 2, 3)
            ]
            pair_inputs[model] = {
                "filter_application_values_equal": cells[original_arm]["values"][
                    "filter"
                ]
                == cells[rewritten_arm]["values"]["filter"],
                "synthesis_payloads_identical": digest(
                    payloads[(original_arm, "synthesize")]
                )
                == digest(payloads[(rewritten_arm, "synthesize")]),
                "synthesis_application_values_equal": cells[original_arm]["values"][
                    "synthesize"
                ]
                == cells[rewritten_arm]["values"]["synthesize"],
                "answer_payload_diff_is_only_selected_records_deletion": digest(
                    original_answer
                )
                == digest(payloads[(rewritten_arm, "answer")]),
            }
        if any(
            float(cells[arm]["correct_against_full_fixture"]["answer"])
            != reported[task["task_id"]]["cells"][arm]["em"]
            for arm in study.ARMS
        ):
            raise PilotError(
                "public-fact final correctness differs from primary analysis"
            )
        traces.append(
            {
                "task_index": index,
                "task_id": task["task_id"],
                "public_facts": facts,
                "cells": cells,
                "within_model_input_pair_checks": pair_inputs,
            }
        )
    return {
        "kind": "typed-factorial-posthoc-public-error-trace-v1",
        "manifest_sha256": manifest_sha256,
        "audit_source_sha256": file_hash(Path(__file__)),
        "source_artifact_sha256": {name: file_hash(directory / name) for name in FILES},
        "scope": "posthoc development diagnosis; no runtime feedback, new treatment, causal stage isolation, native admission or adaptive-policy claim",
        "financial_verification": "Not rerun here; this audit reads committed artifacts only and does not access the campaign ledger.",
        "tasks": traces,
        "tasks_with_final_errors": [
            t["task_index"]
            for t in traces
            if not all(
                c["correct_against_full_fixture"]["answer"] for c in t["cells"].values()
            )
        ],
        "tasks_with_any_stage_error": [
            t["task_index"]
            for t in traces
            if any(
                c["classification"] != "all_stages_correct" for c in t["cells"].values()
            )
        ],
        "by_arm": {
            arm: {
                "correct_by_stage": {
                    stage: sum(
                        t["cells"][arm]["correct_against_full_fixture"][stage]
                        for t in traces
                    )
                    for stage in typed.STAGES
                },
                "policy_consistent_with_actual_synthesis": sum(
                    t["cells"][arm]["answer_matches_actual_synthesis_policy"]
                    for t in traces
                ),
                "classifications": dict(
                    Counter(t["cells"][arm]["classification"] for t in traces)
                ),
            }
            for arm in study.ARMS
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.input, args.manifest_sha256)
    write_json(args.output, report, immutable=True)
    print(
        json.dumps(
            {
                "tasks": len(report["tasks"]),
                "by_arm": report["by_arm"],
                "affected_tasks": report["tasks_with_final_errors"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
