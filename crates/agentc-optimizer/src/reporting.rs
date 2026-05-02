//! Read-only queries + rendering for `agentc optimize report | inspect`.
//!
//! This module is the operator-facing view over the two optimizer DBs:
//!
//! - `cost_model.db` — per-call-site Welford stats, rolling rule divergence,
//!   and the `optimizer_disabled` override table.
//! - `optimizer_audit.db` — ring-buffered history of every plan dispatched
//!   (last 10,000).
//!
//! The split between _query_ functions (hit SQLite, return plain structs) and
//! _render_ functions (pure string formatting over those structs) keeps the
//! rendering snapshot-testable without touching disk.

use std::collections::{BTreeMap, HashSet};
use std::fmt::Write;

use anyhow::{Context, Result};
use rusqlite::{params, Connection};

// =============================================================================
// Report: `agentc optimize report [--window-hours N]`
// =============================================================================

/// One row of the per-rule breakdown in `agentc optimize report`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct RuleBreakdown {
    pub rule: String,
    pub applied: u64,
    pub skipped: u64,
    pub savings_usd: f64,
    pub divergence_mean: Option<f64>,
    pub divergence_samples: u64,
}

/// Aggregate report over the last `window_us` of plan_audit rows.
///
/// - `calls_intercepted` counts every audit row in the window.
/// - `cold_calls` is the slice that ran through the optimizer before any rule
///   was eligible — we tag these by `plan_kind = "pass_through" AND rule IS
///   NULL AND projected_savings_usd IS NULL`. Hot pass-through (a rule was
///   evaluated but nothing safe fired) keeps a `rule` label so we can tell
///   them apart.
/// - Overhead is measured in microseconds in the audit table; we render the
///   average and P99 in milliseconds.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct OptimizerReport {
    pub window_hours: u64,
    pub calls_intercepted: u64,
    pub cold_calls: u64,
    pub hot_pass_through: u64,
    pub hot_optimized: u64,
    pub overhead_mean_ms: f64,
    pub overhead_p99_ms: f64,
    pub rules: Vec<RuleBreakdown>,
    pub total_savings_usd: f64,
    pub baseline_spend_usd: f64,
    pub accuracy_divergence: Option<f64>,
}

impl OptimizerReport {
    /// Percentage of baseline spend saved; 0.0 when the baseline is zero.
    pub fn savings_fraction(&self) -> f64 {
        if self.baseline_spend_usd > 0.0 {
            self.total_savings_usd / self.baseline_spend_usd
        } else {
            0.0
        }
    }
}

/// Build an [`OptimizerReport`] over the last `window_us` of audit history.
///
/// `now_us` is caller-supplied so tests can pin the time reference. The
/// `audit_conn` is `optimizer_audit.db`; the optional `cost_conn` is
/// `cost_model.db` — when absent, the rule divergence mean column stays
/// `None`.
pub fn build_report(
    audit_conn: &Connection,
    cost_conn: Option<&Connection>,
    now_us: i64,
    window_hours: u64,
) -> Result<OptimizerReport> {
    let window_us = (window_hours as i64).saturating_mul(3_600 * 1_000_000);
    let cutoff_us = now_us.saturating_sub(window_us);

    let mut stmt = audit_conn
        .prepare(
            "SELECT plan_kind, rule, projected_savings_usd, \
                    measured_savings_usd, overhead_us, shadow_sampled, \
                    shadow_divergence \
             FROM plan_audit \
             WHERE ts_us >= ?1",
        )
        .context("prepare plan_audit read")?;

    let rows = stmt
        .query_map(params![cutoff_us], |r| {
            Ok(AuditRow {
                plan_kind: r.get::<_, String>(0)?,
                rule: r.get::<_, Option<String>>(1)?,
                projected: r.get::<_, Option<f64>>(2)?,
                measured: r.get::<_, Option<f64>>(3)?,
                overhead_us: r.get::<_, i64>(4)?,
                shadow_sampled: r.get::<_, i64>(5)? != 0,
                shadow_divergence: r.get::<_, Option<f64>>(6)?,
            })
        })
        .context("execute plan_audit read")?;

    let mut overhead_us: Vec<i64> = Vec::new();
    let mut calls_intercepted = 0u64;
    let mut cold = 0u64;
    let mut hot_pass_through = 0u64;
    let mut hot_optimized = 0u64;
    let mut rules: BTreeMap<String, RuleBreakdown> = BTreeMap::new();
    let mut total_savings = 0.0f64;
    let mut divergence_sum = 0.0f64;
    let mut divergence_n = 0u64;

    for row in rows {
        let row = row.context("decode plan_audit row")?;
        calls_intercepted += 1;
        overhead_us.push(row.overhead_us);

        let has_rule = row.rule.is_some();
        match row.plan_kind.as_str() {
            "pass_through" if !has_rule => cold += 1,
            "pass_through" => hot_pass_through += 1,
            _ => hot_optimized += 1,
        }

        if let Some(ref rule) = row.rule {
            let entry = rules.entry(rule.clone()).or_insert_with(|| RuleBreakdown {
                rule: rule.clone(),
                ..RuleBreakdown::default()
            });
            if row.plan_kind == "pass_through" {
                entry.skipped += 1;
            } else {
                entry.applied += 1;
                // Measured savings replaces projected once the executor
                // reports back; projected is kept as a fallback.
                let saved = row.measured.or(row.projected).unwrap_or(0.0);
                entry.savings_usd += saved;
                total_savings += saved;
            }
        }

        if row.shadow_sampled {
            if let Some(div) = row.shadow_divergence {
                divergence_sum += div;
                divergence_n += 1;
            }
        }
    }

    // Attach rule divergence means from cost_model.db when available. The
    // per-rule divergence row is keyed by `(call_site_id, rule)`, so we
    // average across sites weighted by n_samples.
    if let Some(conn) = cost_conn {
        let mut div_stmt = conn
            .prepare("SELECT rule, n_samples, divergence_mean FROM rule_divergence")
            .context("prepare rule_divergence read")?;
        let it = div_stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, f64>(2)?,
                ))
            })
            .context("execute rule_divergence read")?;
        let mut weighted: BTreeMap<String, (f64, i64)> = BTreeMap::new();
        for row in it {
            let (rule, n, mean) = row?;
            let e = weighted.entry(rule).or_insert((0.0, 0));
            e.0 += mean * n as f64;
            e.1 += n;
        }
        for (rule, (weighted_sum, n)) in weighted {
            if n > 0 {
                if let Some(r) = rules.get_mut(&rule) {
                    r.divergence_mean = Some(weighted_sum / n as f64);
                    r.divergence_samples = n as u64;
                }
            }
        }
    }

    let mut rules: Vec<RuleBreakdown> = rules.into_values().collect();
    // Sort by applied desc, then name for stable output.
    rules.sort_by(|a, b| b.applied.cmp(&a.applied).then_with(|| a.rule.cmp(&b.rule)));

    let (overhead_mean_ms, overhead_p99_ms) = overhead_stats(&mut overhead_us);

    // Baseline spend = savings + observed cost. We don't yet record observed
    // cost in the audit table; approximate with the cost_model totals if the
    // cost DB is open.
    let baseline_spend_usd = match cost_conn {
        Some(conn) => compute_baseline_spend(conn, total_savings)?,
        None => total_savings, // no denominator ⇒ render N/A in the report
    };

    let accuracy_divergence = if divergence_n > 0 {
        Some(divergence_sum / divergence_n as f64)
    } else {
        None
    };

    Ok(OptimizerReport {
        window_hours,
        calls_intercepted,
        cold_calls: cold,
        hot_pass_through,
        hot_optimized,
        overhead_mean_ms,
        overhead_p99_ms,
        rules,
        total_savings_usd: total_savings,
        baseline_spend_usd,
        accuracy_divergence,
    })
}

/// Baseline = observed spend (from cost_model) + savings booked by rules.
/// The cost model holds mean cost per call; scaling by `n_observations`
/// recovers a total-spend estimate over the rolling window.
fn compute_baseline_spend(conn: &Connection, savings: f64) -> Result<f64> {
    let observed: f64 = conn
        .query_row(
            "SELECT COALESCE(SUM(cost_usd_mean * n_observations), 0.0) FROM call_site_profile",
            [],
            |r| r.get(0),
        )
        .context("sum observed cost")?;
    Ok(observed + savings)
}

struct AuditRow {
    plan_kind: String,
    rule: Option<String>,
    projected: Option<f64>,
    measured: Option<f64>,
    overhead_us: i64,
    shadow_sampled: bool,
    shadow_divergence: Option<f64>,
}

fn overhead_stats(overhead_us: &mut [i64]) -> (f64, f64) {
    if overhead_us.is_empty() {
        return (0.0, 0.0);
    }
    let sum: i128 = overhead_us.iter().map(|v| *v as i128).sum();
    let mean_ms = (sum as f64) / (overhead_us.len() as f64) / 1000.0;
    overhead_us.sort_unstable();
    // P99 index: ceil(0.99 * n) - 1, clamped to [0, n-1].
    let idx = ((overhead_us.len() as f64 * 0.99).ceil() as usize)
        .saturating_sub(1)
        .min(overhead_us.len() - 1);
    let p99_ms = (overhead_us[idx] as f64) / 1000.0;
    (mean_ms, p99_ms)
}

// =============================================================================
// Inspect: `agentc optimize inspect <call-site>`
// =============================================================================

/// Per-rule firing rate row.
#[derive(Debug, Clone, PartialEq)]
pub struct RuleFiringRate {
    pub rule: String,
    pub fire_rate: f64,
    pub note: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AccuracyStatus {
    pub divergence: Option<f64>,
    pub budget_remaining: Option<f64>,
    pub is_disabled: bool,
    pub reenable_at_us: Option<i64>,
}

/// Data assembled for `agentc optimize inspect <call-site>`.
#[derive(Debug, Clone, PartialEq)]
pub struct CallSiteInspect {
    pub call_site_id: String,
    pub total_invocations: u32,
    pub confidence: f32,
    pub baseline_cost_per_call_usd: f64,
    pub observed_cost_per_call_usd: f64,
    pub savings_fraction: f64,
    pub firing_rates: Vec<RuleFiringRate>,
    pub accuracy: AccuracyStatus,
}

pub fn build_inspect(
    cost_conn: &Connection,
    audit_conn: &Connection,
    call_site_id: &str,
    cost_model_window: u32,
    now_us: i64,
) -> Result<Option<CallSiteInspect>> {
    let profile = crate::cost_model::load_profile(cost_conn, call_site_id)?;
    let Some(profile) = profile else {
        return Ok(None);
    };

    // Rule firing rates over the full audit window for this site.
    let mut stmt = audit_conn
        .prepare(
            "SELECT rule, plan_kind, COUNT(*) FROM plan_audit \
             WHERE call_site_id = ?1 AND rule IS NOT NULL \
             GROUP BY rule, plan_kind",
        )
        .context("prepare firing-rate query")?;
    let rows = stmt
        .query_map(params![call_site_id], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, i64>(2)?,
            ))
        })
        .context("execute firing-rate query")?;

    // (rule → (applied, total))
    let mut counts: BTreeMap<String, (i64, i64)> = BTreeMap::new();
    for row in rows {
        let (rule, kind, n) = row?;
        let entry = counts.entry(rule).or_insert((0, 0));
        entry.1 += n;
        if kind != "pass_through" {
            entry.0 += n;
        }
    }
    let mut firing_rates: Vec<RuleFiringRate> = counts
        .into_iter()
        .map(|(rule, (applied, total))| {
            let rate = if total > 0 {
                applied as f64 / total as f64
            } else {
                0.0
            };
            RuleFiringRate {
                rule,
                fire_rate: rate,
                note: None,
            }
        })
        .collect();
    firing_rates.sort_by(|a, b| {
        b.fire_rate
            .partial_cmp(&a.fire_rate)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.rule.cmp(&b.rule))
    });

    // Shadow divergence: max across all rules for this site. That's the
    // tightest "budget remaining" visible to the operator; per-rule drill-
    // down renders below.
    let divergence = load_max_divergence(cost_conn, call_site_id)?;

    // Disable status: is any rule currently disabled on this site?
    let disabled = load_disabled_entry(cost_conn, call_site_id, now_us)?;

    // Observed vs baseline: observed is the cost_model mean (post-
    // optimization). Baseline is observed + sum of savings booked by
    // optimized plans for this site.
    let observed_cost = profile.cost_usd.mean;
    let booked_savings: f64 = audit_conn
        .query_row(
            "SELECT COALESCE(SUM(COALESCE(measured_savings_usd, projected_savings_usd, 0.0)), 0.0) \
             FROM plan_audit \
             WHERE call_site_id = ?1 AND plan_kind != 'pass_through'",
            params![call_site_id],
            |r| r.get(0),
        )
        .context("sum savings for site")?;
    let savings_per_call = if profile.n_observations > 0 {
        booked_savings / profile.n_observations as f64
    } else {
        0.0
    };
    let baseline_per_call = observed_cost + savings_per_call;
    let savings_fraction = if baseline_per_call > 0.0 {
        savings_per_call / baseline_per_call
    } else {
        0.0
    };

    let budget_remaining = divergence.map(|d| 0.01f64 - d); // CacheHit budget as a default display

    Ok(Some(CallSiteInspect {
        call_site_id: call_site_id.to_string(),
        total_invocations: profile.n_observations,
        confidence: profile.confidence(cost_model_window),
        baseline_cost_per_call_usd: baseline_per_call,
        observed_cost_per_call_usd: observed_cost,
        savings_fraction,
        firing_rates,
        accuracy: AccuracyStatus {
            divergence,
            budget_remaining,
            is_disabled: disabled.is_some(),
            reenable_at_us: disabled,
        },
    }))
}

fn load_max_divergence(conn: &Connection, call_site_id: &str) -> Result<Option<f64>> {
    let got: Option<f64> = conn
        .query_row(
            "SELECT MAX(divergence_mean) FROM rule_divergence WHERE call_site_id = ?1",
            params![call_site_id],
            |r| r.get(0),
        )
        .ok();
    Ok(got)
}

fn load_disabled_entry(conn: &Connection, call_site_id: &str, now_us: i64) -> Result<Option<i64>> {
    let got: Option<i64> = conn
        .query_row(
            "SELECT reenable_at FROM optimizer_disabled \
             WHERE call_site_id = ?1 AND reenable_at > ?2 \
             ORDER BY reenable_at DESC LIMIT 1",
            params![call_site_id, now_us],
            |r| r.get(0),
        )
        .ok();
    Ok(got)
}

// =============================================================================
// Disable: `agentc optimize disable --rule --call-site <glob>`
// =============================================================================

/// Number of rows affected by a disable command, plus the call sites matched.
#[derive(Debug, Clone, Default)]
pub struct DisableSummary {
    pub rule: String,
    pub matched_sites: Vec<String>,
}

/// Convert a GLOB-style pattern (`*` wildcard, `?` single-char) to a SQL LIKE
/// pattern, escaping `%` and `_` literals.
pub fn glob_to_sql_like(glob: &str) -> String {
    let mut out = String::with_capacity(glob.len() + 4);
    for ch in glob.chars() {
        match ch {
            '*' => out.push('%'),
            '?' => out.push('_'),
            '%' | '_' | '\\' => {
                out.push('\\');
                out.push(ch);
            }
            c => out.push(c),
        }
    }
    out
}

/// Insert an `optimizer_disabled` row for every known call site matching the
/// glob. Sites are drawn from `call_site_profile` so we only disable rules on
/// call sites the optimizer has actually observed.
///
/// `reason` is propagated to the row; the spec's default is
/// `"operator override"`.
pub fn disable_rule(
    cost_conn: &mut Connection,
    rule: &str,
    call_site_glob: &str,
    reason: &str,
    now_us: i64,
    reenable_at_us: i64,
) -> Result<DisableSummary> {
    let like = glob_to_sql_like(call_site_glob);
    let matched: Vec<String> = {
        let mut stmt = cost_conn
            .prepare(
                "SELECT call_site_id FROM call_site_profile \
                 WHERE call_site_id LIKE ?1 ESCAPE '\\' \
                 ORDER BY call_site_id",
            )
            .context("prepare match-sites")?;
        let rows = stmt
            .query_map(params![like], |r| r.get::<_, String>(0))
            .context("execute match-sites")?;
        rows.collect::<rusqlite::Result<Vec<_>>>()
            .context("collect matched sites")?
    };

    // Dedup (defensive; call_site_profile has PK uniqueness already).
    let mut seen: HashSet<String> = HashSet::new();
    let mut ordered: Vec<String> = Vec::new();
    for site in matched {
        if seen.insert(site.clone()) {
            ordered.push(site);
        }
    }

    // Operator-override fallback: when the glob is `*` and no call_site_profile
    // rows match (e.g. the cost model is empty because the optimizer has not
    // yet observed any sites — typical for ablation harnesses), write a single
    // wildcard row. `Budget::is_disabled` treats `call_site_id == "*"` as
    // matching every site for the given rule.
    if ordered.is_empty() && call_site_glob == "*" {
        ordered.push("*".to_string());
    }

    let tx = cost_conn.transaction().context("begin disable tx")?;
    for site in &ordered {
        tx.execute(
            "INSERT INTO optimizer_disabled \
                (call_site_id, rule, reason, disabled_at, reenable_at) \
             VALUES (?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT(call_site_id, rule) DO UPDATE SET \
                reason = excluded.reason, \
                disabled_at = excluded.disabled_at, \
                reenable_at = excluded.reenable_at",
            params![site, rule, reason, now_us, reenable_at_us],
        )
        .context("insert optimizer_disabled row")?;
    }
    tx.commit().context("commit disable tx")?;

    Ok(DisableSummary {
        rule: rule.to_string(),
        matched_sites: ordered,
    })
}

// =============================================================================
// Pure rendering helpers
// =============================================================================

fn format_usd(v: f64) -> String {
    format!("${v:.2}")
}

fn format_usd_4(v: f64) -> String {
    format!("${v:.4}")
}

fn format_pct_1(f: f64) -> String {
    format!("{:.1}%", f * 100.0)
}

fn format_pct_0(f: f64) -> String {
    format!("{}%", (f * 100.0).round() as i64)
}

fn format_u64(n: u64) -> String {
    let s = n.to_string();
    let bytes = s.as_bytes();
    let mut out = String::with_capacity(s.len() + s.len() / 3);
    let first = bytes.len() % 3;
    if first > 0 {
        out.push_str(std::str::from_utf8(&bytes[..first]).unwrap());
    }
    for chunk in bytes[first..].chunks(3) {
        if !out.is_empty() {
            out.push(',');
        }
        out.push_str(std::str::from_utf8(chunk).unwrap());
    }
    out
}

pub fn render_report(r: &OptimizerReport) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "Optimizer report (last {}h)", r.window_hours);
    let _ = writeln!(
        out,
        "─────────────────────────────────────────────────────────"
    );
    let _ = writeln!(
        out,
        "Calls intercepted:  {:>10}",
        format_u64(r.calls_intercepted)
    );
    let pct = |n: u64| -> String {
        if r.calls_intercepted == 0 {
            "—".to_string()
        } else {
            format_pct_1(n as f64 / r.calls_intercepted as f64)
        }
    };
    let _ = writeln!(
        out,
        "Cold (profiling):   {:>10}    ({})",
        format_u64(r.cold_calls),
        pct(r.cold_calls)
    );
    let _ = writeln!(
        out,
        "Hot, pass-through:  {:>10}    ({})    # no rule fired",
        format_u64(r.hot_pass_through),
        pct(r.hot_pass_through)
    );
    let _ = writeln!(
        out,
        "Hot, optimized:     {:>10}    ({})",
        format_u64(r.hot_optimized),
        pct(r.hot_optimized)
    );
    let _ = writeln!(
        out,
        "Overhead per call:  {:>8.1}ms    p99 {:.1}ms",
        r.overhead_mean_ms, r.overhead_p99_ms
    );
    let _ = writeln!(out);
    let _ = writeln!(
        out,
        "Rule firings:       applied   skipped   savings"
    );
    for rule in &r.rules {
        let _ = writeln!(
            out,
            "  {:<20}{:>7}   {:>7}   {:>7}",
            rule.rule,
            format_u64(rule.applied),
            format_u64(rule.skipped),
            format_usd(rule.savings_usd),
        );
    }
    if r.rules.is_empty() {
        let _ = writeln!(out, "  (no rule activity in window)");
    }
    let _ = writeln!(out);
    let _ = writeln!(
        out,
        "Savings ({}h):       {}  ({} of baseline spend)",
        r.window_hours,
        format_usd(r.total_savings_usd),
        format_pct_1(r.savings_fraction())
    );
    match r.accuracy_divergence {
        Some(d) => {
            let _ = writeln!(
                out,
                "Accuracy divergence: {}    (shadow-mode sample)",
                format_pct_1(d)
            );
        }
        None => {
            let _ = writeln!(
                out,
                "Accuracy divergence:  —       (no shadow samples)"
            );
        }
    }
    out
}

pub fn render_inspect(i: &CallSiteInspect) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "Call site: {}", i.call_site_id);
    let _ = writeln!(
        out,
        "  Total invocations:       {}",
        format_u64(i.total_invocations as u64)
    );
    let confidence_note = if i.confidence < 0.5 {
        "sparse sample"
    } else if i.confidence < 0.8 {
        "adequate sample size"
    } else {
        "well sampled"
    };
    let _ = writeln!(
        out,
        "  Cost model confidence:   {:.2}   ({})",
        i.confidence, confidence_note
    );
    let _ = writeln!(
        out,
        "  Baseline cost:           {} per call",
        format_usd_4(i.baseline_cost_per_call_usd)
    );
    let _ = writeln!(
        out,
        "  Observed cost:           {} per call",
        format_usd_4(i.observed_cost_per_call_usd)
    );
    let _ = writeln!(
        out,
        "  Savings:                 {}",
        format_pct_1(i.savings_fraction)
    );
    let _ = writeln!(out);
    let _ = writeln!(out, "  Rule firings:");
    if i.firing_rates.is_empty() {
        let _ = writeln!(out, "    (no rules evaluated yet)");
    } else {
        for f in &i.firing_rates {
            let note = f
                .note
                .as_deref()
                .map(|s| format!(" ({s})"))
                .unwrap_or_default();
            let _ = writeln!(
                out,
                "    {:<18}  fires {} of the time{}",
                f.rule,
                format_pct_0(f.fire_rate),
                note
            );
        }
    }
    let _ = writeln!(out);
    let _ = writeln!(out, "  Accuracy:");
    match i.accuracy.divergence {
        Some(d) => {
            let _ = writeln!(
                out,
                "    Shadow divergence     {}",
                format_pct_1(d)
            );
        }
        None => {
            let _ = writeln!(out, "    Shadow divergence     —");
        }
    }
    match i.accuracy.budget_remaining {
        Some(b) => {
            let _ = writeln!(
                out,
                "    Budget remaining      {}",
                format_pct_1(b.max(0.0))
            );
        }
        None => {
            let _ = writeln!(out, "    Budget remaining      —");
        }
    }
    let status = if i.accuracy.is_disabled {
        "disabled (operator override or budget breach)"
    } else {
        "healthy"
    };
    let _ = writeln!(out, "    Status                {}", status);
    out
}

pub fn render_disable_summary(d: &DisableSummary) -> String {
    match d.matched_sites.len() {
        0 => format!(
            "No matching call sites for '{}'. Nothing disabled.\n",
            d.rule
        ),
        n => format!(
            "Disabled {} on {} call site{} matching the pattern.\n",
            d.rule,
            n,
            if n == 1 { "" } else { "s" }
        ),
    }
}

// =============================================================================
// Tests
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audit::{insert_batch, PlanAudit, PlanKind};
    use crate::cost_model::{CostModel, CostModelUpdate};
    use crate::schema::{ensure_audit_schema, ensure_cost_model_schema};

    fn fresh_audit() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        ensure_audit_schema(&c).unwrap();
        c
    }

    fn fresh_cost() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&c).unwrap();
        c
    }

    fn audit(
        ts_us: i64,
        call_site: &str,
        kind: PlanKind,
        rule: Option<&str>,
        savings: Option<f64>,
        overhead_us: i64,
    ) -> PlanAudit {
        PlanAudit {
            ts_us,
            call_site_id: call_site.to_string(),
            span_id: [0u8; 8],
            plan_kind: kind,
            rule: rule.map(|s| s.to_string()),
            projected_savings_usd: savings,
            measured_savings_usd: savings,
            overhead_us,
            shadow_sampled: false,
            shadow_divergence: None,
        }
    }

    #[test]
    fn empty_window_produces_zero_report() {
        let a = fresh_audit();
        let r = build_report(&a, None, 100_000, 24).unwrap();
        assert_eq!(r.calls_intercepted, 0);
        assert_eq!(r.cold_calls, 0);
        assert_eq!(r.hot_pass_through, 0);
        assert_eq!(r.hot_optimized, 0);
        assert_eq!(r.total_savings_usd, 0.0);
        assert!(r.accuracy_divergence.is_none());
    }

    #[test]
    fn report_partitions_cold_hot_pass_through_optimized() {
        let mut a = fresh_audit();
        let rows = vec![
            // Cold: pass_through, no rule.
            audit(100, "site.a", PlanKind::PassThrough, None, None, 400),
            audit(200, "site.a", PlanKind::PassThrough, None, None, 500),
            // Hot pass-through: pass_through WITH rule label.
            audit(300, "site.a", PlanKind::PassThrough, Some("CacheHit"), None, 600),
            // Hot optimized.
            audit(400, "site.a", PlanKind::Cached, Some("CacheHit"), Some(0.02), 700),
            audit(500, "site.a", PlanKind::Rewritten, Some("ModelDowngrade"), Some(0.01), 800),
        ];
        insert_batch(&mut a, &rows).unwrap();
        let r = build_report(&a, None, 10_000, 24).unwrap();
        assert_eq!(r.calls_intercepted, 5);
        assert_eq!(r.cold_calls, 2);
        assert_eq!(r.hot_pass_through, 1);
        assert_eq!(r.hot_optimized, 2);
        // Per-rule.
        let ch = r.rules.iter().find(|x| x.rule == "CacheHit").unwrap();
        assert_eq!(ch.applied, 1);
        assert_eq!(ch.skipped, 1);
        assert!((ch.savings_usd - 0.02).abs() < 1e-9);
        let md = r
            .rules
            .iter()
            .find(|x| x.rule == "ModelDowngrade")
            .unwrap();
        assert_eq!(md.applied, 1);
        assert_eq!(md.skipped, 0);
    }

    #[test]
    fn report_window_filters_out_old_rows() {
        let mut a = fresh_audit();
        let old = audit(100, "site.a", PlanKind::Cached, Some("CacheHit"), Some(1.0), 0);
        let recent = audit(
            10_000,
            "site.a",
            PlanKind::Cached,
            Some("CacheHit"),
            Some(2.0),
            0,
        );
        insert_batch(&mut a, &[old, recent]).unwrap();
        // now_us = 20_000; window = 24h → cutoff far in the past (0 wins).
        // Shrink window to 0h to test filtering:
        let r = build_report(&a, None, 10_001, 0).unwrap();
        // cutoff = 10_001; only rows with ts_us >= 10_001 survive — none here.
        assert_eq!(r.calls_intercepted, 0);

        let r = build_report(&a, None, 10_000, 0).unwrap();
        assert_eq!(r.calls_intercepted, 1);
    }

    #[test]
    fn overhead_p99_ranks_correctly() {
        let mut a = fresh_audit();
        let mut rows: Vec<PlanAudit> = (0i64..100)
            .map(|i| audit(i, "site", PlanKind::PassThrough, None, None, (i + 1) * 10))
            .collect();
        // p99 index = ceil(0.99*100) - 1 = 99 - 1 = 98 → value 990 µs.
        // Append a giant outlier; it becomes the new p99.
        rows.push(audit(101, "site", PlanKind::PassThrough, None, None, 1_000_000));
        insert_batch(&mut a, &rows).unwrap();
        let r = build_report(&a, None, 1_000_000, 24).unwrap();
        // 101 rows sorted: [10, 20, ..., 1000, 1_000_000]. p99 idx =
        // ceil(0.99 * 101) - 1 = 99 → overhead_us[99] = 1000 → 1.0 ms.
        assert!((r.overhead_p99_ms - 1.0).abs() < 1e-9, "got {}", r.overhead_p99_ms);
    }

    #[test]
    fn savings_fraction_is_zero_when_no_baseline() {
        let r = OptimizerReport {
            total_savings_usd: 10.0,
            baseline_spend_usd: 0.0,
            ..OptimizerReport::default()
        };
        assert_eq!(r.savings_fraction(), 0.0);
    }

    #[test]
    fn render_report_renders_empty_window() {
        let r = OptimizerReport {
            window_hours: 24,
            ..OptimizerReport::default()
        };
        let s = render_report(&r);
        assert!(s.contains("Optimizer report (last 24h)"));
        assert!(s.contains("(no rule activity in window)"));
        assert!(s.contains("Accuracy divergence:  —"));
    }

    #[test]
    fn render_report_includes_rule_rows() {
        let r = OptimizerReport {
            window_hours: 24,
            calls_intercepted: 100,
            cold_calls: 10,
            hot_pass_through: 20,
            hot_optimized: 70,
            overhead_mean_ms: 0.4,
            overhead_p99_ms: 1.2,
            rules: vec![RuleBreakdown {
                rule: "CacheHit".to_string(),
                applied: 50,
                skipped: 20,
                savings_usd: 12.34,
                ..RuleBreakdown::default()
            }],
            total_savings_usd: 12.34,
            baseline_spend_usd: 50.00,
            accuracy_divergence: Some(0.004),
        };
        let s = render_report(&r);
        assert!(s.contains("CacheHit"));
        assert!(s.contains("$12.34"));
        assert!(s.contains("0.4%"), "got {s}"); // divergence 0.4%
    }

    #[test]
    fn inspect_returns_none_when_site_absent() {
        let c = fresh_cost();
        let a = fresh_audit();
        assert!(build_inspect(&c, &a, "missing", 50, 0).unwrap().is_none());
    }

    #[test]
    fn inspect_aggregates_firing_rates_and_savings() {
        let c = fresh_cost();
        let mut a = fresh_audit();

        // Seed cost model.
        let cm = CostModel::new();
        for _ in 0..10 {
            cm.observe(CostModelUpdate {
                call_site_id: "app.planner".to_string(),
                input_tokens: 100,
                output_tokens: 50,
                latency_ms: 200.0,
                cost_usd: 0.01,
                output_is_structured: false,
                output_is_short: true,
                now_us: Some(0),
            });
        }
        let mut c_mut = c;
        cm.flush_dirty(&mut c_mut).unwrap();
        let c = c_mut;

        // Audit: CacheHit fires 3/5 times, ModelDowngrade 0/2 times.
        let mut rows = Vec::new();
        for _ in 0..3 {
            rows.push(audit(
                1,
                "app.planner",
                PlanKind::Cached,
                Some("CacheHit"),
                Some(0.005),
                100,
            ));
        }
        for _ in 0..2 {
            rows.push(audit(
                1,
                "app.planner",
                PlanKind::PassThrough,
                Some("CacheHit"),
                None,
                100,
            ));
        }
        for _ in 0..2 {
            rows.push(audit(
                1,
                "app.planner",
                PlanKind::PassThrough,
                Some("ModelDowngrade"),
                None,
                100,
            ));
        }
        insert_batch(&mut a, &rows).unwrap();

        let i = build_inspect(&c, &a, "app.planner", 20, 10_000).unwrap().unwrap();
        assert_eq!(i.call_site_id, "app.planner");
        assert_eq!(i.total_invocations, 10);
        let ch = i.firing_rates.iter().find(|r| r.rule == "CacheHit").unwrap();
        assert!((ch.fire_rate - 0.6).abs() < 1e-9);
        let md = i
            .firing_rates
            .iter()
            .find(|r| r.rule == "ModelDowngrade")
            .unwrap();
        assert_eq!(md.fire_rate, 0.0);
        // savings_fraction: 3 * 0.005 = 0.015 total; per-call 0.0015; observed 0.01 → baseline 0.0115 → 13%
        assert!(i.savings_fraction > 0.0);
    }

    #[test]
    fn glob_maps_star_and_question_to_sql_wildcards() {
        assert_eq!(glob_to_sql_like("app.*"), "app.%");
        assert_eq!(glob_to_sql_like("a?c"), "a_c");
        assert_eq!(glob_to_sql_like("literal%name"), r"literal\%name");
        assert_eq!(glob_to_sql_like("dir\\file"), r"dir\\file");
    }

    #[test]
    fn disable_inserts_row_for_every_matched_site() {
        let c = fresh_cost();
        let mut c = c;
        // Seed two sites.
        let cm = CostModel::new();
        for site in ["app.planner", "app.router", "other.misc"] {
            cm.observe(CostModelUpdate {
                call_site_id: site.to_string(),
                input_tokens: 1,
                output_tokens: 1,
                latency_ms: 1.0,
                cost_usd: 0.01,
                output_is_structured: false,
                output_is_short: true,
                now_us: Some(0),
            });
        }
        cm.flush_dirty(&mut c).unwrap();

        let summary = disable_rule(
            &mut c,
            "ModelDowngrade",
            "app.*",
            "operator override",
            0,
            1_000,
        )
        .unwrap();
        assert_eq!(summary.matched_sites, vec!["app.planner", "app.router"]);
        let count: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM optimizer_disabled WHERE rule = 'ModelDowngrade'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 2);
    }

    /// Spec exit-criterion: the `report` snapshot hits every section in the
    /// mockup. We pin the rendered text so drift from the spec is caught.
    #[test]
    fn render_report_snapshot_matches_spec_shape() {
        let r = OptimizerReport {
            window_hours: 24,
            calls_intercepted: 18_402,
            cold_calls: 2_104,
            hot_pass_through: 3_291,
            hot_optimized: 13_007,
            overhead_mean_ms: 0.4,
            overhead_p99_ms: 1.2,
            rules: vec![
                RuleBreakdown {
                    rule: "CacheHit".to_string(),
                    applied: 5_211,
                    skipped: 7_796,
                    savings_usd: 62.19,
                    ..RuleBreakdown::default()
                },
                RuleBreakdown {
                    rule: "ModelDowngrade".to_string(),
                    applied: 3_018,
                    skipped: 1_482,
                    savings_usd: 41.07,
                    ..RuleBreakdown::default()
                },
            ],
            total_savings_usd: 103.26,
            baseline_spend_usd: 420.0,
            accuracy_divergence: Some(0.004),
        };
        let s = render_report(&r);
        let checks: &[&str] = &[
            "Optimizer report (last 24h)",
            "Calls intercepted:      18,402",
            "Cold (profiling):        2,104    (11.4%)",
            "Hot, pass-through:       3,291    (17.9%)",
            "Hot, optimized:         13,007    (70.7%)",
            "Overhead per call:       0.4ms    p99 1.2ms",
            "  CacheHit              5,211     7,796    $62.19",
            "  ModelDowngrade        3,018     1,482    $41.07",
            "Savings (24h):       $103.26  (24.6% of baseline spend)",
            "Accuracy divergence: 0.4%",
        ];
        for line in checks {
            assert!(s.contains(line), "missing {line:?} in:\n{s}");
        }
        assert!(s.starts_with("Optimizer report (last 24h)\n"), "{s}");
    }

    #[test]
    fn render_inspect_snapshot_matches_spec_shape() {
        let i = CallSiteInspect {
            call_site_id: "app.agents.planner:plan_next_step".to_string(),
            total_invocations: 1_847,
            confidence: 0.92,
            baseline_cost_per_call_usd: 0.0241,
            observed_cost_per_call_usd: 0.0097,
            savings_fraction: 0.598,
            firing_rates: vec![
                RuleFiringRate {
                    rule: "CacheHit".to_string(),
                    fire_rate: 0.58,
                    note: None,
                },
                RuleFiringRate {
                    rule: "ModelDowngrade".to_string(),
                    fire_rate: 0.31,
                    note: Some("to gpt-4o-mini".to_string()),
                },
            ],
            accuracy: AccuracyStatus {
                divergence: Some(0.003),
                budget_remaining: Some(0.007),
                is_disabled: false,
                reenable_at_us: None,
            },
        };
        let s = render_inspect(&i);
        assert!(s.starts_with("Call site: app.agents.planner:plan_next_step\n"), "{s}");
        assert!(s.contains("Total invocations:       1,847"), "{s}");
        assert!(s.contains("Cost model confidence:   0.92   (well sampled)"), "{s}");
        assert!(s.contains("Baseline cost:           $0.0241 per call"), "{s}");
        assert!(s.contains("Observed cost:           $0.0097 per call"), "{s}");
        assert!(s.contains("Savings:                 59.8%"), "{s}");
        assert!(s.contains("CacheHit            fires 58% of the time"), "{s}");
        assert!(
            s.contains("ModelDowngrade      fires 31% of the time (to gpt-4o-mini)"),
            "{s}"
        );
        assert!(s.contains("Shadow divergence     0.3%"), "{s}");
        assert!(s.contains("Budget remaining      0.7%"), "{s}");
        assert!(s.contains("Status                healthy"), "{s}");
    }

    #[test]
    fn render_inspect_marks_disabled_status() {
        let i = CallSiteInspect {
            call_site_id: "x".to_string(),
            total_invocations: 10,
            confidence: 0.1,
            baseline_cost_per_call_usd: 0.01,
            observed_cost_per_call_usd: 0.01,
            savings_fraction: 0.0,
            firing_rates: vec![],
            accuracy: AccuracyStatus {
                divergence: Some(0.05),
                budget_remaining: Some(-0.04),
                is_disabled: true,
                reenable_at_us: Some(999),
            },
        };
        let s = render_inspect(&i);
        assert!(s.contains("disabled"), "{s}");
        // Budget remaining rendered as 0.0% when clamped.
        assert!(s.contains("Budget remaining      0.0%"), "{s}");
    }

    #[test]
    fn render_disable_summary_reports_count() {
        let d = DisableSummary {
            rule: "ModelDowngrade".to_string(),
            matched_sites: vec!["a".to_string(), "b".to_string()],
        };
        assert_eq!(
            render_disable_summary(&d),
            "Disabled ModelDowngrade on 2 call sites matching the pattern.\n"
        );
        let d0 = DisableSummary {
            rule: "X".to_string(),
            matched_sites: vec![],
        };
        assert!(render_disable_summary(&d0).contains("Nothing disabled"));
        let d1 = DisableSummary {
            rule: "X".to_string(),
            matched_sites: vec!["only".to_string()],
        };
        assert!(render_disable_summary(&d1).contains("1 call site "));
    }

    #[test]
    fn disable_refreshes_existing_rows() {
        let c = fresh_cost();
        let mut c = c;
        let cm = CostModel::new();
        cm.observe(CostModelUpdate {
            call_site_id: "app.x".to_string(),
            input_tokens: 1,
            output_tokens: 1,
            latency_ms: 1.0,
            cost_usd: 0.01,
            output_is_structured: false,
            output_is_short: true,
            now_us: Some(0),
        });
        cm.flush_dirty(&mut c).unwrap();

        disable_rule(&mut c, "StateDrop", "app.x", "v1", 100, 200).unwrap();
        disable_rule(&mut c, "StateDrop", "app.x", "v2", 300, 400).unwrap();
        let (reason, disabled_at, reenable_at): (String, i64, i64) = c
            .query_row(
                "SELECT reason, disabled_at, reenable_at FROM optimizer_disabled \
                 WHERE call_site_id = 'app.x' AND rule = 'StateDrop'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .unwrap();
        assert_eq!(reason, "v2");
        assert_eq!(disabled_at, 300);
        assert_eq!(reenable_at, 400);
    }
}
