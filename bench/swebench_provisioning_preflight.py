"""Bounded frozen-cohort image acquisition and official environment controls.

No agent or provider calls. Every assigned task remains in the environment
report. The default downloads at most one pinned image, evicts owned images
after their controls, and preserves baseline daemon resources.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bench.campaign_accounting import digest
from bench.coding_submission import private_write
from bench.guard_evidence import file_sha
from bench.swebench_cohort import require
from bench.swebench_environment import (
    check_cohort,
    load_inputs,
    verify_environment_report,
)
from bench.swebench_image_cache import (
    GIB,
    CacheLimits,
    DockerCapacity,
    ImmutableImageCache,
    references,
)


def run(
    *,
    dataset,
    agent,
    evaluator,
    task_assets,
    image_pins,
    output,
    bootstrap_image,
    limits: CacheLimits,
    timeout=600,
):
    import docker

    limits.verify()
    require(not output.exists(), "provisioning preflight requires fresh output")
    inputs = load_inputs(
        dataset=dataset,
        agent=agent,
        evaluator=evaluator,
        task_assets=task_assets,
        image_pins=image_pins,
    )
    output.mkdir(mode=0o700)
    client = docker.from_env(timeout=60)
    # Keep sampling requests bounded independently of the official grader.
    probe_client = docker.from_env(timeout=5)
    capacity = cache = None
    source_sha = file_sha(Path(__file__))
    baseline = {
        image.id: sorted(references(image.attrs))
        for image in client.images.list(all=True)
    }
    baseline_containers = {c.id for c in client.containers.list(all=True)}
    private_write(
        output / "enrollment.json",
        {
            "schema_version": 1,
            "record_type": "frozen_swebench_provisioning_enrollment",
            "cohort_sha256": digest(inputs.cohort),
            "image_pins_sha256": inputs.image_pins_sha256,
            "source_sha256": source_sha,
            "limits": limits.__dict__,
            "baseline_images": baseline,
            "baseline_containers": sorted(baseline_containers),
            "bootstrap_image": bootstrap_image,
            "physical_provider_calls": 0,
        },
    )
    try:
        capacity = DockerCapacity(probe_client, bootstrap_image, output)
        cache = ImmutableImageCache(
            client=client, output=output / "cache", limits=limits, capacity=capacity
        )
        report = check_cohort(
            inputs,
            output / "environments",
            timeout=timeout,
            client=client,
            image_cache=cache,
        )
        cache.close()
        capacity.close()
        replayed = verify_environment_report(inputs, output / "environments")
        require(replayed == report, "replayed environment report differs")
        current = {
            image.id: references(image.attrs) for image in client.images.list(all=True)
        }
        require(
            all(
                image_id in current and set(refs) <= current[image_id]
                for image_id, refs in baseline.items()
            ),
            "baseline images or references changed during provisioning",
        )
        require(
            {c.id for c in client.containers.list(all=True)} == baseline_containers,
            "container resources changed during provisioning",
        )
        require(cache.owned is None, "owned cache reference remains")
        require(
            file_sha(Path(__file__)) == source_sha,
            "provisioning preflight source changed",
        )
        summary = {
            "schema_version": 1,
            "record_type": "frozen_swebench_provisioning_preflight",
            "assigned_task_count": report["assigned_task_count"],
            "environments_admitted": report["environments_admitted"],
            "environment_report_sha256": file_sha(output / "environments/summary.json"),
            "cache_report_sha256": file_sha(output / "cache/summary.json"),
            "pull_attempts": cache.pull_count,
            "declared_compressed_bytes": cache.compressed_bytes,
            "baseline_resources_preserved": True,
            "owned_resources_remaining": 0,
            "physical_provider_calls": 0,
            "provider_acquisition_ready": False,
            "paper_evidence": False,
        }
        private_write(output / "summary.json", summary)
        return summary
    except BaseException as error:
        private_write(
            output / "failure.json",
            {
                "error_type": type(error).__name__,
                "source_sha256": source_sha,
                "physical_provider_calls": 0,
            },
        )
        raise
    finally:
        try:
            if cache is not None:
                cache.close()
        finally:
            try:
                if capacity is not None:
                    capacity.close()
            finally:
                probe_client.close()
                client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "dataset",
        "agent",
        "evaluator",
        "task-assets",
        "image-pins",
        "output",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--bootstrap-image", required=True)
    parser.add_argument("--max-pulls", type=int, default=1)
    parser.add_argument("--max-total-compressed-gib", type=int, default=4)
    parser.add_argument("--max-owned-image-gib", type=int, default=8)
    parser.add_argument("--min-free-gib", type=int, default=12)
    parser.add_argument("--pull-timeout", type=int, default=900)
    parser.add_argument("--control-timeout", type=int, default=600)
    args = parser.parse_args(argv)
    options = vars(args).copy()
    options.pop("max_pulls")
    options.pop("max_total_compressed_gib")
    options.pop("max_owned_image_gib")
    options.pop("min_free_gib")
    options.pop("pull_timeout")
    options["timeout"] = options.pop("control_timeout")
    result = run(
        **options,
        limits=CacheLimits(
            max_pulls=args.max_pulls,
            max_total_compressed_bytes=args.max_total_compressed_gib * GIB,
            max_owned_image_bytes=args.max_owned_image_gib * GIB,
            min_free_bytes=args.min_free_gib * GIB,
            pull_timeout_seconds=args.pull_timeout,
        ),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
