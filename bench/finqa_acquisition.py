"""Pinned local FinQA acquisition, ID checkpoint, and separate sealed labels.

No provider transport or scoring. Test data is read only for published-example
report exclusions; enrollment uses train/dev. CLI output contains hashes/counts.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import subprocess
import unicodedata
import urllib.request
from collections import Counter
from pathlib import Path

from bench.finqa_selection import select_ids
from bench.finqa_workload import (
    ContractError,
    canonical,
    digest,
    public_task,
    report_id,
)

ROOT = Path(__file__).resolve().parents[1]
VERSION = "finqa-local-acquisition-v1"
REVISION = "0f16e2867befa6840783e58be38c9efb9229d742"
SEED = 2026090901
CALIBRATION_COUNT, FORMAT_COUNT, EVALUATION_COUNT = 70, 6, 64
SOURCES = {
    "train": (
        "dataset/train.json",
        78216616,
        "a6c2c5a60f9abe3fba5a793cc4d9e1dee2e09209",
        6251,
    ),
    "dev": (
        "dataset/dev.json",
        10954658,
        "970f239f591c782b17df420c0d343b780737f3da",
        883,
    ),
    "test": (
        "dataset/test.json",
        14395143,
        "59958c7c3bb3b21f4dff6bc912a0fe0ae710aee0",
        1147,
    ),
    "license": ("LICENSE", 1067, "fb3b513238ba5ec7e0799cb1190ff30c759a5bdf", None),
    "readme": ("README.md", 6518, "8fc89f8402714c39385a80c3d7d5e947ae27fcab", None),
}
EXPOSURES = "bench/repro/finqa-cohort-2026-09-09/exposures.json"
CLOSURE = (
    "bench/finqa_acquisition.py",
    "bench/finqa_selection.py",
    "bench/finqa_workload.py",
    "python/agentc/_attention.py",
    EXPOSURES,
)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def normalize_text(question: str) -> str:
    normalized = unicodedata.normalize("NFKC", question).casefold()
    return "".join(c for c in normalized if c.isalnum())


def question_hash(question: str) -> str:
    """Normalize typography only, never paraphrase or match using gold fields."""
    return sha256(normalize_text(question).encode())


def read_json(raw: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError("duplicate JSON key")
            result[key] = value
        return result

    def invalid(_):
        raise ContractError("nonfinite JSON value")

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
    except (ValueError, UnicodeError):
        raise ContractError("invalid acquisition JSON") from None


def outside_repository(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT.resolve()):
        raise ContractError("corpus cache and bundles must be outside the checkout")
    # Also reject another worktree/repository: ignored corpus is still local only.
    if any((p / ".git").exists() for p in (resolved, *resolved.parents)):
        raise ContractError("corpus cache and bundles must be outside Git repositories")
    return resolved


def write_new(path: Path, raw: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def json_bytes(value) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()


def source_bytes(cache: Path, key: str) -> bytes:
    path, size, blob, _ = SOURCES[key]
    raw = (cache / path).read_bytes()
    actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if len(raw) != size or actual != blob:
        raise ContractError("pinned source size or Git blob mismatch")
    return raw


def download(cache: Path) -> dict:
    cache = outside_repository(cache)
    cache.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(cache, 0o700)
    result = {}
    for key, (path, size, _, _) in SOURCES.items():
        target = cache / path
        target.parent.mkdir(mode=0o700, exist_ok=True)
        if not target.exists():
            url = f"https://raw.githubusercontent.com/czyssrs/FinQA/{REVISION}/{path}"
            with urllib.request.urlopen(url, timeout=60) as response:
                # Retain partial files on failure; never silently replace a cache.
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output:
                    total = 0
                    while block := response.read(1024 * 1024):
                        total += len(block)
                        if total > size:
                            raise ContractError("download exceeded pinned byte count")
                        output.write(block)
        result[key] = sha256(source_bytes(cache, key))
    return {"revision": REVISION, "source_sha256": result, "provider_calls": 0}


def load_sources(cache: Path) -> tuple[dict, dict]:
    rows, provenance = {}, {}
    for key, (path, size, blob, count) in SOURCES.items():
        raw = source_bytes(cache, key)
        provenance[key] = {
            "path": path,
            "bytes": size,
            "git_blob_sha1": blob,
            "sha256": sha256(raw),
        }
        if count is not None:
            values = read_json(raw)
            if not isinstance(values, list) or len(values) != count:
                raise ContractError("source row count mismatch")
            ids = []
            for row in values:
                if not isinstance(row, dict) or not isinstance(row.get("qa"), dict):
                    raise ContractError("invalid source row envelope")
                report_id(row.get("id"))
                if not isinstance(row["qa"].get("question"), str):
                    raise ContractError("invalid public question")
                ids.append(row["id"])
            if len(set(ids)) != len(ids):
                raise ContractError("duplicate source IDs")
            rows[key] = values
    ids = [r["id"] for split in rows.values() for r in split]
    if len(ids) != len(set(ids)):
        raise ContractError("source split IDs overlap")
    return rows, provenance


def exposure_reports(rows: dict, spec: dict) -> dict:
    """Only IDs and public questions are consulted; no answer/program access."""
    known = set(spec["explicit_reports"])
    for report in known:
        report_id(
            report + "/page_0.pdf-0"
        )  # Validate report syntax, not an invented exposure ID.
    wanted = {
        entry["question_sha256"]: entry["location"]
        for entry in spec["question_matches"]
    }
    if len(wanted) != len(spec["question_matches"]):
        raise ContractError("duplicate exposure question fingerprints")
    matches = {key: [] for key in wanted}
    fragments = {entry["location"]: [] for entry in spec["public_fragments"]}
    for split, values in rows.items():
        for row in values:
            key = question_hash(row["qa"]["question"])
            if key in matches:
                matches[key].append({"split": split, "id": row["id"]})
                known.add(report_id(row["id"]))
            for entry in spec["public_fragments"]:
                needle = normalize_text(entry["text"])
                if any(
                    needle in normalize_text(text)
                    for text in row["pre_text"] + row["post_text"]
                ):
                    fragments[entry["location"]].append(
                        {"split": split, "id": row["id"]}
                    )
                    known.add(report_id(row["id"]))
    if any(not values for values in matches.values()):
        raise ContractError("unresolved published example; selection forbidden")
    if any(not values for values in fragments.values()):
        raise ContractError("unresolved published screenshot; selection forbidden")
    return {
        "reports": sorted(known),
        "question_matches": matches,
        "public_fragment_matches": fragments,
    }


def make_selection(cache: Path) -> dict:
    rows, sources = load_sources(cache)
    spec = read_json((ROOT / EXPOSURES).read_bytes())
    exposures = exposure_reports(rows, spec)
    excluded = sorted(
        r["id"]
        for split in rows.values()
        for r in split
        if report_id(r["id"]) in exposures["reports"]
    )
    selected = select_ids(
        [r["id"] for r in rows["train"]],
        [r["id"] for r in rows["dev"]],
        calibration_count=CALIBRATION_COUNT,
        evaluation_count=EVALUATION_COUNT,
        seed=SEED,
        excluded_ids=excluded,
    )
    return {
        "kind": VERSION,
        "revision": REVISION,
        "source": sources,
        "code_sha256": {p: sha256((ROOT / p).read_bytes()) for p in CLOSURE},
        "exposure": exposures,
        "selection": selected,
        "format_ids": selected["calibration_ids"][:FORMAT_COUNT],
        "learning_ids": selected["calibration_ids"][FORMAT_COUNT:],
        "test_split_role": "exposure lookup only; never enrolled",
        "scope": "local development cohort; not a power claim or paid protocol",
        "labels_used_for_selection": False,
        "provider_calls": 0,
    }


def committed_selection(selection: Path, checkpoint: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", checkpoint):
        raise ContractError("full reviewed checkpoint commit required")
    relative = selection.resolve().relative_to(ROOT.resolve()).as_posix()
    for name in (relative, *CLOSURE):
        result = subprocess.run(
            ["git", "show", f"{checkpoint}:{name}"],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if result.returncode or result.stdout != (ROOT / name).read_bytes():
            raise ContractError("selection or source differs from reviewed checkpoint")
    return read_json(selection.read_bytes())


def materialize(selection: Path, checkpoint: str, cache: Path, output: Path) -> dict:
    frozen = committed_selection(selection, checkpoint)
    if frozen != make_selection(cache):
        raise ContractError("selection cannot be reproduced from pinned sources")
    output = outside_repository(output)
    rows, _ = load_sources(cache)
    inventory = {r["id"]: r for key in ("train", "dev") for r in rows[key]}
    ids = frozen["selection"]["calibration_ids"] + frozen["selection"]["evaluation_ids"]
    tasks = [public_task(inventory[key]) for key in ids]
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    public_raw = json_bytes(tasks)
    write_new(output / "public.json", public_raw)
    # The selected public projection is durable before accessing any exe_ans.
    labels = {key: inventory[key]["qa"]["exe_ans"] for key in ids}
    if any(
        not (
            (type(v) in (int, float) and math.isfinite(v))
            or (type(v) is str and v in ("yes", "no"))
        )
        for v in labels.values()
    ):
        raise ContractError("unsupported selected label type; do not resample")
    label_raw = json_bytes(labels)
    write_new(output / "labels.sealed.json", label_raw)
    os.chmod(output / "labels.sealed.json", 0o000)
    write_new(output / "selection.json", selection.read_bytes())
    result = {
        "kind": VERSION,
        "selection_checkpoint": checkpoint,
        "selection_sha256": sha256(selection.read_bytes()),
        "public_sha256": sha256(public_raw),
        "labels_sha256": sha256(label_raw),
        "calibration_count": len(frozen["selection"]["calibration_ids"]),
        "evaluation_count": len(frozen["selection"]["evaluation_ids"]),
        "labels_state": "unscored; permission 000 is a procedural seal, not encryption",
        "external_provider_sharing": "not authorized by this local acquisition",
        "provider_calls": 0,
    }
    write_new(output / "provenance.json", json_bytes(result))
    return result


def public_census(cache: Path) -> dict:
    """All train/dev rows remain in the census, including schema failures."""
    rows, sources = load_sources(cache)
    summary = {}
    for key in ("train", "dev"):
        sizes, invalid = [], 0
        for row in rows[key]:
            try:
                sizes.append(len(canonical(public_task(row)).encode()))
            except ContractError:
                invalid += 1
        sizes.sort()
        summary[key] = {
            "rows": len(rows[key]),
            "valid_public_rows": len(sizes),
            "invalid_public_rows": invalid,
            "public_bytes_min": min(sizes) if sizes else None,
            "public_bytes_median": sizes[len(sizes) // 2] if sizes else None,
            "public_bytes_p95": sizes[math.ceil(0.95 * len(sizes)) - 1]
            if sizes
            else None,
            "public_bytes_max": max(sizes) if sizes else None,
        }
    return {
        "source": sources,
        "splits": summary,
        "labels_used": False,
        "provider_calls": 0,
        "bytes_are_not_provider_tokens_or_capacity": True,
    }


def native_census(bundle: Path, binary: Path, expected_sha256: str) -> dict:
    """Actual native retrieve-stage proposals, not admission or quality evidence."""
    from bench.finqa_workload import call

    attention_path = ROOT / "python/agentc/_attention.py"
    spec = importlib.util.spec_from_file_location("finqa_attention", attention_path)
    attention = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(attention)

    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or sha256(binary.read_bytes()) != expected_sha256
    ):
        raise ContractError("native binary SHA mismatch")
    provenance = read_json((bundle / "provenance.json").read_bytes())
    raw = (bundle / "public.json").read_bytes()
    selected_raw = (bundle / "selection.json").read_bytes()
    if (
        sha256(raw) != provenance["public_sha256"]
        or sha256(selected_raw) != provenance["selection_sha256"]
    ):
        raise ContractError("public bundle hash mismatch")
    selected = read_json(selected_raw)["selection"]
    tasks = read_json(raw)
    if [t["id"] for t in tasks] != selected["calibration_ids"] + selected[
        "evaluation_ids"
    ]:
        raise ContractError("public bundle inventory mismatch")
    counts = {rule: Counter() for rule in ("ContextCompress", "StateDrop")}
    records = []
    for task in tasks:
        request = call(
            task,
            "retrieve",
            {},
            attention,
            model="anthropic/claude-sonnet-4-20250514",
            run_identity=VERSION,
        )
        for rule, counter in counts.items():
            result = subprocess.run(
                [str(binary)],
                input=canonical({"rule": rule, "call": request}).encode(),
                capture_output=True,
                timeout=15,
                check=False,
            )
            if result.returncode:
                raise ContractError("native census rejected a selected request")
            value = read_json(result.stdout)
            retained = value["retained_indices"]
            if value.get("materialization_only") is not True or value["call"][
                "messages"
            ] != [request["messages"][i] for i in retained]:
                raise ContractError("invalid native materialization receipt")
            status = "applied" if value["applied"] else value["abstention_reason"]
            counter[status] += 1
            records.append(
                {
                    "id": task["id"],
                    "rule": rule,
                    "status": status,
                    "removed_messages": len(request["messages"]) - len(retained),
                }
            )
    return {
        "native_sha256": expected_sha256,
        "public_sha256": provenance["public_sha256"],
        "stage": "retrieve",
        "selected_tasks": len(tasks),
        "counts": {k: dict(v) for k, v in counts.items()},
        "records": records,
        "program_stage": "unmeasured; requires actual arm-specific retrieval output",
        "admission_or_quality_evidence": False,
        "labels_used": False,
        "provider_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("download", "select", "materialize", "census", "native-census"),
    )
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--checkpoint")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--binary-sha256")
    args = parser.parse_args()
    try:
        if args.command == "download":
            value = download(args.cache)
        elif args.command == "select":
            value = make_selection(args.cache)
            write_new(args.output, json_bytes(value))
        elif args.command == "materialize":
            value = materialize(
                args.selection, args.checkpoint, args.cache, args.output
            )
        elif args.command == "census":
            value = public_census(args.cache)
            write_new(args.output, json_bytes(value))
        else:
            value = native_census(args.bundle, args.binary, args.binary_sha256)
            write_new(args.output, json_bytes(value))
        print(
            canonical(
                {
                    "command": args.command,
                    "result_sha256": digest(value),
                    "provider_calls": 0,
                }
            )
        )
    except (
        ContractError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        subprocess.SubprocessError,
    ):
        parser.exit(
            1,
            "FinQA acquisition rejected; inspect inputs without printing corpus or labels.\n",
        )


if __name__ == "__main__":
    main()
