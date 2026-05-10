---
title: System And Rules Brief
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# System And Rules Brief

## System Points To Support

- Python interception captures LLM calls and routes optimizer planning through the Rust optimizer.
- The planner waits for hot call sites before applying optimization proposals.
- Rules have explicit preconditions and projected savings.
- If no proposal passes safety checks, the call is passed through unchanged.

## Rule Points

- `ContextCompress`: removes low-salience context from large prompts when attention/salience evidence supports it.
- `ModelDowngrade`: routes simple structured call sites to cheaper models when divergence risk is low.
- `StateDrop`: removes stale state-tagged context when current reads do not require it.

## Evidence Pointers

- `crates/agentc-optimizer/src/planner.rs`
- `crates/agentc-optimizer/src/rules/context_compress.rs`
- `crates/agentc-optimizer/src/rules/model_downgrade.rs`
- `crates/agentc-optimizer/src/rules/state_drop.rs`
- `python/agentc/_intercept.py`
- `python/agentc/_optimizer.py`

