# Agentc

Runtime that intercepts an LLM agent's model calls without changing application code. Current research use: a change-safety study measuring whether cheap call-level signals from sampled counterfactual calls predict task-level damage when a deployed agent's calls are changed (see `bench/repro/change-safety-study-protocol.md`).

## Repo Structure

```
Agentc/
├── CLAUDE.md                ← this file (universal project context)
├── AGENTS.md                ← symlink → CLAUDE.md (Codex/Gemini autoload)
├── README.md                ← project README for GitHub
├── specs/                   ← technical specifications
│   ├── CLAUDE.md            ← spec style guide (authoritative for this directory)
│   ├── profiler.md
│   ├── memoization.md
│   ├── optimizer.md
│   └── working/             ← research, gap analyses, handoff docs
├── crates/                  ← Rust workspace: core, embed, memo, profiler, analyzer, optimizer, cli
├── python/agentc/           ← Python SDK: SDK patches, framework adapters, canonicalizers, interceptor
├── bench/                   ← evaluation harness (agents, drivers, paper_results/, repro/)
├── tests/                   ← Python unit tests
├── main.tex                 ← the manuscript (under submission)
├── figures/                 ← figures the manuscript includes
└── paper-intelligence/      ← paper evidence/literature/venue workspace; not specs
```

> To learn what ships (rule names, counts, CLI subcommands), read the code — this file
> deliberately states no count that can go stale. The runtime, profiler, memoization layer,
> optimizer, and `agentc` CLI are all implemented and tested.

## Languages & Stack

- Rust core runtime (DAG IR, optimizer, executor) — Cargo workspace
- Python bindings via PyO3/maturin for SDK instrumentation and benchmarking
- Python 3.12+

## Conventions

- Commit messages: imperative mood, concise (e.g., `Add profiler span serialization logic`)
- **No standalone bookkeeping commits.** Never commit a status/tracker update on its
  own — no commit whose only change is a resume/status doc or a "record bead closes"
  note. Fold any such update into the substantive commit it accompanies, or
  `git commit --amend` while the commit is still local. Durable cross-session state
  belongs in the tracker (a bead comment / dedicated issue), not a committed doc.
- Branches: `feat/<slug>`, `fix/<slug>`, or `spec/<slug>`
- Default branch: `main`
- No over-engineering — minimum complexity for the current task
- Read the full file before editing any spec
- When editing specs, follow the style guide in `specs/CLAUDE.md`
- Do not introduce dependencies not listed in the stack section without discussion

## Guardrails

- Do NOT force-push, hard-reset, or rebase shared branches without explicit permission
- Do NOT create new top-level directories without discussion
- Do NOT write placeholder or filler code — prefer stubs with TODO comments
- Do NOT modify specs without reading the entire file first (specs have internal cross-references)
- If a command's impact is uncertain, STOP and ask

## Commands

```bash
# Rust (Cargo workspace under crates/)
cargo check --workspace
cargo test --workspace --exclude agentc-profiler   # profiler is a PyO3 cdylib; test via pytest
cargo clippy --workspace --exclude agentc-profiler
cargo build --release --workspace --exclude agentc-profiler   # the extension builds via maturin, not cargo

# Python (SDK + bench under python/, bench/, tests/)
maturin develop --release        # build the PyO3 extension into the active venv
uv run mypy python/agentc
uv run pytest tests/ -v
```

## Start Here

1. This file — project context, conventions, guardrails
2. `specs/README.md` — overview of the three components and build order
3. The relevant spec file for your task (you'll be pointed to one)
4. `specs/CLAUDE.md` — before editing any spec
