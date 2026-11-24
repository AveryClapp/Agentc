"""Stage R: convert the September OpenRouter protected study into study records.

Protocol Section 7.1 Stage R. Development evidence only: never cited as a test
result. No provider call is made; the archived files are read from git (the
tag below) or from a checked-out directory.

What the archive contains (``bench/repro/openrouter-protected-native-learning-2026-09-06``
on ``archive/exp/protected-native-learning``):

- ``study/calls.json``: 508 completed provider calls. ``arm`` is ``source``
  (plain Sonnet reference, 63 tasks x filter/synthesize/answer, each call's
  answer entered the application's history), ``probe`` (130 native off-path
  candidate-plan calls; answers never entered history) or ``HR`` (the same 63
  tasks under fixed Haiku + ContextCompress at filter + StateDrop at answer,
  run on its own history). A probe shares ``pair_id`` with exactly one source
  call (ids ``<pair_id>/source`` and ``<pair_id>/candidate``).
- ``study/protected-events.json``: the per-pair coordinator journal. The
  ``planned`` reply names the candidate plan (execution plan id, call-site
  version, target model, rules), ``feedback_claim`` holds the archived
  source/candidate divergence, ``feedback`` echoes the plan key the native
  store recorded, ``complete`` holds the text the application saw.
- ``native-profile-census.json``: per plan id, its rules and model label.
- ``analysis.json``: the predeclared scoring of every task and arm
  (``exact_accuracy`` in {0, 1}; an invalid stage scores 0).
- ``../openrouter-protected-generated-2026-09-06/labels.json``: the sealed
  expected answer scalar per task, used here only as a cross-check.

Mapping:

- Each probe pair becomes one :class:`ForkRecord`. Baseline is the raw source
  response, fork is the raw probe response, arm is ``R:<plan id prefix>``.
  S1 to S5 are recomputed with :func:`compute_signals`; the archived binary
  divergence is kept in ``components`` for cross-checking only.
- Each task becomes two :class:`TrajectoryRecord` s: ``baseline`` (source) and
  :data:`FIXED_ARM` (HR). Both carry the reference model so they form one
  ``(workload, model)`` group. Reward is the archived ``exact_accuracy``.

The archive contains no identical-plan repeat, so no ``C0`` (A/A) fork is
emitted and H1 cannot be estimated from Stage R. See :func:`convert`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from agentc.change_safety.normalize import action_signature
from agentc.change_safety.records import (
    CallKey,
    ChangeSpec,
    ForkRecord,
    NormalizedResponse,
    TrajectoryRecord,
    to_line,
)
from agentc.change_safety.signals import TerminationMarkers, compute_signals

DEFAULT_REF = "archive/exp/protected-native-learning"
DEFAULT_STUDY_DIR = "bench/repro/openrouter-protected-native-learning-2026-09-06"
DEFAULT_LABELS_DIR = "bench/repro/openrouter-protected-generated-2026-09-06"
REPO_ROOT = Path(__file__).resolve().parents[2]

PROTOCOL = "agentc-change-safety-v1"
STAGE = "R"
WORKLOAD = "openrouter-protected-dev"
WORKFLOW_STAGES = ("filter", "synthesize", "answer")
BASELINE_ARM = "baseline"
FIXED_ARM = "R:fixed-haiku-cc-sd"
REFERENCE_ARM, PROBE_ARM, HR_ARM = "source", "probe", "HR"

#: The typed procurement workload has no tools and no end-of-conversation
#: marker: every call is a structured-output JSON reply.
MARKERS = TerminationMarkers(end_tools=frozenset(), end_tokens=())

#: Per-stage change the HR arm applied: (rules, model). Used only when
#: ``fixed_composite`` relabels the matching probe plans as the HR arm.
FIXED_ARM_CHANGES = {
    "filter": (("ContextCompress", "ModelDowngrade"), "anthropic/claude-haiku-4.5"),
    "synthesize": (("ModelDowngrade",), "anthropic/claude-haiku-4.5"),
    "answer": (("StateDrop", "ModelDowngrade"), "anthropic/claude-haiku-4.5"),
}

CONTEXT_RULES = frozenset({"ContextCompress", "StateDrop"})


class ConversionError(RuntimeError):
    """The archive does not have the structure the converter relies on."""


# ---------------------------------------------------------------------------
# Reading the archive
# ---------------------------------------------------------------------------


class Archive:
    """Read JSON files from a git ref (default) or a directory tree."""

    def __init__(self, ref: str | None = DEFAULT_REF, repo: Path | str = REPO_ROOT):
        self.ref = ref
        self.repo = Path(repo)

    def read_json(self, path: str) -> Any:
        if self.ref is None:
            return json.loads((self.repo / path).read_text(encoding="utf-8"))
        result = subprocess.run(
            ["git", "-C", str(self.repo), "show", f"{self.ref}:{path}"],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise ConversionError(
                f"cannot read {self.ref}:{path}: {result.stderr.decode(errors='replace').strip()}"
            )
        return json.loads(result.stdout)


def load(
    archive: Archive,
    study_dir: str = DEFAULT_STUDY_DIR,
    labels_dir: str = DEFAULT_LABELS_DIR,
) -> dict[str, Any]:
    return {
        "calls": archive.read_json(f"{study_dir}/study/calls.json"),
        "events": archive.read_json(f"{study_dir}/study/protected-events.json"),
        "intents": archive.read_json(f"{study_dir}/study/intents.json"),
        "census": archive.read_json(f"{study_dir}/native-profile-census.json"),
        "analysis": archive.read_json(f"{study_dir}/analysis.json"),
        "labels": archive.read_json(f"{labels_dir}/labels.json"),
        "selection": archive.read_json(f"{labels_dir}/selection.json"),
    }


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------


def _nonneg_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def response_from_call(row: Mapping[str, Any]) -> NormalizedResponse:
    """The recorded provider response of one archived call, unaltered."""
    usage = row.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    cost = row.get("cost_usd")
    latency = row.get("latency_ms")
    answer = row.get("answer")
    if not isinstance(answer, str):
        raise ConversionError(f"call {row.get('id')} has no recorded answer")
    return NormalizedResponse(
        kind="text",
        text=answer,
        finish_reason=row.get("finish_reason"),
        input_tokens=_nonneg_int(usage.get("prompt_tokens")),
        output_tokens=_nonneg_int(usage.get("completion_tokens")),
        cached_input_tokens=_nonneg_int(details.get("cached_tokens")),
        cache_creation_input_tokens=_nonneg_int(details.get("cache_write_tokens")),
        cost_usd=float(cost) if cost is not None else None,
        latency_s=latency / 1000.0 if isinstance(latency, (int, float)) else None,
    )


def plan_arm(execution_plan_id: str) -> str:
    return "R:" + execution_plan_id[:8]


def plan_family(rules: Sequence[str]) -> str:
    """Single family for a probed plan.

    Declared rule, not an inference: any plan with ``ModelDowngrade`` is a
    model swap, otherwise its rules only remove context. Plans combining both
    are flagged as ambiguous in the conversion report.
    """
    return "model_swap" if "ModelDowngrade" in rules else "context_removal"


def _candidate_rules(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    if candidate.get("kind") == "rewritten":
        return (candidate["rule"],)
    if candidate.get("kind") == "composed":
        return tuple(r["rule"] for r in candidate["rules"])
    raise ConversionError(f"unknown candidate plan kind {candidate.get('kind')!r}")


def fixed_arm_spec(reference_model: str) -> ChangeSpec:
    return ChangeSpec(
        change_id=FIXED_ARM,
        family="model_swap",
        params={
            "reference_model": reference_model,
            "per_stage": {
                stage: {"rules": list(rules), "model": model}
                for stage, (rules, model) in FIXED_ARM_CHANGES.items()
            },
            "source": "archived fixed HR comparison block",
        },
    )


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def _index_events(events: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Mapping[str, Any]]]:
    by_pair: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for event in events:
        action = event["action"]
        if action in by_pair[event["pair_id"]]:
            raise ConversionError(f"duplicate {action} event for pair {event['pair_id']}")
        by_pair[event["pair_id"]][action] = event
    return by_pair


def _workflow_rows(calls: Sequence[Mapping[str, Any]], arm: str) -> dict[str, list[Mapping[str, Any]]]:
    rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in calls:
        if row["arm"] == arm:
            rows[row["task_id"]].append(row)
    for task_id, task_rows in rows.items():
        if tuple(r["workflow_stage"] for r in task_rows) != WORKFLOW_STAGES:
            raise ConversionError(f"{arm} workflow of {task_id} is not filter/synthesize/answer")
    return rows


def _label_scalar(text: str) -> Any:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value.get("result") if isinstance(value, dict) else None


def convert(
    data: Mapping[str, Any],
    *,
    embed: bool = True,
    fixed_composite: bool = False,
) -> tuple[list[ForkRecord | TrajectoryRecord], dict[str, Any]]:
    """Convert the loaded archive into records plus a conversion report.

    ``fixed_composite`` relabels the probes whose plan is exactly the HR arm's
    change at that stage (same rules and model) with :data:`FIXED_ARM`, so
    one cell carries both fork signals and trajectory damage. Off by default:
    those probes were planned natively, not by the fixed materializer, and the
    HR arm ran on its own history.
    """
    calls, census = data["calls"], data["census"]
    events = _index_events(data["events"])
    study_ids: list[str] = list(data["selection"]["study_ids"])
    sources = {r["pair_id"]: r for r in calls if r["arm"] == REFERENCE_ARM}
    probes = [r for r in calls if r["arm"] == PROBE_ARM]
    unknown_arms = sorted({r["arm"] for r in calls} - {REFERENCE_ARM, PROBE_ARM, HR_ARM})
    if unknown_arms:
        raise ConversionError(f"unexpected arms {unknown_arms}")
    source_rows = _workflow_rows(calls, REFERENCE_ARM)
    hr_rows = _workflow_rows(calls, HR_ARM)
    models = {r["model"] for rows in source_rows.values() for r in rows}
    if len(models) != 1:
        raise ConversionError(f"reference arm uses several models: {sorted(models)}")
    reference_model = models.pop()
    profiles = {p["execution_plan_id"]: p for p in census["profiles"]}
    short_ids = Counter(plan_arm(pid) for pid in profiles)
    if any(n > 1 for n in short_ids.values()):
        raise ConversionError("plan id prefixes collide; lengthen plan_arm")

    records: list[ForkRecord | TrajectoryRecord] = []
    skipped: Counter[str] = Counter()
    arm_info: dict[str, dict[str, Any]] = {}
    fixed_spec = fixed_arm_spec(reference_model)
    probe_pair_ids = set()
    archived_vs_s2: Counter[str] = Counter()

    for probe in probes:
        pair_id = probe.get("pair_id")
        probe_pair_ids.add(pair_id)
        source = sources.get(pair_id)
        pair_events = events.get(pair_id, {})
        planned = pair_events.get("planned")
        if source is None:
            skipped["no_source_call_for_pair"] += 1
            continue
        context = (planned or {}).get("reply", {}).get("result", {}).get("agentc_exploration_context")
        if context is None:
            skipped["no_planned_candidate"] += 1
            continue
        candidate = context["candidate_plan"]
        observed = candidate["agentc_observation_context"]
        plan_key = observed["key"]
        plan_id = plan_key["execution_plan_id"]
        feedback = pair_events.get("feedback", {}).get("reply", {}).get("result", {})
        if feedback.get("observation", {}).get("key") != plan_key:
            skipped["feedback_plan_key_mismatch"] += 1
            continue
        profile = profiles.get(plan_id)
        if profile is None:
            skipped["plan_not_in_census"] += 1
            continue
        rules = _candidate_rules(candidate)
        if tuple(profile["rules"]) != rules or profile["stage"] != source["workflow_stage"]:
            skipped["plan_rules_or_stage_mismatch"] += 1
            continue
        target_model = observed["runtime_version"]["target_model_id"]
        if probe["model"] != target_model:
            skipped["probe_model_differs_from_plan"] += 1
            continue
        if probe["task_id"] != source["task_id"] or probe["workflow_stage"] != source["workflow_stage"]:
            skipped["probe_source_identity_mismatch"] += 1
            continue

        stage = source["workflow_stage"]
        composite = fixed_composite and FIXED_ARM_CHANGES[stage] == (rules, target_model)
        family = plan_family(rules)
        plan_identity = {
            "execution_plan_id": plan_id,
            "call_site_version": plan_key["call_site_version"],
            "call_site_id": observed["call_site_id"],
            "runtime_version": observed["runtime_version"],
            "rules": list(rules),
            "workflow_stage": stage,
        }
        if composite:
            arm, spec = FIXED_ARM, fixed_spec
        else:
            arm = plan_arm(plan_id)
            spec = ChangeSpec(change_id=arm, family=family, params=plan_identity)
        info = arm_info.setdefault(
            arm,
            {
                "family": spec.family,
                "change_identity": spec.identity,
                "plans": {},
                "pairs": 0,
            },
        )
        info["pairs"] += 1
        info["plans"].setdefault(
            plan_id,
            {
                **plan_identity,
                "model_label": profile["model_label"],
                "family_rule": family,
                "family_ambiguous": "ModelDowngrade" in rules and bool(CONTEXT_RULES & set(rules)),
                "archived_disagreements": profile["disagreements"],
                "archived_paired_observations": profile["paired_observations"],
            },
        )

        baseline = response_from_call(source)
        fork = response_from_call(probe)
        signals = compute_signals(baseline, fork, markers=MARKERS, embed=embed)
        archived = pair_events.get("feedback_claim", {}).get("divergence")
        signals = replace(
            signals,
            components={
                **signals.components,
                "archived_divergence": archived,
                "archived_divergence_threshold": observed.get("divergence_threshold"),
            },
        )
        if archived is not None and signals.S2 is not None:
            archived_vs_s2[f"archived={archived:g},S2>0={signals.S2 > 0}"] += 1
        complete = pair_events.get("complete", {})
        records.append(
            ForkRecord(
                key=CallKey(
                    protocol=PROTOCOL,
                    stage=STAGE,
                    workload=WORKLOAD,
                    model=reference_model,
                    task_id=source["task_id"],
                    repetition=0,
                    call_index=WORKFLOW_STAGES.index(stage),
                ),
                arm=arm,
                change_identity=spec.identity,
                change_applied=True,
                change_evidence={
                    "source": "archived native off-path probe",
                    "plan": plan_identity,
                    "candidate_kind": candidate.get("kind"),
                    "target_model": target_model,
                    "composite_of_fixed_arm": composite,
                    "pair_id": pair_id,
                    "source_call_id": source["id"],
                    "probe_call_id": probe["id"],
                    "application_text": complete.get("application_text"),
                },
                baseline=baseline,
                fork=fork,
                signals=signals,
                baseline_request_tokens=baseline.input_tokens,
            )
        )

    planned_candidates = {
        pid
        for pid, ev in events.items()
        if ev.get("planned", {}).get("reply", {}).get("result", {}).get("agentc_exploration_context")
    }

    # -- trajectories ---------------------------------------------------------
    analysis = {row["task_id"]: row for row in data["analysis"]["paired_tasks"]}
    labels = data["labels"]
    label_mismatch: list[dict[str, Any]] = []
    token_mismatch: list[str] = []
    trajectories = 0
    for task_id in study_ids:
        cells = analysis.get(task_id, {}).get("cells")
        if cells is None:
            raise ConversionError(f"analysis.json has no score for {task_id}")
        for arm_name, rows, out_arm in (
            (REFERENCE_ARM, source_rows.get(task_id), BASELINE_ARM),
            (HR_ARM, hr_rows.get(task_id), FIXED_ARM),
        ):
            if rows is None:
                raise ConversionError(f"{arm_name} workflow missing for {task_id}")
            cell = cells[arm_name]
            responses = [response_from_call(r) for r in rows]
            reward = float(cell["exact_accuracy"])
            # Independent cross-check of the archived score against the label.
            expected = labels[task_id]["expected"]
            answer_ok = _label_scalar(rows[-1]["answer"]) == expected
            if answer_ok != (reward == 1.0):
                label_mismatch.append(
                    {"task_id": task_id, "arm": arm_name, "reward": reward, "answer_matches_label": answer_ok}
                )
            in_tok = sum(r.input_tokens or 0 for r in responses)
            out_tok = sum(r.output_tokens or 0 for r in responses)
            if (in_tok, out_tok) != (cell["prompt_tokens"], cell["completion_tokens"]):
                token_mismatch.append(f"{task_id}/{arm_name}")
            invalid = [s for s, ok in cell["valid_by_stage"].items() if not ok]

            def total(field: str) -> int | None:
                values = [getattr(r, field) for r in responses]
                return None if any(v is None for v in values) else sum(values)

            costs = [r.cost_usd for r in responses]
            changed = 0
            if out_arm == FIXED_ARM:
                changed = sum(1 for r in rows if r["model"] != reference_model)
            records.append(
                TrajectoryRecord(
                    protocol=PROTOCOL,
                    stage=STAGE,
                    workload=WORKLOAD,
                    model=reference_model,
                    task_id=task_id,
                    repetition=0,
                    arm=out_arm,
                    change_identity=fixed_spec.identity if out_arm == FIXED_ARM else None,
                    reward=reward,
                    model_calls=len(rows),
                    tool_calls=sum(len(r.tool_calls) for r in responses),
                    eligible_calls=len(rows),
                    changed_calls=changed,
                    termination="completed" if not invalid else "invalid_stage:" + ",".join(sorted(invalid)),
                    cost_usd=None if any(c is None for c in costs) else round(sum(costs), 9),  # type: ignore[arg-type]
                    input_tokens=total("input_tokens"),
                    output_tokens=total("output_tokens"),
                    cached_input_tokens=total("cached_input_tokens"),
                    cache_creation_input_tokens=total("cache_creation_input_tokens"),
                    action_trace=tuple(action_signature(r) for r in responses),
                )
            )
            trajectories += 1

    fork_records = [r for r in records if isinstance(r, ForkRecord)]
    traj_records = [r for r in records if isinstance(r, TrajectoryRecord)]
    rewards = defaultdict(list)
    for t in traj_records:
        rewards[t.arm].append(t.reward)
    report = {
        "stage": STAGE,
        "protocol": PROTOCOL,
        "workload": WORKLOAD,
        "reference_model": reference_model,
        "development_evidence_only": True,
        "fixed_composite": fixed_composite,
        "tasks": len(study_ids),
        "archived_call_counts": dict(Counter(r["arm"] for r in calls)),
        "probe_pairs": {
            "archived": len(probes),
            "converted": len(fork_records),
            "skipped": sum(skipped.values()),
            "skipped_reasons": dict(skipped),
            "source_calls_without_probe": len(sources) - len(probe_pair_ids & set(sources)),
            "planned_candidates_without_probe_call": len(planned_candidates - probe_pair_ids),
        },
        "trajectories": {
            "records": trajectories,
            "by_arm": {arm: len(v) for arm, v in sorted(rewards.items())},
            "correct_by_arm": {arm: sum(v) for arm, v in sorted(rewards.items())},
            "reward_source": "analysis.json paired_tasks[].cells[arm].exact_accuracy",
            "label_cross_check_mismatches": label_mismatch,
            "token_sum_mismatches_vs_analysis": token_mismatch,
        },
        "arms": {arm: arm_info[arm] for arm in sorted(arm_info)},
        "fixed_arm": {
            "arm": FIXED_ARM,
            "change_identity": fixed_spec.identity,
            "family": fixed_spec.family,
            "per_stage": fixed_spec.params["per_stage"],
        },
        "families": {**{a: i["family"] for a, i in arm_info.items()}, FIXED_ARM: fixed_spec.family},
        "aa_control": {
            "present": False,
            "reason": (
                "Every archived probe ran a non-identity candidate plan (ContextCompress, "
                "ModelDowngrade, StateDrop or a composition); the source block ran each task "
                "once and the pilot used disjoint tasks. No identical-request repeat exists, "
                "so H1 (A/A floor) cannot be estimated from Stage R and no C0 record is emitted."
            ),
        },
        "archived_divergence_vs_recomputed_S2": dict(archived_vs_s2),
        "ambiguities": [
            "Baseline and fork are the raw provider texts. The application saw a canonicalized "
            "text (filter ID sets sorted); for 4 source calls the two differ only in ID order.",
            "Responses are whitespace-free JSON, so S1 (whitespace tokens) is 1 for any textual "
            "difference, including a reordered filter set that the application treats as equal.",
            "The workload has no tools and no termination markers: S4 is 0 for every pair "
            "(text vs text) and S5 reduces to the output-token ratio.",
            "Plans combining ModelDowngrade with a context rule are assigned family model_swap "
            "by declared rule; they are marked family_ambiguous in arms[].plans.",
            "Fork arms are native candidate plans and none equals the HR arm, so without "
            "fixed_composite no cell has both fork signals and trajectory damage.",
            "Trajectory model is the reference model for both arms so baseline and HR pair in one "
            "(workload, model) group; the HR arm actually served anthropic/claude-haiku-4.5.",
            "Probe assignment was adaptive (native exploration with per-site caps and two plans "
            "disabled by the exposure guard), not a hash-randomized fork assignment.",
            "The 6 pilot tasks are not converted: their accuracy was never reported by the "
            "original analysis and they have no probes.",
        ],
    }
    return records, report


def run(
    output_dir: str | Path,
    *,
    ref: str | None = DEFAULT_REF,
    repo: str | Path = REPO_ROOT,
    study_dir: str = DEFAULT_STUDY_DIR,
    labels_dir: str = DEFAULT_LABELS_DIR,
    embed: bool = True,
    fixed_composite: bool = False,
) -> dict[str, Any]:
    """Convert the archive and write ``records.jsonl`` and ``conversion_report.json``."""
    data = load(Archive(ref, repo), study_dir, labels_dir)
    records, report = convert(data, embed=embed, fixed_composite=fixed_composite)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "records.jsonl", "w", encoding="utf-8") as stream:
        for record in records:
            stream.write(to_line(record) + "\n")
    report = {
        **report,
        "source": {"ref": ref, "study_dir": study_dir, "labels_dir": labels_dir},
        "records_written": len(records),
    }
    (out / "conversion_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--ref", default=DEFAULT_REF, help="git ref holding the archive ('' reads the working tree)")
    parser.add_argument("--study-dir", default=DEFAULT_STUDY_DIR)
    parser.add_argument("--labels-dir", default=DEFAULT_LABELS_DIR)
    parser.add_argument("--no-embed", action="store_true", help="skip S3 (embedding) computation")
    parser.add_argument(
        "--fixed-composite",
        action="store_true",
        help="label probes of the HR arm's per-stage change with the HR arm",
    )
    args = parser.parse_args(argv)
    report = run(
        args.out,
        ref=args.ref or None,
        study_dir=args.study_dir,
        labels_dir=args.labels_dir,
        embed=not args.no_embed,
        fixed_composite=args.fixed_composite,
    )
    print(json.dumps({k: report[k] for k in ("tasks", "probe_pairs", "trajectories", "records_written")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
