"""Prospective fixture tests never generate the real selected 69 task bodies."""

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from bench import openrouter_protected_fixture as b
from bench.openrouter_pilot import PilotError


def synthetic_tasks():
    return [
        {
            "task_id": task_id,
            "prompt": "Synthetic public request",
            "meta": {
                "paragraphs": [
                    {"title": f"D{i:02}", "sentences": [f"Synthetic public row {i}"]}
                    for i in range(1, 21)
                ]
            },
            "expected": "M01",
        }
        for task_id in b.ids_for(b.SEED, b.COUNT)
    ]


def manifest(root, value, name="prior"):
    path = root / f"bench/repro/{name}/manifest.json"
    b.write_json(path, value)
    return path


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    original_root = b.ROOT
    for relative in b.SOURCE_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((original_root / relative).read_bytes())
    monkeypatch.setattr(b, "ROOT", root)
    calls = []

    def fake_generator(seed, count):
        calls.append((seed, count))
        return synthetic_tasks()

    monkeypatch.setattr(b.typed, "make_tasks", fake_generator)
    out = root / "bench/repro/prospective"
    selection = b.prepare(out, [])
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "bench"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture Test",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "Freeze synthetic ID checkpoint",
        ],
        cwd=root,
        check=True,
    )
    checkpoint = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    return root, out, selection, checkpoint, calls


def test_ids_match_original_generator_on_nonprospective_test_seed():
    # The prospective seed's bodies remain unopened until independent review.
    tasks = b.generator.make_tasks(-1701, 3)
    assert [task["task_id"] for task in tasks] == b.ids_for(-1701, 3)


def test_prepare_is_id_only_with_exact_split_and_reserved_disjointness(fixture):
    _, out, selection, _, calls = fixture
    assert calls == []
    assert {path.name for path in out.iterdir()} == {"selection.json"}
    assert len(selection["pilot_ids"]) == 6
    assert len(selection["study_ids"]) == 63
    assert selection["pilot_ids"] + selection["study_ids"] == b.ids_for(2026090601, 69)
    assert len(selection["excluded_ids"]) == 169
    assert not set(selection["excluded_ids"]) & set(b.ids_for(b.SEED, b.COUNT))
    assert "not a claim" in selection["exclusion_scope"]
    b.validate_selection(selection)


def test_prior_inventory_is_manifest_only_and_includes_extra_ids(tmp_path, monkeypatch):
    old_ids = b.ids_for(2026090501, 6)
    extra = b.ids_for(-2001, 1)[0]
    old = manifest(
        tmp_path,
        {
            "generator_seed": 2026090501,
            "generator_count": 6,
            "schedule": [{"task_id": old_ids[0]}],
            "labels": "/forbidden/labels.json",
        },
    )
    other = manifest(tmp_path, {"stage": {extra: {}}}, "extra")
    public = manifest(
        tmp_path,
        {"generator_seed": 2026090504, "generator_count": 38, "ids": ["hotpot_abcdef"]},
        "public",
    )
    original_read = b.read
    monkeypatch.setattr(
        b,
        "read",
        lambda path: (
            original_read(path)
            if Path(path).name == "manifest.json"
            else pytest.fail("opened a non-manifest input")
        ),
    )
    selected = b.build_selection([old, old, other, public])
    assert len(selected["prior_manifests"]) == 2
    assert len(selected["excluded_ids"]) == 170
    assert extra in selected["excluded_ids"]
    assert set(old_ids) <= set(selected["excluded_ids"])
    assert b.file_hash(old) in {row["sha256"] for row in selected["prior_manifests"]}


def test_overlap_stops_without_reseeding_or_content_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        b.typed, "make_tasks", lambda *a: pytest.fail("generated content")
    )
    prior = manifest(tmp_path, {"task_id": b.ids_for(b.SEED, b.COUNT)[17]})
    with pytest.raises(PilotError, match="overlap"):
        b.prepare(tmp_path / "new", [prior])
    assert not (tmp_path / "new").exists()


def test_inconsistent_prior_seed_ids_are_rejected(tmp_path):
    prior = manifest(
        tmp_path,
        {
            "generator_seed": 2026090501,
            "generator_count": 6,
            "task_id": b.ids_for(-2002, 1)[0],
        },
    )
    with pytest.raises(PilotError, match="differ"):
        b.build_selection([prior])


@pytest.mark.parametrize("bad", [None, "HEAD", "abc1234", "../main", "f" * 40])
def test_missing_or_invalid_checkpoint_never_generates(fixture, bad):
    _, out, _, _, calls = fixture
    with pytest.raises((PilotError, subprocess.CalledProcessError)):
        b.materialize(out, bad)
    assert calls == []
    assert {path.name for path in out.iterdir()} == {"selection.json"}


def test_uncommitted_selection_and_source_are_rejected(fixture):
    root, out, _, checkpoint, calls = fixture
    path = out / "selection.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(PilotError, match="checkpoint"):
        b.materialize(out, checkpoint)
    assert calls == []
    path.write_bytes(path.read_bytes()[:-1])
    source = root / b.SOURCE_FILES[0]
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(PilotError, match="source changed"):
        b.materialize(out, checkpoint)
    assert calls == []


def test_wrong_generator_runtime_is_rejected_before_generation(fixture, monkeypatch):
    _, out, _, checkpoint, calls = fixture
    monkeypatch.setattr(
        b, "runtime", lambda: {"implementation": "different", "version": "0"}
    )
    with pytest.raises(PilotError, match="runtime"):
        b.materialize(out, checkpoint)
    assert calls == []


def test_exact_materialization_once_public_parity_and_separate_labels(fixture):
    _, out, selection, checkpoint, calls = fixture
    bundle = b.materialize(out, checkpoint)
    assert calls == [(b.SEED, 69)]
    assert bundle["tasks"] == [b.generator._public(task) for task in synthetic_tasks()]
    assert bundle["selection"] == selection
    assert b.read(out / "labels.json") == {
        task["task_id"]: {"expected": "M01"} for task in synthetic_tasks()
    }
    assert b"expected" not in (out / "public.json").read_bytes()
    assert bundle["provenance"]["reviewed_checkpoint"] == checkpoint
    assert bundle["provenance"]["labels_sha256"] == b.file_hash(out / "labels.json")
    with pytest.raises(PilotError, match="replay"):
        b.materialize(out, checkpoint)
    with pytest.raises(PilotError, match="never replace"):
        b.prepare(out, [])
    assert len(calls) == 1


def test_loader_never_opens_labels_or_regenerates(fixture, monkeypatch):
    _, out, _, checkpoint, calls = fixture
    expected = b.materialize(out, checkpoint)
    (out / "labels.json").unlink()  # Evaluator-only artifact need not be mounted.
    original_open = Path.open

    def safe_open(path, *args, **kwargs):
        assert path.name not in {"labels.json", ".env"}
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", safe_open)
    monkeypatch.setattr(
        b.typed, "make_tasks", lambda *a: pytest.fail("regenerated labels")
    )
    loaded = b.load_bundle(out)
    assert loaded == expected
    loaded["tasks"][0]["prompt"] = "mutated copy"
    assert b.load_bundle(out) == expected
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change", ["reverse", "missing", "duplicate", "bad_public", "bad_label"]
)
def test_generator_contract_failure_writes_no_materialization(
    fixture, monkeypatch, change
):
    _, out, _, checkpoint, _ = fixture
    tasks = synthetic_tasks()
    if change == "reverse":
        tasks.reverse()
    elif change == "missing":
        tasks.pop()
    elif change == "duplicate":
        tasks[1] = deepcopy(tasks[0])
    elif change == "bad_public":
        tasks[0]["meta"]["paragraphs"].pop()
    else:
        tasks[0]["expected"] = "not an ID"
    monkeypatch.setattr(b.typed, "make_tasks", lambda *a: tasks)
    with pytest.raises(PilotError):
        b.materialize(out, checkpoint)
    assert {path.name for path in out.iterdir()} == {"selection.json"}


@pytest.mark.parametrize(
    "field",
    [
        "seed",
        "count",
        "pilot_ids",
        "study_ids",
        "excluded_ids",
        "source_contract",
        "prior_cohorts",
        "exclusion_scope",
        "selection_sha256",
        "generator_runtime",
    ],
)
def test_selection_tampering_stops_before_generation(fixture, field):
    _, out, selection, checkpoint, calls = fixture
    changed = deepcopy(selection)
    changed[field] = None
    b.write_json(out / "selection.json", changed)
    with pytest.raises((PilotError, TypeError, KeyError)):
        b.materialize(out, checkpoint)
    assert calls == []


@pytest.mark.parametrize(
    "change",
    [
        "expected",
        "extra_meta",
        "task_order",
        "task_missing",
        "public_bytes",
        "extra_provenance",
        "checkpoint",
        "source",
        "labels_hash",
        "generation",
        "label_use",
        "count",
    ],
)
def test_public_bundle_tampering_rejected(fixture, change):
    _, out, _, checkpoint, _ = fixture
    b.materialize(out, checkpoint)
    tasks, provenance = b.read(out / "public.json"), b.read(out / "provenance.json")
    if change == "expected":
        tasks[0]["expected"] = "M01"
    elif change == "extra_meta":
        tasks[0]["meta"]["gold"] = "private"
    elif change == "task_order":
        tasks.reverse()
    elif change == "task_missing":
        tasks.pop()
    elif change == "public_bytes":
        (out / "public.json").write_bytes((out / "public.json").read_bytes() + b" ")
        with pytest.raises(PilotError):
            b.load_bundle(out)
        return
    elif change == "extra_provenance":
        provenance["extra"] = True
    else:
        key = {
            "checkpoint": "reviewed_checkpoint",
            "source": "source_contract",
            "labels_hash": "labels_sha256",
        }.get(change, change)
        provenance[key] = "altered"
    b.write_json(out / "public.json", tasks)
    b.write_json(out / "provenance.json", provenance)
    with pytest.raises((PilotError, TypeError, KeyError)):
        b.load_bundle(out)


@pytest.mark.parametrize("text", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_strict_json_rejects_duplicate_keys_and_nonfinite(tmp_path, text):
    path = tmp_path / "invalid.json"
    path.write_text(text)
    with pytest.raises(PilotError):
        b.read(path)


def test_prepare_cli_prints_counts_only(tmp_path, monkeypatch, capsys):
    out = tmp_path / "selection"
    monkeypatch.setattr("sys.argv", ["fixture", "prepare", "--output", str(out)])
    monkeypatch.setattr(
        b.typed, "make_tasks", lambda *a: pytest.fail("generated content")
    )
    assert b.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"selection_sha256", "pilot", "study", "materialized"}
    assert result["pilot"] == 6 and result["study"] == 63
    assert result["materialized"] is False
