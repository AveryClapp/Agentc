"""ID-only prospective selection. No corpus download, gold, or provider calls."""

from __future__ import annotations

from bench.finqa_workload import ContractError, digest, report_id

VERSION = "finqa-report-disjoint-id-selection-v1"


def select_ids(
    calibration_ids: list[str],
    evaluation_ids: list[str],
    *,
    calibration_count: int,
    evaluation_count: int,
    seed: int,
    excluded_ids: list[str],
) -> dict:
    """Return a deterministic, report-disjoint predeclaration from source IDs.

    Source revision/raw-file hashes must additionally be frozen by the future
    acquisition manifest. This function is not an acquisition authorization.
    Excluding an exposed ID excludes its entire company/year report. A report
    may have multiple questions in one partition: analysis must cluster by it.
    """
    pools = (calibration_ids, evaluation_ids, excluded_ids)
    for pool in pools:
        if not isinstance(pool, list):
            raise ContractError("ID inventories must be duplicate-free lists")
        for task_id in pool:
            report_id(task_id)
        if len(pool) != len(set(pool)):
            raise ContractError("ID inventories must be duplicate-free lists")
    if set(calibration_ids) & set(evaluation_ids):
        raise ContractError("source split IDs overlap")
    if (
        type(seed) is not int
        or type(calibration_count) is not int
        or type(evaluation_count) is not int
        or calibration_count < 1
        or evaluation_count < 1
    ):
        raise ContractError("integer seed and positive counts required")
    excluded_reports = {report_id(task_id) for task_id in excluded_ids}

    def ranked(pool, partition, blocked):
        return sorted(
            (task_id for task_id in pool if report_id(task_id) not in blocked),
            key=lambda task_id: (digest([VERSION, seed, partition, task_id]), task_id),
        )

    calibration = ranked(calibration_ids, "calibration", excluded_reports)[
        :calibration_count
    ]
    blocked = excluded_reports | {report_id(task_id) for task_id in calibration}
    evaluation = ranked(evaluation_ids, "evaluation", blocked)[:evaluation_count]
    if len(calibration) != calibration_count or len(evaluation) != evaluation_count:
        raise ContractError(
            "insufficient report-disjoint IDs; do not silently shrink or reseed"
        )
    return {
        "version": VERSION,
        "seed": seed,
        "source_inventory_sha256": {
            "calibration": digest(sorted(calibration_ids)),
            "evaluation": digest(sorted(evaluation_ids)),
        },
        "excluded_ids": sorted(excluded_ids),
        "excluded_reports": sorted(excluded_reports),
        "calibration_ids": calibration,
        "evaluation_ids": evaluation,
        "analysis_cluster": "company/year report, not individual question",
        "labels_used": False,
        "provider_calls": 0,
    }
