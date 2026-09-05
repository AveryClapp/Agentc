"""Pinned, ID-first HotpotQA train fixture acquisition; no provider inference.

Selection and materialization are deliberately separate commands. Materialization
requires the exact selection file already committed to Git and an explicit
reviewed checkpoint commit. Never print question, answer, or paragraph content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any

from bench.openrouter_pilot import PilotError, canonical, digest

ROOT = Path(__file__).resolve().parents[1]
KIND = "hotpot-train-typed-fixture-v1"
REVISION = "1908d6afbbead072334abe2965f91bd2709910ab"
REPO = "hotpotqa/hotpot_qa"
TRAIN_ROWS = 90447
SEED = 2026090504
PILOT_COUNT, PRIMARY_COUNT = 6, 32
SHARDS = (
    {
        "path": "distractor/train-00000-of-00002.parquet",
        "bytes": 165624177,
        "sha256": "76d3bb3048a7cc73c1958107c0c5872a00d7e7d00c105b81e92f6769e7822e68",
    },
    {
        "path": "distractor/train-00001-of-00002.parquet",
        "bytes": 166162479,
        "sha256": "713661628434fbb19fff7392e2e321e4ed107e3c7c7784d0690946e5f722763f",
    },
)
PREVIEW_IDS = (
    "5a7272eb5542997f827839d7",
    "5a76a401554299373536012b",
    "5a7722d655429966f1a36c99",
    "5a7a06935542990198eaf050",
    "5a7d0db955429909bec76924",
    "5a7e36045542991319bc9440",
    "5a82171f5542990a1d231f4a",
    "5a83168855429966c78a6b2e",
    "5a832c3455429954d2e2ec41",
    "5a84dd955542997b5ce3ff79",
    "5a879ab05542996e4f30887e",
    "5a89372855429951533612e6",
    "5a8b63755542997f31a41cfe",
    "5a8d7341554299441c6b9fe5",
    "5a8efd3c55429918e830d179",
    "5ab345db55429969a97a8122",
    "5ab381b155429969a97a816b",
    "5ab698885542995eadef002a",
    "5aba66c855429939ce03dcdb",
    "5abd90545542996e802b47d7",
    "5ac2a5d455429921a00ab01b",
    "5ac2a912554299218029dae8",
    "5ac2c3545542990b17b1548b",
    "5adc1309554299438c868d3b",
    "5ade025e5542997dc790711e",
    "5adf44985542993a75d2646d",
    "5adf732a5542993a75d264e9",
    "5ae0d91e55429924de1b7198",
    "5ae3918b5542994393b9e709",
    "5ae3b4d05542992f92d82349",
    "5e9ecfc04957053f60648a3e",
    "62de689d86220b5cb895acea",
    "665ec5b0490242930eb94440",
    "66f8caead3186746f4524419",
    "69625a973527f984b1d0cec1",
    "6e519c9e41327dd543db817b",
)
PREVIEW_SHA256 = "3950ad79cf2ae3cf55f566bca3080a3e18e91d591345bdecf4a788851789b220"
ID_PATTERN = re.compile(r"(?:hotpot_)?([0-9a-f]{24})")


def file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def source() -> dict:
    return {
        "repo": REPO,
        "revision": REVISION,
        "config": "distractor",
        "split": "train",
        "rows": TRAIN_ROWS,
        "license": "CC-BY-SA-4.0",
        "shards": deepcopy(list(SHARDS)),
        "builder_sha256": file_hash(Path(__file__)),
    }


def _read_json(path: Path) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PilotError("duplicate fixture JSON key")
            result[key] = value
        return result

    def invalid(_):
        raise PilotError("nonfinite fixture JSON number")

    return json.loads(
        path.read_text(), object_pairs_hook=unique, parse_constant=invalid
    )


def _write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(
            json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
        )


def raw_id(value: Any) -> str:
    match = ID_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise PilotError("invalid HotpotQA ID")
    return match[1]


def collect_exclusions(manifests: list[Path], root: Path = ROOT) -> dict:
    """Read historical manifests only; extract complete ID-shaped strings only."""
    ids = set(PREVIEW_IDS)
    observed = []
    if digest(sorted(PREVIEW_IDS)) != PREVIEW_SHA256:
        raise PilotError("preview exclusion provenance changed")

    def visit(value):
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and ID_PATTERN.fullmatch(value):
            ids.add(raw_id(value))

    for path in sorted({p.resolve() for p in manifests}):
        if path.name != "manifest.json" or not path.is_relative_to(root.resolve()):
            raise PilotError("exclusions require repository manifest.json files")
        visit(_read_json(path))
        observed.append(
            {"path": str(path.relative_to(root.resolve())), "sha256": file_hash(path)}
        )
    if not observed:
        raise PilotError("historical manifest exclusions are required")
    return {
        "ids": sorted(ids),
        "sha256": digest(sorted(ids)),
        "manifests": observed,
        "preview_sha256": PREVIEW_SHA256,
    }


def verify_shards(cache: Path) -> list[Path]:
    result = []
    for shard in SHARDS:
        path = cache / Path(shard["path"]).name
        if (
            not path.is_file()
            or path.stat().st_size != shard["bytes"]
            or file_hash(path) != shard["sha256"]
        ):
            raise PilotError("pinned train shard size or SHA mismatch")
        result.append(path)
    return result


def download_shards(cache: Path) -> list[Path]:
    """Unauthenticated exact train URLs, bounded streaming, never shared HF cache."""
    cache.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(cache).free < sum(s["bytes"] for s in SHARDS) + 1024**3:
        raise PilotError("insufficient disk for bounded train acquisition")
    for shard in SHARDS:
        path = cache / Path(shard["path"]).name
        if path.exists():
            continue  # verify below; never overwrite or repair cached content
        url = (
            f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/{shard['path']}"
        )
        with (
            urllib.request.urlopen(url, timeout=60) as response,
            path.open("xb") as output,
        ):
            total = 0
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > shard["bytes"]:
                    raise PilotError("train download exceeded exact pinned size")
                output.write(block)
    return verify_shards(cache)


def read_ids(paths: list[Path], parquet=None) -> list[str]:
    if parquet is None:
        from pyarrow import parquet
    ids = []
    for path in paths:
        # No question/answer/supporting/context columns are requested at selection.
        ids.extend(parquet.read_table(path, columns=["id"])["id"].to_pylist())
    if (
        len(ids) != TRAIN_ROWS
        or len(set(ids)) != len(ids)
        or any(raw_id(x) != x for x in ids)
    ):
        raise PilotError("train ID universe is malformed")
    return sorted(ids)


def make_selection(ids: list[str], exclusions: dict) -> dict:
    if (
        len(ids) != TRAIN_ROWS
        or ids != sorted(set(ids))
        or any(raw_id(x) != x for x in ids)
    ):
        raise PilotError("selection requires the complete sorted ID universe")
    eligible = set(ids) - set(exclusions["ids"])
    ranked = sorted(
        eligible,
        key=lambda value: (
            digest(["hotpot-train-typed-v1", REVISION, SEED, value]),
            value,
        ),
    )
    selected = ["hotpot_" + value for value in ranked[: PILOT_COUNT + PRIMARY_COUNT]]
    if len(selected) != PILOT_COUNT + PRIMARY_COUNT:
        raise PilotError("insufficient unexposed train IDs")
    result = {
        "kind": KIND,
        "source": source(),
        "seed": SEED,
        "algorithm": "sha256-compact-json(namespace,revision,seed,raw-id)-then-id-v1",
        "universe": {"count": len(ids), "sha256": digest(ids)},
        "exclusions": deepcopy(exclusions),
        "pilot_ids": selected[:PILOT_COUNT],
        "primary_ids": selected[PILOT_COUNT:],
    }
    result["selection_sha256"] = digest(result)
    validate_selection(result)
    return result


def validate_selection(selection: dict) -> None:
    fields = {
        "kind",
        "source",
        "seed",
        "algorithm",
        "universe",
        "exclusions",
        "pilot_ids",
        "primary_ids",
        "selection_sha256",
    }
    if not isinstance(selection, dict) or set(selection) != fields:
        raise PilotError("unexpected selection fields")
    if selection["selection_sha256"] != digest(
        {k: v for k, v in selection.items() if k != "selection_sha256"}
    ):
        raise PilotError("selection digest mismatch")
    if (
        digest(selection["source"]) != digest(source())
        or selection["kind"] != KIND
        or type(selection["seed"]) is not int
        or selection["seed"] != SEED
    ):
        raise PilotError("selection source or seed changed")
    if (
        selection["algorithm"]
        != "sha256-compact-json(namespace,revision,seed,raw-id)-then-id-v1"
    ):
        raise PilotError("selection algorithm changed")
    universe = selection["universe"]
    if (
        set(universe) != {"count", "sha256"}
        or type(universe["count"]) is not int
        or universe["count"] != TRAIN_ROWS
        or not re.fullmatch(r"[0-9a-f]{64}", universe["sha256"])
    ):
        raise PilotError("invalid universe provenance")
    exclusions = selection["exclusions"]
    if (
        set(exclusions) != {"ids", "sha256", "manifests", "preview_sha256"}
        or exclusions["ids"] != sorted(set(exclusions["ids"]))
        or exclusions["sha256"] != digest(exclusions["ids"])
        or exclusions["preview_sha256"] != PREVIEW_SHA256
        or not set(PREVIEW_IDS) <= set(exclusions["ids"])
    ):
        raise PilotError("invalid exclusion provenance")
    for value in exclusions["ids"]:
        if raw_id(value) != value:
            raise PilotError("exclusions must use raw IDs")
    if not exclusions["manifests"] or any(
        set(item) != {"path", "sha256"}
        or Path(item["path"]).name != "manifest.json"
        or Path(item["path"]).is_absolute()
        or ".." in Path(item["path"]).parts
        or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        for item in exclusions["manifests"]
    ):
        raise PilotError("invalid historical manifest provenance")
    pilot, primary = selection["pilot_ids"], selection["primary_ids"]
    if (
        not isinstance(pilot, list)
        or not isinstance(primary, list)
        or len(pilot) != PILOT_COUNT
        or len(primary) != PRIMARY_COUNT
    ):
        raise PilotError("wrong prospective group sizes")
    selected = pilot + primary
    if len(set(selected)) != len(selected) or any(
        value != "hotpot_" + raw_id(value) or raw_id(value) in exclusions["ids"]
        for value in selected
    ):
        raise PilotError("overlapping, exposed, or malformed selected IDs")


def committed_selection(path: Path, checkpoint: str, root: Path = ROOT) -> dict:
    """Require exact Git object bytes, not a mutable working-tree approval flag."""
    if not re.fullmatch(r"[0-9a-f]{40}", checkpoint):
        raise PilotError("full reviewed checkpoint commit required")
    relative = path.resolve().relative_to(root.resolve())
    recorded = subprocess.run(
        ["git", "show", f"{checkpoint}:{relative}"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    if recorded != path.read_bytes():
        raise PilotError("selection differs from reviewed Git checkpoint")
    selected = _read_json(path)
    validate_selection(selected)
    return selected


def _selected_rows(
    paths: list[Path], selected: list[str], columns: list[str], parquet=None
) -> dict:
    if parquet is None:
        from pyarrow import parquet
    rows = {}
    for path in paths:
        for row in parquet.read_table(
            path, columns=["id"] + columns, filters=[("id", "in", selected)]
        ).to_pylist():
            if row["id"] not in selected or row["id"] in rows:
                raise PilotError(
                    "selected row query returned duplicate or unselected ID"
                )
            rows[row["id"]] = row
    if set(rows) != set(selected):
        raise PilotError("a selected task is missing; resampling forbidden")
    return rows


def materialize(
    selection_path: Path,
    cache: Path,
    checkpoint: str,
    output: Path,
    *,
    parquet=None,
    root: Path = ROOT,
) -> dict:
    selection = committed_selection(selection_path, checkpoint, root)
    for item in selection["exclusions"]["manifests"]:
        if file_hash(root / item["path"]) != item["sha256"]:
            raise PilotError("historical manifest changed after selection")
    paths = verify_shards(cache)
    ids = read_ids(paths, parquet)
    if digest(make_selection(ids, selection["exclusions"])) != digest(selection):
        raise PilotError("selection cannot be reproduced from pinned ID universe")
    if any(
        (output / name).exists()
        for name in ("public.json", "labels.json", "provenance.json")
    ):
        raise PilotError("materialization artifacts already exist")
    ordered = selection["pilot_ids"] + selection["primary_ids"]
    raw = [raw_id(value) for value in ordered]
    public_rows = _selected_rows(paths, raw, ["question", "context"], parquet)
    from bench.typed_public_workload import public_task

    tasks = []
    for task_id in ordered:
        row = public_rows[raw_id(task_id)]
        context = row["context"]
        if set(context) != {"title", "sentences"} or len(context["title"]) != len(
            context["sentences"]
        ):
            raise PilotError("selected context arrays are misaligned")
        task = {
            "task_id": task_id,
            "prompt": row["question"],
            "meta": {
                "paragraphs": [
                    {"title": title, "sentences": sentences}
                    for title, sentences in zip(context["title"], context["sentences"])
                ]
            },
        }
        tasks.append(public_task(task))
    # Public projection is persisted and hashed before opening label columns.
    _write_new(output / "public.json", tasks)
    public_sha = file_hash(output / "public.json")
    label_rows = _selected_rows(paths, raw, ["answer", "supporting_facts"], parquet)
    labels = {}
    for task_id, task in zip(ordered, tasks):
        row = label_rows[raw_id(task_id)]
        facts = row["supporting_facts"]
        if (
            not isinstance(row["answer"], str)
            or not row["answer"]
            or set(facts) != {"title", "sent_id"}
            or len(facts["title"]) != len(facts["sent_id"])
        ):
            raise PilotError("selected evaluator labels are malformed")
        pairs = list(zip(facts["title"], facts["sent_id"]))
        if any(
            not isinstance(title, str)
            or type(index) is not int
            or index < 0
            or not any(
                p["title"] == title and index < len(p["sentences"])
                for p in task["meta"]["paragraphs"]
            )
            for title, index in pairs
        ):
            raise PilotError("selected supporting reference is malformed")
        labels[task_id] = {"answer": row["answer"], "supporting_facts": pairs}
    _write_new(output / "labels.json", labels)
    provenance = {
        "kind": KIND,
        "source": source(),
        "selection_sha256": selection["selection_sha256"],
        "selection_file_sha256": file_hash(selection_path),
        "selection_checkpoint": checkpoint,
        "public_sha256": public_sha,
        "labels_sha256": file_hash(output / "labels.json"),
        "public_contract": "question+all-ordered-context-only-v1",
        "labels_contract": "offline-evaluator-only-v1",
    }
    _write_new(output / "provenance.json", provenance)
    return provenance


def load_bundle(directory: Path) -> dict:
    """No label file is opened; acquisition only needs this public bundle."""
    selection = _read_json(directory / "selection.json")
    provenance = _read_json(directory / "provenance.json")
    if (
        file_hash(directory / "selection.json") != provenance["selection_file_sha256"]
        or file_hash(directory / "public.json") != provenance["public_sha256"]
    ):
        raise PilotError("public fixture file hash mismatch")
    bundle = {
        "kind": KIND,
        "source": selection["source"],
        "selection": selection,
        "provenance": provenance,
        "tasks": _read_json(directory / "public.json"),
    }
    validate_bundle(bundle)
    return bundle


def validate_bundle(bundle: dict) -> None:
    from bench.typed_public_workload import public_task

    if (
        not isinstance(bundle, dict)
        or set(bundle) != {"kind", "source", "selection", "provenance", "tasks"}
        or bundle["kind"] != KIND
    ):
        raise PilotError("invalid public bundle")
    validate_selection(bundle["selection"])
    provenance = bundle["provenance"]
    fields = {
        "kind",
        "source",
        "selection_sha256",
        "selection_file_sha256",
        "selection_checkpoint",
        "public_sha256",
        "labels_sha256",
        "public_contract",
        "labels_contract",
    }
    if (
        set(provenance) != fields
        or provenance["kind"] != KIND
        or digest(bundle["source"]) != digest(source())
        or digest(provenance["source"]) != digest(source())
        or provenance["selection_sha256"] != bundle["selection"]["selection_sha256"]
    ):
        raise PilotError("public bundle source or selection mismatch")
    if (
        provenance["public_contract"] != "question+all-ordered-context-only-v1"
        or provenance["labels_contract"] != "offline-evaluator-only-v1"
        or not re.fullmatch(r"[0-9a-f]{40}", provenance["selection_checkpoint"])
    ):
        raise PilotError("public bundle contract mismatch")
    for field in ("selection_file_sha256", "public_sha256", "labels_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", provenance[field]):
            raise PilotError("malformed artifact hash")
    expected = bundle["selection"]["pilot_ids"] + bundle["selection"]["primary_ids"]
    tasks = bundle["tasks"]
    if (
        not isinstance(tasks, list)
        or [task["task_id"] for task in tasks] != expected
        or any(canonical(public_task(task)) != canonical(task) for task in tasks)
    ):
        raise PilotError("public bundle includes labels, reordered, or missing tasks")
    encoded = (
        json.dumps(tasks, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    if hashlib.sha256(encoded).hexdigest() != provenance["public_sha256"]:
        raise PilotError("public task content hash mismatch")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("download", "select", "materialize"))
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviewed-checkpoint")
    args = parser.parse_args()
    try:
        if args.command == "download":
            download_shards(args.cache)
            print(json.dumps({"verified_train_shards": len(SHARDS)}))
        elif args.command == "select":
            ids = read_ids(verify_shards(args.cache))
            exclusions = collect_exclusions(
                list((ROOT / "bench/repro").rglob("manifest.json"))
            )
            selection = make_selection(ids, exclusions)
            _write_new(args.output / "selection.json", selection)
            print(
                json.dumps(
                    {
                        "selection_sha256": selection["selection_sha256"],
                        "pilot": PILOT_COUNT,
                        "primary": PRIMARY_COUNT,
                    }
                )
            )
        else:
            provenance = materialize(
                args.output / "selection.json",
                args.cache,
                args.reviewed_checkpoint or "",
                args.output,
            )
            print(
                json.dumps(
                    {
                        "public_sha256": provenance["public_sha256"],
                        "labels_sha256": provenance["labels_sha256"],
                    }
                )
            )
        return 0
    except (
        PilotError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.CalledProcessError,
    ):
        print("Pinned public fixture operation stopped; no task content printed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
