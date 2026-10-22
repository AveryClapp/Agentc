"""Format diagnostics must handle shared reference pages and missing tools."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from bench.repro import build_submission


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        (["Introduction", "Conclusion    R EFERENCES\n"], 2),
        (["Body", "R E F E R E N C E S    continuing bibliography entry"], 2),
        (["REFERENCES", "REFERENCES"], None),
        (["Background: crossreferences", "Conclusion"], None),
    ],
)
def test_reference_heading_with_two_columns(pages, expected):
    assert build_submission.references_start_page(pages) == expected


def test_format_audit_keeps_shared_page_and_reports_font_defects(tmp_path, monkeypatch):
    (tmp_path / "main.log").write_text(
        "Overfull \\hbox (10pt too wide)\n"
        "LaTeX Warning: Citation `missing' on page 2 undefined\n"
    )
    monkeypatch.setattr(build_submission.shutil, "which", lambda name: f"/bin/{name}")
    reports = {
        "pdfinfo": "Author: Anonymous Authors\nPages: 27\n",
        "pdffonts": (
            "name type encoding emb sub uni object ID\n"
            "---------------------------------------\n"
            "[none] Type 3 Custom yes no yes 8 0\n"
            "Times-Roman Type 1 WinAnsi no no no 9 0\n"
        ),
        "pdftotext": "\f".join(
            ["Body"] * 23
            + ["R EFERENCES    ing Research, 2024. URL https://openreview.n"]
            + ["Reference entries"] * 3
        ),
    }

    def run(command, **kwargs):
        return CompletedProcess(command, 0, stdout=reports[command[0]], stderr="")

    monkeypatch.setattr(build_submission.subprocess, "run", run)
    result = build_submission.inspect_format(tmp_path, "main")
    assert result["references_start_page"] == 24
    assert result["body_page_count"] is None
    assert result["body_page_bounds"] == [23, 24]
    assert result["ten_page_body_definitely_exceeded"] is True
    assert result["overfull_box_warning_count"] == 1
    assert result["undefined_citation_or_reference_warning_count"] == 1
    assert result["type3_font_count"] == 1
    assert len(result["unembedded_font_rows"]) == 1
    assert result["author_metadata"] == "Anonymous Authors"


@pytest.mark.parametrize(
    ("boundary_page", "expected"), [(9, 8), (8, None), (None, None)]
)
def test_explicit_reference_boundary_must_agree_with_pdf(
    tmp_path, monkeypatch, boundary_page, expected
):
    (tmp_path / "main.log").write_text("")
    if boundary_page is not None:
        (tmp_path / "main.aux").write_text(
            rf"\newlabel{{page:references-start}}{{{{9}}{{{boundary_page}}}{{}}{{}}{{}}}}"
        )
    monkeypatch.setattr(build_submission.shutil, "which", lambda name: f"/bin/{name}")
    reports = {
        "pdfinfo": "Author: Anonymous Authors\nPages: 10\n",
        "pdffonts": "name type encoding emb sub uni object ID\n----\n",
        "pdftotext": "\f".join(["Body"] * 8 + ["REFERENCES", "Entries"]),
    }

    def run(command, **kwargs):
        return CompletedProcess(command, 0, stdout=reports[command[0]], stderr="")

    monkeypatch.setattr(build_submission.subprocess, "run", run)
    result = build_submission.inspect_format(tmp_path, "main")
    assert result["body_page_count"] == expected
    assert result["body_page_bounds"] == ([8, 8] if expected == 8 else [8, 9])
    assert result["ten_page_body_definitely_exceeded"] is False


def test_missing_poppler_does_not_claim_pdf_inspection(tmp_path, monkeypatch):
    (tmp_path / "main.log").write_text("")
    monkeypatch.setattr(build_submission.shutil, "which", lambda name: None)

    def forbidden(*args, **kwargs):
        pytest.fail("PDF inspection must not run without Poppler")

    monkeypatch.setattr(build_submission.subprocess, "run", forbidden)
    result = build_submission.inspect_format(Path(tmp_path), "main")
    assert result["pdf_inspected"] is False
    assert set(result["missing_inspection_tools"]) == {
        "pdfinfo",
        "pdffonts",
        "pdftotext",
    }
    assert "body_page_bounds" not in result
