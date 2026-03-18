# Spec Writing Guide

Read [README.md](README.md) for project context. This file contains the constraints and style guide for writing and editing specs in this directory.

---

## Directory Rules

- **Top-level `.md` files** in `/specs/` are canonical specs. One file per component.
- **`future-work.md`** holds all out-of-scope items, organized by component. Never put future work inline in a spec.
- **`working/`** holds intermediate documents: research, gap analyses, handoff notes, review artifacts. These inform specs but are not specs.
- **File naming:** Specs are `<component>.md` (lowercase, hyphen-delimited). Working docs are `<component>-<purpose>.md`.

---

## Frontmatter

Every spec and working document must have YAML frontmatter:

```yaml
---
title: Profiler
status: active        # draft | active | deprecated
last-updated: 2026-03-17
---
```

Update `last-updated` on every edit. Git history is the changelog — no version numbers within a spec.

---

## Spec Style Guide

### Tone

Specs are implementation contracts. Write in imperative, present-tense voice. State what the system **does**, not what it "would", "could", or "should" do. No hedging. If a design detail is uncertain, file it as an open question — do not bury ambiguity in soft language.

### Required Sections

Every spec follows this order:

1. **Overview** — What this component does, why it exists, where it sits in the runtime. A reader knows whether this spec is relevant within 30 seconds.
2. **Interface** — Every user-facing surface: CLI commands, API signatures, configuration. Mock CLI output is mandatory. Code examples are mandatory for programmatic interfaces.
3. **Architecture** — Internal design: data flow, concurrency, module boundaries, error propagation, schemas.
4. **Dependencies** — External crates, Python packages, system requirements, sibling components.
5. **Evaluation** — How you verify correctness. Benchmarks, test strategies, acceptance criteria. If you cannot describe how to test it, the design is not finished.

Omit a section only if genuinely inapplicable. Justify the omission in a comment.

### Writing Rules

- **No version numbering.** A spec describes one implementation target. No "V1", "V1.1", "Phase 2 might add...". Update the spec in place and bump `last-updated`.
- **No inline future work.** Anything out of scope goes in `future-work.md` under the component's section, not in the spec.
- **Schema definitions must be copy-pasteable.** SQL is SQL, not pseudocode. A developer can paste it into a shell and get a valid result.
- **Design decisions state the choice and the runner-up.** What was chosen (one sentence why), what was rejected (one sentence why not). No exhaustive essays.
- **Open questions are temporary.** Mark with `> **OPEN:**` blockquote. Every open question must be resolved before `status: active`. An open question without an owner is a bug: `> **OPEN (owner, date):** ...`

### Anti-Patterns

| Don't write | Do instead |
|---|---|
| "We could potentially support..." | Move to `future-work.md` or delete |
| `TODO` with no owner or date | `TODO(alice, 2026-03-20): resolve X` |
| "This should be fast enough" | State the latency target and cite a benchmark |
| Pseudocode for schemas/interfaces | Real, executable code |
| Mixing aspirational features with concrete spec | One spec, one implementation target |
| "For now we'll just..." | Describe the actual design; explain minimality in design decisions |
| Passive voice to avoid commitment | Name the component, module, or function that acts |
