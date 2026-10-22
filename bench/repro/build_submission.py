"""Build the anonymous main paper and separate appendix in isolated staging.

Requires an installed Tectonic or latexmk; no Python packages are required.
The output directory must be empty. Source files and compiler intermediates
stay outside the checkout, and the appendix reads the main paper's labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def references_start_page(pages: list[str]) -> int | None:
    # Poppler keeps the two columns on the same line. A reference heading
    # may share that line with body text or a continuing reference entry.
    heading = re.compile(r"\bR\s*E\s*F\s*E\s*R\s*E\s*N\s*C\s*E\s*S\b")
    matches = [index for index, page in enumerate(pages, 1) if heading.search(page)]
    return matches[0] if len(matches) == 1 else None


def inspect_format(output: Path, name: str) -> dict:
    """Record format diagnostics; these cannot certify scientific readiness."""
    log = (output / f"{name}.log").read_text(errors="replace")
    result = {
        "overfull_box_warning_count": len(re.findall(r"Overfull \\[hv]box", log)),
        "undefined_citation_or_reference_warning_count": len(
            re.findall(r"(?:Citation|Reference) [^\n]*undefined", log)
        ),
        "pdf_inspected": False,
    }
    required = ("pdfinfo", "pdffonts", "pdftotext")
    missing = [tool for tool in required if shutil.which(tool) is None]
    if missing:
        result["missing_inspection_tools"] = missing
        return result
    pdf = str(output / f"{name}.pdf")

    def capture(command: list[str]) -> str:
        return subprocess.run(
            command, capture_output=True, text=True, check=True
        ).stdout

    info = capture(["pdfinfo", pdf])
    fonts = capture(["pdffonts", pdf]).splitlines()[2:]
    pages = capture(["pdftotext", "-layout", pdf, "-"]).split("\f")
    author = re.search(r"^Author:\s+(.+)", info, re.M)
    result.update(
        pdf_inspected=True,
        page_count=int(re.search(r"^Pages:\s+(\d+)", info, re.M).group(1)),
        author_metadata=author.group(1) if author is not None else None,
        type3_font_count=sum("Type 3" in row for row in fonts),
        unembedded_font_rows=[row for row in fonts if row.split()[-5] != "yes"],
    )
    if name == "main":
        reference_start = references_start_page(pages)
        auxiliary = output / "main.aux"
        boundary = re.search(
            r"\\newlabel\{page:references-start\}\{\{[^{}]*\}\{(\d+)\}",
            auxiliary.read_text(errors="replace") if auxiliary.is_file() else "",
        )
        # The manuscript emits this label after flushing body floats and
        # starting a reference-only page. Require agreement with the PDF.
        exact_body_pages = (
            reference_start - 1
            if reference_start is not None
            and reference_start >= 2
            and boundary is not None
            and int(boundary.group(1)) == reference_start
            else None
        )
        result.update(
            references_start_page=reference_start,
            body_page_count=exact_body_pages,
            # References may share a page with the final body paragraphs.
            # Report bounds instead of silently excluding that entire page.
            body_page_bounds=[exact_body_pages, exact_body_pages]
            if exact_body_pages is not None
            else [reference_start - 1, reference_start]
            if reference_start is not None
            else None,
            ten_page_body_definitely_exceeded=exact_body_pages > 10
            if exact_body_pages is not None
            else reference_start > 11
            if reference_start is not None
            else None,
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engine", choices=("tectonic", "latexmk"), default="tectonic")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    compiler = shutil.which(args.engine)
    if compiler is None:
        parser.error(f"{args.engine} is not installed")
    sources = [
        Path(name)
        for name in (
            "main.tex",
            "appendix.tex",
            "paper-preamble.tex",
            "references.bib",
            "mlsys2025.sty",
            "mlsys2025.bst",
        )
    ]
    for source in tuple(sources):
        if not (repo / source).is_file():
            parser.error(f"missing source: {source}")
        if source.suffix == ".tex":
            sources.extend(
                Path(name)
                for name in re.findall(
                    r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}",
                    (repo / source).read_text(),
                )
            )
    sources = sorted(set(sources))
    for source in sources:
        if not (repo / source).resolve().is_relative_to(repo):
            parser.error(f"source outside checkout: {source}")
        if not (repo / source).is_file():
            parser.error(f"missing figure: {source}")
    output = args.output.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("output must be a new or empty directory")
    output.mkdir(parents=True, exist_ok=True)
    input_hashes = {}
    with tempfile.TemporaryDirectory(prefix="agentc-paper-") as temporary:
        staging = Path(temporary)
        for source in sources:
            destination = staging / source
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = (repo / source).read_bytes()
            destination.write_bytes(payload)
            input_hashes[str(source)] = hashlib.sha256(payload).hexdigest()
        for name in ("main", "appendix"):
            if args.engine == "tectonic":
                command = [
                    compiler,
                    "--keep-logs",
                    "--keep-intermediates",
                    f"{name}.tex",
                ]
            else:
                command = [
                    compiler,
                    "-pdf",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    f"{name}.tex",
                ]
            try:
                subprocess.run(command, cwd=staging, check=True)
            except subprocess.CalledProcessError:
                log = staging / f"{name}.log"
                if log.exists():
                    shutil.copyfile(log, output / log.name)
                parser.exit(1, f"error: {name} build failed; see {output}\n")
            for suffix in ("pdf", "log", "aux", "bbl", "blg"):
                artifact = staging / f"{name}.{suffix}"
                if artifact.is_file():
                    shutil.copyfile(artifact, output / artifact.name)
    manifest = {
        "engine": args.engine,
        "input_sha256": input_hashes,
        "format_audit": {
            name: inspect_format(output, name) for name in ("main", "appendix")
        },
        "artifact_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.iterdir())
            if path.is_file()
        },
        "submission_ready": False,
        "interpretation": "Compilation does not validate evidence, anonymity, layout, or page budget.",
    }
    (output / "build.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    print(f"Built main.pdf and appendix.pdf in {output}")
    print(json.dumps(manifest["format_audit"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
