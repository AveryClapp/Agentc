"""Verify image receipts, platform selection and incomplete registry captures."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from bench import swebench_images as images
from bench.swebench_cohort import CohortError

SOURCE = "swebench/sweb.eval.x86_64.repo_1776_task-1:latest"


def encode(value):
    return json.dumps(value, separators=(",", ":")).encode()


@pytest.fixture
def registry():
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": "sha256:" + "a" * 64, "size": 1024},
        "layers": [{"digest": "sha256:" + "b" * 64, "size": 4096}],
    }
    raw = encode(manifest)
    descriptor = {
        "mediaType": manifest["mediaType"],
        "digest": images.content_digest(raw),
        "size": len(raw),
        "platform": {"os": "linux", "architecture": "amd64"},
    }
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            descriptor,
            {
                "digest": "sha256:" + "c" * 64,
                "size": 10,
                "platform": {"os": "unknown", "architecture": "unknown"},
            },
        ],
    }
    return index, raw


def resolve(registry):
    index, manifest = registry
    calls = []

    def fetch(reference):
        calls.append(reference)
        return encode(index) if reference == "latest" else manifest

    return images.resolve_image(SOURCE, fetch), calls


def test_digest_selection_ignores_attestation_and_can_be_verified_offline(registry):
    image, calls = resolve(registry)
    assert calls == ["latest", images.content_digest(registry[1])]
    assert image["immutable_image"] == SOURCE.removesuffix(":latest") + "@" + calls[1]
    assert image["config_digest"] == "sha256:" + "a" * 64
    assert image["compressed_layer_bytes"] == 4096
    assert image["platform"] == {"os": "linux", "architecture": "amd64"}
    assert images.verify_receipt(image["receipt"]) == {
        k: v for k, v in image.items() if k != "receipt"
    }


@pytest.mark.parametrize(
    "mutation", ["arm", "duplicate", "no_platform", "bad_digest", "size"]
)
def test_changed_or_ambiguous_platform_descriptor_cannot_pin_image(registry, mutation):
    index, _ = registry
    descriptor = index["manifests"][0]
    if mutation == "arm":
        descriptor["platform"]["architecture"] = "arm64"
    elif mutation == "duplicate":
        index["manifests"].append(deepcopy(descriptor))
    elif mutation == "no_platform":
        descriptor["platform"] = None
    elif mutation == "bad_digest":
        descriptor["digest"] = "../mutable"
    else:
        descriptor["size"] += 1
    with pytest.raises(CohortError):
        resolve(registry)


@pytest.mark.parametrize(
    "mutation", ["body", "config", "negative_layer", "empty_layers"]
)
def test_changed_manifest_content_or_invalid_layer_accounting_is_rejected(
    registry, mutation
):
    index, raw = registry
    manifest = json.loads(raw)
    if mutation == "body":
        raw += b" "  # Even semantically identical JSON must match exact bytes.
    elif mutation == "config":
        manifest["config"]["digest"] = "sha256:invalid"
    elif mutation == "negative_layer":
        manifest["layers"][0]["size"] = -1
    else:
        manifest["layers"] = []
    if mutation != "body":
        raw = encode(manifest)
        index["manifests"][0]["digest"] = images.content_digest(raw)
        index["manifests"][0]["size"] = len(raw)
    with pytest.raises(CohortError):
        resolve((index, raw))


@pytest.mark.parametrize(
    "source", ["other/image:latest", "https://example.com", SOURCE + "x"]
)
def test_arbitrary_network_targets_are_rejected_before_fetch(source):
    calls = []
    with pytest.raises(CohortError, match="unsupported task image"):
        images.resolve_image(source, lambda ref: calls.append(ref))
    assert calls == []


def test_registry_failures_retain_all_tasks_without_environment_admission(registry):
    image, _ = resolve(registry)
    bridge = {
        "assigned_task_count": 150,
        "compatible_task_assets": 150,
        "cohort_sha256": "f" * 64,
        "checks": [
            {
                "task_id": f"task-{i}",
                "stage": "C" if i < 30 else "P" if i < 60 else "T",
                "status": "compatibility_metadata_added",
                "image": SOURCE,
            }
            for i in range(150)
        ],
    }

    def failing_resolver(source):
        raise OSError("PRIVATE REGISTRY ERROR")

    failed = images.capture_images(bridge, failing_resolver)
    assert failed["assigned_task_count"] == len(failed["checks"]) == 150
    assert failed["immutable_images_verified"] == 0
    assert "PRIVATE" not in json.dumps(failed)
    assert all(c["status"] == "image_unavailable" for c in failed["checks"])
    pinned = images.capture_images(bridge, lambda source: image)
    assert images.verify_image_pins(pinned, bridge) is pinned
    assert pinned["immutable_images_verified"] == 150
    assert [c["task_id"] for c in pinned["checks"]] == [
        c["task_id"] for c in bridge["checks"]
    ]
    assert all(c["environment_admitted"] is False for c in pinned["checks"])
    assert all(c["official_score"] is None for c in pinned["checks"])
    assert pinned["image_layers_downloaded"] == pinned["task_environments_started"] == 0
    image["immutable_image"] += "changed"
    altered = images.capture_images(bridge, lambda source: image)
    assert altered["immutable_images_verified"] == 0


@pytest.mark.parametrize(
    "mutation", ["cohort", "bridge", "order", "image", "receipt", "count", "admission"]
)
def test_offline_pins_cannot_change_cohort_bytes_or_admission(registry, mutation):
    image, _ = resolve(registry)
    bridge = {
        "assigned_task_count": 150,
        "compatible_task_assets": 150,
        "cohort_sha256": "f" * 64,
        "checks": [
            {
                "task_id": f"task-{i}",
                "stage": "C",
                "status": "compatibility_metadata_added",
                "image": SOURCE,
            }
            for i in range(150)
        ],
    }
    pins = images.capture_images(bridge, lambda source: image)
    if mutation == "cohort":
        pins["cohort_sha256"] = "changed"
    elif mutation == "bridge":
        pins["compatibility_bridge_sha256"] = "changed"
    elif mutation == "order":
        pins["checks"].reverse()
    elif mutation == "image":
        pins["checks"][0]["immutable_image"] = "different"
    elif mutation == "receipt":
        pins["checks"][0]["receipt"]["manifest_json"] += " "
    elif mutation == "count":
        pins["immutable_images_verified"] = 149
    else:
        pins["checks"][0]["environment_admitted"] = True
    with pytest.raises(CohortError):
        images.verify_image_pins(pins, bridge)


def test_capture_rejects_duplicate_denominators(registry):
    bridge = {
        "assigned_task_count": 150,
        "compatible_task_assets": 150,
        "checks": [{"task_id": "same"}] * 150,
    }
    with pytest.raises(CohortError, match="complete compatible cohort"):
        images.capture_images(bridge, lambda source: None)


def test_continued_capture_queries_only_missing_images_and_preserves_prior(registry):
    image, _ = resolve(registry)
    bridge = {
        "assigned_task_count": 150,
        "compatible_task_assets": 150,
        "cohort_sha256": "f" * 64,
        "checks": [
            {
                "task_id": f"task-{i}",
                "stage": "C",
                "status": "compatibility_metadata_added",
                "image": SOURCE,
            }
            for i in range(150)
        ],
    }
    prior = images.capture_images(bridge, lambda source: image)
    for check in prior["checks"][:52]:
        task_id = check["task_id"]
        check.clear()
        check.update(
            task_id=task_id,
            stage="C",
            source_image=SOURCE,
            status="image_unavailable",
            error_type="HTTPError",
            http_status=429,
            environment_admitted=False,
            official_score=None,
        )
    prior["immutable_images_verified"] = 98
    before = deepcopy(prior)
    calls = []

    def resolver(source):
        calls.append(source)
        return image

    continued = images.capture_images(bridge, resolver, previous=prior)
    assert len(calls) == 52
    assert continued["immutable_images_verified"] == 150
    assert continued["immutable_image_pins_reused"] == 98
    assert continued["previous_image_pins_sha256"] == images.digest(prior)
    assert prior == before
    assert images.verify_image_pins(continued, bridge) is continued
    continued["checks"][-1]["receipt"]["index_json"] = "changed"
    assert prior == before
    prior["cohort_sha256"] = "different"
    calls.clear()
    with pytest.raises(CohortError):
        images.capture_images(bridge, resolver, previous=prior)
    assert calls == []
