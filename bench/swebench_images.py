"""Resolve SWE-bench image tags to verified linux/amd64 manifest digests.

Only registry metadata is fetched. Image layers are never downloaded and no
task container is started. Raw manifest bytes are retained for offline checks.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from copy import deepcopy
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from typing import Callable, Mapping

from bench.campaign_accounting import digest
from bench.swebench_cohort import CohortError, require

INDEX_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
MANIFEST_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def content_digest(raw: bytes) -> str:
    return "sha256:" + sha256(raw).hexdigest()


def descriptor_valid(descriptor: Mapping) -> bool:
    return (
        isinstance(descriptor, Mapping)
        and isinstance(descriptor.get("digest"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", descriptor["digest"]) is not None
        and type(descriptor.get("size")) is int
        and descriptor["size"] > 0
    )


def decode_manifest(raw: bytes) -> dict:
    require(0 < len(raw) <= MAX_MANIFEST_BYTES, "invalid registry manifest size")
    value = json.loads(raw)
    require(
        isinstance(value, dict) and value.get("schemaVersion") == 2,
        "unsupported registry manifest schema",
    )
    return value


def verify_receipt(receipt: dict) -> dict:
    """Reconstruct immutable references from retained bytes, without networking."""
    source = receipt["source_image"]
    require(
        isinstance(source, str)
        and re.fullmatch(r"swebench/sweb\.eval\.x86_64\.[a-z0-9_.-]+:latest", source)
        is not None,
        "unsupported task image reference",
    )
    index_raw = receipt["index_json"].encode()
    manifest_raw = receipt["manifest_json"].encode()
    index = decode_manifest(index_raw)
    require(index.get("mediaType") in INDEX_TYPES, "image has no platform index")
    require(isinstance(index.get("manifests"), list), "invalid image index")
    matches = [
        item
        for item in index["manifests"]
        if isinstance(item, Mapping)
        and isinstance(item.get("platform"), Mapping)
        and item["platform"].get("os") == "linux"
        and item["platform"].get("architecture") == "amd64"
        and item["platform"].get("variant") in (None, "")
    ]
    require(len(matches) == 1, "image must have one linux/amd64 manifest")
    descriptor = matches[0]
    require(
        descriptor_valid(descriptor) and descriptor.get("mediaType") in MANIFEST_TYPES,
        "invalid platform manifest descriptor",
    )
    require(
        content_digest(manifest_raw) == descriptor["digest"]
        and len(manifest_raw) == descriptor["size"],
        "platform manifest bytes differ from the image index",
    )
    manifest = decode_manifest(manifest_raw)
    require(
        manifest.get("mediaType") == descriptor["mediaType"]
        and descriptor_valid(manifest.get("config"))
        and isinstance(manifest.get("layers"), list)
        and bool(manifest["layers"])
        and all(descriptor_valid(layer) for layer in manifest["layers"]),
        "invalid image configuration or layers",
    )
    repository = source.removesuffix(":latest")
    return {
        "source_image": source,
        "index_digest": content_digest(index_raw),
        "manifest_digest": descriptor["digest"],
        "config_digest": manifest["config"]["digest"],
        "immutable_image": repository + "@" + descriptor["digest"],
        "platform": {"os": "linux", "architecture": "amd64"},
        "compressed_layer_bytes": sum(layer["size"] for layer in manifest["layers"]),
        "layer_count": len(manifest["layers"]),
    }


def resolve_image(source: str, fetch: Callable[[str], bytes]) -> dict:
    require(
        isinstance(source, str)
        and re.fullmatch(r"swebench/sweb\.eval\.x86_64\.[a-z0-9_.-]+:latest", source)
        is not None,
        "unsupported task image reference",
    )
    index_raw = fetch("latest")
    index = decode_manifest(index_raw)
    require(index.get("mediaType") in INDEX_TYPES, "image has no platform index")
    require(isinstance(index.get("manifests"), list), "invalid image index")
    matches = [
        item
        for item in index["manifests"]
        if isinstance(item, Mapping)
        and isinstance(item.get("platform"), Mapping)
        and item["platform"].get("os") == "linux"
        and item["platform"].get("architecture") == "amd64"
        and item["platform"].get("variant") in (None, "")
    ]
    require(len(matches) == 1, "image must have one linux/amd64 manifest")
    require(descriptor_valid(matches[0]), "invalid platform manifest descriptor")
    receipt = {
        "source_image": source,
        "index_json": index_raw.decode(),
        "manifest_json": fetch(matches[0]["digest"]).decode(),
    }
    return {**verify_receipt(receipt), "receipt": receipt}


def _get(url: str, headers: dict) -> bytes:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        require(response.status == 200, "registry metadata request failed")
        raw = response.read(MAX_MANIFEST_BYTES + 1)
    require(len(raw) <= MAX_MANIFEST_BYTES, "registry metadata exceeds byte limit")
    return raw


def registry_image(source: str) -> dict:
    require(
        isinstance(source, str)
        and re.fullmatch(r"swebench/sweb\.eval\.x86_64\.[a-z0-9_.-]+:latest", source)
        is not None,
        "unsupported task image reference",
    )
    repository = source.removesuffix(":latest")
    query = urllib.parse.urlencode(
        {"service": "registry.docker.io", "scope": f"repository:{repository}:pull"}
    )
    token = json.loads(_get("https://auth.docker.io/token?" + query, {}))["token"]
    require(isinstance(token, str) and bool(token), "registry did not issue read token")
    # The anonymous read token stays in memory and never enters a receipt.
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": ", ".join(sorted(INDEX_TYPES | MANIFEST_TYPES)),
    }

    def fetch(reference):
        require(
            reference == "latest"
            or re.fullmatch(r"sha256:[0-9a-f]{64}", reference) is not None,
            "invalid registry reference",
        )
        return _get(
            f"https://registry-1.docker.io/v2/{repository}/manifests/{reference}",
            headers,
        )

    return resolve_image(source, fetch)


def capture_images(
    bridge: dict, resolver: Callable = registry_image, *, previous: dict | None = None
) -> dict:
    """Preserve every task, including registry failures; never admit environments."""
    require(
        bridge["assigned_task_count"] == 150
        and len(bridge["checks"]) == 150
        and bridge["compatible_task_assets"] == 150
        and len({c["task_id"] for c in bridge["checks"]}) == 150
        and all(
            c["status"] == "compatibility_metadata_added" for c in bridge["checks"]
        ),
        "image capture requires a complete compatible cohort",
    )
    if previous is not None:
        verify_image_pins(previous, bridge)
    prior = {c["task_id"]: c for c in previous["checks"]} if previous else {}

    def capture(task):
        cached = prior.get(task["task_id"])
        if cached is not None and cached["status"] == "immutable_image_verified":
            return deepcopy(cached)
        result = {
            "task_id": task["task_id"],
            "stage": task["stage"],
            "source_image": task["image"],
            "environment_admitted": False,
            "official_score": None,
        }
        try:
            image = resolver(task["image"])
            verified = verify_receipt(image["receipt"])
            require(
                all(image[key] == value for key, value in verified.items())
                and verified["source_image"] == task["image"],
                "resolved image receipt differs",
            )
            result.update(status="immutable_image_verified", **image)
        except (CohortError, OSError, KeyError, TypeError, ValueError) as error:
            result.update(status="image_unavailable", error_type=type(error).__name__)
            if isinstance(error, HTTPError):
                result["http_status"] = error.code
        return result

    with ThreadPoolExecutor(max_workers=4) as workers:
        checks = list(workers.map(capture, bridge["checks"]))
    return {
        "schema_version": 1,
        "record_type": "frozen_swebench_image_pins",
        "cohort_sha256": bridge["cohort_sha256"],
        "compatibility_bridge_sha256": digest(bridge),
        "previous_image_pins_sha256": digest(previous)
        if previous is not None
        else None,
        "immutable_image_pins_reused": sum(
            c["status"] == "immutable_image_verified" for c in prior.values()
        ),
        "checks": checks,
        "assigned_task_count": len(checks),
        "immutable_images_verified": sum(
            c["status"] == "immutable_image_verified" for c in checks
        ),
        "image_layers_downloaded": 0,
        "task_environments_started": 0,
        "environment_admission_completed": False,
        "provider_acquisition_ready": False,
        "paper_evidence": False,
    }


def verify_image_pins(pins: dict, bridge: dict) -> dict:
    """Reuse captured pins only for their exact cohort and compatibility bridge."""
    require(
        pins.get("schema_version") == 1
        and pins.get("record_type") == "frozen_swebench_image_pins"
        and pins.get("cohort_sha256") == bridge["cohort_sha256"]
        and pins.get("compatibility_bridge_sha256") == digest(bridge)
        and pins.get("assigned_task_count") == len(bridge["checks"]) == 150
        and isinstance(pins.get("checks"), list)
        and len(pins["checks"]) == 150,
        "image pins differ from the compatibility bridge",
    )
    count = 0
    for check, task in zip(pins["checks"], bridge["checks"], strict=True):
        require(
            check["task_id"] == task["task_id"]
            and check["stage"] == task["stage"]
            and check["source_image"] == task["image"]
            and check.get("environment_admitted") is False
            and check.get("official_score") is None,
            "image pin changed a task or claimed environment admission",
        )
        if check["status"] == "immutable_image_verified":
            verified = verify_receipt(check["receipt"])
            require(
                all(check.get(key) == value for key, value in verified.items())
                and verified["source_image"] == task["image"],
                "image pin differs from its manifest receipt",
            )
            count += 1
        else:
            require(
                check["status"] == "image_unavailable"
                and "receipt" not in check
                and "immutable_image" not in check,
                "invalid unresolved image pin",
            )
    require(
        pins.get("immutable_images_verified") == count
        and pins.get("image_layers_downloaded") == 0
        and pins.get("task_environments_started") == 0
        and pins.get("environment_admission_completed") is False
        and pins.get("provider_acquisition_ready") is False
        and pins.get("paper_evidence") is False,
        "image pin summary differs from verified metadata",
    )
    return pins
