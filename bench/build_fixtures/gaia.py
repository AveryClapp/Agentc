"""Build ``bench/fixtures/gaia_router.json`` from the GAIA benchmark.

Pulls the first 80 questions from the ``validation`` split of
``gaia-benchmark/GAIA`` (levels 1 + 2, skipping multimodal tasks that
require attached files — the router agent doesn't see images).

GAIA is a gated dataset: you must request access at
https://huggingface.co/datasets/gaia-benchmark/GAIA and have the
``HF_TOKEN`` env var set. The builder bubbles up a clear error if
either condition is unmet.

Accuracy signal: ``Final answer`` goes into ``expected`` verbatim.
GAIA's ship-gate check is an exact-match after normalization; the
synthetic substring check is a weak stand-in that the ship-gate runner
can override with GAIA's official ``scorer.py``.
"""

from __future__ import annotations

import os

from bench.build_fixtures._common import require_datasets, write_fixture

AGENT_KEY = "gaia_router"
N_TASKS = 80
DATASET = "gaia-benchmark/GAIA"
CONFIG = "2023_all"
SPLIT = "validation"


def build() -> None:
    ds_lib = require_datasets()

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token:
        raise SystemExit(
            "GAIA is gated. Set HF_TOKEN in your environment (or .env) "
            "and request access at:\n"
            "    https://huggingface.co/datasets/gaia-benchmark/GAIA"
        )

    print(f"loading {DATASET} [{CONFIG}/{SPLIT}] ...")
    ds = ds_lib.load_dataset(DATASET, CONFIG, split=SPLIT, token=token)

    rows = []
    for row in ds:
        # Skip multimodal tasks — file_name is set when a PDF / image
        # attachment is required. Our router agent is text-only.
        if row.get("file_name"):
            continue
        question = row.get("Question") or row.get("question")
        answer = row.get("Final answer") or row.get("final_answer")
        if not question or not answer:
            continue
        rows.append(
            {
                "task_id": f"gaia-{row.get('task_id', len(rows))}",
                "prompt": question,
                "expected": str(answer),
                "meta": {
                    "level": row.get("Level") or row.get("level"),
                    "annotator_metadata": row.get("Annotator Metadata")
                    or row.get("annotator_metadata"),
                },
            }
        )
        if len(rows) >= N_TASKS:
            break

    if not rows:
        raise SystemExit(
            "No GAIA rows materialized — dataset format may have changed, "
            "or your token doesn't have access yet."
        )

    write_fixture(AGENT_KEY, rows)


if __name__ == "__main__":
    build()
