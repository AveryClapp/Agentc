"""One ID-before-content draw for a prospective protected learning experiment.

This is the unchanged generated development distribution, not an independent
task benchmark. No provider access, inference, profile seeding, or capacity
estimate. Public loading never opens labels or regenerates solved tasks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
from copy import deepcopy
from pathlib import Path

from bench import structured_relational_workload as generator
from bench import typed_relational_workload as typed
from bench.openrouter_matrix import ROOT, file_hash, write_json
from bench.openrouter_pilot import PilotError, canonical, digest

SEED, PILOT_COUNT, STUDY_COUNT = 2026090601, 6, 63
COUNT = PILOT_COUNT + STUDY_COUNT
KIND = "protected-procurement-fixture-v1"
SELECTION_KIND = "protected-procurement-id-selection-v1"
SOURCE_FILES = (
    "bench/openrouter_protected_fixture.py",
    "bench/structured_relational_workload.py",
    "bench/typed_relational_workload.py",
)
# Include the entire previously reserved 16+3+64+48 study cohort even where no
# paid study ran. This is a conservative exclusion, not a claim of acquisition.
PRIOR_COHORTS = ((2026090501, 6), (2026090502, 131), (2026090503, 32))
FROZEN_MANIFESTS = (
    "openrouter-structured-local-pilot-2026-09-05",
    "openrouter-structured-format-v2-pilot-2026-09-05",
    "openrouter-structured-pilot-2026-09-05",
    "openrouter-typed-schema-pilot-2026-09-05",
    "openrouter-typed-schema-v2-pilot-2026-09-05",
    "openrouter-typed-schema-v3-pilot-2026-09-05",
    "openrouter-typed-set-pilot-2026-09-05",
    "openrouter-typed-factorial-2026-09-05",
    "openrouter-typed-factorial-repeat-2026-09-05",
)
EXCLUSION_SCOPE = "Three reserved generator cohorts plus supplied frozen manifest IDs; not a claim to enumerate unrecorded external draws. Public Hotpot IDs occupy a distinct namespace."
GENERATION = "unchanged make_tasks(seed,count) once; no filtering or reordering"
LABEL_USE = "evaluator only after complete protocol; never allocation or feedback"
_ID = re.compile(re.escape(generator.VERSION) + r"-[0-9a-f]{24}")
_HEX = re.compile(r"[0-9a-f]{64}")


def _require(condition, message):
    if not condition:
        raise PilotError(message)


def read(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate fixture JSON key")
            result[key] = value
        return result

    def invalid(_):
        raise PilotError("nonfinite fixture JSON")

    return json.loads(
        Path(path).read_text(), object_pairs_hook=unique, parse_constant=invalid
    )


def ids_for(seed, count):
    """Use only the original ID formula; never invoke the record generator."""
    _require(
        type(seed) is int and type(count) is int and 0 <= count <= 100000,
        "invalid prior cohort seed/count",
    )
    return [
        f"{generator.VERSION}-{generator._digest([generator.VERSION, seed, index])[:24]}"
        for index in range(count)
    ]


def source_contract():
    return {
        "source_files": {path: file_hash(ROOT / path) for path in SOURCE_FILES},
        "generator_version": generator.VERSION,
        "workload": typed.WORKLOAD,
        "format_variant": typed.FORMAT_VARIANT,
        "typed_contract_sha256": typed.contract_hash(),
        "schemas_sha256": digest(
            {stage: typed.schema_for(stage) for stage in typed.STAGES}
        ),
    }


def runtime():
    return {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
    }


def _ids_in(value):
    if isinstance(value, str):
        return {value} if _ID.fullmatch(value) else set()
    if isinstance(value, list):
        return set().union(*(_ids_in(item) for item in value))
    if isinstance(value, dict):
        return set().union(
            *(_ids_in(item) for item in (*value.keys(), *value.values()))
        )
    return set()


def manifest_record(path):
    """Read manifest metadata only, never a referenced fixture or label file."""
    path = Path(path)
    _require(
        path.name == "manifest.json" and path.parent.parent.name == "repro",
        "only prior experiment manifest metadata may be inspected",
    )
    value = read(path)
    _require(isinstance(value, dict), "invalid historical manifest object")
    ids = _ids_in(value)
    if not ids:
        return None
    seed, count = value.get("generator_seed"), value.get("generator_count")
    if seed is not None or count is not None:
        generated = set(ids_for(seed, count))
        _require(
            ids <= generated, "prior manifest IDs differ from its generator metadata"
        )
        ids |= generated
    return {
        "path": f"bench/repro/{path.parent.name}/manifest.json",
        "sha256": file_hash(path),
        "generator_seed": seed,
        "generator_count": count,
        "ids": sorted(ids),
    }


def _excluded(records):
    ids = {item for seed, count in PRIOR_COHORTS for item in ids_for(seed, count)}
    for record in records:
        _require(
            set(record)
            == {"path", "sha256", "generator_seed", "generator_count", "ids"}
            and isinstance(record["sha256"], str)
            and _HEX.fullmatch(record["sha256"]),
            "invalid historical manifest provenance",
        )
        _require(
            isinstance(record["path"], str)
            and re.fullmatch(r"bench/repro/[^/.][^/]*/manifest\.json", record["path"]),
            "invalid historical manifest path",
        )
        values = record["ids"]
        _require(
            values == sorted(set(values))
            and all(isinstance(item, str) and _ID.fullmatch(item) for item in values),
            "invalid prior generated IDs",
        )
        if (
            record["generator_seed"] is not None
            or record["generator_count"] is not None
        ):
            _require(
                set(values)
                == set(ids_for(record["generator_seed"], record["generator_count"])),
                "prior cohort IDs differ from recorded seed/count",
            )
        ids.update(values)
    return sorted(ids)


def build_selection(prior_manifests):
    records = {}
    for path in prior_manifests:
        record = manifest_record(path)
        if record is not None:
            records[record["sha256"]] = record
    records = [records[key] for key in sorted(records)]
    excluded, selected = _excluded(records), ids_for(SEED, COUNT)
    _require(
        not set(selected) & set(excluded),
        "prospective IDs overlap prior cohorts; no reseed or replacement",
    )
    result = {
        "kind": SELECTION_KIND,
        "seed": SEED,
        "count": COUNT,
        "pilot_ids": selected[:PILOT_COUNT],
        "study_ids": selected[PILOT_COUNT:],
        "source_contract": source_contract(),
        "generator_runtime": runtime(),
        "prior_cohorts": [
            {"seed": seed, "count": count} for seed, count in PRIOR_COHORTS
        ],
        "exclusion_scope": EXCLUSION_SCOPE,
        "prior_manifests": records,
        "excluded_ids": excluded,
        "excluded_ids_sha256": digest(excluded),
        "selected_ids_sha256": digest(selected),
    }
    result["selection_sha256"] = digest(result)
    return result


def validate_selection(selection):
    expected_keys = {
        "kind",
        "seed",
        "count",
        "pilot_ids",
        "study_ids",
        "source_contract",
        "generator_runtime",
        "prior_cohorts",
        "prior_manifests",
        "exclusion_scope",
        "excluded_ids",
        "excluded_ids_sha256",
        "selected_ids_sha256",
        "selection_sha256",
    }
    _require(
        set(selection) == expected_keys
        and selection["kind"] == SELECTION_KIND
        and type(selection["seed"]) is int
        and selection["seed"] == SEED
        and type(selection["count"]) is int
        and selection["count"] == COUNT,
        "prospective selection contract changed",
    )
    _require(
        canonical(selection["source_contract"]) == canonical(source_contract()),
        "generator, renderer, parser or schema source changed",
    )
    _require(
        selection["prior_cohorts"]
        == [{"seed": seed, "count": count} for seed, count in PRIOR_COHORTS],
        "known prior cohort coverage changed",
    )
    _require(
        selection["exclusion_scope"] == EXCLUSION_SCOPE
        and set(selection["generator_runtime"]) == {"implementation", "version"}
        and all(
            isinstance(value, str) and value
            for value in selection["generator_runtime"].values()
        ),
        "exclusion or generator runtime provenance changed",
    )
    records = selection["prior_manifests"]
    _require(
        [row["sha256"] for row in records]
        == sorted({row["sha256"] for row in records}),
        "prior manifest inventory duplicated or reordered",
    )
    excluded, selected = _excluded(records), ids_for(SEED, COUNT)
    _require(
        selection["excluded_ids"] == excluded
        and selection["excluded_ids_sha256"] == digest(excluded)
        and selection["pilot_ids"] == selected[:PILOT_COUNT]
        and selection["study_ids"] == selected[PILOT_COUNT:]
        and selection["selected_ids_sha256"] == digest(selected)
        and not set(selected) & set(excluded),
        "selection IDs, split or disjointness changed",
    )
    _require(
        selection["selection_sha256"]
        == digest(
            {
                key: value
                for key, value in selection.items()
                if key != "selection_sha256"
            }
        ),
        "selection content hash changed",
    )


def prepare(directory, prior_manifests):
    directory = Path(directory)
    _require(
        not any(
            (directory / name).exists()
            for name in (
                "selection.json",
                "public.json",
                "labels.json",
                "provenance.json",
            )
        ),
        "fixture already has a checkpoint or materialization; never replace",
    )
    selection = build_selection(prior_manifests)
    write_json(directory / "selection.json", selection, immutable=True)
    return selection


def reviewed_selection(path, checkpoint):
    _require(
        isinstance(checkpoint, str) and re.fullmatch(r"[0-9a-f]{40}", checkpoint),
        "full independently reviewed checkpoint commit required",
    )
    path = Path(path)
    selection = read(path)
    validate_selection(selection)
    for relative in (str(path.resolve().relative_to(ROOT.resolve())), *SOURCE_FILES):
        recorded = subprocess.run(
            ["git", "show", f"{checkpoint}:{relative}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        _require(
            recorded == (ROOT / relative).read_bytes(),
            "selection/source differs from reviewed Git checkpoint",
        )
    return selection


def materialize(directory, checkpoint):
    directory = Path(directory)
    _require(
        not any(
            (directory / name).exists()
            for name in ("public.json", "labels.json", "provenance.json")
        ),
        "materialization already exists; replay is forbidden",
    )
    selection = reviewed_selection(directory / "selection.json", checkpoint)
    _require(
        selection["generator_runtime"] == runtime(),
        "generation runtime differs from ID checkpoint",
    )
    tasks = typed.make_tasks(SEED, COUNT)  # Exactly once, after checkpoint proof.
    selected = selection["pilot_ids"] + selection["study_ids"]
    _require(
        [task["task_id"] for task in tasks] == selected,
        "generator IDs changed; no filtering, resampling or reordering",
    )
    public = [generator._public(task) for task in tasks]
    labels = {task["task_id"]: {"expected": task["expected"]} for task in tasks}
    _require(
        all(
            isinstance(label["expected"], str)
            and re.fullmatch(r"M(?:0[1-9]|[1-9][0-9])", label["expected"])
            for label in labels.values()
        ),
        "generator labels violate its scalar domain",
    )
    write_json(directory / "public.json", public, immutable=True)
    write_json(directory / "labels.json", labels, immutable=True)
    provenance = {
        "kind": KIND,
        "reviewed_checkpoint": checkpoint,
        "count": COUNT,
        "selection_sha256": selection["selection_sha256"],
        "selection_file_sha256": file_hash(directory / "selection.json"),
        "public_sha256": file_hash(directory / "public.json"),
        "labels_sha256": file_hash(directory / "labels.json"),
        "source_contract": selection["source_contract"],
        "generator_runtime": selection["generator_runtime"],
        "generation": GENERATION,
        "label_use": LABEL_USE,
    }
    write_json(directory / "provenance.json", provenance, immutable=True)
    return load_bundle(directory)


def validate_bundle(bundle):
    _require(
        set(bundle) == {"kind", "selection", "provenance", "tasks"}
        and bundle["kind"] == KIND,
        "invalid public bundle shape",
    )
    selection, provenance, tasks = (
        bundle[key] for key in ("selection", "provenance", "tasks")
    )
    validate_selection(selection)
    _require(
        set(provenance)
        == {
            "kind",
            "reviewed_checkpoint",
            "count",
            "selection_sha256",
            "selection_file_sha256",
            "public_sha256",
            "labels_sha256",
            "source_contract",
            "generator_runtime",
            "generation",
            "label_use",
        }
        and provenance["generation"] == GENERATION
        and provenance["label_use"] == LABEL_USE
        and type(provenance["count"]) is int,
        "invalid materialization provenance shape",
    )
    _require(
        [task["task_id"] for task in tasks]
        == selection["pilot_ids"] + selection["study_ids"]
        and canonical(tasks) == canonical([generator._public(task) for task in tasks]),
        "public bundle has missing IDs, labels or extra fields",
    )
    _require(
        provenance["kind"] == KIND
        and provenance["count"] == COUNT
        and re.fullmatch(r"[0-9a-f]{40}", provenance["reviewed_checkpoint"])
        and provenance["selection_sha256"] == selection["selection_sha256"]
        and provenance["selection_file_sha256"]
        == hashlib.sha256(canonical(selection) + b"\n").hexdigest()
        and provenance["public_sha256"]
        == hashlib.sha256(canonical(tasks) + b"\n").hexdigest()
        and _HEX.fullmatch(provenance["labels_sha256"])
        and canonical(provenance["source_contract"])
        == canonical(selection["source_contract"])
        and provenance["generator_runtime"] == selection["generator_runtime"],
        "public fixture provenance changed",
    )


def load_bundle(directory):
    """Read selection/public/provenance only; never open or regenerate labels."""
    directory = Path(directory)
    result = {
        "kind": KIND,
        "selection": read(directory / "selection.json"),
        "provenance": read(directory / "provenance.json"),
        "tasks": read(directory / "public.json"),
    }
    validate_bundle(result)
    for name, key in (
        ("selection.json", "selection_file_sha256"),
        ("public.json", "public_sha256"),
    ):
        _require(
            file_hash(directory / name) == result["provenance"][key],
            "public artifact bytes changed",
        )
    return deepcopy(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "materialize"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prior-manifest", type=Path, action="append", default=[])
    parser.add_argument("--reviewed-checkpoint")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(
                args.output,
                [
                    *(
                        ROOT / "bench/repro" / name / "manifest.json"
                        for name in FROZEN_MANIFESTS
                    ),
                    *args.prior_manifest,
                ],
            )
            print(
                json.dumps(
                    {
                        "selection_sha256": result["selection_sha256"],
                        "pilot": PILOT_COUNT,
                        "study": STUDY_COUNT,
                        "materialized": False,
                    }
                )
            )
        else:
            result = materialize(args.output, args.reviewed_checkpoint)
            print(
                json.dumps(
                    {
                        "tasks": len(result["tasks"]),
                        "public_sha256": result["provenance"]["public_sha256"],
                        "labels_sha256": result["provenance"]["labels_sha256"],
                    }
                )
            )
        return 0
    except (
        PilotError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ):
        print("Fixture preparation stopped; no resampling or raw task diagnostics.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
