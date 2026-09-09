"""Synthetic sources only: checkpoint integrity, exposures, and label isolation."""

import copy
import hashlib
import os
import shutil
import subprocess

import pytest

from bench import finqa_acquisition as a
from bench.finqa_workload import ContractError, report_id


def row(name, *, question="Invented question", text="Invented report"):
    return {
        "id": f"{name}/1900/page_1.pdf-0",
        "qa": {
            "question": question,
            "exe_ans": 5.0,
            "program": "SECRET_PROGRAM",
            "gold_inds": {"SECRET": "SECRET"},
        },
        "pre_text": [text],
        "post_text": [],
        "table": [["row", "year"], ["invented", "5"]],
        "model_input": "SECRET_RETRIEVAL",
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo, cache = tmp_path / "repo", tmp_path / "cache"
    repo.mkdir()
    cache.mkdir()
    for name in a.CLOSURE:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(a.ROOT / name, target)
    spec = {
        "explicit_reports": ["EXPOSED/1900"],
        "question_matches": [
            {
                "location": "invented figure",
                "question_sha256": a.question_hash("Known exposed question?"),
            }
        ],
        "public_fragments": [
            {"location": "invented screenshot", "text": "known screenshot fragment"}
        ],
    }
    (repo / a.EXPOSURES).write_bytes(a.json_bytes(spec))
    splits = {
        "train": [row(f"CAL{i}") for i in range(8)] + [row("EXPOSED")],
        "dev": [row(f"EVAL{i}") for i in range(8)],
        "test": [
            row(
                "PAPER",
                question="Known exposed question?",
                text="Known screenshot fragment.",
            )
        ],
    }
    sources = {}
    for key, values in splits.items():
        raw = a.json_bytes(values)
        name = f"dataset/{key}.json"
        target = cache / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(raw)
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        sources[key] = (name, len(raw), blob, len(values))
    monkeypatch.setattr(a, "ROOT", repo)
    monkeypatch.setattr(a, "SOURCES", sources)
    monkeypatch.setattr(a, "CALIBRATION_COUNT", 3)
    monkeypatch.setattr(a, "FORMAT_COUNT", 1)
    monkeypatch.setattr(a, "EVALUATION_COUNT", 4)
    return repo, cache, splits, spec


def checkpoint(repo, cache):
    selected = repo / "selection.json"
    selected.write_bytes(a.json_bytes(a.make_selection(cache)))
    for args in (
        ("init", "-q"),
        ("add", "."),
        (
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Freeze synthetic inputs",
        ),
    ):
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    return selected, commit


def test_selection_is_report_disjoint_and_never_enrolls_test(setup):
    _, cache, _, _ = setup
    value = a.make_selection(cache)
    s = value["selection"]
    assert len(s["calibration_ids"]) == 3 and len(s["evaluation_ids"]) == 4
    assert len(value["format_ids"]) == 1 and len(value["learning_ids"]) == 2
    assert not (
        {report_id(x) for x in s["calibration_ids"]}
        & {report_id(x) for x in s["evaluation_ids"]}
    )
    assert all(
        "EXPOSED" not in x and "PAPER" not in x
        for x in s["calibration_ids"] + s["evaluation_ids"]
    )
    assert value["exposure"]["reports"] == ["EXPOSED/1900", "PAPER/1900"]
    assert value["labels_used_for_selection"] is False


def test_exposure_matching_does_not_access_gold(setup):
    _, _, splits, spec = setup

    class PublicQA(dict):
        def __getitem__(self, key):
            assert key == "question", "gold accessed during exposure matching"
            return super().__getitem__(key)

    for values in splits.values():
        for item in values:
            item["qa"] = PublicQA(item["qa"])
    before = a.exposure_reports(splits, spec)
    for values in splits.values():
        for item in values:
            item["qa"]["exe_ans"] = object()
            item["qa"]["gold_inds"] = object()
            item["qa"]["program"] = object()
    assert a.exposure_reports(splits, spec) == before


@pytest.mark.parametrize("field", ["question_matches", "public_fragments"])
def test_unresolved_paper_exposure_blocks_selection(setup, field):
    _, _, rows, spec = setup
    spec = copy.deepcopy(spec)
    if field == "question_matches":
        spec[field][0]["question_sha256"] = a.question_hash(
            "Missing published question"
        )
    else:
        spec[field][0]["text"] = "missing published fragment"
    with pytest.raises(ContractError, match="unresolved"):
        a.exposure_reports(rows, spec)


def test_source_tamper_is_rejected_before_parsing(setup):
    _, cache, _, _ = setup
    source = cache / "dataset/train.json"
    source.write_bytes(source.read_bytes().replace(b"SECRET", b"HIDDEN"))
    with pytest.raises(ContractError, match="blob"):
        a.make_selection(cache)


@pytest.mark.parametrize(
    "raw", [b'{"id":1,"id":2}', b'{"x":NaN}', b'{"x":Infinity}', b"{broken"]
)
def test_ambiguous_json_is_rejected(raw):
    with pytest.raises(ContractError):
        a.read_json(raw)


def test_insufficient_inventory_does_not_shrink(setup, monkeypatch):
    _, cache, _, _ = setup
    monkeypatch.setattr(a, "EVALUATION_COUNT", 100)
    with pytest.raises(ContractError, match="shrink"):
        a.make_selection(cache)


@pytest.mark.parametrize("changed", ["selection", "code", "attention"])
def test_materialization_requires_exact_checkpoint_bytes(setup, tmp_path, changed):
    repo, cache, _, _ = setup
    selection, commit = checkpoint(repo, cache)
    target = {
        "selection": selection,
        "code": repo / "bench/finqa_workload.py",
        "attention": repo / "python/agentc/_attention.py",
    }[changed]
    target.write_bytes(target.read_bytes() + b"\n")
    output = tmp_path / "bundle"
    with pytest.raises(ContractError, match="checkpoint"):
        a.materialize(selection, commit, cache, output)
    assert not output.exists()


def test_materialization_writes_public_before_reading_selected_labels(
    setup, tmp_path, monkeypatch
):
    repo, cache, _, _ = setup
    selection, commit = checkpoint(repo, cache)
    output = tmp_path / "bundle"
    original = a.load_sources
    accesses = []

    class ObservedQA(dict):
        def __getitem__(self, key):
            if key == "exe_ans":
                assert (output / "public.json").is_file()
                accesses.append(key)
            return super().__getitem__(key)

    def guarded(path):
        rows, provenance = original(path)
        for values in rows.values():
            for item in values:
                item["qa"] = ObservedQA(item["qa"])
        return rows, provenance

    monkeypatch.setattr(a, "load_sources", guarded)
    result = a.materialize(selection, commit, cache, output)
    assert len(accesses) == 7
    assert result["calibration_count"] == 3 and result["evaluation_count"] == 4
    assert (output / "labels.sealed.json").stat().st_mode & 0o777 == 0
    assert output.stat().st_mode & 0o777 == 0o700
    raw = (output / "public.json").read_bytes()
    assert b"SECRET" not in raw and b"exe_ans" not in raw and b"program" not in raw
    assert a.sha256(raw) == result["public_sha256"]
    with pytest.raises(FileExistsError):
        a.materialize(selection, commit, cache, output)


def test_download_and_bundle_cannot_enter_a_git_checkout(setup):
    repo, cache, _, _ = setup
    checkpoint(repo, cache)
    with pytest.raises(ContractError, match="checkout"):
        a.download(repo / "private")
    with pytest.raises(ContractError, match="checkout"):
        a.outside_repository(repo / "bundle")


def test_census_keeps_all_public_rows_without_labels(setup):
    _, cache, _, _ = setup
    result = a.public_census(cache)
    assert result["splits"]["train"]["rows"] == 9
    assert result["splits"]["dev"]["rows"] == 8
    assert result["splits"]["train"]["invalid_public_rows"] == 0
    assert result["labels_used"] is False and result["provider_calls"] == 0


def test_native_census_does_not_open_sealed_labels(setup, tmp_path, monkeypatch):
    repo, cache, _, _ = setup
    selection, commit = checkpoint(repo, cache)
    bundle = tmp_path / "bundle"
    a.materialize(selection, commit, cache, bundle)
    # A missing label file is fine for public-only coverage.
    (bundle / "labels.sealed.json").unlink()
    binary = tmp_path / "native"
    binary.write_text(
        "#!/usr/bin/env python3\nimport json,sys\nr=json.load(sys.stdin)\nc=r['call']\nprint(json.dumps({'call':c,'retained_indices':list(range(len(c['messages']))),'applied':False,'abstention_reason':'not_applicable','materialization_only':True}))\n"
    )
    os.chmod(binary, 0o700)
    result = a.native_census(bundle, binary, a.sha256(binary.read_bytes()))
    assert result["selected_tasks"] == 7
    assert result["counts"] == {
        rule: {"not_applicable": 7} for rule in ("ContextCompress", "StateDrop")
    }
    with pytest.raises(ContractError, match="SHA"):
        a.native_census(bundle, binary, "0" * 64)


def test_write_new_never_overwrites(tmp_path):
    path = tmp_path / "sealed"
    a.write_new(path, b"original")
    with pytest.raises(FileExistsError):
        a.write_new(path, b"replacement")
    assert path.read_bytes() == b"original"
