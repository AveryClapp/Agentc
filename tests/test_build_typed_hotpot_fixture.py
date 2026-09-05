"""Pinned fixture tests use synthetic records only, never protected QA data."""

import json
import subprocess
from copy import deepcopy

import pytest

from bench import build_typed_hotpot_fixture as b
from bench.openrouter_pilot import PilotError, digest


class Table:
    def __init__(self, values):
        self.values = values

    def to_pylist(self):
        return deepcopy(self.values)

    def __getitem__(self, key):
        return Table([row[key] for row in self.values])


class Parquet:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def read_table(self, path, *, columns, filters=None):
        self.calls.append((list(columns), deepcopy(filters)))
        chosen = (
            self.rows
            if filters is None
            else [row for row in self.rows if row["id"] in filters[0][2]]
        )
        return Table([{key: row[key] for key in columns} for row in chosen])


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "TRAIN_ROWS", 48)
    root = tmp_path / "repo"
    root.mkdir()
    manifest = root / "bench/repro/prior/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "schedule": [{"task_id": "hotpot_" + f"{1:024x}"}],
                "excluded_question_ids": [f"{2:024x}"],
            }
        )
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    shard = cache / "train.parquet"
    shard.write_bytes(b"synthetic parquet bytes")
    monkeypatch.setattr(
        b,
        "SHARDS",
        (
            {
                "path": "distractor/train.parquet",
                "bytes": shard.stat().st_size,
                "sha256": b.file_hash(shard),
            },
        ),
    )
    ids = [f"{i:024x}" for i in range(48)]
    rows = [
        {
            "id": value,
            "question": "Synthetic question?",
            "context": {
                "title": [f"Document {i}" for i in range(10)],
                "sentences": [["Synthetic fact."] for _ in range(10)],
            },
            "answer": "Synthetic answer",
            "supporting_facts": {"title": ["Document 0"], "sent_id": [0]},
            "type": "unused",
            "level": "unused",
        }
        for value in ids
    ]
    exclusions = b.collect_exclusions([manifest], root)
    selection = b.make_selection(ids, exclusions)
    out = root / "bench/repro/new"
    out.mkdir(parents=True)
    selection_path = out / "selection.json"
    b._write_new(selection_path, selection)
    return root, cache, out, selection, Parquet(rows)


def test_selection_requests_id_column_only_and_is_label_blind(fixture):
    _, cache, _, selection, parquet = fixture
    ids = b.read_ids(b.verify_shards(cache), parquet)
    assert parquet.calls == [(["id"], None)]
    changed = deepcopy(parquet.rows)
    for row in changed:
        row.update(
            question=object(),
            answer=object(),
            supporting_facts=object(),
            context=object(),
            type=object(),
            level=object(),
        )
    assert (
        b.make_selection(
            b.read_ids(b.verify_shards(cache), Parquet(changed)),
            selection["exclusions"],
        )
        == selection
    )
    assert len(selection["pilot_ids"]) == 6 and len(selection["primary_ids"]) == 32
    assert not {
        b.raw_id(x) for x in selection["pilot_ids"] + selection["primary_ids"]
    } & set(selection["exclusions"]["ids"])
    assert selection["universe"]["sha256"] == digest(ids)
    assert {f"{1:024x}", f"{2:024x}"} <= set(selection["exclusions"]["ids"])


def test_source_pins_preview_exclusions_and_manifest_only(fixture):
    root, _, _, _, _ = fixture
    assert digest(sorted(b.PREVIEW_IDS)) == b.PREVIEW_SHA256
    assert len(b.PREVIEW_IDS) == 36
    with pytest.raises(PilotError, match="manifest"):
        b.collect_exclusions([root / "fixture.json"], root)
    with pytest.raises(PilotError, match="required"):
        b.collect_exclusions([], root)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(seed=True),
        lambda s: s["source"].update(revision="forged"),
        lambda s: s["source"].update(split="validation"),
        lambda s: s["pilot_ids"].__setitem__(0, s["primary_ids"][0]),
        lambda s: s["pilot_ids"].__setitem__(0, "hotpot_" + b.PREVIEW_IDS[0]),
        lambda s: s["exclusions"]["ids"].pop(),
        lambda s: s["universe"].update(count=True),
        lambda s: s["exclusions"]["manifests"][0].update(path="../manifest.json"),
    ],
)
def test_tampered_selection_fails_even_with_recomputed_self_hash(fixture, mutation):
    selection = deepcopy(fixture[3])
    mutation(selection)
    selection["selection_sha256"] = digest(
        {k: v for k, v in selection.items() if k != "selection_sha256"}
    )
    with pytest.raises(PilotError):
        b.validate_selection(selection)


def test_actual_git_checkpoint_required_before_content(fixture):
    root, _, out, selection, _ = fixture
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "Freeze synthetic selection",
        ],
        cwd=root,
        check=True,
    )
    checkpoint = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert b.committed_selection(out / "selection.json", checkpoint, root) == selection
    with pytest.raises(PilotError):
        b.committed_selection(out / "selection.json", "", root)
    with (out / "selection.json").open("a") as stream:
        stream.write(" ")
    with pytest.raises(PilotError, match="differs"):
        b.committed_selection(out / "selection.json", checkpoint, root)


def test_materialization_opens_public_before_labels_then_bundle_never_reads_labels(
    fixture, monkeypatch
):
    root, cache, out, selection, parquet = fixture
    monkeypatch.setattr(b, "committed_selection", lambda *a: deepcopy(selection))
    original_read = parquet.read_table

    def ordered_read(path, *, columns, filters=None):
        if "answer" in columns:
            assert (out / "public.json").is_file()
            assert "answer" not in (out / "public.json").read_text()
        return original_read(path, columns=columns, filters=filters)

    parquet.read_table = ordered_read
    provenance = b.materialize(
        out / "selection.json", cache, "a" * 40, out, parquet=parquet, root=root
    )
    assert [entry[0] for entry in parquet.calls] == [
        ["id"],
        ["id", "question", "context"],
        ["id", "answer", "supporting_facts"],
    ]
    assert provenance["public_sha256"] == b.file_hash(out / "public.json")
    labels_before = b.file_hash(out / "labels.json")
    (out / "labels.json").write_text("labels deliberately unreadable")
    bundle = b.load_bundle(out)
    assert bundle["provenance"]["labels_sha256"] == labels_before
    assert all(set(task) == {"task_id", "prompt", "meta"} for task in bundle["tasks"])
    assert len(bundle["tasks"]) == 38
    bad = deepcopy(bundle)
    bad["tasks"][0]["expected"] = "leaked"
    with pytest.raises(PilotError):
        b.validate_bundle(bad)
    with pytest.raises(PilotError, match="already exist"):
        b.materialize(
            out / "selection.json", cache, "a" * 40, out, parquet=parquet, root=root
        )


def test_no_resampling_for_selected_malformed_or_missing_context(fixture, monkeypatch):
    root, cache, out, selection, parquet = fixture
    monkeypatch.setattr(b, "committed_selection", lambda *a: deepcopy(selection))
    chosen = b.raw_id(selection["pilot_ids"][0])
    next(row for row in parquet.rows if row["id"] == chosen)["context"]["title"].pop()
    with pytest.raises(PilotError, match="misaligned"):
        b.materialize(
            out / "selection.json", cache, "a" * 40, out, parquet=parquet, root=root
        )
    assert not (out / "public.json").exists()
    assert not any("answer" in columns for columns, _ in parquet.calls)


def test_missing_checkpoint_blocks_all_parquet_reads(fixture):
    root, cache, out, _, parquet = fixture
    with pytest.raises(PilotError, match="checkpoint"):
        b.materialize(
            out / "selection.json", cache, "", out, parquet=parquet, root=root
        )
    assert parquet.calls == []


def test_shard_tamper_fails_before_id_read(fixture):
    _, cache, _, _, parquet = fixture
    next(cache.iterdir()).write_bytes(b"wrong")
    with pytest.raises(PilotError):
        b.read_ids(b.verify_shards(cache), parquet)
    assert parquet.calls == []


def test_shard_missing_exposed_duplicate_universe_never_replaced(fixture):
    _, cache, _, selection, parquet = fixture
    parquet.rows[1]["id"] = parquet.rows[0]["id"]
    with pytest.raises(PilotError, match="universe"):
        b.read_ids(b.verify_shards(cache), parquet)
    with pytest.raises(PilotError, match="complete"):
        b.make_selection(["0" * 24], selection["exclusions"])
