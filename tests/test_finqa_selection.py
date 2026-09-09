"""Synthetic ID inventories only; no public corpus or labels."""

import pytest

from bench.finqa_selection import select_ids
from bench.finqa_workload import ContractError, report_id


CAL = [f"INVENTED{i}/1900/page_1.pdf-0" for i in range(10)]
EVAL = [f"INVENTED{i}/1900/page_2.pdf-0" for i in range(20)]


def select(cal=None, evaluation=None, **kwargs):
    return select_ids(
        CAL if cal is None else cal,
        EVAL if evaluation is None else evaluation,
        calibration_count=kwargs.pop("calibration_count", 4),
        evaluation_count=kwargs.pop("evaluation_count", 6),
        seed=kwargs.pop("seed", -2026090901),
        excluded_ids=kwargs.pop("excluded_ids", []),
        **kwargs,
    )


def test_order_invariant_hash_selection_and_report_disjointness():
    result = select()
    assert result == select(list(reversed(CAL)), list(reversed(EVAL)))
    assert not (
        {report_id(x) for x in result["calibration_ids"]}
        & {report_id(x) for x in result["evaluation_ids"]}
    )
    assert result["labels_used"] is False and result["provider_calls"] == 0
    assert "report" in result["analysis_cluster"]


def test_exposed_question_excludes_whole_report():
    result = select(excluded_ids=["INVENTED1/1900/page_99.pdf-8"])
    assert result["excluded_reports"] == ["INVENTED1/1900"]
    assert all(
        report_id(x) != "INVENTED1/1900"
        for x in result["calibration_ids"] + result["evaluation_ids"]
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seed": True},
        {"calibration_count": 0},
        {"evaluation_count": -1},
        {"evaluation_count": True},
        {"calibration_count": 999},
        {"evaluation_count": 999},
        {"excluded_ids": ["bad"]},
        {"excluded_ids": [CAL[0], CAL[0]]},
    ],
)
def test_fail_closed_no_shrinking_or_reseeding(kwargs):
    with pytest.raises(ContractError):
        select(**kwargs)


@pytest.mark.parametrize(
    "cal,evaluation",
    [
        ([CAL[0], CAL[0]], EVAL),
        (CAL, [CAL[0]] + EVAL),
        ("not-list", EVAL),
        ([{}], EVAL),
        ([1], EVAL),
        ([None], EVAL),
    ],
)
def test_bad_inventory(cal, evaluation):
    with pytest.raises(ContractError):
        select(cal, evaluation)
