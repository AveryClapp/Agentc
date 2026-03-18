# Agentc Profiler: Competitive & Technical Gap Analysis

> Generated 2026-03-17. Based on research into OTel GenAI conventions, six competing/adjacent tools, PyO3/maturin patterns, SDK monkey-patching approaches, and similarity hashing techniques.

---

## 1. OTel `gen_ai.*` Semantic Conventions Alignment

### Key Findings

The OTel GenAI semantic conventions have matured significantly since the SIG launched in April 2024. All `gen_ai.*` attributes remain in **Development** stability status (not yet Stable), but the surface area is now large and well-defined.

**Current attribute families:**

| Family | Examples | Status |
|---|---|---|
| Core span attrs | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model` | Development |
| Token usage | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_creation.input_tokens`, `gen_ai.usage.cache_read.input_tokens` | Development |
| Request params | `gen_ai.request.temperature`, `gen_ai.request.top_p`, `gen_ai.request.max_tokens`, `gen_ai.request.seed`, `gen_ai.request.stop_sequences`, `gen_ai.request.frequency_penalty`, `gen_ai.request.presence_penalty` | Development |
| Response metadata | `gen_ai.response.id`, `gen_ai.response.finish_reasons` | Development |
| Agent attrs | `gen_ai.agent.id`, `gen_ai.agent.name`, `gen_ai.agent.description`, `gen_ai.agent.version` | Development |
| Tool attrs | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.description`, `gen_ai.tool.type`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` | Development |
| Content (opt-in) | `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions` | Development |
| Conversation | `gen_ai.conversation.id` | Development |
| Retrieval | `gen_ai.retrieval.documents`, `gen_ai.retrieval.query.text`, `gen_ai.data_source.id` | Development |

**Standardized operation names** (`gen_ai.operation.name`):
- `chat`, `embeddings`, `text_completion`, `generate_content`, `retrieval`, `execute_tool`, `create_agent`, `invoke_agent`

**Standardized metrics:**
- `gen_ai.client.token.usage` (histogram, unit: `{token}`)
- `gen_ai.client.operation.duration` (histogram, unit: `s`)
- `gen_ai.server.request.duration` (histogram, unit: `s`)
- `gen_ai.server.time_per_output_token` (histogram, unit: `s`)
- `gen_ai.server.time_to_first_token` (histogram, unit: `s`)

**Content capture model:** OTel now supports content capture in **two** ways:
1. As **span attributes** (`gen_ai.input.messages`, `gen_ai.output.messages`) — stored on the span itself
2. As **events** (via `gen_ai.client.inference.operation.details`) — opt-in, independent from traces

The convention explicitly says content SHOULD be in structured form (not flat text), and MUST be structured when recorded as events.

**Agent conventions (new):** There is an active proposal ([Issue #2664](https://github.com/open-telemetry/semantic-conventions/issues/2664)) to extend conventions for agentic systems with `gen_ai.task.*`, `gen_ai.action.*`, `gen_ai.team.*`, `gen_ai.artifact.*`, and `gen_ai.memory.*` families. This is still early (0/10 subtasks complete) but signals the direction.

**Versioning:** Instrumentations can opt into `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` to emit the latest experimental conventions.

### Gaps in the Agentc Spec

1. **Missing attributes the spec should capture:**
   - `gen_ai.request.temperature`, `gen_ai.request.top_p`, `gen_ai.request.max_tokens`, `gen_ai.request.seed` — the spec mentions capturing model and tokens but not request parameters. These are important for the optimizer (model overkill detection, reproducibility).
   - `gen_ai.response.id` — provider-assigned response ID, essential for correlating with provider logs.
   - `gen_ai.response.finish_reasons` — important for distinguishing `stop` vs `max_tokens` vs `tool_use` (tells you if output was truncated).
   - `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` — tool use is fundamental to agent workloads. The spec mentions tool spans but doesn't specify how tool call attributes map to OTel conventions.
   - `gen_ai.conversation.id` — useful for session-level analysis.

2. **Content capture format mismatch:** The spec stores content as compressed blobs in a separate `content` table keyed by SHA-256. OTel conventions expect content as **structured** attributes or events (JSON array of message objects with role/content). The Agentc format is more efficient for the analyzer but loses OTel interop for content — an OTLP export would need to reconstruct structured messages from the blob.

3. **Events not mentioned:** The spec only discusses spans. OTel GenAI conventions define events (`gen_ai.client.inference.operation.details`) for opt-in content capture. If Agentc exports to OTel backends, it should emit these events.

4. **Metrics not mentioned:** OTel defines standard histograms (`gen_ai.client.token.usage`, `gen_ai.client.operation.duration`, `gen_ai.server.time_to_first_token`). The spec captures this data in spans but doesn't emit OTel metrics. Backends that expect metrics (Grafana, Datadog) will miss this signal.

5. **`gen_ai.operation.name` values:** The spec uses `kind` values `llm`, `tool`, `agent`. OTel uses `chat`, `text_completion`, `embeddings`, `execute_tool`, `invoke_agent`, `create_agent`. These need to map cleanly. The spec should either adopt the OTel operation names or define a mapping.

6. **Semconv version pinning:** The spec doesn't mention which semconv version it targets. Given all attributes are in Development status and may change, the spec should pin to a semconv version (e.g., v1.29.0) and document the opt-in behavior.

### Recommendations

- Add a "GenAI Attributes Mapping" table to the spec that maps every captured field to the corresponding `gen_ai.*` attribute name, with the semconv version noted.
- Capture `temperature`, `top_p`, `max_tokens`, `seed`, `response_id`, `finish_reasons`, and tool call attributes at instrumentation time.
- Store content in structured form (array of `{role, content}` messages) internally, even if also storing compressed blobs for similarity analysis. This makes OTLP export straightforward.
- Define an event emission path for OTLP export.
- Consider emitting OTel metrics alongside traces (can be done from the background writer thread).

---

## 2. Competitive/Adjacent Tool Analysis

### Tool-by-Tool Breakdown

#### OpenLLMetry / Traceloop
- **What it is:** Open-source OTel instrumentation library for LLMs. Ships as separate pip packages per provider (`opentelemetry-instrumentation-anthropic`, `-openai`, etc.). Pure Python. Apache 2.0.
- **Strengths:** Broadest provider coverage (20+ providers), framework integrations (LangChain, LlamaIndex, CrewAI, Haystack), vector DB instrumentation (Pinecone, Chroma, Weaviate), Go and Ruby support. Plugs into any OTel backend.
- **Weaknesses:** No analysis layer, no waste detection, no cost computation. No local storage — needs an external backend. No Rust, no performance optimization of the instrumentation layer itself.
- **How streaming is handled:** Wraps the stream iterator, accumulates chunks, emits span on stream completion. Uses `TRACELOOP_TRACE_CONTENT` env var to toggle content capture.
- **Relevance to Agentc:** This is the closest open-source comparable for the instrumentation layer. Agentc's instrumentation will be compared directly against OpenLLMetry. The key differentiator must be the analysis/optimizer layer and the Rust performance story.

#### Langfuse
- **What it is:** Open-source LLM observability platform (self-hostable). YC W23. Apache 2.0 core.
- **Data model:** Traces > Observations (Events, Spans, Generations). Observation-centric — trace attributes propagated to every observation to avoid joins. Uses ClickHouse for analytics.
- **Strengths:** Rich UI (playground, prompt management, prompt versioning, datasets for eval), native OTEL SDK v3 (thin layer on OTel client), cost tracking with automatic model pricing, 50+ integrations, session-level analytics, LLM-as-a-Judge evaluation tracing, user feedback collection.
- **Weaknesses:** Requires deploying a server (ClickHouse + app). No local-first mode. No waste detection or optimization. No similarity analysis. Heavy operational overhead for a solo developer or small team.
- **What Agentc doesn't have:** Prompt management, prompt versioning, evaluation framework, datasets for testing, UI/dashboard, user feedback collection, session analytics.
- **What Agentc has that Langfuse doesn't:** Rust core for analysis performance, local-first SQLite storage (zero ops), content fingerprinting for similarity, waste pattern detection, cost optimization recommendations, designed as a foundation for an optimizer (not just an observer).

#### Arize Phoenix
- **What it is:** Open-source AI observability + evaluation. Fully free, no feature gates. Self-hostable single container.
- **Strengths:** Built on OpenTelemetry + OpenInference (their own semantic conventions, complementary to OTel). Deep agent evaluation (multi-step traces with decision analysis). Prompt management (added April 2025). Playground for prompt optimization. Framework-agnostic. Supports Claude Agent SDK, OpenAI Agents SDK, LangGraph, Mastra, Vercel AI SDK, CrewAI, DSPy.
- **Weaknesses:** Server-required (though single container). OpenInference conventions are non-standard (not pure OTel `gen_ai.*`). No waste detection, no cost optimization.
- **Key differentiator vs Agentc:** Phoenix focuses on evaluation and debugging — understanding *what happened* and *was it correct*. Agentc focuses on efficiency — understanding *where tokens were wasted* and *what could be cheaper*. Complementary, not directly competing.

#### LangSmith
- **What it is:** Commercial observability platform from LangChain. Proprietary, SaaS-first.
- **Strengths:** Deep LangChain/LangGraph integration (zero-config). Trace Mode in Studio. Automatic trace clustering and failure mode detection. Scheduled trace exports. End-to-end OTel support. Python/TS/Go/Java SDKs.
- **Weaknesses:** Vendor lock-in to LangChain ecosystem. Per-user pricing ($39/user/mo). Not open-source. Limited cost optimization.
- **Relevance to Agentc:** LangSmith is optimized for LangChain users. Agentc targets framework-agnostic, any-SDK workloads. The automatic trace clustering and failure mode detection is interesting — Agentc's waste detectors serve a similar but more focused purpose.

#### Helicone
- **What it is:** Open-source LLM observability via API proxy. YC W23. Rust gateway.
- **Strengths:** 8ms P50 proxy latency (Rust). Intelligent caching (Redis, configurable TTL, up to 95% cost reduction). Smart load balancing across providers. Rate limiting. Self-hostable via Docker. Session-level metrics.
- **Weaknesses:** Proxy-only — blind to agent-level structure, tool execution, prompt assembly (exactly the limitation the Agentc spec calls out). Cannot see workflow nesting.
- **What Agentc can learn:** Helicone's caching layer is a concrete cost-reduction mechanism that Agentc's analyzer could *recommend* but doesn't *implement*. The 8ms P50 Rust proxy is a good benchmark for Agentc's overhead budget.

#### Braintrust
- **What it is:** Commercial AI observability + eval platform. $80M Series B (Feb 2026). Proprietary.
- **Strengths:** AI Proxy with automatic caching (sub-100ms cached responses, edge deployment). 25+ built-in scorers. Production traces become eval cases with one click. "Loop" AI assistant generates custom scorers from natural language. Brainstore database: 80x faster than traditional DBs for AI workload patterns. Unified offline eval + production monitoring UI. CI/CD integration for eval-on-PR.
- **Weaknesses:** Proprietary, SaaS-dependent. Not self-hostable at the core analytics layer. Expensive at scale.
- **What Agentc can learn:** The "production trace -> eval case" workflow is powerful and Agentc could enable this by exporting traces in formats compatible with eval frameworks. The edge caching proxy is outside Agentc's scope but the waste detector could identify *where* caching would help.

### Competitive Positioning Matrix

| Capability | Agentc | OpenLLMetry | Langfuse | Phoenix | LangSmith | Helicone | Braintrust |
|---|---|---|---|---|---|---|---|
| Local-first (no server) | **Yes** | No | No | No | No | No | No |
| Zero-config auto-instrument | **Yes** | Yes | Partial | Yes | Yes (LangChain) | Yes (proxy) | Yes (proxy) |
| OTel native | **Yes** | Yes | Yes (v3) | Partial (OpenInference) | Yes | No | Partial |
| Streaming instrumentation | **Yes** | Yes | Yes | Yes | Yes | Yes (proxy) | Yes (proxy) |
| Waste detection | **Yes** | No | No | No | No | No | No |
| Cost optimization recs | **Yes** | No | No | No | No | No | No |
| Content fingerprinting | **Yes** | No | No | No | No | No | No |
| Agent-level tracing | **Yes** | Yes | Yes | Yes | Yes | No | Yes |
| Prompt management | No | No | **Yes** | **Yes** | **Yes** | No | No |
| Eval framework | No | No | **Yes** | **Yes** | **Yes** | No | **Yes** |
| UI/Dashboard | No (CLI) | No | **Yes** | **Yes** | **Yes** | **Yes** | **Yes** |
| Framework integrations | No | **20+** | **50+** | **10+** | **10+** | LiteLLM | **13+** |
| Caching/cost reduction | No | No | No | No | No | **Yes** | **Yes** |
| Rust core | **Yes** | No | No | No | No | **Yes** (gateway) | No |

### Unique Differentiation for Agentc

1. **Local-first, zero-ops profiling.** Every competitor requires deploying a server or SaaS subscription. Agentc is `pip install agentc` + `agentc.init()` + SQLite. This is uniquely valuable for individual developers and researchers.

2. **Waste detection and optimization focus.** No competitor answers "where did my tokens go?" with concrete waste pattern flags. They show you what happened; Agentc tells you what to fix.

3. **Content fingerprinting for similarity analysis.** No competitor computes SimHash/MinHash at capture time for redundant call detection. This enables analysis that is impossible with other tools.

4. **Foundation for an optimizer, not just an observer.** The Rust core, content fingerprinting, and waste taxonomy are designed to feed a future optimizer. Competitors are observability endpoints; Agentc is a stepping stone to automated optimization.

### Gaps the Spec Should Address

1. **No framework integrations at all.** Even V1 should document how the `@trace` decorator and span API could be used by LangGraph, CrewAI, etc. — not by building integrations, but by ensuring the API doesn't preclude them.

2. **No OTLP push export in V1.** The spec says V1 exports to JSON files and push is a follow-up. But every competitor speaks OTLP natively. Without OTLP push, Agentc traces can't flow into Jaeger/Tempo/Grafana/Datadog in real-time. This should be promoted to V1 or V1.0.1.

3. **No evaluation hooks.** The trace data Agentc collects is exactly what eval frameworks need. The spec should define an export format compatible with at least one eval framework (Braintrust datasets, Langfuse datasets, or a simple JSONL format).

4. **No UI story.** The CLI is fine for power users, but the spec should mention how traces could be visualized. Even a "pipe to Jaeger" section in the docs would help.

5. **Vector DB / RAG instrumentation not mentioned.** Competitors instrument Pinecone, Chroma, Weaviate, etc. For agent workloads that use retrieval, this is a significant blindspot. Even if out of V1 scope, the schema should accommodate retrieval spans (`gen_ai.retrieval.*`).

---

## 3. PyO3 + Maturin Patterns

### Key Findings

**Recommended project layout for mixed Rust/Python with Cargo workspace:**

The Agentc spec proposes:
```
agentc/
├── Cargo.toml              # workspace root
├── crates/
│   ├── agentc-core/
│   ├── agentc-profiler/
│   ├── agentc-analyzer/
│   └── agentc-cli/
├── python/
│   └── agentc/
```

The maturin-recommended pattern for a workspace is:
```
agentc/
├── Cargo.toml              # workspace root
├── pyproject.toml           # points to the PyO3 crate via manifest-path
├── crates/
│   ├── agentc-core/
│   ├── agentc-profiler/    # the PyO3 crate (lib.rs with #[pymodule])
│   ├── agentc-analyzer/
│   └── agentc-cli/
├── python/
│   └── agentc/
│       ├── __init__.py
│       ├── _native.pyi     # type stubs for the Rust extension
│       └── ...pure Python...
```

Key configuration:
```toml
# pyproject.toml
[tool.maturin]
manifest-path = "crates/agentc-profiler/Cargo.toml"
python-source = "python"
module-name = "agentc._native"
```

**Critical maturin patterns:**
- Use `manifest-path` in `[tool.maturin]` to point to the specific crate within the workspace that has the `#[pymodule]`.
- Use `python-source` to specify where the pure Python package lives.
- Use `module-name` to control the import path of the native extension (e.g., `agentc._native` so users import `agentc` and the `__init__.py` re-exports from `_native`).
- Add `.pyi` type stubs for the Rust-exposed API. Include a `py.typed` marker file.
- Maturin's `develop` command (`maturin develop`) for iterative development builds the extension in-place.

**Async Python + PyO3 gotchas:**

1. **`pyo3-async-runtimes`** (successor to `pyo3-asyncio`) is the bridge. It provides `future_into_py` to convert Rust futures to Python awaitables.
2. **Main thread ownership:** Python must own the main thread. Don't use `#[tokio::main]` on the Rust side. Instead, use `pyo3_async_runtimes::tokio::main` or run the Rust event loop in a background thread.
3. **GIL interaction:** Even in async functions, the GIL is held during Rust future execution unless explicitly released with `py.allow_threads(|| ...)`. For CPU-bound Rust work, always release the GIL.
4. **`contextvars` preservation:** `pyo3-async-runtimes` 0.15+ correctly preserves Python `contextvars` across async boundaries. This is critical for Agentc's span tracking which uses `contextvars.ContextVar`.
5. **Coroutine reuse:** Awaiting the same coroutine twice raises `RuntimeError`. Always create fresh coroutines.
6. **uvloop compatibility:** `pyo3-async-runtimes` works with uvloop if the event loop is configured before Rust initialization.

### Gaps in the Agentc Spec

1. **No `pyproject.toml` configuration specified.** The spec describes the directory layout but not the maturin configuration. The `manifest-path`, `python-source`, and `module-name` settings are essential and should be documented.

2. **No type stubs mentioned.** For a good developer experience, the Rust-exposed API needs `.pyi` files. These affect IDE autocomplete, type checking, and documentation generation.

3. **Async boundary not specified.** The spec says the background writer is a daemon thread, which is correct and avoids async complexity. But the `@trace` decorator and span API must work with both sync and async Python. The spec should explicitly state that:
   - No Rust async is exposed to Python (all Rust work is synchronous from Python's perspective).
   - The background writer uses a standard `threading.Thread`, not a Rust async task.
   - `contextvars` are used for span tracking, which works correctly with both threads and asyncio tasks.

4. **GIL release strategy not mentioned.** Any Rust work called from Python (e.g., zstd compression, SimHash computation, SQLite writes) should release the GIL during CPU-bound operations. This should be documented as a design principle.

### Recommendations

- Add a `pyproject.toml` example to the spec with the correct maturin configuration.
- Define the FFI boundary precisely: which functions cross from Python to Rust, and which direction data flows.
- Add type stubs (`.pyi`) to the deliverables for Phase 0.
- Document the GIL release strategy: release for compression, hashing, and SQLite writes.
- Avoid exposing Rust async to Python. Keep the Rust side synchronous and let Python own the event loop. The background writer thread can use blocking Rust calls.

---

## 4. Monkey-Patching LLM SDKs

### Key Findings

**How OpenLLMetry patches Anthropic:**
- Uses the standard OTel `BaseInstrumentor` pattern: `AnthropicInstrumentor().instrument()`.
- Patches class methods on the resource classes (e.g., `anthropic.resources.messages.Messages.create`), not on instances.
- Wraps the original method: capture start time, call original, capture end time + response metadata, create OTel span.
- Content capture controlled by `TRACELOOP_TRACE_CONTENT` env var.
- Streaming: wraps the returned iterator/context manager, accumulates chunks, emits span on stream completion.

**How the official OTel OpenAI instrumentation works (`opentelemetry-instrumentation-openai-v2`):**
- Uses `wrapt` for wrapping (not raw monkey-patching).
- Wraps `openai.resources.chat.completions.Completions.create` and the async variant.
- For streaming: keeps the span open until the stream completes. The wrapper intercepts the `Stream` object and wraps it in a traced iterator.
- Content capture is opt-in via `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`.
- Token usage extraction relies on `stream_options={"include_usage": True}` for streaming — the instrumentation must inject this option if the user didn't set it.

**Known issues and version compatibility concerns:**

1. **Anthropic SDK `v0.84.0+`:** Streaming event handling changed. The SDK now expects `event: message_start` SSE event types. Proxy services (e.g., Bedrock) that send only `data:` lines without `event:` fields cause silent event dropping. Instrumentation code that depends on specific event types must handle missing event fields gracefully.

2. **Anthropic fine-grained tool streaming (beta):** A newer feature that streams tool call parameters incrementally (`content_block_delta` for tool JSON). Instrumentation must handle partial JSON in tool call arguments.

3. **Anthropic beta API paths:** The spec correctly identifies `anthropic.resources.beta.messages.Messages.create` as a patch target. Beta paths change frequently — the instrumentation should discover available beta submodules dynamically rather than hardcoding paths.

4. **OpenAI streaming `include_usage` injection:** The instrumentation needs to inject `stream_options={"include_usage": True}` into the request kwargs if not already present. This modifies user behavior slightly (they'll see a `usage` field on the final chunk they might not have expected). Document this side effect.

5. **Google `google.generativeai` vs `google-genai`:** Google has two Python SDKs. The older `google.generativeai` and the newer `google-genai` (`from google import genai`). The spec only targets the older one. The newer SDK is the recommended one for Gemini 2.0+.

### Gaps in the Agentc Spec

1. **Missing patch target: Google `google-genai` (new SDK).** The spec targets `google.generativeai.GenerativeModel.generate_content`, which is the legacy SDK. The new `google-genai` SDK uses `google.genai.Client` with a different API surface. V1 should support both, or at minimum document which SDK versions are supported.

2. **No `wrapt` or patching library mentioned.** OpenLLMetry and the official OTel instrumentations use `wrapt` for reliable monkey-patching (handles descriptors, class methods, static methods correctly). Raw `setattr`-based patching is fragile. The spec should specify the patching mechanism.

3. **`stream_options` injection not mentioned.** For OpenAI streaming, the instrumentation must inject `stream_options={"include_usage": True}` to get token counts. The spec notes "Must inject stream option if not present" in the streaming table but doesn't discuss the implications (modifying user requests).

4. **No SDK version test matrix.** The spec lists patch targets by path but doesn't specify minimum SDK versions. The Anthropic SDK reorganized its module structure between versions. The spec should define minimum supported versions and include version-checking logic.

5. **No `uninstrument()` / cleanup.** OpenLLMetry provides `AnthropicInstrumentor().uninstrument()`. The spec's `agentc.init()` has no corresponding teardown. This matters for testing (patching in test setup, unpatching in teardown).

6. **Tool call streaming not addressed.** The streaming table covers content streaming but not tool call streaming. Anthropic's fine-grained tool streaming beta sends tool arguments incrementally via `content_block_delta`. The instrumentation needs to reconstruct complete tool call arguments from these deltas.

7. **No mention of `httpx` transport-level instrumentation.** Both Anthropic and OpenAI Python SDKs use `httpx` internally. An alternative or complementary instrumentation approach is to patch at the `httpx` transport layer, which catches all API calls including ones made through beta/preview methods not in the patch table. OpenLLMetry does not do this, but it's worth considering as a fallback.

### Recommendations

- Use `wrapt.wrap_function_wrapper` for all monkey-patching. It handles edge cases (descriptors, `classmethod`, `staticmethod`) that raw `setattr` does not.
- Add `agentc.shutdown()` (or make `init()` return a context manager) for clean teardown. This is essential for testing and for processes that reinitialize.
- Add the new Google `google-genai` SDK to the patch targets, or explicitly document it as out-of-scope for V1.
- Define minimum SDK versions: e.g., `anthropic>=0.30.0`, `openai>=1.0.0`, `google-generativeai>=0.3.0`.
- Add version-detection logic: at `init()` time, check the installed SDK version and select the appropriate patch targets. Log warnings for unsupported versions.
- Document the `stream_options` injection behavior and its user-visible side effect.

---

## 5. SimHash vs MinHash for Prompt Similarity

### Key Findings

**SimHash:**
- Produces a single fixed-size hash (e.g., 64-bit) per document.
- Compares via Hamming distance.
- Storage-efficient: one hash per document.
- Approximates **cosine similarity** of feature vectors.
- Weakness: lower accuracy for high-similarity pairs. Benchmark shows F1=0.85 vs MinHash's F1=0.95.
- Speed: **slower** than MinHash in benchmarks (626s vs 11s for one dataset).

**MinHash:**
- Produces a signature of N hash values (e.g., 128 or 256 hashes) per document.
- Compares via signature overlap (Jaccard similarity estimate).
- Higher storage: N hashes per document (N * 4 or 8 bytes).
- With LSH (Locality-Sensitive Hashing), enables sublinear search time.
- Industry standard for LLM training data deduplication (used by BigCode, Dolma, RedPajama).
- Better accuracy: F1=0.95 in benchmarks.

**Both SimHash and MinHash limitations:**
- Operate on character/word n-grams. Only find **orthographic** (surface-level) similarity.
- Degrade on short texts and very long texts.
- Miss semantic duplicates (same meaning, different wording).

**Embedding-based alternatives:**
- **SemHash** (MinishLab): Uses `model2vec` (8M parameter static embedding model) + `vicinity` (usearch-backed vector store). Finds **semantic** duplicates that hash-based methods miss. Benchmarks: deduplicates 130K samples in 7 seconds. Supports multi-column datasets.
- **SemDeDup** (academic): Uses larger embedding models. Comparable accuracy to MinHash but with semantic coverage. Higher computational overhead.
- Best practice in 2025-2026: **use MinHash first for surface dedup, then embedding-based methods for semantic dedup**. They are complementary.

### Gaps in the Agentc Spec

1. **SimHash is the wrong default choice.** The spec says "SimHash/MinHash fingerprints computed at capture time." Based on research, MinHash is strictly better for the use case (higher accuracy, actually faster, industry standard). SimHash's only advantage — single hash, lower storage — is negligible given that MinHash signatures of 128 hashes at 4 bytes each = 512 bytes per span, trivial compared to compressed content.

2. **No semantic similarity mentioned.** For the waste detectors ("redundant calls" and "cache miss on repeat"), semantic similarity is more useful than n-gram similarity. Two prompts asking the same question in different words are redundant, but SimHash/MinHash won't catch them.

3. **No threshold calibration discussed.** The spec says ">90% SimHash similarity" for redundant call detection. This threshold needs empirical validation. MinHash gives calibrated Jaccard similarity estimates; SimHash Hamming distance thresholds are harder to interpret.

4. **No mention of what n-gram size or shingle size to use.** Both SimHash and MinHash depend on the shingling strategy. For LLM prompts, word-level 3-grams or 5-grams are typical, but this isn't specified.

### Recommendations

- **Use MinHash (128 permutations) as the primary fingerprint.** Store the 512-byte signature per span. Use LSH for fast similarity queries in the analyzer.
- **Add optional embedding-based similarity for V1.1.** Use `model2vec` (potion-base-8M) — it's 8MB, fast, and doesn't require GPU. Compute embeddings at capture time, store as a float16 vector (768 dims = 1.5KB). Use cosine similarity for semantic redundancy detection.
- **Keep SimHash as a secondary, storage-cheap fingerprint** for fast screening, but don't rely on it for the waste detectors.
- **Specify the shingling strategy:** word-level 3-grams, lowercased, whitespace-normalized. Document why.
- **Calibrate thresholds empirically** in Phase 3 using real SWE-bench traces. Document the precision/recall tradeoff at different Jaccard thresholds.

---

## Summary: Priority-Ordered Recommendations

### Must-fix before implementation (spec-level gaps):

| # | Gap | Impact | Effort |
|---|---|---|---|
| 1 | Add missing OTel attributes (temperature, max_tokens, response_id, finish_reasons, tool call attrs) | Incorrect OTel compliance claims | Low |
| 2 | Switch from SimHash to MinHash as primary fingerprint | Core waste detector accuracy | Low |
| 3 | Add `wrapt` as the patching mechanism | Instrumentation reliability | Low |
| 4 | Add `agentc.shutdown()` / teardown API | Testability, clean process exit | Low |
| 5 | Specify minimum SDK versions and version detection | Deployment reliability | Medium |
| 6 | Add `pyproject.toml` and maturin configuration to spec | Build system correctness | Low |
| 7 | Map spec's `kind` values to OTel `gen_ai.operation.name` values | OTel interop | Low |

### Should-fix before V1 ship:

| # | Gap | Impact | Effort |
|---|---|---|---|
| 8 | Add OTLP gRPC/HTTP push export (not just JSON file) | Integration with real observability stacks | Medium |
| 9 | Add Google `google-genai` (new SDK) to patch targets | Provider coverage for modern Gemini | Medium |
| 10 | Document GIL release strategy for Rust-side CPU work | Performance under concurrency | Low |
| 11 | Add `.pyi` type stubs for Rust-exposed API | Developer experience | Medium |
| 12 | Store content in structured form (role/content messages) alongside compressed blobs | OTLP export correctness | Medium |
| 13 | Document `stream_options` injection behavior for OpenAI | Transparency, avoid user surprises | Low |

### Should-fix before V1.1 ship:

| # | Gap | Impact | Effort |
|---|---|---|---|
| 14 | Add optional embedding-based similarity (model2vec) | Catch semantic duplicates in waste detection | Medium |
| 15 | Define eval-compatible export format (JSONL for Braintrust/Langfuse datasets) | Ecosystem integration | Medium |
| 16 | Add OTel metrics emission (token histograms, duration) | Backend compatibility | Medium |
| 17 | Add OTel events for content capture (`gen_ai.client.inference.operation.details`) | Spec compliance | Medium |
| 18 | Document framework integration points (LangGraph, CrewAI hooks) | Adoption | Low |
| 19 | Add tool call streaming support (Anthropic fine-grained tool streaming) | Completeness | Medium |

### Out of scope but worth noting:

- **Prompt management** (Langfuse, Phoenix have it) — not in Agentc's thesis. The profiler observes, it doesn't manage prompts.
- **UI/Dashboard** — CLI-first is correct for V1. If adoption grows, consider a TUI (Ratatui) or a simple web viewer.
- **Caching proxy** (Helicone, Braintrust have it) — Agentc could *recommend* where to cache, but implementing a proxy is a different product.
- **Eval framework** — Agentc could export data to eval tools, but building an eval framework is scope creep.

---

## Sources

### OTel GenAI Semantic Conventions
- [Semantic conventions for generative AI systems](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [GenAI client spans](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/)
- [GenAI agent spans](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/)
- [GenAI events](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-events/)
- [GenAI metrics](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-metrics/)
- [GenAI attribute registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
- [Agentic systems proposal (Issue #2664)](https://github.com/open-telemetry/semantic-conventions/issues/2664)
- [OTel for GenAI blog post](https://opentelemetry.io/blog/2024/otel-generative-ai/)

### Competing Tools
- [OpenLLMetry / Traceloop](https://github.com/traceloop/openllmetry)
- [Traceloop Anthropic instrumentation](https://github.com/traceloop/openllmetry/tree/main/packages/opentelemetry-instrumentation-anthropic)
- [Langfuse documentation](https://langfuse.com/docs)
- [Langfuse data model](https://langfuse.com/docs/observability/data-model)
- [Arize Phoenix](https://github.com/Arize-ai/phoenix)
- [OpenInference](https://github.com/Arize-ai/openinference)
- [LangSmith observability](https://www.langchain.com/langsmith/observability)
- [LangSmith OTel support](https://blog.langchain.com/end-to-end-opentelemetry-langsmith/)
- [Helicone](https://github.com/Helicone/helicone)
- [Braintrust](https://www.braintrust.dev)
- [Braintrust AI proxy](https://www.braintrust.dev/docs/guides/proxy)
- [Braintrust Series B](https://siliconangle.com/2026/02/17/braintrust-lands-80m-series-b-funding-round-become-observability-layer-ai/)

### PyO3 + Maturin
- [PyO3 async/await guide](https://pyo3.rs/v0.23.3/ecosystem/async-await.html)
- [pyo3-async-runtimes](https://github.com/PyO3/pyo3-async-runtimes)
- [Maturin project layout](https://www.maturin.rs/project_layout.html)
- [Complex Python + Rust layout (Issue #1372)](https://github.com/PyO3/maturin/issues/1372)
- [Maturin workspace support (Issue #291)](https://github.com/PyO3/maturin/issues/291)

### Monkey-Patching
- [OTel OpenAI instrumentation v2](https://pypi.org/project/opentelemetry-instrumentation-openai-v2/)
- [OTel OpenAI Agents instrumentation](https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/instrumentation-genai/opentelemetry-instrumentation-openai-agents-v2)
- [Instrumenting OpenAI and Anthropic with OTel](https://oneuptime.com/blog/post/2026-02-06-instrument-openai-anthropic-api-opentelemetry/view)
- [Anthropic SDK releases](https://github.com/anthropics/anthropic-sdk-python/releases)
- [Anthropic streaming docs](https://docs.anthropic.com/en/api/messages-streaming)

### Similarity Hashing
- [In Defense of MinHash Over SimHash (ResearchGate)](https://www.researchgate.net/publication/264005347_In_Defense_of_MinHash_Over_SimHash)
- [MinHash LSH in Milvus](https://milvus.io/blog/minhash-lsh-in-milvus-the-secret-weapon-for-fighting-duplicates-in-llm-training-data.md)
- [SemHash](https://github.com/MinishLab/semhash)
- [SemHash blog post](https://minishlab.github.io/semhash-blogpost/)
- [text-dedup](https://github.com/ChenghaoMou/text-dedup)
- [Large-scale dedup behind BigCode](https://huggingface.co/blog/dedup)
