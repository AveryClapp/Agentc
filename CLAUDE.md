# Agentc

JIT optimization runtime for multi-step LLM agent workloads. Intercepts LLM calls and applies principled optimizations to reduce token spend without changing application code.

<!-- NOTE: Multi-agent orchestration (beads, Agent Mail, NTM) is available.
     See orchestration-CLAUDE.md for the full architecture.
     Activate by symlinking .claude/CLAUDE.md → orchestration-CLAUDE.md.
     Not required for solo contribution — it's for coordinated agent sessions. -->

## Repo Structure

```
Agentc/
├── CLAUDE.md                ← this file (universal project context)
├── AGENTS.md                ← symlink → CLAUDE.md (Codex/Gemini autoload)
├── README.md                ← project README for GitHub
├── orchestration-CLAUDE.md  ← multi-agent coordination rules (ignore unless in NTM session)
├── specs/                   ← technical specifications
│   ├── CLAUDE.md            ← spec style guide (authoritative for this directory)
│   ├── profiler.md
│   ├── memoization.md
│   ├── optimizer.md
│   └── working/             ← research, gap analyses, handoff docs
└── (submodules planned)
```

## Languages & Stack

- Rust core runtime (DAG IR, optimizer, executor) — Cargo workspace
- Python bindings via PyO3/maturin for SDK instrumentation and benchmarking
- Python 3.12+

## Conventions

- Commit messages: imperative mood, concise (e.g., `Add profiler span serialization logic`)
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

No implementation code exists yet. When it does:

```bash
# Rust (planned)
cargo check                      # Type check
cargo test                       # Unit tests
cargo clippy                     # Lint

# Python (planned)
uv run mypy src/                 # Type check
uv run pytest tests/ -v          # Tests
```

## Start Here

1. This file — project context, conventions, guardrails
2. `specs/README.md` — overview of the three components and build order
3. The relevant spec file for your task (you'll be pointed to one)
4. `specs/CLAUDE.md` — before editing any spec
