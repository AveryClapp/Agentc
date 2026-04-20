"""Build ``bench/fixtures/swebench_planner.json`` from SWE-bench Lite.

Pulls the first 50 tasks from the public ``test`` split of
``princeton-nlp/SWE-bench_Lite``. No HF auth required.

Accuracy signal: we store the touched files from ``patch`` under
``meta.expected_files`` and the repo name (lowercase first segment of
``repo``) under ``expected`` — a planner that mentions the repo in its
answer gets a weak but directional substring match. The ship-gate
runner can swap in the full SWE-bench evaluation harness later by
reading ``meta.instance_id`` + ``meta.base_commit``.
"""

from __future__ import annotations

import re

from bench.build_fixtures._common import require_datasets, write_fixture

AGENT_KEY = "swebench_planner"
N_TASKS = 50
DATASET = "princeton-nlp/SWE-bench_Lite"
SPLIT = "test"


_DIFF_FILE_RE = re.compile(r"^diff --git a/(.+?) b/", re.MULTILINE)


def _extract_patch_files(patch: str) -> list[str]:
    """Return the unique set of files touched in a unified diff."""
    return sorted({m.group(1) for m in _DIFF_FILE_RE.finditer(patch or "")})


def build() -> None:
    ds_lib = require_datasets()
    print(f"loading {DATASET} [{SPLIT}] ...")
    ds = ds_lib.load_dataset(DATASET, split=SPLIT)
    ds = ds.select(range(min(N_TASKS, len(ds))))

    rows = []
    for row in ds:
        patch_files = _extract_patch_files(row.get("patch", ""))
        repo = row.get("repo", "")
        # Top-level segment of "owner/repo" — a valid plan usually
        # names the project. Weak signal, but harder to game than
        # "any alphanumeric".
        expected = repo.split("/")[-1].lower() if repo else ""
        rows.append(
            {
                "task_id": row["instance_id"],
                "prompt": row["problem_statement"],
                "expected": expected,
                "meta": {
                    "repo": repo,
                    "instance_id": row["instance_id"],
                    "base_commit": row.get("base_commit"),
                    "patch_files": patch_files,
                    "version": row.get("version"),
                },
            }
        )

    write_fixture(AGENT_KEY, rows)


if __name__ == "__main__":
    build()
