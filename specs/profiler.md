---
title: Profiler
status: active
last-updated: 2026-03-18
---

# Profiler

Answers the question: **where did my tokens go, and how much did the waste cost?** A zero-config profiler for LLM agent pipelines. `pip install agentc && agentc record -- python my_agent.py` gives you a per-call cost breakdown and waste report with no code changes.

Ships as the first Agentc artifact. Collection and analysis ship together. Has immediate utility independent of the optimizer and generates the empirical data the cost model requires.

---

## What You Get

```bash
pip install agentc
agentc record -- python my_agent.py
agentc analyze <trace-id>
```

```
$ agentc record -- python my_agent.py
[... normal agent output ...]

──── agentc ────
Captured 23 spans in trace a3f8c012 | 252,031 tokens | $0.89 | 4 waste flags
Run `agentc analyze a3f8c012` for details.
```

```
$ agentc analyze a3f8c012

 Trace: code-review-agent | 47.2s | 23 spans | 252,031 tokens | $0.89

 CALL BREAKDOWN
 #   AGENT              MODEL                 IN       OUT     COST    FLAGS
 1   orchestrator       claude-sonnet-4        12,301   1,204  $0.05   --
 2   review-agent       claude-sonnet-4       102,400      87  $0.31   context_bloat
 3   review-agent       claude-sonnet-4       102,892      92  $0.31   context_bloat, redundant_call (~#2)
 4   test-agent         claude-sonnet-4         8,201   2,102  $0.04   --
 5   format-agent       claude-opus-4             340      56  $0.01   model_overkill
 ...

 WASTE REPORT
 context_bloat (2 calls, ~$0.62 wasted)
   Calls #2, #3 sent >80% of context window but received <100 output tokens.
   -> Consider: truncate context to relevant sections, or split into focused sub-queries.

 redundant_call (1 pair, ~$0.31 wasted)
   Calls #2 and #3 have 94% input similarity and near-identical outputs.
   -> Consider: cache the first result and reuse, or deduplicate upstream.

 model_overkill (1 call, ~$0.008 saved by downgrade)
   Call #5 used claude-opus-4 for a 340-token input / 56-token output.
   -> Consider: use claude-sonnet-4 or claude-haiku for simple formatting tasks.

 Total flagged waste: ~$0.63 of $0.89 spend (deduplicated — call #3 overlaps context_bloat + redundant_call)
```

```
$ agentc traces

 TRACE ID       STARTED              DURATION  SPANS  TOKENS     COST     WASTE FLAGS
 a3f8c012...    2026-03-17 14:23:01  47.2s     23     252,031    $0.89    context_bloat (2), redundant_call (1), model_overkill (1)
 b7e1d045...    2026-03-17 14:19:44  12.1s     8      31,450     $0.19    --
 c9a2f078...    2026-03-17 14:15:33  63.8s     41     312,087    $1.87    retry_storm (1), model_overkill (3)

3 traces | 595,568 total tokens | $2.95 total cost | 8 waste flags
```

```
$ agentc report --last 50

 SUMMARY (last 50 traces, 2026-03-10 to 2026-03-17)
 Total tokens: 8,412,301 | Total cost: $48.23 | Avg cost/trace: $0.96

 BY MODEL
 claude-sonnet-4      6,102,400 tokens  $32.12  (66.6%)
 claude-opus-4        1,890,301 tokens  $14.89  (30.9%)
 claude-haiku         419,600 tokens    $1.22   (2.5%)

 BY AGENT
 review-agent         4,201,000 tokens  $24.11  (50.0%)
 test-agent           2,102,301 tokens  $12.44  (25.8%)
 orchestrator         1,209,000 tokens  $7.68   (15.9%)
 format-agent         900,000 tokens    $4.00   (8.3%)

 WASTE SUMMARY
 context_bloat        34 flags  ~$12.40 estimated waste
 redundant_call       18 flags  ~$8.90 estimated waste
 model_overkill       12 flags  ~$3.20 estimated waste
 cache_miss_repeat    8 flags   ~$2.10 estimated waste
 retry_storm          3 flags   ~$1.80 estimated waste
                      ─────────────────────────────
                      75 flags  ~$28.40 estimated waste (58.9% of total spend)
```

---

## Overview

Most agent pipelines waste 30-60% of their token budget on redundant calls, bloated context, and model overkill — but developers cannot see this because LLM API calls are opaque. The profiler instruments every LLM call in your pipeline, computes what each call cost, and flags specific waste patterns with estimated dollar amounts.

It works with zero code changes (`agentc record -- python my_agent.py`), stores everything locally in SQLite, and exports to any OpenTelemetry-compatible backend. Traces conform to the OTel `gen_ai.*` semantic conventions (pinned to semconv v1.29.0).

It uses a **hybrid instrumentation approach**: auto-instrumentation at the LLM client layer (monkey-patching via `wrapt`) for zero-config call capture, plus an explicit Python API for agent/workflow-level spans that developers opt into. Both streaming and non-streaming calls are instrumented.

---

## Repo Structure

Monorepo with Cargo workspace crate boundaries. Components can be extracted to separate repos later if needed.

```
agentc/
├── Cargo.toml              # workspace root
├── pyproject.toml           # maturin config, points to agentc-profiler crate
├── crates/
│   ├── agentc-core/        # spans, traces, storage, content dedup, OTLP export
│   ├── agentc-profiler/    # PyO3 bindings (#[pymodule]), bridges Python -> Rust
│   ├── agentc-analyzer/    # waste detection, cost computation, reporting
│   └── agentc-cli/         # CLI binary, depends on core + analyzer (no PyO3 dependency)
├── python/
│   └── agentc/             # Python package
│       ├── __init__.py     # public API: init(), shutdown(), trace, span
│       ├── _native.pyi     # type stubs for the Rust extension
│       ├── _patches/       # SDK monkey-patching via wrapt
│       │   ├── _anthropic.py
│       │   ├── _openai.py
│       │   └── _google.py
│       ├── _adapters/      # version-dispatched SDK adapters
│       └── py.typed        # PEP 561 marker
├── bench/                  # benchmarking harness, SWE-bench evaluation scripts
└── specs/                  # this directory
```

**Maturin configuration:**

```toml
# pyproject.toml
[build-system]
requires = ["maturin>=1.0,<2.0"]
build-backend = "maturin"

[project]
dependencies = ["wrapt>=1.14.0,<2.0"]

[tool.maturin]
manifest-path = "crates/agentc-profiler/Cargo.toml"
python-source = "python"
module-name = "agentc._native"
```

Users import `agentc`; the `__init__.py` re-exports from `agentc._native` (Rust) and the pure Python patches.

---

## Rust / Python Boundary

The Python layer is as thin as possible. It captures data and hands it to Rust. All heavy lifting is Rust.

**Span write boundary:** The Python layer calls a single Rust function per span: `_native.write_span(span_dict: dict)`. The dict contains all span fields: `span_id`, `trace_id`, `parent_span_id`, `name`, `kind`, `start_time`, `end_time`, `status`, `model`, `provider`, `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `attributes`, `input_messages`, `output_messages`. The Rust side converts this to its internal `Span` struct, computes content hashes, compresses content, computes embeddings, and writes to SQLite. Both the background writer path (queue-drained spans) and the root span bypass path call `write_span()` — the Rust function performs the same work (hashing, compression, embedding, SQLite write) regardless of caller. The `input_messages` and `output_messages` fields are consumed by Rust to produce `input_content_id`, `output_content_id`, `input_embedding`, `output_embedding`, and the `input_content`/`output_content` table rows; they do not appear as schema columns.

| Component | Language | Rationale |
|---|---|---|
| SDK patches, decorators, context propagation | **Python** | Must be Python — wrapping Python SDK objects |
| Span creation, queue management | **Python** | Thin layer, feeds Rust core via PyO3 |
| Storage (SQLite writes, zstd compression, content dedup) | **Rust (PyO3)** | Build once, avoid rewrite later |
| Embedding computation (model2vec) | **Rust** | Reimplemented in Rust: `tokenizers` crate for tokenization + matrix lookup + mean pooling. No Python model2vec dependency, no numpy. |
| Analysis, waste detection, OTLP export | **Rust** | Performance matters — scanning thousands of spans |
| CLI (`agentc` binary) | **Rust** | Single binary, no Python runtime needed |

**Embedding model distribution:** potion-base-8M weights (~10MB) are bundled into the Rust binary via `include_bytes!()`. No download-on-first-use, no network dependency. Lazy-loaded on first embedding request via `once_cell::sync::OnceCell`. If loading fails, embeddings are NULL on all spans — waste detectors that need embeddings skip gracefully, SHA-256-based detectors (`cache_miss_repeat`) are unaffected.

**GIL release strategy:** All Rust work called from Python (zstd compression, embedding computation, SQLite writes) releases the GIL via `py.allow_threads(|| ...)` during CPU-bound operations.

**Async boundary:** No Rust async is exposed to Python. The background writer uses a standard `threading.Thread`, not a Rust async task. `contextvars.ContextVar` handles span tracking for both threads and asyncio tasks.

**PyO3 panic safety:** All `#[pyfunction]` exports use `Result<T, PyErr>` return types. Panics are avoided in the hot path. PyO3 converts uncaught panics to `pyo3.PanicException` (a `BaseException` subclass). The Python wrapper layer catches `BaseException` (not just `Exception`) at every Rust call site to ensure panics are logged and suppressed under `fail_open=True`.

**Install:** `pip install agentc` installs pre-built maturin wheels (Linux x86_64/aarch64, macOS x86_64/arm64, Windows x86_64). No Rust toolchain needed for users.

---

## Design Decisions

### Why not proxy-based?

A proxy (like Helicone) only sees the network boundary. It captures request/response payloads but is blind to:
- Agent-to-agent communication
- Tool execution within an agent step
- Prompt assembly and RAG retrieval logic
- Workflow-level structure (which agent spawned which)

For a profiler whose purpose is to feed an optimizer, we need the full execution graph, not just the API calls. SDK-based instrumentation gives us nested spans, agent identity, and workflow context.

### Why OTel `gen_ai.*` as the schema?

- Emerging industry standard (OTel GenAI SIG, started April 2024)
- Interoperable with existing backends (Jaeger, Grafana Tempo, Langfuse, Arize Phoenix)
- Covers the right attributes: tokens, latency, model, cache, agent identity
- Agent-level conventions (`gen_ai.agent.*`) directly map to multi-agent workloads
- Avoids inventing a proprietary schema that locks users in
- Pinned to semconv v1.29.0; opt-in via `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`

### Why Rust core + Python bindings?

- **Rust core**: Trace storage, aggregation, analysis, and the CLI. Systems-level work that is core to the Agentc thesis — this is not just a profiler, it's the foundation for the optimizer runtime.
- **Python bindings (PyO3)**: Auto-instrumentation hooks for LLM SDKs, the `@trace` decorator API, and integration with the Python agent ecosystem. This is where users interact with the profiler.
- **Build toolchain**: `maturin` for PyO3 extension modules, Cargo workspace for crate management.

### Content Capture Strategy

Full prompt/response text is captured and stored. This is essential for:
- Redundant call detection (semantic similarity analysis)
- Training the cost model (the optimizer needs to understand prompt structure)
- Research-quality execution traces for the paper

**Storage approach:**
- Full text stored by default, compressed with zstd (prompts compress 5-10x due to repetitive natural language)
- Content stored in separate `input_content` and `output_content` tables, each linked by SHA-256 content hash — deduplicates repeated prompts and repeated responses independently
- **Content hashing:** SHA-256 is computed over the canonical JSON serialization of the messages array (keys sorted, compact separators, no trailing whitespace). Python extracts messages as Python objects from SDK response/request data and passes them to Rust via PyO3. Rust performs canonical JSON serialization using `serde_json` (keys sorted, compact format equivalent to `json.dumps(messages, sort_keys=True, separators=(',', ':'))`) and computes SHA-256 over the result. The `json.dumps` notation is illustrative of the output format, not the implementation — the Rust core owns both serialization and hashing to prevent Python/Rust inconsistencies.
- Semantic embeddings (model2vec, potion-base-8M, 256-dim) computed by the background writer thread after dequeue and stored per span for similarity queries. ~10-20 microseconds per embedding on CPU. No GPU required. The embedding input is the concatenated text content of all message `content` fields in the canonical JSON array, joined by newlines. For messages with structured content (arrays of text/image blocks), only `text` type blocks are concatenated. Image and tool_use blocks are excluded from embedding input.
- Public release mode: `agentc.init(capture_content=False)` stores only hashes + token counts. Embeddings are also skipped by default when content capture is off (low-dimensional embeddings can leak bag-of-words-level information via inversion attacks). To keep embeddings for similarity analysis without content, use `agentc.init(capture_content=False, capture_embeddings=True)` — this is an explicit opt-in acknowledging the trade-off.

---

## Interface

### 1. CLI — Zero Code Changes (start here)

```bash
agentc record -- python my_agent.py
```

This instruments any Python program with zero code changes. See the "What You Get" section above for example output.

`agentc record` prints a summary on process exit showing trace ID, token count, cost, and waste flag count. Run `agentc analyze <trace-id>` for the full breakdown.

**Post-exit summary flow:** After the child process exits (triggering `shutdown()` which merges spans into the canonical `traces.db`), the `agentc record` CLI binary identifies the trace by the trace_id set during that session. It loads bundled pricing into `model_pricing` via `INSERT OR IGNORE`, loads user overrides from `~/.agentc/pricing.toml` via `INSERT OR REPLACE`, backfills `cost_usd` on all spans in the trace (same UPDATE logic as `agentc analyze`), runs waste detectors, and prints the summary line. If the child process crashed before `shutdown()`, `agentc record` performs orphan detection and merge-on-read before the summary step.

#### How `agentc record` Works

Uses Python's `sitecustomize.py` mechanism:

1. Creates a temp directory (0700 permissions) containing a `sitecustomize.py` that calls `import agentc; agentc.init()`
2. Prepends that temp dir to `PYTHONPATH`
3. Spawns the user's command as a child process (e.g., `python my_agent.py`) and waits for it to exit
4. Python's startup machinery automatically imports `sitecustomize.py` before user code runs
5. All SDK patches are in place before the first LLM call
6. After the child exits, `agentc record` performs merge-on-read, cost backfill, waste detection, and prints the summary line
7. Temp directory is cleaned up on exit

Works with any Python entry point: `python script.py`, `pytest`, `gunicorn`, `uvicorn`, `celery`.

**Chaining existing sitecustomize:** If the user's environment has a `sitecustomize.py`, Agentc's version temporarily removes its temp dir from `sys.path`, imports the original `sitecustomize`, restores the path, then runs `agentc.init()`. If the original `sitecustomize` raises an exception, Agentc re-raises it (preserving the user's expected startup behavior). `agentc.init()` is called in a `finally` block so that profiling is attempted regardless of whether the original `sitecustomize` succeeded.

**Exit code propagation:** `agentc record` propagates the child process's exit code. If the child exits with code 1, `agentc record` exits with code 1. If the child is killed by a signal, `agentc record` exits with 128 + signal number (standard Unix convention).

**Limitations:** Does not work with `python -I` (isolated mode), frozen executables (PyInstaller), or environments that disable `sitecustomize`.

**Configuration via environment variables:** Since `agentc record` cannot pass Python kwargs, all `init()` options are configurable via env vars (see Configuration section below).

**Interaction with user-code `init()` calls:** Because `sitecustomize.py` calls `init()` before user code runs, a subsequent `init()` in user code is a no-op (first-caller-wins idempotency). To override configuration under `agentc record`, set environment variables (e.g., `AGENTC_CAPTURE_CONTENT=false`) which are read by the `sitecustomize`-injected `init()` call. Do not rely on `init()` kwargs in code that may run under `agentc record`.

### 2. Auto-Instrumentation (one line of code)

```python
import agentc

agentc.init()  # patches anthropic, openai SDKs via wrapt

# All LLM calls are now traced automatically — streaming and non-streaming
client = anthropic.Anthropic()
response = client.messages.create(model="claude-sonnet-4-20250514", ...)
# ^ captured: tokens, latency, model, cache hits, cost, full prompt/response
```

`agentc.init()` monkey-patches supported SDKs using `wrapt.wrap_function_wrapper`. Each call becomes a `gen_ai.chat` span with full OTel attributes.

**Idempotency:** `init()` is idempotent — second calls are a no-op (logged at debug level). First-caller-wins for configuration. After `shutdown()`, `init()` can be called again to re-initialize.

**Supported SDKs:**
- `anthropic` (Python) >= 0.30.0 — sync + async clients
- `openai` (Python) >= 1.0.0 — sync + async clients

**Future work (not in this spec):**
- `google.generativeai` (Python) — legacy SDK
- `google-genai` (Python) — new SDK for Gemini 2.0+

### 3. Shutdown / Flush API

```python
import agentc

agentc.init()
# ... run your agent ...
agentc.shutdown(timeout_ms=5000)  # flush queued spans, merge per-process DB
```

- `agentc.shutdown(timeout_ms)` flushes the span queue, writes remaining spans to SQLite, merges the per-process DB into the canonical `traces.db`, and tears down all patches (`uninstrument()`). If the queue cannot be fully drained within `timeout_ms`, shutdown logs a warning with the number of un-flushed spans, proceeds with the merge of whatever was written, and returns normally. Shutdown does not raise on timeout — only on merge failure.
- **Reentrant shutdown guard:** `shutdown()` sets an atomic `_shutdown_in_progress` flag (`threading.Event`) before doing any work. If a second call arrives (e.g., `SIGTERM` fires while `atexit` is already running `shutdown()`), it detects the flag and returns immediately as a no-op. This prevents reentrant SQLite writes that would corrupt the database.
- An `atexit` handler calls `shutdown(timeout_ms=3000)` wrapped in a `try/except BaseException` — `atexit` failures are logged, never propagated. `SIGTERM` and `SIGINT` handlers also trigger shutdown via the same safe wrapper.
- The root `@trace` span ending also triggers a flush (not a full shutdown).
- `wrapt`-based patches enable clean `uninstrument()` — essential for testing (patch in setup, unpatch in teardown).
- `shutdown()` is an explicit user call and raises on merge failure (not suppressed by `fail_open`). Timeout is a warning, not an error.

### 4. Explicit Span API (opt-in, for agent/workflow structure)

```python
from agentc import trace, span

@trace(name="code-review-agent")
def review_agent(files: list[str]):
    with span("read-files", kind="execute_tool") as s:
        contents = read_files(files)
        s.set_attribute("file_count", len(files))

    with span("analyze", kind="chat") as s:
        # LLM call inside here is auto-captured as a child span
        analysis = client.messages.create(...)

    with span("write-review", kind="execute_tool") as s:
        write_review(analysis)
```

The `@trace` decorator creates a root span for the agent invocation. `span()` context managers create child spans. LLM calls inside a span are automatically nested as children. Works with both sync and async functions. `@trace` inspects the decorated function with `asyncio.iscoroutinefunction()`. If true, the decorator returns an async wrapper that creates and manages the span around an awaited call. Async generators are not supported — `@trace` on an async generator raises `TypeError` at decoration time. `name` defaults to the decorated function's `__qualname__` if not provided.

**Behavior without `init()`:** `@trace` and `span()` check an internal initialization flag. If `init()` has not been called, they are no-ops: the decorated function runs normally, the context manager is a passthrough. A single debug-level log message is emitted on first use: `"agentc.init() not called — instrumentation disabled."` This ensures library code using agentc decorators does not break when the profiler is not active.

**Nesting rules:**
- **Nested `@trace`:** An inner `@trace` creates a child span of the outer trace, not a new root. The inner trace inherits the outer's `trace_id`. This models sub-agent invocation within a parent agent.
- **`span()` outside `@trace`:** Creates a root span and starts a new trace (generates a fresh `trace_id`). This is valid but produces a single-span trace — useful for one-off instrumentation but the analyzer warns if it detects traces with no `@trace` root.
- **`span()` inside `@trace`:** Creates a child span of the current context (either the `@trace` root or an enclosing `span()`).

**Exception behavior:** If an exception is raised inside a `span()` block, the context manager sets `status=ERROR` and `error.type`/`error.message` attributes on the span, sets `end_time`, enqueues the span, and **re-raises the exception**. The profiler never suppresses user exceptions.

### 5. Multi-Agent Correlation

Context propagation links spans across agents, threads, and processes into a single trace tree.

**Threads** — use a traced executor wrapper:
```python
from agentc import trace, traced_executor
from concurrent.futures import ThreadPoolExecutor

@trace(name="orchestrator")
def orchestrator():
    with traced_executor(ThreadPoolExecutor(max_workers=4)) as pool:
        # context auto-propagated to child threads
        review_future = pool.submit(review_agent, files)
        test_future = pool.submit(test_agent, files)
```

`traced_executor` uses `contextvars.copy_context()` to snapshot the parent context and `ctx.run(fn, *args)` in each child thread. Note: `contextvars` are **not** automatically copied to threads — bare `threading.Thread` or `ThreadPoolExecutor` without the wrapper will produce disconnected spans (not an error, but a data quality issue). For `ProcessPoolExecutor`, use explicit context passing below.

**Processes** — explicit context passing:
```python
from agentc import trace, get_trace_context, attach_trace_context

@trace(name="orchestrator")
def orchestrator():
    ctx = get_trace_context()  # returns {"trace_id": str, "span_id": str, "trace_flags": int}
    subprocess.run(["python", "worker.py", "--trace-ctx", json.dumps(ctx)])

# In worker.py:
def main():
    agentc.init()  # must be called before attach_trace_context — starts patches + background writer
    ctx = json.loads(args.trace_ctx)
    attach_trace_context(ctx)
    # subsequent spans in this process are now children of the orchestrator trace
```

`get_trace_context()` returns a dict: `{"trace_id": str, "span_id": str, "trace_flags": int}`. `trace_id` is 32 lowercase hex characters (16 bytes). `span_id` is 16 lowercase hex characters (8 bytes). `trace_flags` is an integer (1 = sampled). These map directly to W3C traceparent fields: `00-{trace_id}-{span_id}-{trace_flags:02x}`. `attach_trace_context(ctx)` validates the dict keys and types, raises `ValueError` on malformed input. After validation, it sets the process-local `ContextVar` so that the provided `trace_id` and `span_id` become the active trace context: subsequent `span()` calls and auto-instrumented LLM calls use `ctx["trace_id"]` as their `trace_id` and `ctx["span_id"]` as their `parent_span_id`. `init()` must be called before `attach_trace_context()` — if `init()` has not been called, `attach_trace_context()` is a no-op (same behavior as `@trace` and `span()` without initialization). The parent trace does not need to exist locally — parent references are resolved at merge/analysis time.

**Remote calls** — W3C traceparent header:
```python
from agentc import inject_trace_headers

headers = inject_trace_headers({})
# headers now contains {"traceparent": "00-<trace_id>-<span_id>-01"}
requests.post("http://worker/run", headers=headers)
```

### 6. CLI Commands

```bash
# Collection
agentc record -- python my_agent.py    # instrument and run (via sitecustomize.py)
agentc traces                           # list 20 most recent traces by start_time (descending)
agentc traces --limit 50               # change the count
agentc traces --since 2026-03-10       # filter by date
agentc export <trace-id>               # write OTLP/HTTP JSON to stdout
agentc export <trace-id> --output f.json  # write to file instead

# Analysis
agentc analyze                          # analyze the most recent trace (by MAX(start_time))
agentc analyze <trace-id>               # analyze a specific trace (prefix matching, min 4 chars)
# Ambiguous prefix prints matching IDs and exits with error.
agentc report --last 50                 # aggregate over 50 most recent traces by start_time
agentc report --since 2026-03-10       # filter by date
agentc report --agent review-agent     # filter by agent name
agentc report --model claude-sonnet-4  # filter by model ID
# --last N reports on all available if fewer than N exist.
agentc pricing update                   # fetch latest model pricing

# Maintenance
agentc embed --backfill                  # compute embeddings for spans with NULL input_embedding or output_embedding
# Reads content from input_content/output_content tables, decompresses, extracts text, computes model2vec embedding, writes float16 BLOB.
# Skips spans whose content_id references are NULL (capture_content=False at capture time).
agentc migrate                           # apply schema migrations to traces.db
```

### 7. Configuration

**Precedence order:** `agentc.init()` kwargs > environment variables > `~/.agentc/config.toml` > defaults.

| Option | `init()` kwarg | Environment variable | Default |
|---|---|---|---|
| Capture content | `capture_content=True` | `AGENTC_CAPTURE_CONTENT=true` | `true` |
| Capture embeddings | `capture_embeddings=True` | `AGENTC_CAPTURE_EMBEDDINGS=true` | follows `capture_content` |
| Fail-open mode | `fail_open=True` | `AGENTC_FAIL_OPEN=true` | `true` |
| Storage path | `storage_path="~/.agentc"` | `AGENTC_STORAGE_PATH=~/.agentc` | `~/.agentc` |

`storage_path` uses `pathlib.Path.home()` with a fallback to a temp directory if `HOME` is not set (containers, CI).

**`config.toml` schema:**

```toml
capture_content = true
capture_embeddings = true
fail_open = true
storage_path = "~/.agentc"
```

All keys are optional. Missing keys use the defaults above. Unknown keys are ignored with a warning log.

### 8. Storage Format

Each process writes to its own SQLite file during execution. Files are merged into a canonical store on shutdown or CLI read.

**Per-process storage (`~/.agentc/active/pid-<PID>.db`):**

During execution, each process has exclusive write access to its own DB file. Zero write contention across processes. Per-process DBs use the same schema as the canonical DB, including `INSERT OR IGNORE` on content tables. Content is deduplicated within a single process run. The canonical merge also deduplicates across processes.

**Canonical storage (`~/.agentc/traces.db`):**

Merged from per-process files on `agentc.shutdown()` or on CLI read (for orphaned files from crashed processes).

**Merge protocol:**
1. Acquire a lockfile (`~/.agentc/traces.db.lock`) before writing to the canonical DB. Lock acquisition uses `flock()` (POSIX advisory lock) on the lockfile descriptor with a 10-second timeout. If `flock()` cannot acquire within 10 seconds, the lockfile's mtime is checked — if older than 60 seconds, it is considered stale and removed, then `flock()` is retried once on a fresh lockfile. Using `flock()` eliminates TOCTOU races inherent in check-then-remove. If the retry also fails, the merge is skipped and retried on next CLI read.
2. Run `PRAGMA wal_checkpoint(TRUNCATE)` on the per-process DB to recover any incomplete WAL.
3. Copy all spans and content into the canonical DB inside a single SQLite transaction using `INSERT OR IGNORE` (span_id is the primary key — duplicates from concurrent merges are silently skipped). This makes merge idempotent. If the transaction fails (e.g., disk full, I/O error), it is rolled back and the per-process DB is preserved intact for retry on next CLI read.
4. Delete the per-process DB file only after the merge transaction is fully committed. The delete is a separate operation — a crash between commit and delete is harmless because step 3 is idempotent.

**Orphan detection:** A per-process file is considered orphaned if the PID is not alive and the file mtime is older than 60 seconds. The CLI merge-on-read path merges all orphans before querying.

**Schema versioning:** The canonical DB stores a schema version via `PRAGMA user_version`. On open, the version is checked. Forward-compatible migrations (ALTER TABLE ADD COLUMN) are applied automatically. Incompatible versions produce a clear error: `"Run agentc migrate to update your trace database."`

**SQLite schema (user_version = 1):**

```sql
CREATE TABLE spans (
    span_id             TEXT PRIMARY KEY,   -- 8-byte random, 16 lowercase hex chars (OTel spec)
    trace_id            TEXT NOT NULL,      -- 16-byte random, 32 lowercase hex chars (OTel spec)
    parent_span_id      TEXT,              -- NULL for root span
    name                TEXT NOT NULL,
    kind                TEXT NOT NULL,      -- 'chat', 'execute_tool', 'invoke_agent'
    start_time          INTEGER NOT NULL,   -- Unix microseconds
    end_time            INTEGER,            -- Unix microseconds, NULL if span still open
    status              TEXT DEFAULT 'OK',  -- 'OK', 'ERROR'
    -- Promoted attributes (queried by analyzer hot paths)
    model               TEXT,              -- gen_ai.request.model
    provider            TEXT,              -- gen_ai.provider.name
    input_tokens        INTEGER,           -- gen_ai.usage.input_tokens
    output_tokens       INTEGER,           -- gen_ai.usage.output_tokens
    cache_creation_tokens INTEGER,         -- gen_ai.usage.cache_creation.input_tokens
    cache_read_tokens   INTEGER,           -- gen_ai.usage.cache_read.input_tokens
    cost_usd            REAL,              -- agentc.cost_usd (NULL if pricing unknown)
    -- Remaining attributes
    attributes          TEXT NOT NULL,      -- JSON: all other gen_ai.* and agentc.* attributes
    input_content_id    TEXT REFERENCES input_content(content_id),
    output_content_id   TEXT REFERENCES output_content(content_id),
    input_embedding     BLOB,              -- float16 256-dim model2vec vector (512 bytes)
    output_embedding    BLOB,              -- float16 256-dim model2vec vector (512 bytes)
    embedding_model     TEXT DEFAULT 'potion-base-8M'  -- tracks which model produced the embedding
);

CREATE INDEX idx_spans_trace_id ON spans(trace_id);
CREATE INDEX idx_spans_start_time ON spans(start_time);
CREATE INDEX idx_spans_input_content_id ON spans(input_content_id);
CREATE INDEX idx_spans_kind ON spans(kind);

CREATE TABLE input_content (
    content_id      TEXT PRIMARY KEY,  -- SHA-256 of canonical JSON serialization
    content_text    BLOB NOT NULL,     -- zstd-compressed prompt text (structured JSON messages)
    created_at      INTEGER NOT NULL   -- Unix microseconds
);

CREATE TABLE output_content (
    content_id      TEXT PRIMARY KEY,  -- SHA-256 of canonical JSON serialization
    content_text    BLOB NOT NULL,     -- zstd-compressed response text (structured JSON messages)
    created_at      INTEGER NOT NULL   -- Unix microseconds
);

-- Trace metadata derived from spans via view, not a separate table.
-- This VIEW exists only in the canonical DB (traces.db), not in per-process DBs.
CREATE VIEW traces AS
SELECT
    trace_id,
    MIN(start_time) AS start_time,
    MAX(end_time) AS end_time,
    (SELECT span_id FROM spans s2
     WHERE s2.trace_id = s1.trace_id AND s2.parent_span_id IS NULL
     ORDER BY s2.start_time ASC LIMIT 1) AS root_span_id,
    (SELECT COUNT(*) FROM spans s2
     WHERE s2.trace_id = s1.trace_id AND s2.parent_span_id IS NULL) AS root_span_count
FROM spans s1
GROUP BY trace_id;

CREATE TABLE model_pricing (
    model_id                TEXT PRIMARY KEY,
    input_cost              REAL NOT NULL,      -- USD per 1M input tokens
    output_cost             REAL NOT NULL,      -- USD per 1M output tokens
    cache_creation_cost     REAL,               -- USD per 1M cache creation tokens (NULL = use input_cost)
    cache_read_cost         REAL,               -- USD per 1M cache read tokens (NULL = use input_cost)
    context_window          INTEGER,
    updated_at              INTEGER NOT NULL,    -- Unix microseconds
    source                  TEXT DEFAULT 'bundled'  -- 'bundled', 'user', 'fetched'
);
```

**Cost computation (post-hoc):**

At capture time, `cost_usd` is written as NULL. The Python SDK does not perform cost computation — it has no access to the pricing table. Cost is computed by the Rust analyzer on first `agentc analyze` or `agentc report` invocation, which backfills `cost_usd` on all spans with known pricing. This avoids a Rust-to-Python pricing data path and keeps the capture path minimal.

```
cost = (input_tokens * input_cost / 1M)
     + (output_tokens * output_cost / 1M)
     + (cache_creation_tokens * (cache_creation_cost ?? input_cost) / 1M)
     + (cache_read_tokens * (cache_read_cost ?? input_cost) / 1M)
```

**Key schema decisions:**
- **No `traces` table** — derived as a view from spans. Avoids dual-write consistency issues. `root_span_count > 1` is a data quality signal the analyzer flags.
- **Timestamps are INTEGER (Unix microseconds)** — correct sorting, fast range queries, no format ambiguity.
- **Hot-path attributes promoted to columns** — `model`, `provider`, `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `cost_usd` are queried by every analyzer operation. Avoids JSON parsing on the hot path. Remaining attributes stay in the JSON blob.
- **`agent_name` is NOT promoted** — the CLI extracts `gen_ai.agent.name` from the `attributes` JSON blob for display. This is not a hot-path query — trace display scans at most hundreds of spans. Promotion would add write overhead to every span for a field only present on agent-level spans.
- **Separate `input_content` and `output_content` tables** — identical prompts share one row regardless of response. Identical responses also deduplicate.
- **Content stored as structured JSON messages** (`[{role, content}, ...]`) — makes OTLP export straightforward without reconstructing from flat blobs.
- **Embeddings stored on the span** — 512 bytes each (float16, 256 dims). `embedding_model` column tracks provenance. The analyzer upcasts to float32 before computing cosine similarity to avoid quantization-induced threshold artifacts.
- **`kind` values use OTel `gen_ai.operation.name` conventions** — `chat`, `execute_tool`, `invoke_agent` (not `llm`, `tool`, `agent`).
- **`trace_id` and `span_id` format** — `trace_id` is 16-byte (128-bit) random, encoded as 32 lowercase hex chars. `span_id` is 8-byte (64-bit) random, encoded as 16 lowercase hex chars. Both follow the W3C Trace Context / OTel specification. Generated via cryptographically secure random source (`os.urandom` in Python, `getrandom` in Rust).
- **Cache pricing columns** — Anthropic cache creation (1.25x base) and cache read (0.1x base) have materially different costs. NULL falls back to standard input_cost.

---

## OTel Attributes Mapping

All captured fields map to `gen_ai.*` semantic convention attributes. Pinned to semconv v1.29.0.

### Span Attributes (captured on every LLM span)

| Attribute | Source | Notes |
|---|---|---|
| `gen_ai.operation.name` | Derived from call type | `chat`, `execute_tool`, `invoke_agent` |
| `gen_ai.provider.name` | SDK detection | `anthropic`, `openai` |
| `gen_ai.request.model` | Request kwargs | Model requested by user |
| `gen_ai.response.model` | Response metadata | Model actually used (may differ) |
| `gen_ai.usage.input_tokens` | Response metadata | Provider-reported input token count |
| `gen_ai.usage.output_tokens` | Response metadata | Provider-reported output token count |
| `gen_ai.usage.cache_creation.input_tokens` | Response metadata | Anthropic cache creation tokens |
| `gen_ai.usage.cache_read.input_tokens` | Response metadata | Anthropic cache read tokens |
| `gen_ai.request.temperature` | Request kwargs | |
| `gen_ai.request.top_p` | Request kwargs | |
| `gen_ai.request.max_tokens` | Request kwargs | |
| `gen_ai.request.seed` | Request kwargs | OpenAI only |
| `gen_ai.response.id` | Response metadata | Provider-assigned response ID |
| `gen_ai.response.finish_reasons` | Response metadata | `["stop"]`, `["max_tokens"]`, `["tool_use"]` |

### Agent Span Attributes (on `@trace` / `span()` spans)

| Attribute | Source | Notes |
|---|---|---|
| `gen_ai.agent.name` | `@trace(name=...)` | |
| `gen_ai.agent.id` | Auto-generated | Unique per agent instance |

### Tool Call Attributes (on tool execution spans)

| Attribute | Source | Notes |
|---|---|---|
| `gen_ai.tool.name` | Tool call response | |
| `gen_ai.tool.call.id` | Tool call response | Provider-assigned tool call ID |
| `gen_ai.tool.call.arguments` | Tool call response | JSON string |
| `gen_ai.tool.call.result` | Tool result message | JSON string |

### Agentc-Specific Attributes (not in OTel spec)

| Attribute | Description |
|---|---|
| `agentc.ttft_ms` | Time to first token (streaming only) |
| `agentc.cost_usd` | Computed cost in USD (null if model pricing unknown) |
| `agentc.content.input_content_id` | SHA-256 of input content |
| `agentc.content.output_content_id` | SHA-256 of output content |

---

## Monkey-Patching Strategy

All patches use `wrapt.wrap_function_wrapper` (`wrapt >= 1.14.0, < 2.0`) for reliable wrapping of descriptors, class methods, and static methods. This provides clean `uninstrument()` support.

### Version-Dispatched Adapters

At `agentc.init()` time, the installed SDK version is detected. The appropriate adapter is loaded:

```
anthropic >= 0.30.0  ->  adapter_anthropic_v030 (current module paths)
anthropic >= 0.20.0  ->  adapter_anthropic_v020 (legacy module paths)
openai >= 1.0.0      ->  adapter_openai_v1 (current module paths)
unknown version      ->  httpx transport fallback (catches all API calls)
```

Each adapter is a small module that knows the patch targets for its SDK version range. New SDK versions require adding an adapter, not rewriting the patching logic.

**httpx transport fallback:** Both Anthropic and OpenAI SDKs use `httpx` internally. If no adapter matches the installed version, patching at the `httpx.Client` / `httpx.AsyncClient` transport layer intercepts outgoing API calls. The fallback:
- Only activates for a provider if no SDK adapter matched that provider.
- Filters by destination URL (e.g., `api.anthropic.com`, `api.openai.com`). Other HTTP traffic is ignored.
- Response parsing is best-effort: extracts `usage` from JSON response body if present, otherwise token counts are NULL.
- SDK adapter patches set a context flag that the httpx fallback checks to prevent double-instrumentation.

### Patch Targets

Each SDK has sync, async, and streaming variants. All must be patched.

**Anthropic (>= 0.30.0):**
| Method | Path |
|---|---|
| Sync create | `anthropic.resources.messages.Messages.create` |
| Async create | `anthropic.resources.messages.AsyncMessages.create` |
| Sync stream | `anthropic.resources.messages.Messages.stream` |
| Async stream | `anthropic.resources.messages.AsyncMessages.stream` |
| Beta variants | Discovered dynamically from `anthropic.resources.beta` |

**OpenAI (>= 1.0.0):**
| Method | Path |
|---|---|
| Sync create | `openai.resources.chat.completions.Completions.create` |
| Async create | `openai.resources.chat.completions.AsyncCompletions.create` |

### Patching Rules

- If an SDK is not installed, skip silently (no ImportError)
- If a patch target does not exist (SDK version changed), log a warning and fall back to httpx transport patching
- Async methods must return coroutines, not sync wrappers
- Patches are applied to the class, not to instances — covers clients created before and after `init()`
- Each patch wraps the original method: capture start time -> call original -> capture end time + response -> emit span
- Beta API paths (Anthropic) are discovered dynamically, not hardcoded

### Streaming Instrumentation

Streaming changes the instrumentation model. Instead of wrapping a single call, we wrap the response iterator.

**Non-streaming flow:**
```
call start -> SDK.create() -> call end -> emit span with full data
```

**Streaming flow:**
```
call start -> SDK.stream() -> returns wrapped iterator
    -> first chunk yielded -> record TTFT
    -> chunks yielded to caller (transparent)
    -> final chunk (or stream end) -> extract usage data -> emit span
    -> stream abandoned (exception) -> emit span with status=ERROR, partial data
```

Per-provider streaming details:

| Provider | TTFT source | Usage data source | Notes |
|---|---|---|---|
| Anthropic | First `content_block_delta` event | `message_delta` event (final) | `message_start` has model info |
| OpenAI | First chunk with `delta.content` | Final chunk (requires `stream_options={"include_usage": True}`) | Must inject stream option if not present |

**OpenAI `stream_options` injection:** The instrumentation injects `stream_options={"include_usage": True}` into request kwargs only if the user did not already set `stream_options`. If `stream_options` is present but `include_usage` is not a key, it is merged (not replaced). If the injection causes an API error (e.g., OpenAI-compatible endpoint does not support `stream_options`), the error is caught, the call is retried without injection, and token counts are NULL for that call.

**Tool call streaming:** Anthropic's fine-grained tool streaming sends tool arguments incrementally via `content_block_delta`. The instrumentation reconstructs complete tool call arguments from these deltas before emitting the span.

**Error recovery in streaming:** If the profiler's stream wrapper encounters an internal error while processing a chunk, it switches to pass-through mode: all subsequent chunks are yielded directly to the caller without profiler processing. A partial span is emitted with `status=ERROR` and an `agentc.error` attribute describing the failure. The accumulated data (TTFT, partial token count) is preserved on the partial span. The user's stream is never interrupted by a profiler bug.

---

## Error Handling

**Principle: the profiler must never break the application it observes.**

**`fail_open` boundary:** `fail_open=True` applies to the `@trace` decorator wrapper, `span()` `__enter__`/`__exit__`, all SDK patch wrappers, and the background writer. It does NOT apply to `init()` and `shutdown()`, which are explicit user calls and should raise on failure.

- All instrumentation code catches `BaseException` (not just `Exception`) at the outermost boundary to intercept PyO3 `PanicException`. Profiler failures are logged via Python's `logging` module (logger name: `agentc`), never propagated to user code.
- Failed LLM calls are recorded as spans with `status=ERROR` and error attributes (`error.type`, `error.message`) per OTel conventions. These are valuable profiling data — retries and failures are exactly what the waste detector needs to see.
- If SQLite storage fails (disk full, permissions, corruption), spans are buffered in the queue. If the queue is full (see backpressure below), new spans are dropped.
- On the Rust side, panics are avoided in the hot path. All `#[pyfunction]` exports use `Result<T, PyErr>` return types.

---

## Concurrency Model

- **Span tracking**: Uses `contextvars.ContextVar` for the current span stack. This works correctly with both threads (`contextvars` are per-thread by default) and async tasks (`contextvars` are per-task in asyncio). Note: `contextvars` are NOT automatically copied to `ThreadPoolExecutor` threads — the `traced_executor` wrapper is required for cross-thread context propagation.
- **Span collection**: Completed spans are pushed to a bounded `queue.Queue` (max 1000 spans). On `queue.Full`, the new span is dropped (tail-drop policy). Drop count is tracked and logged via the `agentc` logger every 100 drops. **Root span reservation:** Since spans are enqueued on completion and root spans complete last, tail-drop can lose the root span. To prevent this, root spans (those with `parent_span_id=None`) bypass the queue and call `_native.write_span()` directly on the calling thread. The Rust function performs the same work as the background writer path — content hashing, compression, embedding, and SQLite write. The direct write has a 100ms timeout (embedding adds ~20us, well within budget). If it fails or times out, the root span is dropped with a warning log under `fail_open=True`, or raises under `fail_open=False`. This guarantees every trace has its root span even under backpressure.
- **Background writer**: A single daemon thread drains the queue and writes to SQLite via the Rust core (PyO3). This keeps SQLite writes off the hot path. The writer flushes to disk every 100 spans or every 5 seconds (whichever comes first), ensuring a hard kill loses at most a few seconds of data. For long-running processes, the background writer triggers a periodic merge every 10,000 spans or every 30 minutes (whichever comes first), flushing completed traces from the per-process DB to the canonical store. This ensures data is visible to CLI analysis without waiting for process exit.
- **Per-process isolation**: Each process writes to its own SQLite file (`~/.agentc/active/pid-<PID>.db`). Zero write contention across processes. Files are merged into `~/.agentc/traces.db` on `shutdown()` or on CLI read.
- **Shutdown**: `agentc.shutdown(timeout_ms)` drains the queue, writes remaining spans, merges per-process DB into canonical store, and unpatches all SDKs. Called automatically via `atexit`, `SIGTERM`, and `SIGINT` handlers. `atexit` is best-effort — it does not run on `SIGKILL` or `os._exit()`. The periodic flush ensures minimal data loss in those cases.

---

## Pricing

Model pricing is needed for cost attribution in the analyzer.

- **Bundled table**: Ships with known pricing for current Anthropic and OpenAI models, including cache pricing.
- **User overrides**: `~/.agentc/pricing.toml` for custom or private model pricing. Schema:

```toml
[my-private-model]
input_cost = 2.50        # USD per 1M input tokens (required)
output_cost = 10.00      # USD per 1M output tokens (required)
context_window = 128000  # optional
cache_creation_cost = 3.125  # optional
cache_read_cost = 0.25       # optional
```

Each table key is a `model_id`. `input_cost` and `output_cost` are required; all other fields are optional.

- **Update command**: `agentc pricing update` fetches latest pricing from `https://raw.githubusercontent.com/<org>/agentc/main/data/pricing.json`. On fetch failure, keeps existing prices and warns.
- **Unknown models**: If a model ID is not in the pricing table, `cost_usd` is `null` (not zero). The analyzer warns on unknown models.
- **Staleness warning**: If the most recent `updated_at` for `source='bundled'` is older than 90 days, the analyzer emits a warning suggesting `agentc pricing update`.
- **Single source of truth**: The `model_pricing` table lives in the canonical `traces.db` only. The CLI binary loads bundled pricing into `traces.db` on first access via `INSERT OR IGNORE`. The Python `init()` path does not load pricing — it writes spans with `cost_usd = NULL`. Pricing is a CLI/analyzer concern, not a capture-time concern. User overrides from `~/.agentc/pricing.toml` are loaded by the CLI on startup, merged via `INSERT OR REPLACE` (user overrides win). No auto-fetch outside explicit `agentc pricing update`.

---

## Waste Detection (analyzer crate)

Heuristic waste detectors that flag common anti-patterns. Run post-hoc over stored traces. Each flag includes an estimated dollar cost impact.

| Pattern | Detection Heuristic | Flag | Recommendation |
|---|---|---|---|
| **Context bloat** | `input_tokens / context_window > 0.8` and `output_tokens < 100` and `finish_reasons` does not contain `tool_use` | `context_bloat` | Truncate context to relevant sections; use RAG to select only needed content; split into focused sub-queries |
| **Redundant calls** | All `chat` spans in the same trace are compared pairwise by cosine similarity on `input_embedding`. Pairs exceeding 0.90 are clustered via single-linkage clustering. Each cluster of size >= 2 produces one `redundant_call` flag. If both spans also have `output_embedding` cosine similarity > 0.90, the pair is marked `confidence=high`; otherwise `confidence=input_only`. | `redundant_call` | Cache the first result and reuse; deduplicate prompts upstream; check if the same question is being asked by multiple agents |
| **Retry storm** | Spans are sorted by `start_time`. A storm is detected by scanning spans with the same `gen_ai.request.model`: for each span, collect all subsequent spans within 5 seconds (by `start_time`) with cosine similarity > 0.95 on `input_embedding`. If the group has 3+ spans, flag the entire group. Overlapping groups are merged. | `retry_storm` | Add exponential backoff; check for error-retry loops; verify the retry condition is actually transient |
| **Model overkill** | Frontier-tier model used for `chat` span with `input_tokens < 500` and `output_tokens < 100` and `finish_reasons` does not contain `tool_use` | `model_overkill` | Downgrade to a smaller/cheaper model; estimated savings shown in output |
| **Cache miss on repeat** | Same `input_content_id` (exact match) sent twice without cache hit (`cache_read_cost IS NOT NULL` in `model_pricing` for that model and `gen_ai.usage.cache_read.input_tokens` is 0 or absent on the second call) | `cache_miss_repeat` | Enable prompt caching (Anthropic) or seed-based caching (OpenAI); consider application-level caching |

**Frontier-tier models:** Defined as models with `input_cost >= $3.00 / 1M tokens` in the `model_pricing` table. This currently includes `claude-opus-*` and `gpt-4o`. The threshold is a single constant in the analyzer, easy to adjust as pricing evolves.

**NULL handling in detectors:**
- `context_bloat`: Requires `context_window` in `model_pricing`. If `context_window` is NULL for the model, the detector is skipped for that span (cannot compute utilization ratio).
- `model_overkill`: Requires pricing data. If the model is not in `model_pricing`, the detector is skipped.
- `redundant_call`: Requires `input_embedding`. If NULL, the detector is skipped for that span (see Graceful Degradation below).
- `retry_storm`: Requires `input_embedding`. If NULL, the detector is skipped for that span (see Graceful Degradation below).
- `cache_miss_repeat`: Uses `input_content_id` (always present). No pricing dependency.

**Cost estimation per flag:**
- `redundant_call` = total cost of all calls in the cluster except the first (by `start_time`).
- `retry_storm` = cost of all retries beyond the first in each storm group.
- `model_overkill` = delta between actual cost and estimated cost on the cheapest model from the same provider in `model_pricing` with `input_cost < $3.00 / 1M tokens`. If no cheaper model exists, the flag is raised but savings estimate is NULL.
- `cache_miss_repeat` = cost of the second (and subsequent) calls with the same `input_content_id`.
- `context_bloat` = total cost of the flagged call (`input_tokens * input_cost / 1_000_000 + output_tokens * output_cost / 1_000_000`). The entire call is considered waste because it consumed >80% of the context window but produced <100 output tokens — the call was unproductive regardless of how many input tokens were "excess."

**Waste deduplication:** When computing the total flagged waste, each span's waste contribution is the maximum of its individual flag estimates, not their sum. This prevents double-counting when a single call is flagged by multiple detectors (e.g., both `context_bloat` and `redundant_call`).

**Similarity detection uses model2vec embeddings** (256-dim, stored on each span). Embeddings are stored as float16 for space efficiency; the analyzer upcasts to float32 before computing cosine similarity. Thresholds are calibrated empirically against real SWE-bench traces during the benchmark phase. SHA-256 exact match (`input_content_id`) is used for `cache_miss_repeat` where exact equality is the right check.

**Graceful degradation:** If embeddings are NULL (model loading failed, or `agentc embed --backfill` has not been run), embedding-based detectors (`redundant_call`, `retry_storm`) are skipped. SHA-256-based detectors (`cache_miss_repeat`) and token-count-based detectors (`context_bloat`, `model_overkill`) still run.

**Zero-vector guard:** If either embedding in a cosine similarity comparison has L2 norm < 1e-7, cosine similarity returns 0.0 and the span is excluded from embedding-based detectors (`redundant_call`, `retry_storm`). This prevents division-by-zero when empty or near-empty input produces an all-zeros embedding vector.

These heuristics are intentionally conservative. The flags are informational; the profiler does not modify execution.

---

## Security Considerations

The profiler captures full prompt and response content by default. LLM prompts routinely contain API keys, PII, and proprietary code.

- **Local-only storage:** All data stays on the user's machine in `~/.agentc/`. Nothing is sent to external servers. This is a security advantage over proxy-based tools.
- **File permissions:** `~/.agentc/` is created with 0700 permissions. DB files are created with 0600.
- **Export sensitivity:** `agentc export` output contains full content and should be treated as sensitive.
- **Redacted mode:** `agentc.init(capture_content=False)` stores only content hashes + token counts. No prompt/response text. Embeddings are also skipped by default (opt in with `capture_embeddings=True`). See the Content Capture Strategy section for details on the privacy trade-off.
- **`agentc record` temp files:** The temp directory for `sitecustomize.py` is created with `tempfile.mkdtemp()` (0700 on Unix) and cleaned up on exit.

---

## Evaluation

### Overhead Budget

The profiler must not meaningfully impact the workload it measures:
- **Latency**: <5ms added per LLM call on the calling thread (span creation + queue push only). For queued spans, embedding computation (~10-20 microseconds) and SQLite writes happen asynchronously on the background writer thread after dequeue — neither blocks the LLM call path. Root spans bypass the queue and call `write_span()` synchronously (adding ~20us for embedding + SQLite write to the calling thread), but root spans occur at most once per trace.
- **Memory**: <100MB resident for traces with full content capture (zstd compression) + ~10MB for model2vec weights (bundled in binary). <50MB without content.
- **Disk**: ~2KB per span metadata (including 1KB embeddings) + compressed content (varies). A 1000-call agent session ~ 5-50MB depending on prompt sizes.
- **Sampling**: Captures all calls, no sampling. The overhead budget assumes typical agent workloads (10-100 LLM calls per session).

### Correctness

- Every LLM call through a patched SDK is captured (no data loss) — including streaming, async, and error cases
- Token counts match provider-reported values exactly
- Span tree structure accurately reflects execution nesting
- Streaming TTFT is accurate to within 10ms

### Utility

The profiler is useful if it can:
1. Capture a complete execution trace for any agent pipeline with `agentc record` + no code changes
2. Export traces to standard OTel backends (Jaeger, Tempo)
3. Provide per-call token counts, latency, and model info via CLI
4. Detect redundant calls, context bloat, and other waste patterns with dollar amounts
5. Produce a cost breakdown by agent, model, and waste category

### Benchmark Plan

1. Instrument a multi-agent pipeline running 50 SWE-bench tasks
2. Produce a token waste report (breakdown by waste pattern with dollar amounts)
3. Compute per-agent cost attribution
4. Measure profiler overhead (latency delta with/without profiler)
5. Validate token counts against provider response data
6. Calibrate similarity thresholds: precision/recall at cosine similarity 0.80, 0.85, 0.90, 0.95 (using float16 storage with float32 computation)

---

---

## Implementation Phases

### Phase 0: Build pipeline (week 1)
- Cargo workspace setup (agentc-core, agentc-profiler, agentc-analyzer, agentc-cli crates)
- PyO3 scaffolding via maturin, basic FFI roundtrip test
- `pyproject.toml` with maturin configuration, `.pyi` type stubs scaffolding
- CI: build + test on macOS and Linux
- SQLite schema (per-process + canonical) with `PRAGMA user_version`, basic read/write from Rust
- Bundled model_pricing table with cache pricing columns

### Phase 1: Core collection — Anthropic only (weeks 2-3)
- Rust: Span struct, trace view, per-process SQLite storage with WAL mode, content tables with zstd compression, canonical JSON serialization + SHA-256 content dedup
- Rust: model2vec inference (tokenizer + matrix lookup + mean pooling), weights bundled via `include_bytes!()`, lazy-loaded via `OnceCell`
- Python: `init()`, `shutdown()`, Anthropic SDK patches via wrapt (sync + async + streaming), version detection + adapter selection, `@trace` decorator, `span()` context manager
- Python: Background writer thread with bounded queue + tail-drop, fail-open error handling, `atexit`/`SIGTERM`/`SIGINT` shutdown handlers, periodic flush (100 spans / 5s)
- Python: Embedding computation on background writer thread (post-dequeue) via Rust, stored as float16
- CLI: `agentc record` (via sitecustomize.py with chaining), `agentc traces`, `agentc export`
- Test: instrument a single-agent Anthropic script, verify trace output and OTLP export

### Phase 2: Multi-provider + multi-agent + analysis (weeks 4-6)
- Python: OpenAI SDK patches via wrapt (sync + async + streaming), version detection + adapter, httpx transport fallback with URL filtering
- Python: Context propagation — `traced_executor` (with `copy_context()`), `get/attach_trace_context`, `inject_trace_headers`
- Rust: Provider normalization, per-process DB merge logic (lockfile + INSERT OR IGNORE + orphan GC), content dedup across processes
- Rust: agentc-analyzer crate — cost computation (with cache pricing), waste pattern detection (float32 upcast for cosine similarity), dollar amount estimation per flag, aggregation
- CLI: `agentc analyze` (with mock output format), `agentc report`, `agentc pricing update`, `agentc embed --backfill`
- Pricing: bundled table + user overrides + fetch command + staleness warning
- Test: multi-agent pipeline with mixed providers, verify cross-agent trace linking and waste detection

### Phase 3: Benchmark + hardening (weeks 7-8)
- Benchmark: run against real multi-agent SWE-bench workload
- Calibrate embedding similarity thresholds for waste detectors (using float16 storage + float32 computation)
- Produce token waste report and cost attribution analysis with dollar amounts
- Measure profiler overhead (latency delta with/without profiler)
- Validate token counts against provider response data
- Harden: edge cases, error paths, large-scale traces, schema migration path
- Security audit: file permissions, content sensitivity documentation
- Write up: profiling dataset and waste taxonomy as empirical contribution
