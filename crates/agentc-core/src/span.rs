//! Span data model for profiler traces.

use serde::{Deserialize, Serialize};

/// A single profiler span representing one LLM call.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Span {
    pub span_id: String,
    pub trace_id: String,
    pub parent_span_id: Option<String>,
    pub name: String,
    /// Span kind: "chat", "execute_tool", "invoke_agent".
    pub kind: String,
    /// Start time as Unix microseconds.
    pub start_time: i64,
    /// End time as Unix microseconds (None if still running).
    pub end_time: Option<i64>,
    /// Status code: "OK", "ERROR", etc.
    pub status: String,
    pub model: Option<String>,
    pub provider: Option<String>,
    pub input_tokens: Option<i64>,
    pub output_tokens: Option<i64>,
    pub cache_creation_tokens: Option<i64>,
    pub cache_read_tokens: Option<i64>,
    /// Cost in USD (NULL until backfilled by analyzer).
    pub cost_usd: Option<f64>,
    /// JSON blob of additional attributes.
    pub attributes: String,
    /// SHA-256 content ID for input dedup.
    pub input_content_id: Option<String>,
    /// SHA-256 content ID for output dedup.
    pub output_content_id: Option<String>,
    /// float16 256-dim embedding (512 bytes).
    pub input_embedding: Option<Vec<u8>>,
    /// float16 256-dim embedding (512 bytes).
    pub output_embedding: Option<Vec<u8>>,
    pub embedding_model: Option<String>,
}

/// Summary of a trace (from the traces VIEW).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TraceSummary {
    pub trace_id: String,
    pub start_time: i64,
    pub end_time: Option<i64>,
    pub root_span_id: Option<String>,
    pub root_span_count: i64,
}

/// Pricing entry for a model.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelPricing {
    pub model_id: String,
    /// Cost per input token in USD.
    pub input_cost: f64,
    /// Cost per output token in USD.
    pub output_cost: f64,
    pub cache_creation_cost: Option<f64>,
    pub cache_read_cost: Option<f64>,
    pub context_window: Option<i64>,
    /// Unix microseconds.
    pub updated_at: i64,
    /// "bundled" or "user".
    pub source: String,
}

/// Which content table to target.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContentTable {
    InputContent,
    OutputContent,
}

impl ContentTable {
    pub fn table_name(self) -> &'static str {
        match self {
            ContentTable::InputContent => "input_content",
            ContentTable::OutputContent => "output_content",
        }
    }
}
