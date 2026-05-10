---
title: Artifact Evaluation Plan
status: draft
last-updated: 2026-05-08
owner: paper-intelligence
---

# Artifact Evaluation Plan

This file tracks what a reviewer or collaborator could reproduce.

## Artifact Questions

- Which datasets/fixtures can be regenerated from public sources?
- Which results require API keys?
- What model versions and pricing assumptions matter?
- What can be released as committed artifacts?
- What remains local-only?
- What does each artifact verify?

## Current Status

| ID | Status | Artifact | Verifies | Release/Rerun Notes |
|---|---|---|---|---|
| `AE-001` | `open` | `bench/paper_results/*.csv` | canonical current results | committed, but validation metadata should be added before submission |
| `AE-002` | `open` | benchmark scripts | reproduction path | API keys and fixtures required for full reruns |
| `AE-003` | `open` | paper intelligence references | paper context | source docs can be tracked unless sensitivity/licensing concern appears |

