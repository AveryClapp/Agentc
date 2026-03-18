---
title: Semantic Memoization
status: draft
last-updated: 2026-03-17
---

# Semantic Memoization

A content-addressed caching layer that deduplicates LLM inference at the semantic level. Uses locality-sensitive hashing (LSH) over embeddings so that semantically equivalent prompts — not just exact matches — return cached results.

---

## Overview

Most agent pipelines make redundant LLM calls: rephrased versions of the same question, repeated lookups across agents, re-analysis of unchanged context. Semantic memoization intercepts these calls and serves cached results when the input is semantically close enough to a prior call, cutting token spend without changing agent behavior.

Includes negative caching — if agent A explored a dead-end path, agent B skips it. The cache becomes shared memory that gets smarter as more agents run.

---

## Interface

<!-- TODO -->

---

## Architecture

<!-- TODO -->
<!-- - Embedding generation for incoming prompts -->
<!-- - LSH index for approximate nearest-neighbor lookup -->
<!-- - Cache storage (Redis/SQLite) with TTL and eviction -->
<!-- - Similarity threshold tuning: when is "close enough" safe? -->
<!-- - Negative cache: dead-end signal propagation -->

---

## Dependencies

- Profiler traces inform cache hit/miss analysis and tuning
- Requires an embedding model (local or API-based) for LSH

---

## Evaluation

<!-- TODO -->
<!-- - Cache hit rate on real agent workloads -->
<!-- - Token savings vs. accuracy degradation -->
<!-- - False positive rate: how often does a "close enough" match return a wrong result? -->
<!-- - Comparison against exact-match caching baseline -->
