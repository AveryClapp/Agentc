"""Build every fixture we can, skipping any that fail.

Useful for ``python -m bench.build_fixtures`` when GAIA approval is
still pending — SWE-bench + RAG + multi-agent will succeed and GAIA
will print a clear "pending approval" message.
"""

from __future__ import annotations

from bench.build_fixtures import gaia, multiagent, rag, swebench


def main() -> int:
    builders = [
        ("swebench_planner", swebench.build),
        ("rag_summarizer", rag.build),
        ("multiagent_research", multiagent.build),
        ("gaia_router", gaia.build),
    ]

    failures: list[tuple[str, str]] = []
    for name, fn in builders:
        print(f"\n=== {name} ===")
        try:
            fn()
        except SystemExit as e:
            msg = str(e) or "(no message)"
            failures.append((name, msg))
            print(f"  SKIPPED: {msg.splitlines()[0]}")
        except BaseException as e:  # noqa: BLE001 — keep building others
            failures.append((name, f"{type(e).__name__}: {e}"))
            print(f"  FAILED: {type(e).__name__}: {e}")

    if failures:
        print("\nSome builders did not complete:")
        for name, msg in failures:
            print(f"  - {name}: {msg.splitlines()[0]}")
        return 1
    print("\nAll fixtures built successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
