//! agentc-core: spans, traces, storage, content dedup, embeddings, OTLP export.

pub mod db;
pub mod embedding;
pub mod hardening;
pub mod merge;
pub mod span;
pub mod storage;

/// Re-export key types at crate root.
pub use span::{ContentTable, ModelPricing, Span, TraceSummary};
pub use storage::{SpanInput, WriteSpanOptions};
