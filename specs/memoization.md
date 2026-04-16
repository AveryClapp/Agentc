---
title: Semantic Memoization
status: active
last-updated: 2026-04-16
---

# Semantic Memoization

An opt-in caching layer that deduplicates LLM inference at the prompt level. Exact-prompt matches return cached outputs in microseconds; semantically similar prompts are detected via locality-sensitive hashing (LSH) over embeddings and served when cosine similarity exceeds a user-tunable threshold. Cache state lives in the profiler's canonical SQLite store (`traces.db`) and reuses its cross-process merge infrastructure.

---

## Overview

Agent workloads repeat themselves. The profiler's `redundant_call` waste detector routinely surfaces call sites where the same prompt runs dozens of times per session — re-summarizations, repeated classifications, identical tool-grounding questions. Memoization caches those outputs keyed on `(prompt, model, parameters)` and serves subsequent calls from the cache.

The runtime answers one question on each LLM call: **has this prompt already been answered?** The answer path is tiered:

1. **Exact hash lookup** — SHA-256 over the canonical prompt JSON. O(1) SQLite primary-key query. Returns verbatim output when hit.
2. **LSH similarity lookup** (fallback on exact miss) — 256-dim embedding → 64-bit hyperplane signature → 8-band hash lookup → cosine rerank. Returns the highest-similarity cached output above threshold.
3. **Miss** — the LLM call proceeds. The response is canonicalized, hashed, embedded, and written to the cache.

Memoization is **opt-in per call site**. Users annotate functions with `@agentc.memoize(...)` or pass `agentc_memoize=True` on individual LLM calls. The profiler suggests candidates via `agentc analyze`; the user promotes them with full knowledge of what's being cached. This keeps the trust boundary explicit — a false-positive cache hit cannot silently corrupt a production agent that the user didn't sign up for.

**What memoization does:**

- Caches LLM outputs keyed on exact and semantically-similar prompts.
- Deduplicates cache value storage via the profiler's existing `output_content` table.
- Coordinates across processes via the same flock-merged canonical SQLite store.
- Reports cache performance (hit rate, savings, p99 latency) through `agentc cache stats`.

**What memoization does not do:**

- Does not rewrite, summarize, or transform cached content. Cached output is served verbatim.
- Does not cache across distinct `(model, parameters)` tuples. Swapping `temperature` from `0.0` to `0.2` invalidates the entry.
- Does not cache tool calls as a unit. Each LLM call is an independent cache entry; the tool layer above it runs normally on every invocation.
- Does not run automatically. Without an explicit opt-in, every LLM call misses the cache.
- Does not propagate negative results (dead-end caching). That lives in `future-work.md`.

---

## Interface

### Python API

```python
import agentc

agentc.init()

# Decorator form: all LLM calls inside this function are memoized.
@agentc.memoize(ttl=3600)
def summarize(text: str) -> str:
    return openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": f"Summarize: {text}"}],
    ).choices[0].message.content

# Per-call form: one LLM invocation is memoized.
response = openai_client.chat.completions.create(
    model="gpt-4o",
    messages=[...],
    extra_headers={"agentc-memoize": "true", "agentc-memoize-ttl": "3600"},
)

# Tighter similarity threshold (default 0.92; 1.0 = exact match only).
@agentc.memoize(ttl=3600, similarity=0.95)
def classify(text: str) -> str:
    ...

# Disable LSH entirely; exact match only.
@agentc.memoize(ttl=3600, similarity=1.0)
def lookup_definition(term: str) -> str:
    ...

# Manual invalidation.
agentc.cache_invalidate(pattern="app.nlp:summarize")   # per call site
agentc.cache_invalidate(pattern="app.nlp:*")           # glob prefix
agentc.cache_invalidate_all()                          # nuke the cache
```

The `@agentc.memoize` decorator accepts the following keyword arguments:

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `ttl` | `int` (seconds) | `3600` | TTL for new cache entries. |
| `similarity` | `float` | `0.92` | Minimum cosine similarity for an LSH hit. `1.0` disables LSH. |
| `models` | `list[str] \| None` | `None` | If set, only cache calls where `model` is in this allowlist. |
| `call_site_id` | `str \| None` | `None` | Override the auto-derived `"module.function:line"` identifier. |
| `enabled` | `bool \| Callable[..., bool]` | `True` | Gate caching on a runtime predicate (e.g., env flag). |

### CLI

```
$ agentc cache stats
Cache summary (last 24h)
─────────────────────────────────────────────────────────
Entries:          12,841     (2.3 GB on disk)
Exact hits:       4,103      (31.2%)
LSH hits:         1,108      (8.4%)
Misses:           7,948      (60.4%)

Savings:          $147.23    82,419 tokens
p99 lookup:       1.4ms      (exact 0.3ms, lsh 3.1ms)

Top call sites by hit rate:
  app.agents.planner:plan_next_step          94.2% hit
  app.tools.web:summarize_page               61.7% hit
  app.agents.router:classify_intent          58.3% hit
  app.tools.code:lint_snippet                41.9% hit

$ agentc cache inspect e4c1a9...
Cache entry: e4c1a9d2b7f340...
  Call site:       app.agents.planner:plan_next_step
  Model:           gpt-4o
  Hit count:       42
  Created:         2026-04-15 09:12:33 UTC
  Last hit:        2026-04-16 14:23:11 UTC
  Expires:         2026-04-16 18:12:33 UTC
  Input tokens:    1,842
  Output tokens:   317
  Prompt (first 200 chars):
    "You are a planning agent. Given the user goal and the
     current trace, return the next step as a JSON object..."
  Output (first 200 chars):
    "{\"action\": \"call_tool\", \"tool\": \"web.search\",..."

$ agentc cache evict --older-than 7d
Evicted 2,018 expired entries (418 MB reclaimed).

$ agentc cache evict --pattern "app.agents.router:*"
Evicted 312 entries matching "app.agents.router:*".

$ agentc cache bench --call-site app.nlp:summarize --runs 200
Running baseline (memoization disabled)...
Running memoized...
─────────────────────────────────────────────────────────
Baseline:     $4.82   (avg $0.0241, p95 1,240ms)
Memoized:     $1.91   (avg $0.0096, p95 1,310ms*)
Savings:      60.4%   ($2.91)
Hit rate:     58.0%   (exact 46.0%, lsh 12.0%)
Divergence:   0.5%    (shadow mode)

* p95 on misses only; hits complete in <3ms.
```

### Configuration

Memoization reads from the same `agentc.toml` the profiler uses:

```toml
[memoization]
enabled = true                      # Master switch. False disables all memoize decorators.
default_ttl_seconds = 3600
default_similarity = 0.92
max_entries = 100_000               # LRU eviction triggers above this.
max_bytes = 2_147_483_648           # 2 GB cap on cache table size.
ttl_sweep_interval_seconds = 300    # Background eviction cadence.
```

Environment overrides (take precedence over the TOML):

| Variable | Effect |
|---|---|
| `AGENTC_MEMOIZE=0` | Disables memoization globally; decorators become no-ops. |
| `AGENTC_MEMOIZE_SIMILARITY=1.0` | Overrides default similarity threshold. |
| `AGENTC_MEMOIZE_TTL=86400` | Overrides default TTL. |

### Rust API

```rust
pub trait Cache: Send + Sync {
    fn lookup(&self, key: &CacheKey) -> Result<Option<CacheHit>>;
    fn insert(&self, key: CacheKey, value: CacheValue, ttl_seconds: u64) -> Result<()>;
    fn invalidate(&self, pattern: &InvalidationPattern) -> Result<u64>;
    fn stats(&self, window_seconds: u64) -> Result<CacheStats>;
}

pub struct CacheKey {
    pub prompt_hash: [u8; 32],      // SHA-256 over canonical prompt JSON
    pub model: String,
    pub parameters_hash: [u8; 32],  // SHA-256 over canonical (temperature, top_p, tools, ...)
    pub call_site_id: String,       // "module.function:line"
}

pub struct CacheHit {
    pub value: CacheValue,
    pub source: CacheSource,
    pub age_seconds: u64,
}

pub enum CacheSource {
    Exact,
    Lsh { similarity: f32 },
}

pub struct CacheValue {
    pub output_content_hash: [u8; 32],  // Points into the shared output_content table.
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub recorded_cost_usd: f32,
}

pub enum InvalidationPattern {
    CallSiteGlob(String),           // "app.agents.router:*"
    OlderThan { micros: i64 },
    All,
}
```

The canonical implementation is `SqliteCache` in `agentc-memo/src/cache.rs`. The trait exists so the optimizer spec can depend on `Cache` without dragging in the SQLite concretion.

### FFI surface

The Python → Rust boundary adds four functions to `agentc._native`:

```python
# python/agentc/_native.pyi
def cache_lookup(
    prompt_hash: bytes,
    model: str,
    parameters_hash: bytes,
    similarity_threshold: float,
) -> dict[str, Any] | None:
    """Return the cached output dict or None. Keys match CacheHit."""

def cache_insert(
    prompt_hash: bytes,
    model: str,
    parameters_hash: bytes,
    call_site_id: str,
    output_content_hash: bytes,
    input_tokens: int,
    output_tokens: int,
    recorded_cost_usd: float,
    ttl_seconds: int,
    embedding: bytes,  # 256 × f32, little-endian
) -> None: ...

def cache_invalidate(pattern: str) -> int:
    """GLOB pattern against call_site_id. Returns rows deleted."""

def cache_stats(window_seconds: int) -> dict[str, int]:
    """Aggregated counts and totals over the window."""
```

---

## Architecture

### Lookup flow

```
┌───────────────────────┐
│ @memoize-wrapped call │
└──────────┬────────────┘
           ▼
┌──────────────────────────────────┐
│ Canonicalize(prompt, params)     │   Python
│   → canonical JSON bytes         │
└──────────┬───────────────────────┘
           ▼
┌──────────────────────────────────┐
│ SHA-256 → prompt_hash            │   Python
│ SHA-256(params) → parameters_hash│
└──────────┬───────────────────────┘
           ▼
┌──────────────────────────────────┐
│ cache_lookup(hashes, threshold)  │   FFI
└──────────┬───────────────────────┘
           ▼
┌──────────────────────────────────┐
│ SELECT … WHERE cache_key_hash=?  │   Rust
│  ──→ hit: return CacheHit::Exact │
│  ──→ miss: continue              │
└──────────┬───────────────────────┘
           ▼
┌──────────────────────────────────┐
│ if threshold < 1.0:              │   Rust
│   embed(prompt) → e              │
│   sig = hyperplanes(e)           │
│   candidates = lsh_buckets(sig)  │
│   winner = argmax cosine(e, c)   │
│            above threshold       │
│   return CacheHit::Lsh or None   │
└──────────────────────────────────┘
```

Hot-path cost budget:

| Step | Target | Worst case |
|---|---|---|
| Canonicalize + SHA-256 | 50 μs | 200 μs (for 100 KB prompts) |
| SQLite primary-key lookup | 100 μs | 500 μs (cold cache) |
| Embed (model2vec potion-base-8M) | 400 μs | 1.2 ms (long prompts) |
| LSH bucket query | 200 μs | 1 ms (dense buckets) |
| Cosine rerank (≤50 candidates) | 50 μs | 200 μs |
| **Total exact hit** | **≤150 μs** | ≤700 μs |
| **Total LSH hit** | **≤1 ms** | ≤3 ms |

If the budget is exceeded the lookup aborts and returns `None` — a cache miss is always safe to return.

### Insert flow

Inserts do not block the caller. The SDK enqueues a `CacheInsert` message onto the existing background writer queue:

```python
# python/agentc/_writer.py
@dataclass
class CacheInsertMsg:
    prompt_hash: bytes
    model: str
    parameters_hash: bytes
    call_site_id: str
    output_bytes: bytes
    input_tokens: int
    output_tokens: int
    recorded_cost_usd: float
    ttl_seconds: int
    prompt_text: str    # retained only until embed+hash in writer thread

def enqueue_cache_insert(msg: CacheInsertMsg) -> None: ...
```

The writer thread processes `CacheInsertMsg` entries alongside existing `Span` messages. Processing one `CacheInsertMsg` runs the following in a single SQLite transaction:

1. SHA-256 the output bytes → `output_content_hash`.
2. `INSERT OR IGNORE INTO output_content (hash, bytes_zstd) VALUES (?, ?)` — reuses the profiler's dedup table.
3. Embed the prompt → 256×f32.
4. Compute the 64-bit hyperplane signature; split into 8 bands of 8 bits each.
5. `INSERT INTO memoization_cache (...)`.
6. `INSERT INTO memoization_lsh_bucket` × 8 bands.
7. `INSERT INTO memoization_embedding (cache_key_hash, embedding)`.

All four writes share the same transaction. If any step fails, the transaction rolls back and the writer logs a warning — the application's LLM call has already completed, so a cache-insert failure is never user-visible.

### Prompt canonicalization

Prompts enter in vendor-specific formats (OpenAI `messages`, Anthropic `messages`, Cohere `chat_history`, raw strings). Canonicalization maps all of them to a normalized JSON structure so that semantically identical prompts hash identically:

```python
def canonicalize_prompt(raw: Any, provider: str) -> bytes:
    """
    Output: UTF-8 JSON bytes, deterministic key order, no insignificant whitespace.
    Structure:
      {
        "provider": "openai" | "anthropic" | "cohere" | "raw",
        "messages": [
          {"role": "system" | "user" | "assistant" | "tool", "content": <normalized>},
          ...
        ],
        "tools": [{"name": ..., "schema_hash": <sha256>}, ...],   # sorted by name
        "response_schema_hash": <sha256> | null
      }
    """
```

Normalization rules:

- Role names lowercased (`System` → `system`).
- Content coerced to string; vendor-specific content arrays (OpenAI's `[{"type": "text", "text": "..."}]`) collapsed to their text concatenation when only text parts are present; multi-modal parts hashed separately (`{"type": "image", "sha256": "..."}`).
- Tool definitions sorted by `name`; each tool's JSON schema hashed to a fixed-length `schema_hash` so prompt hashes are stable across schema identity.
- Trailing/leading whitespace on content fields stripped.
- No timestamp-like fields, no request IDs, no per-call nonces make it into the canonical form.

Parameters canonicalize separately:

```python
def canonicalize_parameters(raw: dict) -> bytes:
    """
    Output: UTF-8 JSON bytes, deterministic key order.
    Keys retained: temperature, top_p, top_k, max_tokens, stop, seed,
                   response_format, tool_choice, frequency_penalty,
                   presence_penalty, logit_bias.
    Keys dropped: stream, user, metadata, extra_headers, agentc_*.
    Floats rounded to 6 decimals. Arrays sorted when order is not semantic.
    """
```

Every SDK-specific adapter lives in `python/agentc/_canonicalize/` and has a table-driven test suite (`tests/memoize/canonicalize_openai.py`, etc.).

### Embedding

Reuses the profiler's bundled model2vec `potion-base-8M` (256-dim static embeddings, `include_bytes!` at compile time). The memoization crate imports it through a shared `agentc_embed` crate rather than duplicating the asset:

```rust
// crates/agentc-embed/src/lib.rs
pub fn embed(text: &str) -> [f32; 256] { ... }
pub const EMBED_DIM: usize = 256;
```

Performance on the reference hardware (Apple M-series, single thread): ~400 μs for a 1 KB prompt, ~1 ms for a 10 KB prompt. No GPU dependency. No ONNX runtime dependency. No network calls.

### LSH

Hyperplane-based LSH over cosine similarity:

1. At startup, the cache loads 64 fixed random hyperplanes from `crates/agentc-memo/data/hyperplanes.f32` (256 × 64 f32, 64 KB, seeded deterministically so signatures are stable across processes and releases).
2. For an embedding `e`, the signature is `sig[i] = sign(e · h_i)` for `i ∈ [0, 64)`, packed into a `u64`.
3. The signature splits into 8 bands of 8 bits each: `band_j = (sig >> (8 * j)) & 0xFF`.
4. Candidate retrieval: for each band `j`, `SELECT cache_key_hash FROM memoization_lsh_bucket WHERE band_ix = ? AND bucket_id = ?` with `(j, band_j)`. Union the results.
5. Rerank: for each candidate, load the full 256-dim embedding from `memoization_embedding`, compute cosine similarity against the query embedding. Keep candidates ≥ `similarity_threshold`.
6. Return the highest-similarity candidate.

Expected collision probability given cosine similarity `s` and the 8×8 banding:

| Cosine similarity `s` | P(collision in ≥1 band) |
|---|---|
| 0.98 | 0.97 |
| 0.92 | 0.83 |
| 0.85 | 0.52 |
| 0.75 | 0.20 |
| 0.60 | 0.06 |
| 0.40 | 0.008 |

This sigmoid is calibrated to the default threshold of 0.92: true paraphrases collide reliably, dissimilar prompts rarely produce candidates, and the cosine rerank filters the residual noise.

### SQLite schema

```sql
-- Added to the canonical traces.db schema as migration 0003_memoization.sql.

CREATE TABLE memoization_cache (
    cache_key_hash          BLOB(32) PRIMARY KEY NOT NULL,
    prompt_hash             BLOB(32) NOT NULL,
    model                   TEXT     NOT NULL,
    parameters_hash         BLOB(32) NOT NULL,
    output_content_hash     BLOB(32) NOT NULL REFERENCES output_content(hash),
    input_tokens            INTEGER  NOT NULL,
    output_tokens           INTEGER  NOT NULL,
    recorded_cost_usd       REAL     NOT NULL,
    created_at              INTEGER  NOT NULL,  -- microseconds since epoch
    expires_at              INTEGER  NOT NULL,
    last_hit_at             INTEGER  NOT NULL,
    hit_count               INTEGER  NOT NULL DEFAULT 0,
    call_site_id            TEXT     NOT NULL
) STRICT;

CREATE INDEX idx_memo_prompt_hash    ON memoization_cache(prompt_hash);
CREATE INDEX idx_memo_expires_at     ON memoization_cache(expires_at);
CREATE INDEX idx_memo_call_site      ON memoization_cache(call_site_id);
CREATE INDEX idx_memo_last_hit       ON memoization_cache(last_hit_at);

CREATE TABLE memoization_lsh_bucket (
    band_ix         INTEGER  NOT NULL,   -- 0..7
    bucket_id       INTEGER  NOT NULL,   -- 0..255 (8 bits)
    cache_key_hash  BLOB(32) NOT NULL REFERENCES memoization_cache(cache_key_hash) ON DELETE CASCADE,
    PRIMARY KEY (band_ix, bucket_id, cache_key_hash)
) STRICT, WITHOUT ROWID;

CREATE INDEX idx_lsh_lookup ON memoization_lsh_bucket(band_ix, bucket_id);

CREATE TABLE memoization_embedding (
    cache_key_hash  BLOB(32) PRIMARY KEY NOT NULL REFERENCES memoization_cache(cache_key_hash) ON DELETE CASCADE,
    embedding       BLOB     NOT NULL    -- 256 × f32 = 1024 bytes, little-endian
) STRICT;

CREATE VIEW memoization_stats AS
    SELECT
        call_site_id,
        COUNT(*)                     AS entries,
        SUM(hit_count)               AS total_hits,
        SUM(recorded_cost_usd * hit_count) AS estimated_savings_usd,
        MAX(last_hit_at)             AS last_hit_at
    FROM memoization_cache
    GROUP BY call_site_id;
```

`cache_key_hash` is computed as `SHA-256(prompt_hash || model.as_bytes() || parameters_hash)` in Rust and matches the FFI input from Python.

### Cross-process coordination

The cache lives in the canonical `traces.db`. Writes go through the same per-process → canonical merge pipeline as spans:

- Each process writes to its own `traces.<pid>.<rand>.db` (already the profiler's convention).
- `agentc_core::merge::merge_all_pending` is extended to fold memoization tables in addition to span tables.
- Merge order: `output_content` first (dedup), then `memoization_cache` (`ON CONFLICT(cache_key_hash) DO UPDATE SET hit_count = hit_count + excluded.hit_count, last_hit_at = MAX(last_hit_at, excluded.last_hit_at)`), then `memoization_lsh_bucket` (`ON CONFLICT DO NOTHING`), then `memoization_embedding` (`ON CONFLICT DO NOTHING`).

Lookups read the canonical `traces.db` directly — they do not trigger a merge. Consequence: a cache entry inserted in process A is not visible to process B until the next merge tick (default 30 minutes, or 10,000 spans, or on-exit via `agentc record`). The worst-case behavior is a redundant LLM call in process B; process B then writes its own entry, and the two entries collapse into one at merge time via the `ON CONFLICT` clause.

This is intentional. Merging on every lookup would add flock contention to the hot path and is not justified for a cache where a 30-minute staleness window is acceptable.

### Eviction

Three eviction triggers:

1. **TTL sweep.** Every `ttl_sweep_interval_seconds` (default 300s), the background writer runs:
   ```sql
   DELETE FROM memoization_cache WHERE expires_at < ?;
   ```
   The `ON DELETE CASCADE` on `memoization_lsh_bucket` and `memoization_embedding` cleans up the associated index entries.

2. **Size cap (LRU).** On insert, if `COUNT(*) > max_entries` or `page_count * page_size > max_bytes`, evict the least-recently-hit 5% of entries:
   ```sql
   DELETE FROM memoization_cache
   WHERE cache_key_hash IN (
       SELECT cache_key_hash FROM memoization_cache
       ORDER BY last_hit_at ASC
       LIMIT ?
   );
   ```

3. **Explicit invalidation.** `agentc.cache_invalidate(pattern=...)` maps to `DELETE FROM memoization_cache WHERE call_site_id GLOB ?`. `cache_invalidate_all()` maps to `DELETE FROM memoization_cache` (no WHERE).

`VACUUM` runs opportunistically after an eviction that reclaims >64 MB, gated on the canonical DB's flock so it never races with a concurrent merge.

### Repo layout

```
crates/
├── agentc-embed/                   # Shared embedding crate (new)
│   ├── Cargo.toml
│   └── src/
│       ├── lib.rs
│       └── model_data.rs           # include_bytes! of potion-base-8M
├── agentc-memo/                    # Memoization crate (new)
│   ├── Cargo.toml
│   ├── data/
│   │   └── hyperplanes.f32         # 64 × 256 × f32, seeded build artifact
│   ├── src/
│   │   ├── lib.rs
│   │   ├── cache.rs                # Cache trait, SqliteCache impl
│   │   ├── key.rs                  # CacheKey, hashing helpers
│   │   ├── canonical.rs            # Prompt + parameters canonicalization (Rust mirror for tests)
│   │   ├── lsh.rs                  # Hyperplane LSH, banding, signature
│   │   ├── schema.rs               # DDL migration 0003
│   │   ├── eviction.rs             # TTL + LRU sweep
│   │   └── ffi.rs                  # PyO3 bindings exposed via agentc-profiler
│   └── tests/
│       ├── lookup.rs
│       ├── insert.rs
│       ├── lsh_collision.rs
│       └── eviction.rs
└── agentc-profiler/                # Extended to re-export memo FFI
    └── src/lib.rs

python/agentc/
├── _memoize.py                     # @memoize decorator, cache_invalidate()
├── _canonicalize/
│   ├── __init__.py
│   ├── openai.py
│   ├── anthropic.py
│   ├── cohere.py
│   └── raw.py
├── _writer.py                      # Extended with CacheInsertMsg handling
└── _native.pyi                     # Extended with cache_* stubs
```

### Python ↔ Rust boundary

| Responsibility | Python | Rust |
|---|---|---|
| `@memoize` decorator | ✓ | |
| Provider-specific prompt extraction | ✓ | |
| Canonicalization | ✓ (primary) | ✓ (mirror, for tests) |
| SHA-256 hashing | | ✓ |
| FFI `cache_lookup` dispatch | | ✓ |
| Exact SQLite lookup | | ✓ |
| Embedding | | ✓ |
| LSH signature + bucket query | | ✓ |
| Cosine rerank | | ✓ |
| `CacheInsertMsg` enqueue | ✓ | |
| Writer thread insert transaction | | ✓ |
| TTL sweep | | ✓ |
| Pattern invalidation (FFI → SQL) | ✓ (call site) | ✓ (execution) |
| CLI `agentc cache …` | | ✓ |

### Error handling

Memoization fails open. The decorator wraps both lookup and insert in try/except:

```python
def memoize(ttl=3600, similarity=0.92, ...):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = _maybe_build_key(fn, args, kwargs)
            if key is None:
                return fn(*args, **kwargs)
            try:
                hit = agentc._native.cache_lookup(*key, similarity)
                if hit is not None:
                    return _deserialize(hit)
            except BaseException:
                logger.debug("cache lookup failed", exc_info=True)
            result = fn(*args, **kwargs)
            try:
                _writer.enqueue_cache_insert(_build_msg(key, result, ttl))
            except BaseException:
                logger.debug("cache insert enqueue failed", exc_info=True)
            return result
        return wrapper
    return decorator
```

No memoization failure (DB corruption, FFI panic, canonicalization error) ever propagates to the user's call stack. The wrapped function always runs; caching is best-effort.

### Concurrency invariants

- **Lookup is read-only.** No cache-side mutation on hit; `hit_count` and `last_hit_at` are updated asynchronously via a per-process counter flushed by the writer thread every 5 seconds. This avoids a write on every lookup.
- **Insert is serialized per-process** by the existing writer thread; no cross-thread contention.
- **Merge is flock-serialized** across processes by the profiler's existing merge coordinator.
- **Lookup and insert never share a connection.** Lookup uses a read-only connection (`mode=ro`); insert uses the writer thread's single writer connection.

---

## Dependencies

### Sibling components

- **agentc-profiler** — owns `traces.db`, the writer thread, the merge coordinator, `output_content`. Memoization extends each of these in place.
- **agentc-core** — SQLite schema coordination (migrations), canonical DB path resolution.
- **agentc-embed** (new) — shared embedding crate; stores the model2vec asset once.
- **Optimizer** (future) — may invoke the `Cache` trait as a pre-execution pass; see `specs/optimizer.md` for the contract.

### Rust crates

Already in the workspace:
- `rusqlite` (with `bundled` feature)
- `sha2`
- `serde`, `serde_json`
- `zstd`
- `pyo3`, `pyo3-log`

New workspace additions:
- None.

### Python packages

Memoization has no new runtime Python dependencies.

---

## Evaluation

### Correctness

| Check | Test fixture |
|---|---|
| Exact-prompt hit returns verbatim output | `tests/memoize/lookup_hit.py` |
| Exact-prompt hit survives parameter reordering (canonicalization) | `tests/memoize/canonicalize_*.py` |
| Non-matching parameters produce distinct cache keys | `tests/memoize/key_separation.py` |
| LSH hit above threshold returns nearest candidate | `tests/memoize/lookup_lsh.py` |
| LSH candidate below threshold is rejected | `tests/memoize/lookup_lsh_below.py` |
| TTL expiry produces a miss | `tests/memoize/ttl_expiry.py` |
| Pattern invalidation removes exactly the matching call sites | `tests/memoize/invalidation.py` |
| Two processes inserting same key produce one canonical row | `tests/memoize/concurrent_insert.py` |
| DB corruption during lookup yields miss, not exception | `tests/memoize/corruption_fail_open.py` |
| FFI panic during insert does not crash the writer thread | `tests/memoize/insert_panic.py` |

### Performance targets

Benchmarks live in `bench/memoize_bench.py` and run on every CI pass:

| Metric | Target | Measurement |
|---|---|---|
| p50 exact hit latency | < 200 μs | 100k-entry cache, cold page cache |
| p99 exact hit latency | < 1 ms | Same |
| p50 LSH hit latency | < 1 ms | 100k-entry cache, 8 bands × 256 buckets each |
| p99 LSH hit latency | < 3 ms | Same |
| Insert throughput | > 500 inserts/s | Single writer thread |
| Memory overhead per 100k entries | < 250 MB on-disk | Including embeddings |

### Hit rate / savings (real workloads)

Validated against three reference agents in `bench/agents/`:

| Agent | Workload | Target hit rate | Target savings |
|---|---|---|---|
| `bench/agents/swebench_planner.py` | 50 SWE-bench tasks, 20 steps each | > 25% | > 20% |
| `bench/agents/gaia_router.py` | 80 GAIA questions | > 40% | > 35% |
| `bench/agents/rag_summarizer.py` | 200 document summarizations | > 60% | > 55% |

Savings = `sum(recorded_cost_usd_on_hit) / sum(recorded_cost_usd_if_disabled)`.

### LSH threshold calibration

`tests/memoize/golden_paraphrases.jsonl` contains 500 `(prompt_a, prompt_b, should_hit)` tuples covering:

- Paraphrases that should hit (same intent, different wording).
- Near-miss pairs that should not hit (same topic, different answer).
- Adversarial pairs (short prompts with shared prefixes but different semantics).

The calibration harness (`bench/memoize_threshold.py`) sweeps `similarity` from 0.80 to 0.99 in 0.01 steps and reports precision/recall. The default of 0.92 is chosen to minimize `false_positive_rate` subject to `recall ≥ 0.70`.

Acceptance criterion before promoting the default: `false_positive_rate < 0.01` at the ship threshold.

### Shadow-mode divergence

`agentc cache bench --shadow` runs every cached call twice (once from cache, once fresh) and records the Jaccard similarity of the output tokens. A divergence rate > 1% on a reference workload is a regression and blocks release.

### Acceptance criteria (ship gate)

The memoization crate reaches `status: active` when:

- All correctness tests pass.
- All three reference agents hit their savings targets with shadow divergence < 1%.
- p99 latencies meet the performance targets on the reference hardware.
- Documentation covers the opt-in workflow end to end with a working example.

---

## Design Decisions

### Exact-hash primary, LSH secondary

Exact-hash lookup costs microseconds and has zero false-positive risk. Production workloads empirically skew toward literal prompt reuse (same agent revisiting the same context), so gating LSH behind an exact miss avoids paying embedding cost on every call. **Rejected: LSH-primary.** Inverts the hit/miss ratio; on a workload with 90% exact hits it pays ~450 μs × 10× more embeddings than necessary. **Rejected: hybrid with cheap-model validation.** Doubles latency on every hit and introduces a validator-model dependency.

### Opt-in per call site

Memoization can silently change agent behavior when an LSH false positive returns a stale answer. Opt-in via `@memoize` keeps the trust boundary explicit and pairs with the profiler's `redundant_call` detector, which surfaces concrete candidates. **Rejected: opt-out.** One false positive in production can corrupt downstream state (e.g., a cached "task complete" response triggering premature termination); the correctness risk outweighs the win for users who forget to annotate. **Rejected: shadow-then-promote default.** Shadow mode doubles spend during calibration; users who want it run `agentc cache bench --shadow` explicitly.

### Piggyback on `traces.db`

The profiler already solved cross-process SQLite: flock-merged, per-process write DBs, canonical read DB, idempotent migrations. Adding memoization tables reuses all of it. **Rejected: Redis.** Adds a network dependency and a second failure mode; conflicts with the project's hermetic-runtime posture. **Rejected: in-process LMDB with SQLite write-through.** Fastest possible lookups, but a second storage format and a tiered-cache coherence problem aren't worth the latency win at 100k-entry scales.

### model2vec for embeddings

Already bundled in the profiler. Zero additional dependency, zero additional binary size. ~400 μs CPU inference is fast enough for the fallback path. **Rejected: sentence-transformers/BGE.** Higher-quality embeddings, but requires ONNX runtime and adds 100 MB+ to the binary; paraphrase matching doesn't need the extra quality.

### 8 × 8 LSH banding

Calibrated against a default threshold of 0.92 cosine similarity. With 8 bands of 8 rows, `P(collision at s=0.92) ≈ 0.83` — true paraphrases hit reliably — and `P(collision at s=0.70) ≈ 0.20` — dissimilar prompts are filtered by cosine rerank. 64-bit signatures pack into a `u64` for fast bucket math. **Rejected: 16 × 4.** Higher recall but catastrophic candidate counts at dense buckets. **Rejected: 4 × 16.** Too selective; misses paraphrases below s=0.98.

### Async lookup stats

`hit_count` and `last_hit_at` update asynchronously via a per-process counter flushed every 5 seconds, not on every lookup. A write on every lookup would put the hot path behind the writer thread's flock. The worst case is a 5-second staleness on eviction ordering, which is acceptable for LRU.

### Fail-open everywhere

Every memoization operation (lookup, insert, invalidate, stats) catches `BaseException` and returns a safe fallback (cache miss, log, no-op). A memoization bug must never crash a user's LLM call. The profiler adopts the same posture; the memoization crate follows suit for consistency.

---

## Open Questions

> **OPEN (avery, 2026-05-01):** Confirm the hyperplane asset stays stable across releases. If we ever regenerate `hyperplanes.f32`, existing caches become unusable — every entry's LSH signature would be incompatible. Options: (1) pin the asset forever, (2) version the schema and include `hyperplane_version` in `memoization_cache`, (3) wipe caches on mismatch.

> **OPEN (avery, 2026-05-01):** Decide whether multi-modal prompt parts (images, audio) are in scope for canonicalization. Today's draft hashes them by content SHA-256 only, which means two visually-identical-but-byte-different images (resized, re-encoded) miss. Out-of-scope placeholder in `future-work.md` if deferred.
