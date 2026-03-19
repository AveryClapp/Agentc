//! Waste detectors: context_bloat, redundant_call, retry_storm, model_overkill, cache_miss_repeat.
//!
//! All detectors operate on a single trace at a time.
//! Waste deduplication: per-span MAX of flag estimates, not sum.

use std::collections::{HashMap, HashSet};

use anyhow::Result;
use rusqlite::{params, Connection};

use agentc_core::embedding::{cosine_similarity, is_zero_embedding};

use crate::cost::FRONTIER_THRESHOLD_USD;

/// Confidence level for waste flags.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Confidence {
    High,
    InputOnly,
    Low,
}

/// A waste flag raised by a detector.
#[derive(Debug, Clone)]
pub struct WasteFlag {
    pub detector: String,
    pub span_ids: Vec<String>,
    pub estimated_cost: Option<f64>,
    pub confidence: Confidence,
    pub description: String,
}

/// Aggregated waste analysis for a trace.
#[derive(Debug, Clone, Default)]
pub struct WasteAnalysis {
    pub flags: Vec<WasteFlag>,
    /// Deduplicated total waste (per-span MAX, not sum).
    pub total_waste_usd: f64,
}

/// A span as loaded for waste analysis.
#[derive(Debug, Clone)]
struct AnalysisSpan {
    span_id: String,
    kind: String,
    model: Option<String>,
    provider: Option<String>,
    start_time: i64,
    input_tokens: Option<i64>,
    output_tokens: Option<i64>,
    cache_read_tokens: Option<i64>,
    cost_usd: Option<f64>,
    attributes: String,
    input_content_id: Option<String>,
    input_embedding: Option<Vec<u8>>,
    output_embedding: Option<Vec<u8>>,
}

/// Extract finish_reasons from span attributes JSON.
fn extract_finish_reasons(attributes: &str) -> Vec<String> {
    let Ok(attrs) = serde_json::from_str::<serde_json::Value>(attributes) else {
        return Vec::new();
    };
    match attrs.get("gen_ai.response.finish_reasons") {
        Some(serde_json::Value::Array(arr)) => arr
            .iter()
            .filter_map(|v| v.as_str().map(|s| s.to_string()))
            .collect(),
        Some(serde_json::Value::String(s)) => vec![s.clone()],
        _ => Vec::new(),
    }
}

/// Check if finish_reasons contain tool_use.
fn has_tool_use(attributes: &str) -> bool {
    extract_finish_reasons(attributes)
        .iter()
        .any(|r| r == "tool_use")
}

/// Load all spans for a trace, ordered by start_time.
fn load_trace_spans(conn: &Connection, trace_id: &str) -> Result<Vec<AnalysisSpan>> {
    let mut stmt = conn.prepare(
        "SELECT span_id, kind, model, provider, start_time, \
         input_tokens, output_tokens, cache_read_tokens, cost_usd, \
         attributes, input_content_id, input_embedding, output_embedding \
         FROM spans WHERE trace_id = ?1 ORDER BY start_time",
    )?;

    let spans = stmt
        .query_map(params![trace_id], |row| {
            Ok(AnalysisSpan {
                span_id: row.get(0)?,
                kind: row.get(1)?,
                model: row.get(2)?,
                provider: row.get(3)?,
                start_time: row.get(4)?,
                input_tokens: row.get(5)?,
                output_tokens: row.get(6)?,
                cache_read_tokens: row.get(7)?,
                cost_usd: row.get(8)?,
                attributes: row.get(9)?,
                input_content_id: row.get(10)?,
                input_embedding: row.get(11)?,
                output_embedding: row.get(12)?,
            })
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;

    Ok(spans)
}

/// Get context_window for a model from model_pricing.
fn get_context_window(conn: &Connection, model: &str) -> Option<i64> {
    conn.query_row(
        "SELECT context_window FROM model_pricing WHERE model_id = ?1",
        params![model],
        |row| row.get(0),
    )
    .ok()
    .flatten()
}

/// Get input_cost for a model from model_pricing.
fn get_input_cost(conn: &Connection, model: &str) -> Option<f64> {
    conn.query_row(
        "SELECT input_cost FROM model_pricing WHERE model_id = ?1",
        params![model],
        |row| row.get(0),
    )
    .ok()
}

/// Find cheapest same-provider model below frontier threshold.
fn cheapest_non_frontier_model(conn: &Connection, _provider: &str) -> Option<(String, f64)> {
    conn.query_row(
        "SELECT model_id, input_cost FROM model_pricing \
         WHERE input_cost < ?1 \
         ORDER BY input_cost ASC LIMIT 1",
        params![FRONTIER_THRESHOLD_USD],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )
    .ok()
}

/// Check if an embedding is usable (non-null and non-zero).
fn has_usable_embedding(embedding: &Option<Vec<u8>>) -> bool {
    matches!(embedding, Some(bytes) if !bytes.is_empty() && !is_zero_embedding(bytes))
}

// --- Detector 1: context_bloat ---

fn detect_context_bloat(
    conn: &Connection,
    spans: &[AnalysisSpan],
) -> Vec<WasteFlag> {
    let mut flags = Vec::new();

    for span in spans {
        if span.kind != "chat" {
            continue;
        }

        let Some(ref model) = span.model else {
            continue;
        };
        let Some(input_tokens) = span.input_tokens else {
            continue;
        };
        let Some(output_tokens) = span.output_tokens else {
            continue;
        };
        let Some(context_window) = get_context_window(conn, model) else {
            continue;
        };

        if context_window == 0 {
            continue;
        }

        let usage_ratio = input_tokens as f64 / context_window as f64;

        if usage_ratio > 0.8 && output_tokens < 100 && !has_tool_use(&span.attributes) {
            flags.push(WasteFlag {
                detector: "context_bloat".to_string(),
                span_ids: vec![span.span_id.clone()],
                estimated_cost: span.cost_usd,
                confidence: Confidence::High,
                description: format!(
                    "Context {:.0}% full ({input_tokens}/{context_window} tokens) with only {output_tokens} output tokens",
                    usage_ratio * 100.0
                ),
            });
        }
    }

    flags
}

// --- Detector 2: redundant_call ---

fn detect_redundant_call(spans: &[AnalysisSpan]) -> Vec<WasteFlag> {
    let mut flags = Vec::new();

    // Filter to chat spans with usable input embeddings.
    let eligible: Vec<&AnalysisSpan> = spans
        .iter()
        .filter(|s| s.kind == "chat" && has_usable_embedding(&s.input_embedding))
        .collect();

    if eligible.len() < 2 {
        return flags;
    }

    // Compute pairwise cosine similarities > 0.90.
    let n = eligible.len();
    let mut adjacency: Vec<Vec<usize>> = vec![Vec::new(); n];

    for i in 0..n {
        for j in (i + 1)..n {
            let sim = cosine_similarity(
                eligible[i].input_embedding.as_ref().unwrap(),
                eligible[j].input_embedding.as_ref().unwrap(),
            );
            if sim > 0.90 {
                adjacency[i].push(j);
                adjacency[j].push(i);
            }
        }
    }

    // Single-linkage clustering via BFS.
    let mut visited = vec![false; n];
    for start in 0..n {
        if visited[start] || adjacency[start].is_empty() {
            continue;
        }

        let mut cluster = Vec::new();
        let mut queue = vec![start];
        while let Some(node) = queue.pop() {
            if visited[node] {
                continue;
            }
            visited[node] = true;
            cluster.push(node);
            for &neighbor in &adjacency[node] {
                if !visited[neighbor] {
                    queue.push(neighbor);
                }
            }
        }

        if cluster.len() >= 2 {
            cluster.sort();

            // Check output similarity for confidence.
            let all_have_output = cluster
                .iter()
                .all(|&i| has_usable_embedding(&eligible[i].output_embedding));

            let confidence = if all_have_output {
                let mut all_high = true;
                'outer: for ci in 0..cluster.len() {
                    for cj in (ci + 1)..cluster.len() {
                        let sim = cosine_similarity(
                            eligible[cluster[ci]].output_embedding.as_ref().unwrap(),
                            eligible[cluster[cj]].output_embedding.as_ref().unwrap(),
                        );
                        if sim <= 0.90 {
                            all_high = false;
                            break 'outer;
                        }
                    }
                }
                if all_high {
                    Confidence::High
                } else {
                    Confidence::InputOnly
                }
            } else {
                Confidence::InputOnly
            };

            // Cost: all but first (by start_time).
            let redundant_cost: f64 = cluster
                .iter()
                .skip(1)
                .filter_map(|&i| eligible[i].cost_usd)
                .sum();

            let span_ids: Vec<String> = cluster
                .iter()
                .map(|&i| eligible[i].span_id.clone())
                .collect();

            flags.push(WasteFlag {
                detector: "redundant_call".to_string(),
                span_ids,
                estimated_cost: if redundant_cost > 0.0 {
                    Some(redundant_cost)
                } else {
                    None
                },
                confidence,
                description: format!(
                    "Cluster of {} calls with input cosine > 0.90",
                    cluster.len()
                ),
            });
        }
    }

    flags
}

// --- Detector 3: retry_storm ---

fn detect_retry_storm(spans: &[AnalysisSpan]) -> Vec<WasteFlag> {
    let mut flags = Vec::new();

    let eligible: Vec<&AnalysisSpan> = spans
        .iter()
        .filter(|s| s.kind == "chat" && has_usable_embedding(&s.input_embedding))
        .collect();

    if eligible.len() < 3 {
        return flags;
    }

    let n = eligible.len();
    let mut in_storm: HashSet<usize> = HashSet::new();
    let mut storm_groups: Vec<Vec<usize>> = Vec::new();

    for i in 0..n {
        if in_storm.contains(&i) {
            continue;
        }

        let mut group = vec![i];

        for j in (i + 1)..n {
            if in_storm.contains(&j) {
                continue;
            }

            // Within 5 seconds.
            let time_diff = (eligible[j].start_time - eligible[i].start_time).abs();
            if time_diff > 5_000_000 {
                // start_time is in microseconds
                break; // Sorted by start_time, so all subsequent are further.
            }

            // Same model.
            if eligible[i].model != eligible[j].model {
                continue;
            }

            // Cosine > 0.95.
            let sim = cosine_similarity(
                eligible[i].input_embedding.as_ref().unwrap(),
                eligible[j].input_embedding.as_ref().unwrap(),
            );
            if sim > 0.95 {
                group.push(j);
            }
        }

        if group.len() >= 3 {
            for &idx in &group {
                in_storm.insert(idx);
            }
            storm_groups.push(group);
        }
    }

    for group in storm_groups {
        let span_ids: Vec<String> = group
            .iter()
            .map(|&i| eligible[i].span_id.clone())
            .collect();

        // Cost: all retries beyond first.
        let retry_cost: f64 = group
            .iter()
            .skip(1)
            .filter_map(|&i| eligible[i].cost_usd)
            .sum();

        flags.push(WasteFlag {
            detector: "retry_storm".to_string(),
            span_ids,
            estimated_cost: if retry_cost > 0.0 {
                Some(retry_cost)
            } else {
                None
            },
            confidence: Confidence::High,
            description: format!(
                "Storm of {} calls within 5 seconds with cosine > 0.95",
                group.len()
            ),
        });
    }

    flags
}

// --- Detector 4: model_overkill ---

fn detect_model_overkill(
    conn: &Connection,
    spans: &[AnalysisSpan],
) -> Vec<WasteFlag> {
    let mut flags = Vec::new();

    for span in spans {
        if span.kind != "chat" {
            continue;
        }

        let Some(ref model) = span.model else {
            continue;
        };
        let Some(input_cost) = get_input_cost(conn, model) else {
            continue;
        };

        // Must be frontier tier.
        if input_cost < FRONTIER_THRESHOLD_USD {
            continue;
        }

        let input_tokens = span.input_tokens.unwrap_or(0);
        let output_tokens = span.output_tokens.unwrap_or(0);

        if input_tokens >= 500 || output_tokens >= 100 {
            continue;
        }

        if has_tool_use(&span.attributes) {
            continue;
        }

        // Calculate savings vs cheapest non-frontier model.
        let provider = span.provider.as_deref().unwrap_or("");
        let savings = cheapest_non_frontier_model(conn, provider).and_then(
            |(_cheap_model, _cheap_input_cost)| {
                // Recompute with cheaper model pricing.
                span.cost_usd.map(|actual_cost| {
                    // Rough savings estimate: proportional to input cost ratio.
                    let ratio = _cheap_input_cost / input_cost;
                    actual_cost * (1.0 - ratio)
                })
            },
        );

        flags.push(WasteFlag {
            detector: "model_overkill".to_string(),
            span_ids: vec![span.span_id.clone()],
            estimated_cost: savings,
            confidence: Confidence::Low,
            description: format!(
                "Frontier model ({model}) used for small task ({input_tokens} input, {output_tokens} output tokens)"
            ),
        });
    }

    flags
}

// --- Detector 5: cache_miss_repeat ---

fn detect_cache_miss_repeat(
    conn: &Connection,
    spans: &[AnalysisSpan],
) -> Vec<WasteFlag> {
    let mut flags = Vec::new();

    // Group by input_content_id.
    let mut by_content: HashMap<&str, Vec<&AnalysisSpan>> = HashMap::new();
    for span in spans {
        if let Some(ref cid) = span.input_content_id {
            by_content.entry(cid.as_str()).or_default().push(span);
        }
    }

    for (content_id, group) in &by_content {
        if group.len() < 2 {
            continue;
        }

        // Check for cache misses on second+ calls.
        let mut repeat_spans = Vec::new();
        for span in group.iter().skip(1) {
            let cache_read = span.cache_read_tokens.unwrap_or(0);
            // Check if cache costs exist for this model in pricing.
            let has_cache_pricing = span.model.as_ref().and_then(|m| {
                conn.query_row(
                    "SELECT cache_read_cost FROM model_pricing WHERE model_id = ?1",
                    params![m],
                    |row| row.get::<_, Option<f64>>(0),
                )
                .ok()
                .flatten()
            });

            if has_cache_pricing.is_some() && cache_read == 0 {
                repeat_spans.push(span);
            }
        }

        if !repeat_spans.is_empty() {
            let span_ids: Vec<String> = repeat_spans
                .iter()
                .map(|s| s.span_id.clone())
                .collect();
            let repeat_cost: f64 = repeat_spans
                .iter()
                .filter_map(|s| s.cost_usd)
                .sum();

            flags.push(WasteFlag {
                detector: "cache_miss_repeat".to_string(),
                span_ids,
                estimated_cost: if repeat_cost > 0.0 {
                    Some(repeat_cost)
                } else {
                    None
                },
                confidence: Confidence::High,
                description: format!(
                    "Repeated input (content_id={}) without cache hit",
                    &content_id[..8.min(content_id.len())]
                ),
            });
        }
    }

    flags
}

/// Deduplicate waste: per-span MAX of flag estimates, not sum.
fn deduplicate_waste(flags: &[WasteFlag]) -> f64 {
    let mut per_span_max: HashMap<&str, f64> = HashMap::new();

    for flag in flags {
        if let Some(cost) = flag.estimated_cost {
            // Distribute cost equally across spans in the flag.
            let per_span = cost / flag.span_ids.len().max(1) as f64;
            for span_id in &flag.span_ids {
                let entry = per_span_max.entry(span_id.as_str()).or_insert(0.0);
                if per_span > *entry {
                    *entry = per_span;
                }
            }
        }
    }

    per_span_max.values().sum()
}

/// Run all 5 waste detectors on a single trace.
pub fn analyze_trace(conn: &Connection, trace_id: &str) -> Result<WasteAnalysis> {
    let spans = load_trace_spans(conn, trace_id)?;

    let mut all_flags = Vec::new();

    all_flags.extend(detect_context_bloat(conn, &spans));
    all_flags.extend(detect_redundant_call(&spans));
    all_flags.extend(detect_retry_storm(&spans));
    all_flags.extend(detect_model_overkill(conn, &spans));
    all_flags.extend(detect_cache_miss_repeat(conn, &spans));

    let total_waste_usd = deduplicate_waste(&all_flags);

    Ok(WasteAnalysis {
        flags: all_flags,
        total_waste_usd,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentc_core::db::{create_db, insert_span};
    use agentc_core::embedding::f32_to_f16_bytes;
    use agentc_core::span::{ModelPricing, Span};
    use tempfile::TempDir;

    fn base_span(id: &str, trace_id: &str) -> Span {
        Span {
            span_id: id.to_string(),
            trace_id: trace_id.to_string(),
            parent_span_id: None,
            name: "test".to_string(),
            kind: "chat".to_string(),
            start_time: 1_000_000,
            end_time: Some(2_000_000),
            status: "OK".to_string(),
            model: Some("claude-sonnet-4-20250514".to_string()),
            provider: Some("anthropic".to_string()),
            input_tokens: Some(1000),
            output_tokens: Some(200),
            cache_creation_tokens: None,
            cache_read_tokens: None,
            cost_usd: Some(0.01),
            attributes: "{}".to_string(),
            input_content_id: None,
            output_content_id: None,
            input_embedding: None,
            output_embedding: None,
            embedding_model: None,
        }
    }

    fn setup_db_with_pricing() -> (TempDir, Connection) {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("traces.db");
        let conn = create_db(&path, true).unwrap();

        // Frontier model.
        agentc_core::db::insert_pricing(
            &conn,
            &ModelPricing {
                model_id: "claude-sonnet-4-20250514".to_string(),
                input_cost: 3.0,
                output_cost: 15.0,
                cache_creation_cost: Some(3.75),
                cache_read_cost: Some(0.30),
                context_window: Some(200000),
                updated_at: 1000000,
                source: "bundled".to_string(),
            },
        )
        .unwrap();

        // Non-frontier model.
        agentc_core::db::insert_pricing(
            &conn,
            &ModelPricing {
                model_id: "claude-haiku-3-5-20241022".to_string(),
                input_cost: 0.80,
                output_cost: 4.0,
                cache_creation_cost: Some(1.0),
                cache_read_cost: Some(0.08),
                context_window: Some(200000),
                updated_at: 1000000,
                source: "bundled".to_string(),
            },
        )
        .unwrap();

        (dir, conn)
    }

    /// Generate a deterministic embedding vector.
    fn make_embedding(seed: f32) -> Vec<u8> {
        let v: Vec<f32> = (0..256).map(|i| seed + (i as f32) * 0.001).collect();
        f32_to_f16_bytes(&v)
    }

    /// Generate an embedding very similar to another (for redundancy testing).
    fn make_similar_embedding(seed: f32) -> Vec<u8> {
        let v: Vec<f32> = (0..256).map(|i| seed + (i as f32) * 0.001 + 0.0001).collect();
        f32_to_f16_bytes(&v)
    }

    // --- context_bloat tests ---

    #[test]
    fn test_context_bloat_flags_high_usage() {
        let (_dir, conn) = setup_db_with_pricing();

        let mut span = base_span("s1", "t1");
        span.input_tokens = Some(180000); // 90% of 200K context.
        span.output_tokens = Some(50);
        insert_span(&conn, &span).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();
        let bloat_flags: Vec<_> = analysis
            .flags
            .iter()
            .filter(|f| f.detector == "context_bloat")
            .collect();
        assert_eq!(bloat_flags.len(), 1);
        assert_eq!(bloat_flags[0].confidence, Confidence::High);
    }

    #[test]
    fn test_context_bloat_skips_tool_use() {
        let (_dir, conn) = setup_db_with_pricing();

        let mut span = base_span("s1", "t1");
        span.input_tokens = Some(180000);
        span.output_tokens = Some(50);
        span.attributes =
            r#"{"gen_ai.response.finish_reasons": ["tool_use"]}"#.to_string();
        insert_span(&conn, &span).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();
        let bloat_flags: Vec<_> = analysis
            .flags
            .iter()
            .filter(|f| f.detector == "context_bloat")
            .collect();
        assert_eq!(bloat_flags.len(), 0);
    }

    #[test]
    fn test_context_bloat_skips_null_context_window() {
        let (_dir, conn) = setup_db_with_pricing();

        let mut span = base_span("s1", "t1");
        span.model = Some("unknown-model".to_string()); // No pricing → no context_window.
        span.input_tokens = Some(180000);
        span.output_tokens = Some(50);
        insert_span(&conn, &span).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();
        let bloat_flags: Vec<_> = analysis
            .flags
            .iter()
            .filter(|f| f.detector == "context_bloat")
            .collect();
        assert_eq!(bloat_flags.len(), 0);
    }

    // --- redundant_call tests ---

    #[test]
    fn test_redundant_call_flags_similar_pair() {
        let (_dir, conn) = setup_db_with_pricing();

        let emb1 = make_embedding(1.0);
        let emb2 = make_similar_embedding(1.0);

        // Verify they're similar enough.
        let sim = cosine_similarity(&emb1, &emb2);
        assert!(sim > 0.90, "Expected cosine > 0.90, got {sim}");

        let mut s1 = base_span("s1", "t1");
        s1.input_embedding = Some(emb1.clone());
        s1.output_embedding = Some(emb1.clone());

        let mut s2 = base_span("s2", "t1");
        s2.start_time = 1_500_000;
        s2.input_embedding = Some(emb2.clone());
        s2.output_embedding = Some(emb2);

        insert_span(&conn, &s1).unwrap();
        insert_span(&conn, &s2).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();
        let redundant: Vec<_> = analysis
            .flags
            .iter()
            .filter(|f| f.detector == "redundant_call")
            .collect();
        assert_eq!(redundant.len(), 1);
        assert_eq!(redundant[0].span_ids.len(), 2);
        assert_eq!(redundant[0].confidence, Confidence::High);
    }

    #[test]
    fn test_redundant_call_input_only_confidence() {
        let (_dir, conn) = setup_db_with_pricing();

        let emb1 = make_embedding(1.0);
        let emb2 = make_similar_embedding(1.0);
        // Create a truly orthogonal output embedding.
        let different_output = {
            let mut v = vec![0.0f32; 256];
            // Alternating positive/negative to be orthogonal to the monotonic pattern.
            for i in 0..256 {
                v[i] = if i % 2 == 0 { 1.0 } else { -1.0 };
            }
            f32_to_f16_bytes(&v)
        };

        // Verify output cosine is low.
        let out_sim = cosine_similarity(&emb1, &different_output);
        assert!(out_sim < 0.90, "Expected output cosine < 0.90, got {out_sim}");

        let mut s1 = base_span("s1", "t1");
        s1.input_embedding = Some(emb1.clone());
        s1.output_embedding = Some(emb1);

        let mut s2 = base_span("s2", "t1");
        s2.start_time = 1_500_000;
        s2.input_embedding = Some(emb2);
        s2.output_embedding = Some(different_output);

        insert_span(&conn, &s1).unwrap();
        insert_span(&conn, &s2).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();
        let redundant: Vec<_> = analysis
            .flags
            .iter()
            .filter(|f| f.detector == "redundant_call")
            .collect();
        assert_eq!(redundant.len(), 1);
        assert_eq!(redundant[0].confidence, Confidence::InputOnly);
    }

    #[test]
    fn test_redundant_call_skips_null_embeddings() {
        let (_dir, conn) = setup_db_with_pricing();

        let mut s1 = base_span("s1", "t1");
        s1.input_embedding = None;
        let mut s2 = base_span("s2", "t1");
        s2.input_embedding = None;

        insert_span(&conn, &s1).unwrap();
        insert_span(&conn, &s2).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();
        let redundant: Vec<_> = analysis
            .flags
            .iter()
            .filter(|f| f.detector == "redundant_call")
            .collect();
        assert_eq!(redundant.len(), 0);
    }

    // --- retry_storm tests ---

    #[test]
    fn test_retry_storm_flags_three_rapid_calls() {
        let (_dir, conn) = setup_db_with_pricing();

        let emb = make_embedding(1.0);
        let emb2 = make_similar_embedding(1.0);

        // Verify cosine > 0.95.
        let sim = cosine_similarity(&emb, &emb2);
        assert!(sim > 0.95, "Expected cosine > 0.95, got {sim}");

        for i in 0..3 {
            let mut span = base_span(&format!("s{i}"), "t1");
            span.start_time = 1_000_000 + i as i64 * 1_000_000; // 1s apart.
            span.input_embedding = Some(if i == 0 { emb.clone() } else { emb2.clone() });
            insert_span(&conn, &span).unwrap();
        }

        let analysis = analyze_trace(&conn, "t1").unwrap();
        let storms: Vec<_> = analysis
            .flags
            .iter()
            .filter(|f| f.detector == "retry_storm")
            .collect();
        assert_eq!(storms.len(), 1);
        assert_eq!(storms[0].span_ids.len(), 3);
    }

    #[test]
    fn test_retry_storm_does_not_flag_two() {
        let (_dir, conn) = setup_db_with_pricing();

        let emb = make_embedding(1.0);

        for i in 0..2 {
            let mut span = base_span(&format!("s{i}"), "t1");
            span.start_time = 1_000_000 + i as i64 * 1_000_000;
            span.input_embedding = Some(emb.clone());
            insert_span(&conn, &span).unwrap();
        }

        let analysis = analyze_trace(&conn, "t1").unwrap();
        let storms: Vec<_> = analysis
            .flags
            .iter()
            .filter(|f| f.detector == "retry_storm")
            .collect();
        assert_eq!(storms.len(), 0);
    }

    // --- model_overkill tests ---

    #[test]
    fn test_model_overkill_flags_frontier_small_task() {
        let (_dir, conn) = setup_db_with_pricing();

        let mut span = base_span("s1", "t1");
        span.input_tokens = Some(200);
        span.output_tokens = Some(50);
        insert_span(&conn, &span).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();
        let overkill: Vec<_> = analysis
            .flags
            .iter()
            .filter(|f| f.detector == "model_overkill")
            .collect();
        assert_eq!(overkill.len(), 1);
    }

    #[test]
    fn test_model_overkill_does_not_flag_standard_model() {
        let (_dir, conn) = setup_db_with_pricing();

        let mut span = base_span("s1", "t1");
        span.model = Some("claude-haiku-3-5-20241022".to_string()); // input_cost = 0.80
        span.input_tokens = Some(200);
        span.output_tokens = Some(50);
        insert_span(&conn, &span).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();
        let overkill: Vec<_> = analysis
            .flags
            .iter()
            .filter(|f| f.detector == "model_overkill")
            .collect();
        assert_eq!(overkill.len(), 0);
    }

    #[test]
    fn test_model_overkill_savings_calculation() {
        let (_dir, conn) = setup_db_with_pricing();

        let mut span = base_span("s1", "t1");
        span.input_tokens = Some(200);
        span.output_tokens = Some(50);
        span.cost_usd = Some(0.10);
        insert_span(&conn, &span).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();
        let overkill: Vec<_> = analysis
            .flags
            .iter()
            .filter(|f| f.detector == "model_overkill")
            .collect();
        assert_eq!(overkill.len(), 1);
        // Should have savings (cheaper model exists).
        assert!(overkill[0].estimated_cost.is_some());
        assert!(overkill[0].estimated_cost.unwrap() > 0.0);
    }

    // --- cache_miss_repeat tests ---

    #[test]
    fn test_cache_miss_repeat_flags_duplicate_content() {
        let (_dir, conn) = setup_db_with_pricing();

        let mut s1 = base_span("s1", "t1");
        s1.input_content_id = Some("sha256-abc123".to_string());
        s1.cache_read_tokens = Some(100);

        let mut s2 = base_span("s2", "t1");
        s2.start_time = 2_000_000;
        s2.input_content_id = Some("sha256-abc123".to_string()); // Same.
        s2.cache_read_tokens = Some(0); // No cache hit.

        insert_span(&conn, &s1).unwrap();
        insert_span(&conn, &s2).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();
        let cache_flags: Vec<_> = analysis
            .flags
            .iter()
            .filter(|f| f.detector == "cache_miss_repeat")
            .collect();
        assert_eq!(cache_flags.len(), 1);
    }

    #[test]
    fn test_cache_miss_repeat_does_not_flag_with_cache_hit() {
        let (_dir, conn) = setup_db_with_pricing();

        let mut s1 = base_span("s1", "t1");
        s1.input_content_id = Some("sha256-abc123".to_string());

        let mut s2 = base_span("s2", "t1");
        s2.start_time = 2_000_000;
        s2.input_content_id = Some("sha256-abc123".to_string());
        s2.cache_read_tokens = Some(500); // Cache hit!

        insert_span(&conn, &s1).unwrap();
        insert_span(&conn, &s2).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();
        let cache_flags: Vec<_> = analysis
            .flags
            .iter()
            .filter(|f| f.detector == "cache_miss_repeat")
            .collect();
        assert_eq!(cache_flags.len(), 0);
    }

    // --- Deduplication test ---

    #[test]
    fn test_waste_dedup_takes_max_not_sum() {
        let flags = vec![
            WasteFlag {
                detector: "context_bloat".to_string(),
                span_ids: vec!["s1".to_string()],
                estimated_cost: Some(0.10),
                confidence: Confidence::High,
                description: String::new(),
            },
            WasteFlag {
                detector: "model_overkill".to_string(),
                span_ids: vec!["s1".to_string()],
                estimated_cost: Some(0.05),
                confidence: Confidence::Low,
                description: String::new(),
            },
        ];

        let total = deduplicate_waste(&flags);
        // MAX(0.10, 0.05) = 0.10, not 0.15.
        assert!((total - 0.10).abs() < 1e-10);
    }

    // --- Zero vector test ---

    #[test]
    fn test_zero_vector_excluded() {
        let zero = f32_to_f16_bytes(&vec![0.0f32; 256]);
        assert!(is_zero_embedding(&zero));
        assert!(!has_usable_embedding(&Some(zero)));
    }

    // --- Integration tests ---

    #[test]
    fn test_integration_all_detectors_on_synthetic_trace() {
        let (_dir, conn) = setup_db_with_pricing();

        let emb = make_embedding(1.0);
        let emb_similar = make_similar_embedding(1.0);

        // Span 1: context_bloat candidate.
        let mut s1 = base_span("s1", "t1");
        s1.input_tokens = Some(180000);
        s1.output_tokens = Some(30);
        s1.cost_usd = Some(0.50);
        insert_span(&conn, &s1).unwrap();

        // Span 2-3: redundant pair.
        let mut s2 = base_span("s2", "t1");
        s2.start_time = 3_000_000;
        s2.input_embedding = Some(emb.clone());
        s2.output_embedding = Some(emb.clone());
        s2.cost_usd = Some(0.02);

        let mut s3 = base_span("s3", "t1");
        s3.start_time = 4_000_000;
        s3.input_embedding = Some(emb_similar.clone());
        s3.output_embedding = Some(emb_similar.clone());
        s3.cost_usd = Some(0.02);

        insert_span(&conn, &s2).unwrap();
        insert_span(&conn, &s3).unwrap();

        // Span 4: model_overkill.
        let mut s4 = base_span("s4", "t1");
        s4.start_time = 10_000_000;
        s4.input_tokens = Some(100);
        s4.output_tokens = Some(20);
        s4.cost_usd = Some(0.005);
        insert_span(&conn, &s4).unwrap();

        // Span 5-6: cache_miss_repeat.
        let mut s5 = base_span("s5", "t1");
        s5.start_time = 15_000_000;
        s5.input_content_id = Some("sha-repeat".to_string());
        s5.cache_read_tokens = Some(100);
        s5.cost_usd = Some(0.01);

        let mut s6 = base_span("s6", "t1");
        s6.start_time = 16_000_000;
        s6.input_content_id = Some("sha-repeat".to_string());
        s6.cache_read_tokens = Some(0);
        s6.cost_usd = Some(0.01);

        insert_span(&conn, &s5).unwrap();
        insert_span(&conn, &s6).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();

        let detector_names: Vec<&str> = analysis.flags.iter().map(|f| f.detector.as_str()).collect();
        assert!(detector_names.contains(&"context_bloat"));
        assert!(detector_names.contains(&"redundant_call"));
        assert!(detector_names.contains(&"model_overkill"));
        assert!(detector_names.contains(&"cache_miss_repeat"));

        assert!(analysis.total_waste_usd > 0.0);
    }

    #[test]
    fn test_integration_null_embeddings_only_sha_detectors() {
        let (_dir, conn) = setup_db_with_pricing();

        // No embeddings — redundant_call and retry_storm should not fire.
        let mut s1 = base_span("s1", "t1");
        s1.input_content_id = Some("sha-test".to_string());
        s1.cache_read_tokens = Some(100);

        let mut s2 = base_span("s2", "t1");
        s2.start_time = 2_000_000;
        s2.input_content_id = Some("sha-test".to_string());
        s2.cache_read_tokens = Some(0);

        insert_span(&conn, &s1).unwrap();
        insert_span(&conn, &s2).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();

        let detector_names: Vec<&str> = analysis.flags.iter().map(|f| f.detector.as_str()).collect();
        assert!(!detector_names.contains(&"redundant_call"));
        assert!(!detector_names.contains(&"retry_storm"));
        // cache_miss_repeat should still fire.
        assert!(detector_names.contains(&"cache_miss_repeat"));
    }

    #[test]
    fn test_integration_no_waste_trace() {
        let (_dir, conn) = setup_db_with_pricing();

        // Normal span — no waste patterns.
        let mut span = base_span("s1", "t1");
        span.model = Some("claude-haiku-3-5-20241022".to_string()); // Non-frontier.
        span.input_tokens = Some(5000);
        span.output_tokens = Some(500);
        insert_span(&conn, &span).unwrap();

        let analysis = analyze_trace(&conn, "t1").unwrap();
        assert!(analysis.flags.is_empty());
        assert_eq!(analysis.total_waste_usd, 0.0);
    }
}
