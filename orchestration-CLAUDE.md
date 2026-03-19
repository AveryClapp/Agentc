# Agentc — Multi-Agent Orchestration

<!-- Checked-in reference copy of the orchestration config.
     To activate: symlink .claude/CLAUDE.md → orchestration-CLAUDE.md (gitignored).
     If you're not running a multi-agent NTM session, you can ignore this file. -->

---

## 1) Authority and Precedence

- Root `CLAUDE.md` applies to all agents universally
- This file applies only to orchestrated multi-agent sessions (NTM + beads + Agent Mail)
- `specs/CLAUDE.md` governs spec editing style — authoritative for that directory
- If any instruction here conflicts with root `CLAUDE.md`, root wins

---

## 2) Workspace Topology

```
Agentc/                          ← monorepo root
├── CLAUDE.md                    ← universal project context (checked in)
├── AGENTS.md                    ← symlink → CLAUDE.md (Codex/Gemini autoload)
├── orchestration-CLAUDE.md      ← this file (checked in, reference copy)
├── .claude/CLAUDE.md            ← symlink → orchestration-CLAUDE.md (gitignored)
├── .beads/                      ← issue tracking (monorepo-level, single graph)
├── .mcp.json                    ← Agent Mail MCP server config
├── specs/
│   ├── CLAUDE.md                ← spec style guide
│   ├── AGENTS.md                ← symlink → specs/CLAUDE.md
│   ├── profiler.md              ← active spec
│   ├── memoization.md           ← outline
│   └── optimizer.md             ← outline
└── (submodules planned)
```

---

## 3) Agent Coordination

### Session Layout

- Pane 1: User/Orchestrator
- Panes 2–N: Worker agents (callsigns assigned at bootstrap via `ntm send`)
- Use `ntm status <session> --json` to see current pane assignments and identities

### Pane Title Persistence

After renaming a pane (e.g., `tmux select-pane -t %N -T "AgentName"`), lock the title:

```bash
tmux set-option -p -t %N allow-set-title off
```

Without this, Claude Code's status spinner overwrites the title via OSC escape sequences. This is especially important for panes moved via `tmux join-pane` (which bypass NTM's `allow-set-title off` setup). NTM-spawned panes already have this set.

### Communication

- Primary: MCP Agent Mail (threaded messaging, file reservations)
- Fallback: `ntm send <session> --skip-first "message"` if Agent Mail unavailable
- One `thread_id` per coordination topic
- Include thread ID in Agent Mail subjects and bead notes

---

## 4) Beads (Issue Tracking)

### Core Commands

```bash
br ready                         # Show unblocked work
br create "Title"                # Create new bead
br claim <id>                    # Claim a bead (atomic)
br close <id>                    # Close after validation passes
br show <id>                     # Full bead details
br list --status=in_progress     # What's currently claimed
br sync                          # Commit + push bead changes
br sync --flush-only             # Checkpoint without git side-effects
```

### Rules

- Single `.beads/` at monorepo root — no submodule-level beads
- Single-bead-single-worker: NEVER have two agents working the same bead
- If `br claim` fails → do NOT start work, return to `br ready`
- Beads survive context compaction — use `br show <id>` to recover state after restart

### Lifecycle

```
open → claimed (br claim) → in_progress → validate → closed (br close)
                                              ↓
                                         blocked → create follow-up bead
```

---

## 5) File Reservation Protocol

- Reserve file surfaces BEFORE substantive edits
- Announce reservations in Agent Mail with bead ID
- Release reservations immediately on completion or handoff
- Force-release if holder inactive >= 30 minutes (document reason + notify holder)
- NEVER overwrite another agent's active edits unless data-loss or security risk

---

## 6) Multi-Agent Execution Lifecycle

For each bead:

1. **Claim** — `br claim <id>` (atomic, fail = don't start)
2. **Reserve** — announce file surfaces in Agent Mail
3. **Implement** — do the work
4. **Validate** — run all applicable gates (lint, type check, tests)
5. **Close** — `br close <id>` only after ALL gates pass
6. **Release** — release file reservations, update Agent Mail thread

---

## 7) Quality Gates

### Fail-Closed Rule

Do NOT close beads, declare completion, or push while required gates are failing — unless a documented exception exists.

### Validation Commands (when code exists)

```bash
# Rust
cargo check                      # Type check
cargo test                       # Unit tests
cargo clippy                     # Lint

# Python
uv run mypy src/                 # Type check
uv run pytest tests/ -v          # Tests
```

### Testing Standard for Behavior Changes

- Happy path coverage
- Edge/boundary coverage
- Error-handling coverage
- Regression test for bug fixes

---

## 8) Git / Branch Semantics

- Default branch: `main`
- Bead work branches: `bead/<id>-<short-slug>` (e.g., `bead/7-profiler-span-serialization`)
- After `br sync`, verify push succeeded: `git log --oneline origin/main..HEAD` should be empty
- Never push directly to `main` from a worker pane — use PR or orchestrator merge
- `br sync` includes git side-effects by default (commit + pull + push)
- `br sync --flush-only` for checkpoint exports without git ops

---

## 9) Automation Command Contract

- All commands agents execute MUST be non-interactive (no TUI, no pager, no interactive prompts)
- Prefer `--json` or structured output flags where available
- If a command requires interactive input, STOP and ask the orchestrator for an alternative
- Never use: `git rebase -i`, `git add -i`, editors via `$EDITOR`, or any command that opens a pager
- Do NOT invoke `bv` in automated sessions (TUI) — use `br ready`, `br show`, `br list` instead

---

## 10) Destructive-Action Safety

- If command impact is uncertain → STOP and ask
- Two-step confirmation for destructive operations:
  1. Restate exact command and affected scope
  2. Wait for explicit user confirmation
- Safer alternatives first: check status/diff/stash/backup before deletions
- No force deletes, force pushes, or hard resets without explicit permission

---

## 11) Checkpoint vs Completion

| | Checkpoint | Completion |
|---|---|---|
| Purpose | Save progress snapshot | Declare work done |
| Command | `br sync --flush-only` | `br close <id>` + `br sync` |
| Gates required | No | All must pass |
| Reservations | Kept | Released |
| When | Pre-handoff, pre-context-switch, major milestone | All acceptance criteria met |

---

## 12) Handoff Schema

When handing off work (session end, context limit, agent swap), include:

- Bead IDs and current status
- Agent Mail thread ID
- Files touched
- Commands/tests run (pass/fail/blocked)
- Blockers with named owner and next action
- Reservation release status

---

## 13) Blocker Classification

### external_block (credential/paywall/approval/legal)
- Document in bead notes
- Open follow-up bead
- Continue unrelated work

### structural_block (code/schema/parser defect)
- Create immediate fix bead
- Link dependency explicitly
- Continue unless hard-block

### env_block (disk/runtime/broken service)
- Create immediate fix bead
- Continue unless hard-block

### Hard-block criteria (stop integration work)
- Worker/session hang
- Schema invalidation
- Transaction corruption
- Otherwise: catalog, open fix bead, move on

---

## 14) Exception Template

When deviating from any policy above:

```
- Clause: <which section>
- Reason: <why deviation needed>
- Approver: <user/captain>
- Expiry: <timebox or condition>
- Follow-up bead: <id>
```

---

## 15) Blocker Escalation Timer

- If blocked for > 30 minutes with no resolution path: escalate to orchestrator via Agent Mail with subject `ESCALATION: <bead-id> — <blocker-type>`
- Do NOT spin on a blocker. After two failed resolution attempts, catalog the blocker, checkpoint (`br sync --flush-only`), and move to the next `br ready` item.

---

## 16) NTM Send-Confirm Contract

When using NTM fallback (Agent Mail unavailable):

1. Type the full `ntm send` command
2. Press Enter
3. Verify the output shows delivery confirmation (e.g., `Sent to N pane(s)`)
4. If delivery fails, retry once, then escalate to orchestrator

Do NOT assume delivery without confirmation output.

---

## 17) Artifact Hygiene

### Canonical (tracked, changes require bead + validation)
- Specs: `specs/*.md` at top level
- CLAUDE.md files (root, specs/, orchestration)
- README.md

### Transient (ephemeral, may be deleted without ceremony)
- `specs/working/*` — research, gap analyses, handoff docs
- `.ntm/` runtime state
- `.beads/beads.db` (derived from issues.jsonl)
- Scratch notes, draft outlines, exploration logs

### Promotion Gate
To promote a transient artifact to canonical: create a bead, ensure it meets the spec style guide, get orchestrator acknowledgment, check in and close the bead.

---

## 18) Plan-to-Bead Decomposition Gate

Before beginning implementation of any plan or spec section:

1. Decompose into beads — one bead per independently-testable deliverable
2. Check for blocking ambiguities:
   - Are acceptance criteria clear enough to validate?
   - Are file surfaces identified (no two beads touching the same file)?
   - Are dependencies between beads explicit?
3. If ANY ambiguity would block implementation: do NOT create beads. Document the ambiguity, post clarifying questions, and resolve before proceeding.
4. Post the bead graph to Agent Mail for orchestrator review before workers begin claiming.

---

## 19) Skill Activation Scoping

- **Captain/Orchestrator** (pane 1): may use orchestrator-routing skills (palette-first-orchestrator, state-aware-orchestrator-nudge)
- **Lane workers** (panes 2–N): implementation and validation only. Do NOT use orchestrator skills unless explicitly assigned orchestration duties.
