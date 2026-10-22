"""Protect paired statistics from malformed or duplicated observations."""

from __future__ import annotations

from pathlib import Path

import pytest

from bench.paired_analysis import _load, main, summarize


HEADER = "agent_module,config,task_id,baseline_passed,optimized_passed\n"


def test_load_valid_pairs_and_group_task_ids(tmp_path: Path) -> None:
    path = tmp_path / "pairs.csv"
    path.write_text(
        HEADER
        + "agent,cc,t1,1,1\n"
        + "agent,cc,t2,1,0\n"
        + "agent,cc,t3,0,1\n"
        + "agent,cc,t4,0,0\n"
        + "agent,sd,t1,1,0\n"
        + "other,cc,t1,0,1\n"
    )

    grouped = _load(path)
    summary = summarize("agent", "cc", grouped[("agent", "cc")])
    assert summary.n == 4
    assert (summary.n_BB, summary.n_BF, summary.n_FB, summary.n_FF) == (1, 1, 1, 1)
    assert summary.delta_pp == 0.0
    assert summary.mcnemar_p == 1.0
    assert grouped[("agent", "sd")] == [("t1", True, False)]
    assert grouped[("other", "cc")] == [("t1", False, True)]


@pytest.mark.parametrize("second_pair", ["1,0", "0,1"])
def test_duplicate_tasks_cannot_inflate_sample_count(
    tmp_path: Path, second_pair: str,
) -> None:
    path = tmp_path / "pairs.csv"
    path.write_text(HEADER + "agent,cc,t1,1,0\n" + f"agent,cc,t1,{second_pair}\n")

    with pytest.raises(ValueError, match="line 3: duplicate task"):
        _load(path)


@pytest.mark.parametrize("field", ["baseline_passed", "optimized_passed"])
@pytest.mark.parametrize("invalid", ["2", "-1", "true", "0.5"])
def test_pass_flags_must_be_binary(tmp_path: Path, field: str, invalid: str) -> None:
    path = tmp_path / "pairs.csv"
    values = {"baseline_passed": "1", "optimized_passed": "0", field: invalid}
    path.write_text(
        HEADER + f"agent,cc,t1,{values['baseline_passed']},{values['optimized_passed']}\n"
    )

    with pytest.raises(ValueError, match=f"line 2: {field} must be 0 or 1"):
        _load(path)


@pytest.mark.parametrize("field_index", range(5))
def test_missing_values_are_rejected(tmp_path: Path, field_index: int) -> None:
    path = tmp_path / "pairs.csv"
    values = ["agent", "cc", "t1", "1", "0"]
    values[field_index] = " "
    path.write_text(HEADER + ",".join(values) + "\n")

    with pytest.raises(ValueError, match="line 2: missing"):
        _load(path)


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("", "missing required columns"),
        (HEADER, "no paired task observations"),
        ("agent_module,config,task_id,baseline_passed\n", "optimized_passed"),
        (HEADER.rstrip() + ",task_id\n", "duplicate column names"),
        (HEADER + "agent,cc,t1,1\n", "missing optimized_passed"),
        (HEADER + "agent,cc,t1,1,0,extra\n", "more values than columns"),
    ],
)
def test_invalid_csv_shape_is_rejected(
    tmp_path: Path, contents: str, message: str,
) -> None:
    path = tmp_path / "pairs.csv"
    path.write_text(contents)

    with pytest.raises(ValueError, match=message):
        _load(path)


def test_extra_named_columns_remain_compatible(tmp_path: Path) -> None:
    path = tmp_path / "pairs.csv"
    path.write_text(HEADER.rstrip() + ",duration_ms\nagent,cc,t1,1,0,10\n")

    assert _load(path)[("agent", "cc")] == [("t1", True, False)]


def test_cli_rejects_bad_data_without_writing_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "pairs.csv"
    path.write_text(HEADER + "agent,cc,t1,1,0\nagent,cc,t1,1,0\n")
    output = tmp_path / "summary.csv"
    output.write_text("existing result\n")

    with pytest.raises(SystemExit) as exc:
        main([str(path), "--out", str(output)])

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "duplicate task" in captured.err
    assert captured.out == ""
    assert output.read_text() == "existing result\n"
