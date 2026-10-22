//! Scoped published-token tariffs, kept separate from reported provider bills.
//! A declaration precedes dispatch; immutable original SDK usage resolves it.

use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::provider_receipts::{Completion, Invocation, Role, Status};
use crate::provider_usage::Usage;

pub const VERSION: &str = "provider-token-tariff-v1";
pub const QUANTITY: &str = "published_token_tariff_usd";

/// These are dated public tariff observations, not account/contract claims.
/// Rates are integer nanodollars per token: no floating-point price rounding.
pub fn snapshot() -> serde_json::Value {
    serde_json::json!({
        "version":VERSION,"quantity":QUANTITY,"observed_date":"2026-10-12",
        "scope":"direct-global-text-client-tools-v1",
        "sources":[
            {"url":"https://developers.openai.com/api/docs/pricing.md",
             "body_sha256":"e0767b5009dcc929a2f9eeb9ce6096a57ff12e5d8c011de9d69ff145aef2fc28"},
            {"url":"https://platform.claude.com/docs/en/about-claude/pricing",
             "body_sha256":null}
        ],
        "nanodollars_per_token":{
            "gpt-4o":{"default":[2500,1250,10000],"priority":[4250,2125,17000]},
            "gpt-4o-mini":{"default":[150,75,600],"priority":[250,125,1000]},
            "claude-sonnet-4-5":{"standard":[3000,300,3750,6000,15000]},
            "claude-haiku-4-5":{"standard":[1000,100,1250,2000,5000]}
        }
    })
}

pub fn snapshot_sha256() -> String {
    agentc_core::storage::content_hash(&agentc_core::storage::canonical_json(&snapshot()))
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Declaration {
    pub version: String,
    pub snapshot_sha256: String,
    pub endpoint_sha256: Option<String>,
    pub request_scope_supported: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Interval {
    pub lower_nanodollars: u64,
    pub upper_nanodollars: u64,
}

impl Interval {
    pub fn checked_add(self, other: Self) -> Result<Self> {
        if self.lower_nanodollars > self.upper_nanodollars
            || other.lower_nanodollars > other.upper_nanodollars
        {
            bail!("tariff interval is invalid");
        }
        Ok(Self {
            lower_nanodollars: self
                .lower_nanodollars
                .checked_add(other.lower_nanodollars)
                .context("tariff interval overflow")?,
            upper_nanodollars: self
                .upper_nanodollars
                .checked_add(other.upper_nanodollars)
                .context("tariff interval overflow")?,
        })
    }
}

/// Numeric savings certificate only; this supplies no quality/admission token.
/// Every operand must describe the same quantity and pinned tariff snapshot.
pub fn savings_lower_bound(
    reference: &Bound,
    candidate: &Bound,
    acquisition: &Bound,
    remaining: u64,
) -> Result<i128> {
    for value in [reference, candidate, acquisition] {
        if value.quantity != QUANTITY
            || value.snapshot_sha256 != reference.snapshot_sha256
            || value.snapshot_sha256 != snapshot_sha256()
            || value.interval.lower_nanodollars > value.interval.upper_nanodollars
        {
            bail!("tariff savings quantities or snapshots are incompatible");
        }
    }
    if remaining == 0 {
        bail!("tariff amortization horizon is empty");
    }
    let spend = acquisition.interval.upper_nanodollars;
    let amortized = spend.div_ceil(remaining);
    Ok(i128::from(reference.interval.lower_nanodollars)
        - i128::from(candidate.interval.upper_nanodollars)
        - i128::from(amortized))
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Bound {
    pub quantity: String,
    pub snapshot_sha256: String,
    pub interval: Interval,
}

fn endpoint(protocol: &str) -> &'static str {
    if protocol == "openai.chat.completions.v1" {
        "https://api.openai.com/v1"
    } else {
        "https://api.anthropic.com"
    }
}

fn scope_hash(protocol: &str) -> String {
    agentc_core::storage::content_hash(&format!("billing-scope-v1\0{}", endpoint(protocol)))
}

/// Return an abstention reason instead of guessing any missing pricing dimension.
pub fn quote(invocation: &Invocation, completion: &Completion) -> Result<Bound, &'static str> {
    let declaration = invocation
        .tariff_declaration
        .as_ref()
        .ok_or("undeclared_tariff")?;
    if declaration.version != VERSION || declaration.snapshot_sha256 != snapshot_sha256() {
        return Err("unsupported_tariff_snapshot");
    }
    if !declaration.request_scope_supported {
        return Err("unsupported_request_scope");
    }
    let expected = scope_hash(&invocation.provider_protocol);
    if declaration.endpoint_sha256.as_ref() != Some(&expected)
        || completion.billing_scope_sha256.as_ref() != Some(&expected)
    {
        return Err("unsupported_endpoint");
    }
    if !invocation.transport_verified
        || invocation.transport_sha256.is_none()
        || invocation.request_sha256.is_none()
        || completion.http_attempts != Some(1)
    {
        return Err("unverified_dispatch");
    }
    if completion.status != Status::Completed {
        return Err("incomplete_response");
    }
    let projected = completion.billing_usage.as_ref().ok_or("missing_usage")?;
    projected
        .validate(
            &invocation.provider_protocol,
            completion.input_tokens,
            completion.output_tokens,
        )
        .map_err(|_| "invalid_usage")?;
    let model = completion
        .model_version
        .as_deref()
        .ok_or("missing_model_revision")?;
    let request = invocation.requested_model_id.as_str();
    let interval = match &projected.usage {
        Usage::Openai {
            input_tokens,
            output_tokens,
            input_details,
            output_details,
            service_tier,
            unrecognized_fields,
            ..
        } => {
            if *unrecognized_fields {
                return Err("unrecognized_usage_fields");
            }
            if input_details
                .as_ref()
                .is_some_and(|d| d.audio_tokens.unwrap_or(0) > 0 || d.image_tokens.unwrap_or(0) > 0)
                || output_details
                    .as_ref()
                    .is_some_and(|d| d.audio_tokens.unwrap_or(0) > 0)
            {
                return Err("unsupported_modality");
            }
            let family = match model {
                "gpt-4o-2024-08-06" | "gpt-4o-2024-11-20"
                    if request == "gpt-4o" || request == model =>
                {
                    "gpt-4o"
                }
                "gpt-4o-mini-2024-07-18" if request == "gpt-4o-mini" || request == model => {
                    "gpt-4o-mini"
                }
                _ => return Err("unsupported_model_revision"),
            };
            let tier = match service_tier.as_deref() {
                Some("default") => "default",
                Some("priority" | "fast") => "priority",
                _ => return Err("unsupported_service_tier"),
            };
            let rates = snapshot()["nanodollars_per_token"][family][tier]
                .as_array()
                .unwrap()
                .clone();
            let input = rates[0].as_u64().unwrap();
            let cached = rates[1].as_u64().unwrap();
            let output = rates[2].as_u64().unwrap();
            let base = u64::from(*output_tokens) * output;
            if let Some(cache) = input_details.as_ref().and_then(|d| d.cached_tokens) {
                let exact =
                    base + u64::from(*input_tokens - cache) * input + u64::from(cache) * cached;
                Interval {
                    lower_nanodollars: exact,
                    upper_nanodollars: exact,
                }
            } else {
                Interval {
                    lower_nanodollars: base + u64::from(*input_tokens) * cached,
                    upper_nanodollars: base + u64::from(*input_tokens) * input,
                }
            }
        }
        Usage::Anthropic {
            input_tokens,
            output_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
            cache_creation,
            server_tool_use,
            service_tier,
            inference_geo,
            unrecognized_fields,
        } => {
            if *unrecognized_fields {
                return Err("unrecognized_usage_fields");
            }
            if service_tier.as_deref() != Some("standard") {
                return Err("unsupported_service_tier");
            }
            if inference_geo.as_deref().is_some_and(|geo| geo != "global") {
                return Err("unsupported_geography");
            }
            if server_tool_use.as_ref().is_some_and(|tools| {
                tools.web_search_requests.unwrap_or(0) > 0
                    || tools.web_fetch_requests.unwrap_or(0) > 0
            }) {
                return Err("unsupported_server_tools");
            }
            let family = match model {
                "claude-sonnet-4-5-20250929"
                    if request == "claude-sonnet-4-5" || request == model =>
                {
                    "claude-sonnet-4-5"
                }
                "claude-haiku-4-5-20251001"
                    if request == "claude-haiku-4-5" || request == model =>
                {
                    "claude-haiku-4-5"
                }
                _ => return Err("unsupported_model_revision"),
            };
            let rates = snapshot()["nanodollars_per_token"][family]["standard"]
                .as_array()
                .unwrap()
                .clone();
            let rates: Vec<u64> = rates.iter().map(|value| value.as_u64().unwrap()).collect();
            let writes = cache_creation_input_tokens.ok_or("missing_cache_counters")?;
            let reads = cache_read_input_tokens.ok_or("missing_cache_counters")?;
            let base = u64::from(*input_tokens) * rates[0]
                + u64::from(reads) * rates[1]
                + u64::from(*output_tokens) * rates[4];
            let five = cache_creation
                .as_ref()
                .and_then(|c| c.ephemeral_5m_input_tokens);
            let hour = cache_creation
                .as_ref()
                .and_then(|c| c.ephemeral_1h_input_tokens);
            let low_five = five.unwrap_or(writes - hour.unwrap_or(0));
            let high_hour = hour.unwrap_or(writes - five.unwrap_or(0));
            Interval {
                lower_nanodollars: base
                    + u64::from(low_five) * rates[2]
                    + u64::from(writes - low_five) * rates[3],
                upper_nanodollars: base
                    + u64::from(writes - high_hour) * rates[2]
                    + u64::from(high_hour) * rates[3],
            }
        }
    };
    Ok(Bound {
        quantity: QUANTITY.into(),
        snapshot_sha256: declaration.snapshot_sha256.clone(),
        interval,
    })
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch("CREATE TABLE IF NOT EXISTS provider_tariff_policy (
        snapshot_sha256 TEXT PRIMARY KEY,snapshot_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS provider_tariff_invocation (
        invocation_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,owner TEXT NOT NULL,
        phase TEXT NOT NULL,charged INTEGER NOT NULL,terminal INTEGER NOT NULL,
        lower_nanodollars INTEGER NOT NULL,upper_nanodollars INTEGER NOT NULL,
        unknown_cost INTEGER NOT NULL,result_json TEXT,
        FOREIGN KEY(invocation_id) REFERENCES provider_receipt_invocation(invocation_id));
        CREATE INDEX IF NOT EXISTS provider_tariff_batch ON provider_tariff_invocation(batch_id);
        CREATE TABLE IF NOT EXISTS provider_tariff_batch_status (
        batch_id TEXT PRIMARY KEY,persistence_failed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS provider_tariff_cohort (
        owner TEXT PRIMARY KEY,owned_invocations INTEGER NOT NULL DEFAULT 0,
        charged_invocations INTEGER NOT NULL DEFAULT 0,unknown_invocations INTEGER NOT NULL DEFAULT 0,
        lower_nanodollars INTEGER NOT NULL DEFAULT 0,upper_nanodollars INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS provider_tariff_coverage (
        batch_id TEXT PRIMARY KEY,owner TEXT NOT NULL,complete INTEGER NOT NULL,
        pricing_compatible INTEGER NOT NULL CHECK(pricing_compatible IN (0,1)),
        FOREIGN KEY(batch_id) REFERENCES provider_receipt_batch(batch_id));
        CREATE TABLE IF NOT EXISTS provider_tariff_coverage_cohort (
        owner TEXT PRIMARY KEY,batches INTEGER NOT NULL DEFAULT 0,
        incomplete_batches INTEGER NOT NULL DEFAULT 0,
        snapshot_sha256 TEXT NOT NULL,
        incompatible_batches INTEGER NOT NULL DEFAULT 0
          CHECK(typeof(incompatible_batches)='integer' AND incompatible_batches BETWEEN 0 AND batches),
        CHECK(typeof(batches)='integer' AND typeof(incomplete_batches)='integer'
          AND batches>=0 AND incomplete_batches>=0 AND incomplete_batches<=batches));")?;
    let qualified: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('provider_tariff_coverage') WHERE name='pricing_compatible')",
        [], |r| r.get(0),
    )?;
    if !qualified {
        // A previously materialized structural summary proves no snapshot
        // compatibility. Upgrade atomically; only explicit recovery can qualify it.
        let result = conn.execute_batch(
            "SAVEPOINT tariff_coverage_upgrade;
             ALTER TABLE provider_tariff_coverage ADD COLUMN pricing_compatible INTEGER NOT NULL DEFAULT 0 CHECK(pricing_compatible IN (0,1));
             ALTER TABLE provider_tariff_coverage_cohort ADD COLUMN incompatible_batches INTEGER NOT NULL DEFAULT 0 CHECK(typeof(incompatible_batches)='integer' AND incompatible_batches BETWEEN 0 AND batches);
             UPDATE provider_tariff_coverage_cohort SET incompatible_batches=batches;
             RELEASE tariff_coverage_upgrade;",
        );
        if let Err(error) = result {
            conn.execute_batch(
                "ROLLBACK TO tariff_coverage_upgrade; RELEASE tariff_coverage_upgrade;",
            )?;
            return Err(error.into());
        }
    }
    Ok(())
}

/// A materialized lifetime interval, without inspecting physical history.
/// Old stores stay held until explicit recovery reconstructs their coverage.
/// This accounting result carries no quality evidence or admission permission.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Acquisition {
    pub quantity: String,
    pub snapshot_sha256: String,
    pub owned_batches: u64,
    pub coverage_batches: u64,
    pub coverage_complete: bool,
    pub pricing_complete: bool,
    pub owned_invocations: u64,
    pub charged_invocations: u64,
    pub unknown_invocations: u64,
    pub lower_nanodollars: u64,
    pub upper_nanodollars: Option<u64>,
    pub observed_charged_lower_nanodollars: u64,
    pub bounded_observed_upper_nanodollars: u64,
}

const COHORT_SQL: &str = "SELECT owned_invocations,charged_invocations,unknown_invocations,lower_nanodollars,upper_nanodollars FROM provider_tariff_cohort WHERE owner=?1";
const COVERAGE_SQL: &str =
    "SELECT batches,incomplete_batches,snapshot_sha256,incompatible_batches FROM provider_tariff_coverage_cohort WHERE owner=?1";

pub fn summarize_acquisition(
    conn: &Connection,
    owner: &crate::plan_profile::CallSiteVersion,
) -> Result<Acquisition> {
    ensure_schema(conn)?;
    if conn.is_autocommit() {
        let transaction = conn.unchecked_transaction()?;
        let summary = acquisition_in_transaction(&transaction, owner)?;
        transaction.commit()?;
        return Ok(summary);
    }
    acquisition_in_transaction(conn, owner)
}

fn acquisition_in_transaction(
    conn: &Connection,
    owner: &crate::plan_profile::CallSiteVersion,
) -> Result<Acquisition> {
    let economics = crate::provider_economics::summarize_cohort(conn, owner)?;
    let cohort: Option<(u64, u64, u64, u64, u64)> = conn
        .query_row(COHORT_SQL, [owner.as_str()], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
        })
        .optional()?;
    let (owned, charged, unknown, lower, upper) = cohort.unwrap_or_default();
    let coverage: Option<(u64, u64, String, u64)> = conn
        .query_row(COVERAGE_SQL, [owner.as_str()], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
        })
        .optional()?;
    let (batches, incomplete, snapshot, incompatible) = coverage.unwrap_or_default();
    let pricing_complete = economics.owned_batches > 0
        && snapshot == snapshot_sha256()
        && economics.owned_batches == batches
        && incompatible == 0;
    let complete = pricing_complete
        && economics.incomplete_batches == 0
        && economics.uncovered_profile_executions == 0
        && economics.pending_billing_verifications == 0
        && incomplete == 0;
    Ok(Acquisition {
        quantity: QUANTITY.into(),
        snapshot_sha256: snapshot_sha256(),
        owned_batches: economics.owned_batches,
        coverage_batches: batches,
        coverage_complete: complete,
        pricing_complete,
        owned_invocations: owned,
        charged_invocations: charged,
        unknown_invocations: unknown,
        lower_nanodollars: if pricing_complete { lower } else { 0 },
        upper_nanodollars: (complete && unknown == 0).then_some(upper),
        observed_charged_lower_nanodollars: lower,
        bounded_observed_upper_nanodollars: upper,
    })
}

/// Refresh the changed batch inside its original transaction. Only enrollment,
/// new physical invocations and sealing can change structural coverage.
/// Explicit recovery can also reconstruct coverage for preexisting stores.
pub(crate) fn refresh_batch_coverage(conn: &Connection, batch: &str) -> Result<()> {
    refresh_coverage(conn, batch, false)
}

fn refresh_coverage(conn: &Connection, batch: &str, recovering: bool) -> Result<()> {
    let owner: Option<String> = conn
        .query_row(
            "SELECT owner_call_site_version FROM provider_economic_batch WHERE batch_id=?1",
            [batch],
            |r| r.get(0),
        )
        .optional()?;
    let Some(owner) = owner else { return Ok(()) };
    let old: Option<(String, bool, bool)> = conn.query_row(
        "SELECT owner,complete,pricing_compatible FROM provider_tariff_coverage WHERE batch_id=?1",
        [batch], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    ).optional()?;
    if old
        .as_ref()
        .is_some_and(|(previous, _, _)| previous != &owner)
    {
        bail!("tariff coverage owner conflicts");
    }
    // New terminal projections can only quote the registered current snapshot.
    // Historical projections need a one-time check, or explicit recovery.
    let compatible = match &old {
        Some((_, _, pricing)) if !recovering => *pricing,
        _ => !conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM provider_tariff_invocation WHERE batch_id=?1 AND unknown_cost=0 AND (COALESCE(json_extract(result_json,'$.bound.snapshot_sha256'),'')<>?2 OR COALESCE(json_extract(result_json,'$.bound.quantity'),'')<>?3))",
            params![batch, snapshot_sha256(), QUANTITY], |r| r.get::<_, bool>(0),
        )?,
    };
    let (sealed, expected): (bool, Option<String>) = conn.query_row(
        "SELECT sealed,expected_calls_json FROM provider_receipt_batch WHERE batch_id=?1",
        [batch],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let complete = if !sealed || !compatible {
        false
    } else {
        let missing: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM provider_receipt_invocation r LEFT JOIN provider_tariff_invocation t USING(invocation_id) WHERE r.batch_id=?1 AND (t.invocation_id IS NULL OR t.owner<>?2 OR t.batch_id<>?1 OR (t.unknown_cost=0 AND (COALESCE(json_extract(t.result_json,'$.bound.snapshot_sha256'),'')<>?3 OR COALESCE(json_extract(t.result_json,'$.bound.quantity'),'')<>?4))))",
            params![batch, owner, snapshot_sha256(), QUANTITY], |r| r.get(0),
        )?;
        // A native-owned, sealed empty journal is a known no-dispatch skip.
        // Native foreground/observer completeness remains a separate hold.
        let empty: bool = conn.query_row(
            "SELECT NOT EXISTS(SELECT 1 FROM provider_receipt_invocation WHERE batch_id=?1)",
            [batch],
            |r| r.get(0),
        )?;
        let failed: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM provider_tariff_batch_status WHERE batch_id=?1 AND persistence_failed=1)",
            [batch], |r| r.get(0),
        )?;
        !missing
            && !failed
            && ((empty && expected.as_deref() == Some("[]")) || coverage(conn, batch)?)
    };
    if old
        .as_ref()
        .is_some_and(|(_, previous, pricing)| *previous == complete && *pricing == compatible)
    {
        return Ok(());
    }
    let inserted = i64::from(old.is_none());
    let delta =
        i64::from(!complete) - i64::from(old.as_ref().is_some_and(|(_, previous, _)| !previous));
    let price_delta =
        i64::from(!compatible) - i64::from(old.as_ref().is_some_and(|(_, _, pricing)| !pricing));
    conn.execute(
        "INSERT INTO provider_tariff_coverage VALUES(?1,?2,?3,?4) ON CONFLICT(batch_id) DO UPDATE SET complete=excluded.complete,pricing_compatible=excluded.pricing_compatible",
        params![batch, owner, complete, compatible],
    )?;
    conn.execute(
        "INSERT INTO provider_tariff_coverage_cohort(owner,snapshot_sha256) VALUES(?1,?2) ON CONFLICT DO NOTHING",
        params![owner, snapshot_sha256()],
    )?;
    conn.execute(
        "UPDATE provider_tariff_coverage_cohort SET batches=batches+?1,incomplete_batches=incomplete_batches+?2,incompatible_batches=incompatible_batches+?3 WHERE owner=?4",
        params![inserted, delta, price_delta, owner],
    )?;
    Ok(())
}

/// Register trusted facts before a declaration can be journaled at dispatch.
pub fn register(conn: &Connection) -> Result<String> {
    ensure_schema(conn)?;
    let sha = snapshot_sha256();
    let json = agentc_core::storage::canonical_json(&snapshot());
    conn.execute(
        "INSERT INTO provider_tariff_policy VALUES(?1,?2) ON CONFLICT DO NOTHING",
        params![sha, json],
    )?;
    let old: String = conn.query_row(
        "SELECT snapshot_json FROM provider_tariff_policy WHERE snapshot_sha256=?1",
        [&sha],
        |r| r.get(0),
    )?;
    if old != json {
        bail!("registered tariff snapshot conflicts");
    }
    Ok(sha)
}

pub fn validate_declaration(conn: &Connection, value: &Declaration) -> Result<()> {
    if value.version != VERSION
        || value.snapshot_sha256 != snapshot_sha256()
        || value.endpoint_sha256.as_ref().is_some_and(|s| {
            s.len() != 64
                || !s
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
    {
        bail!("tariff declaration is invalid");
    }
    let old: Option<String> = conn
        .query_row(
            "SELECT snapshot_json FROM provider_tariff_policy WHERE snapshot_sha256=?1",
            [&value.snapshot_sha256],
            |r| r.get(0),
        )
        .optional()?;
    if old.as_deref() != Some(&agentc_core::storage::canonical_json(&snapshot())) {
        bail!("tariff snapshot was not registered before dispatch");
    }
    Ok(())
}

/// Called inside the original SDK receipt transaction. Every physical owner,
/// role, failure and late completion has one interval row and one cohort delta.
pub fn refresh(conn: &Connection, batch: &str, id: &str) -> Result<()> {
    let (raw,terminal):(String,Option<String>)=conn.query_row("SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 AND invocation_id=?2",params![batch,id],|r|Ok((r.get(0)?,r.get(1)?)))?;
    let invocation: Invocation = serde_json::from_str(&raw)?;
    let owner: Option<(String, String)> = conn
        .query_row(
            "SELECT owner_call_site_version,phase FROM provider_economic_batch WHERE batch_id=?1",
            [batch],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    let (owner, phase) = owner.unwrap_or_else(|| (format!("batch:{batch}"), "unclassified".into()));
    let completion: Option<Completion> = terminal
        .as_ref()
        .map(|s| serde_json::from_str(s))
        .transpose()?;
    let receipt_sha256 = terminal
        .as_ref()
        .map(|done| receipt_hash(batch, id, &raw, done));
    let old:Option<(bool,bool,u64,u64,bool,Option<String>)>=conn.query_row("SELECT charged,terminal,lower_nanodollars,upper_nanodollars,unknown_cost,result_json FROM provider_tariff_invocation WHERE invocation_id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).optional()?;
    let (bound, result) = completion
        .as_ref()
        .map(|c| match quote(&invocation, c) {
            Ok(bound) => (
                Some(bound.clone()),
                serde_json::json!({"status":"bounded","bound":bound,"receipt_sha256":receipt_sha256}),
            ),
            Err(reason) => (None, serde_json::json!({"status":"held","reason":reason,"receipt_sha256":receipt_sha256})),
        })
        .map(|(bound, result)| (bound, Some(result.to_string())))
        .unwrap_or((None, None));
    if old
        .as_ref()
        .is_some_and(|(_, done, _, _, _, previous)| *done && previous != &result)
    {
        bail!("tariff resolution conflicts with original terminal evidence");
    }
    let charged = phase != "deployment"
        || invocation.role != Role::Primary
        || invocation.ordinal != 0
        || bound.is_none();
    let interval = bound.as_ref().map_or(
        Interval {
            lower_nanodollars: 0,
            upper_nanodollars: 0,
        },
        |b| b.interval,
    );
    conn.execute("INSERT INTO provider_tariff_invocation VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10) ON CONFLICT(invocation_id) DO UPDATE SET charged=excluded.charged,terminal=excluded.terminal,lower_nanodollars=excluded.lower_nanodollars,upper_nanodollars=excluded.upper_nanodollars,unknown_cost=excluded.unknown_cost,result_json=excluded.result_json",params![id,batch,owner,phase,charged,completion.is_some(),to_i64(interval.lower_nanodollars)?,to_i64(interval.upper_nanodollars)?,bound.is_none(),result])?;
    let (old_charged, _, old_lower, old_upper, old_unknown, _) =
        old.clone().unwrap_or((false, false, 0, 0, false, None));
    let delta_lower = to_i64(if charged {
        interval.lower_nanodollars
    } else {
        0
    })? - to_i64(if old_charged { old_lower } else { 0 })?;
    let delta_upper = to_i64(if charged {
        interval.upper_nanodollars
    } else {
        0
    })? - to_i64(if old_charged { old_upper } else { 0 })?;
    conn.execute(
        "INSERT INTO provider_tariff_cohort(owner) VALUES(?1) ON CONFLICT DO NOTHING",
        [&owner],
    )?;
    let totals: (u64, u64) = conn.query_row(
        "SELECT lower_nanodollars,upper_nanodollars FROM provider_tariff_cohort WHERE owner=?1",
        [&owner],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let lower = to_i64(totals.0)?
        .checked_add(delta_lower)
        .filter(|v| *v >= 0)
        .context("tariff cohort overflow")?;
    let upper = to_i64(totals.1)?
        .checked_add(delta_upper)
        .filter(|v| *v >= 0)
        .context("tariff cohort overflow")?;
    conn.execute("UPDATE provider_tariff_cohort SET owned_invocations=owned_invocations+?1,charged_invocations=charged_invocations+?2,unknown_invocations=unknown_invocations+?3,lower_nanodollars=?4,upper_nanodollars=?5 WHERE owner=?6",params![i64::from(old.is_none()),i64::from(charged)-i64::from(old_charged),i64::from(charged && bound.is_none())-i64::from(old_charged && old_unknown),lower,upper,owner])?;
    Ok(())
}

fn to_i64(value: u64) -> Result<i64> {
    i64::try_from(value).context("tariff total exceeds SQLite integer range")
}

/// Finish only missing terminal projections from their original declarations.
/// No policy is assigned to a historical/undeclared receipt during recovery.
pub fn recover(conn: &mut Connection, owner: &str) -> Result<()> {
    ensure_schema(conn)?;
    let rows = {
        let mut query=conn.prepare("SELECT r.batch_id,r.invocation_id FROM provider_receipt_invocation r LEFT JOIN provider_tariff_invocation t USING(invocation_id) LEFT JOIN provider_economic_batch b ON b.batch_id=r.batch_id WHERE COALESCE(b.owner_call_site_version,'batch:'||r.batch_id)=?1 AND (t.invocation_id IS NULL OR (t.terminal=0 AND r.completion_json IS NOT NULL))")?;
        let rows = query
            .query_map([owner], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    for (batch, id) in rows {
        let txn = conn.transaction()?;
        refresh(&txn, &batch, &id)?;
        txn.commit()?;
    }
    let batches = {
        let mut query = conn.prepare(
            "SELECT batch_id FROM provider_economic_batch WHERE owner_call_site_version=?1",
        )?;
        let rows = query
            .query_map([owner], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    let txn = conn.transaction()?;
    for batch in batches {
        refresh_coverage(&txn, &batch, true)?;
    }
    txn.commit()?;
    Ok(())
}

pub fn report(conn: &mut Connection, batch: &str) -> Result<serde_json::Value> {
    let owner: Option<String> = conn
        .query_row(
            "SELECT owner_call_site_version FROM provider_economic_batch WHERE batch_id=?1",
            [batch],
            |r| r.get(0),
        )
        .optional()?;
    let owner = owner.unwrap_or_else(|| format!("batch:{batch}"));
    recover(conn, &owner)?;
    let exists: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM provider_receipt_batch WHERE batch_id=?1)",
        [batch],
        |r| r.get(0),
    )?;
    if !exists {
        bail!("tariff batch does not exist");
    }
    let mut query=conn.prepare("SELECT invocation_id,lower_nanodollars,upper_nanodollars,unknown_cost,result_json FROM provider_tariff_invocation WHERE batch_id=?1 ORDER BY invocation_id")?;
    let rows = query.query_map([batch], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, u64>(1)?,
            r.get::<_, u64>(2)?,
            r.get::<_, bool>(3)?,
            r.get::<_, Option<String>>(4)?,
        ))
    })?;
    let mut total = Interval {
        lower_nanodollars: 0,
        upper_nanodollars: 0,
    };
    let mut unknown = 0;
    let mut compatible = true;
    let mut evidence = Vec::new();
    for row in rows {
        let (id, lower, upper, held, result) = row?;
        total = total.checked_add(Interval {
            lower_nanodollars: lower,
            upper_nanodollars: upper,
        })?;
        unknown += u64::from(held);
        let value = result
            .map(|s| serde_json::from_str::<serde_json::Value>(&s))
            .transpose()?;
        if let Some(value) = &value {
            let (original,done):(String,String)=conn.query_row("SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 AND invocation_id=?2",params![batch,id],|row|Ok((row.get(0)?,row.get(1)?)))?;
            if value["receipt_sha256"] != receipt_hash(batch, &id, &original, &done) {
                bail!("tariff evidence no longer matches original SDK receipt");
            }
            if let Some(bound) = value.get("bound") {
                compatible &=
                    bound["snapshot_sha256"] == snapshot_sha256() && bound["quantity"] == QUANTITY;
            }
        }
        evidence.push(serde_json::json!({"invocation_id":id,"result":value}));
    }
    let (sealed,owner):(bool,Option<String>)=conn.query_row("SELECT sealed,(SELECT owner_call_site_version FROM provider_economic_batch e WHERE e.batch_id=b.batch_id) FROM provider_receipt_batch b WHERE batch_id=?1",[batch],|r|Ok((r.get(0)?,r.get(1)?)))?;
    let owner = owner.unwrap_or_else(|| format!("batch:{batch}"));
    let cohort:Option<(u64,u64,u64,u64,u64)>=conn.query_row("SELECT owned_invocations,charged_invocations,unknown_invocations,lower_nanodollars,upper_nanodollars FROM provider_tariff_cohort WHERE owner=?1",[&owner],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
    let (owned, charged, held, lower, upper) = cohort.unwrap_or_default();
    let unclassified: u64 = conn.query_row(
        "SELECT COUNT(*) FROM provider_tariff_invocation WHERE owner=?1 AND phase='unclassified'",
        [&owner],
        |r| r.get(0),
    )?;
    let pricing_complete = if owner.starts_with("batch:") {
        compatible
    } else {
        summarize_acquisition(
            conn,
            &crate::plan_profile::CallSiteVersion::parse(owner.clone())?,
        )?
        .pricing_complete
    };
    Ok(
        serde_json::json!({"quantity":QUANTITY,"snapshot_sha256":snapshot_sha256(),"batch_id":batch,
        "lower_nanodollars":if compatible{total.lower_nanodollars}else{0},"upper_nanodollars":if compatible && unknown==0 && sealed && !evidence.is_empty() && coverage(conn,batch)?{Some(total.upper_nanodollars)}else{None},
        "bounded_observed_upper_nanodollars":total.upper_nanodollars,"unknown_invocations":unknown,"invocations":evidence,
        "acquisition":{"owner":owner,"owned_invocations":owned,"charged_invocations":charged,"unknown_invocations":held,
            "lower_nanodollars":if unclassified==0 && pricing_complete{lower}else{0},"upper_nanodollars":if unclassified==0 && pricing_complete && held==0 && owned>0 && owner_coverage(conn,&owner)?{Some(upper)}else{None},
            "unclassified_invocations":unclassified,"phase_classification_complete":unclassified==0,
            "observed_charged_lower_nanodollars":lower,"bounded_observed_upper_nanodollars":upper}}),
    )
}

fn receipt_hash(batch: &str, id: &str, invocation: &str, completion: &str) -> String {
    agentc_core::storage::content_hash(&agentc_core::storage::canonical_json(&serde_json::json!({
        "version":"provider-billing-binding-v1","batch_id":batch,"invocation_id":id,
        "invocation":invocation,"completion":completion
    })))
}

pub fn seal(conn: &Connection, batch: &str, failed: bool) -> Result<()> {
    if failed {
        conn.execute("INSERT INTO provider_tariff_batch_status VALUES(?1,1) ON CONFLICT(batch_id) DO UPDATE SET persistence_failed=1",[batch])?;
    }
    Ok(())
}

fn owner_coverage(conn: &Connection, owner: &str) -> Result<bool> {
    if !owner.starts_with("batch:") {
        let owner = crate::plan_profile::CallSiteVersion::parse(owner.to_owned())?;
        let economics = crate::provider_economics::summarize_cohort(conn, &owner)?;
        // Slot ownership exists before the first SDK receipt. Missing/failed
        // batches and uncovered historical work cannot disappear from bounds.
        // Unreported billed USD is a separate quantity and is not a tariff hold.
        if economics.owned_batches == 0
            || economics.incomplete_batches != 0
            || economics.uncovered_profile_executions != 0
            || economics.pending_billing_verifications != 0
        {
            return Ok(false);
        }
    }
    let mut query =
        conn.prepare("SELECT DISTINCT batch_id FROM provider_tariff_invocation WHERE owner=?1")?;
    let batches = query
        .query_map([owner], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for batch in batches {
        if !coverage(conn, &batch)? {
            return Ok(false);
        }
    }
    Ok(true)
}

fn coverage(conn: &Connection, batch: &str) -> Result<bool> {
    let (metadata,sealed,expected):(String,bool,Option<String>)=conn.query_row("SELECT metadata_json,sealed,expected_calls_json FROM provider_receipt_batch WHERE batch_id=?1",[batch],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
    let failed:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM provider_tariff_batch_status WHERE batch_id=?1 AND persistence_failed=1)",[batch],|r|r.get(0))?;
    if !sealed || failed || expected.is_none() {
        return Ok(false);
    }
    let metadata: crate::provider_receipts::Batch = serde_json::from_str(&metadata)?;
    let expected: std::collections::BTreeSet<crate::multi_call::CallPosition> =
        serde_json::from_str(&expected.unwrap())?;
    let mut query=conn.prepare("SELECT invocation_json FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call,ordinal")?;
    let rows = query.query_map([batch], |r| r.get::<_, String>(0))?;
    let mut roots = std::collections::BTreeSet::new();
    let mut ordinals = std::collections::BTreeMap::new();
    let mut count = 0;
    for row in rows {
        let call: Invocation = serde_json::from_str(&row?)?;
        let expected_key:Option<String>=conn.query_row("SELECT key_json FROM provider_economic_branch WHERE batch_id=?1 AND item=?2 AND logical_call=?3",params![batch,call.position.item,call.position.call],|r|r.get(0)).optional()?;
        if let Some(expected_key) = expected_key {
            if call.logical_plan_key.as_ref()
                != Some(
                    &serde_json::from_str::<crate::plan_profile::PlanProfileKey>(&expected_key)?,
                )
            {
                return Ok(false);
            }
        }
        count += 1;
        let ordinal = ordinals.entry(call.position).or_insert(0);
        if call.ordinal != *ordinal {
            return Ok(false);
        }
        *ordinal += 1;
        if matches!(call.role, Role::Primary | Role::Fallback)
            || metadata.callback_version == "standalone-token-tariff-v1"
        {
            roots.insert(call.position);
        }
    }
    Ok(!expected.is_empty()
        && roots == expected
        && (metadata.callback_version != "standalone-token-tariff-v1" || count == 1))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn acquisition_queries_search_owner_indexes_without_physical_history() {
        let conn = Connection::open_in_memory().unwrap();
        ensure_schema(&conn).unwrap();
        for sql in [COHORT_SQL, COVERAGE_SQL] {
            let mut query = conn.prepare(&format!("EXPLAIN QUERY PLAN {sql}")).unwrap();
            let steps = query
                .query_map(["a".repeat(64)], |r| r.get::<_, String>(3))
                .unwrap()
                .collect::<rusqlite::Result<Vec<_>>>()
                .unwrap();
            assert!(
                steps.iter().any(|step| step.starts_with("SEARCH")),
                "{steps:?}"
            );
            assert!(
                !steps.iter().any(|step| step.starts_with("SCAN")),
                "{steps:?}"
            );
            assert!(
                !steps.iter().any(|step| step.contains("invocation")),
                "{steps:?}"
            );
        }
    }
}
