---
title: Future Work
status: active
last-updated: 2026-04-16
---

# Future Work

Items explicitly out of scope for current specs. Each section corresponds to a canonical spec. Entries here may become their own specs if pursued.

---

## Profiler

- **Real-time dashboard**: Streaming trace data to a live dashboard. Current design is post-hoc analysis only.
- **OTLP push export**: Direct OTLP gRPC/HTTP push to backends. Currently exports to JSON files.
- **Framework integrations**: Auto-instrumentation for LangGraph, CrewAI, and Claude Code's tool loop. The span API does not preclude it.
- **Google SDK support**: Both `google.generativeai` (legacy) and `google-genai` (new, Gemini 2.0+).
- **OTel events**: Content capture via `gen_ai.client.inference.operation.details` events for full OTel spec compliance.
- **OTel metrics**: Standard histograms (`gen_ai.client.token.usage`, `gen_ai.client.operation.duration`).
- **Eval export**: Trace-to-dataset export format compatible with eval frameworks (Braintrust, Langfuse JSONL).
- **Sampling**: Head-based or tail-based sampling for high-throughput production deployments.
- **`unused_output` waste detector**: Needs application-level tracing to know whether output was consumed. Not feasible with SDK-level instrumentation.

---

## Semantic Memoization

- **Negative caching (dead-end propagation)**: Cache known-failed or known-unproductive paths so agents skip them on rediscovery. Current design caches only successful outputs.
- **Cross-model cache entries**: Allow a cache entry from `gpt-4o` to satisfy a lookup for `gpt-4o-mini` when the output shape is known to be model-agnostic. Current design scopes each entry to a single model.
- **Multi-modal prompt canonicalization**: Today's canonicalizer hashes image/audio parts by SHA-256 of their raw bytes, so visually-identical but byte-different inputs miss. A perceptual hash (pHash for images, fingerprinting for audio) would close that gap.
- **Semantic eviction**: Evict entries whose prompt/output semantics are covered by other, higher-hit-count entries. Current eviction is LRU + TTL only.
- **Distributed cache**: Optional Redis or S3 backend for shared caching across machines. Current design is single-workspace SQLite.

---

## Optimizer

- **Streaming LLM responses**: Cache replay, parallel fan-out, and model downgrade all need special handling for streaming. Current implementation disables the optimizer when `stream=True`.
- **Vendor-side prompt cache interaction**: `ContextCompress` is conservatively disabled when `cache_control` markers are present; a proper cost model for cached-prefix length gained/lost would recover the savings.
- **Rule composition**: First-match-wins is the initial contract. A disciplined composition scheme with cumulative accuracy budget and per-rule divergence attribution could stack ContextCompress + ModelDowngrade on the right call sites.
- **Learned cost predictor**: Replace the per-call-site empirical model with a small learned predictor (e.g., gradient-boosted trees) that generalizes across call sites. Buys accuracy on rare call sites at the cost of training infrastructure.
- **Speculative pre-execution**: When the DAG context makes the next call's inputs predictable, fire it speculatively. Orthogonal to `ParallelBranch`, which only parallelizes already-issued calls.
- **Additional rewrite rules**: `PromptTemplateRewrite` (replace a high-cost template with a distilled alternative), `ToolCallElision` (skip tools whose outputs the LLM ignores), `BatchAcrossCalls` (combine small sibling calls into one).
- **Cost-model eviction policy**: Time-decay or structural-hash-based invalidation for `call_site_profile` rows whose underlying prompt template has changed.
- **Non-Python SDK support**: Current intercept and provenance tagging are Python-only. TypeScript / Node parity requires a mirror SDK.
