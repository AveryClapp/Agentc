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

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for durable project knowledge and `bd memories` / `bd recall`
  to retrieve it — do NOT use MEMORY.md files.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work and update in-progress items
4. **SYNC AND PUSH** - This is MANDATORY:
   ```bash
   bd dolt pull            # merge the authoritative remote issue history
   bd dolt push            # publish local issue-history commits
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
