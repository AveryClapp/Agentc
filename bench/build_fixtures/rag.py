"""Build ``bench/fixtures/rag_summarizer.json`` from CNN/DailyMail.

Pulls the first 200 rows from the ``test`` split of
``abisee/cnn_dailymail`` (config ``3.0.0``, public, no HF auth needed).

Each article is split into ~3 paragraph-level chunks and stashed under
``meta.chunks`` so the RAG agent's fan-out path runs one LLM call per
chunk — that's what exercises the optimizer's ``ParallelBranch`` rule.

Accuracy signal: the reference ``highlights`` summary goes into
``expected``. The synthetic substring check is weak here (long strings
rarely match exactly); the ship-gate runner should swap in ROUGE-L
against ``meta.reference_summary`` for real numbers.
"""

from __future__ import annotations

from bench.build_fixtures._common import require_datasets, write_fixture

AGENT_KEY = "rag_summarizer"
N_TASKS = 200
DATASET = "abisee/cnn_dailymail"
CONFIG = "3.0.0"
SPLIT = "test"

# Target chunk size in characters — ~500 chars ≈ 100-150 tokens, small
# enough that a 3-chunk article gives the fan-out path real work.
CHUNK_TARGET_CHARS = 500


def _chunk_article(article: str, target: int = CHUNK_TARGET_CHARS) -> list[str]:
    """Split ``article`` into chunks on paragraph boundaries, packing
    paragraphs together until each chunk hits ``target`` characters."""
    paragraphs = [p.strip() for p in article.split("\n") if p.strip()]
    if not paragraphs:
        return [article]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for p in paragraphs:
        if current_len + len(p) > target and current:
            chunks.append(" ".join(current))
            current = [p]
            current_len = len(p)
        else:
            current.append(p)
            current_len += len(p) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks


def build() -> None:
    ds_lib = require_datasets()
    print(f"loading {DATASET}/{CONFIG} [{SPLIT}] ...")
    ds = ds_lib.load_dataset(DATASET, CONFIG, split=SPLIT)
    ds = ds.select(range(min(N_TASKS, len(ds))))

    rows = []
    for row in ds:
        article = row.get("article") or ""
        highlights = row.get("highlights") or ""
        if not article or not highlights:
            continue
        chunks = _chunk_article(article)
        rows.append(
            {
                "task_id": f"cnn-{row.get('id', len(rows))}",
                "prompt": "Summarize the following article.",
                "expected": highlights,
                "meta": {
                    "chunks": chunks,
                    "reference_summary": highlights,
                    "n_chunks": len(chunks),
                },
            }
        )

    write_fixture(AGENT_KEY, rows)


if __name__ == "__main__":
    build()
