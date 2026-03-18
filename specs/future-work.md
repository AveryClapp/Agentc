---
title: Future Work
status: active
last-updated: 2026-03-17
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

_(No future-work items yet — spec is still in outline stage.)_

---

## Optimizer

_(No future-work items yet — spec is still in outline stage.)_
