//! No-provider bridge for the protected-pair coordinator. No synthetic warming.
//! The coordinator fences every callback in its original accounting journal.
//! A lost callback acknowledgement is deliberately not retried.

use std::{
    io::{self, Read},
    path::PathBuf,
    sync::Arc,
};

use agentc_core::storage::canonical_json;
use agentc_optimizer::{
    budget::SampleOutcome,
    ffi,
    model_catalog::ModelCatalog,
    rules::{ContextCompressRule, ModelDowngradeRule, StateDropRule},
    schema::ensure_cost_model_schema,
    Budget, Call, CostModel, ExplorationController, Optimizer, OptimizerConfig, Outcome, PlanGuard,
    PlanProfiles,
};
use anyhow::{anyhow, bail, Context, Result};
use rusqlite::{Connection, OptionalExtension};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    binding: Value,
    catalog: ModelCatalog,
    callback_id: Option<String>,
    ledger_prefix: Option<Prefix>,
    previous_prefix: Option<Prefix>,
    command: Command,
}

#[derive(Deserialize, serde::Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Prefix {
    count: u64,
    sha256: String,
}

fn checkpoint(conn: &Connection) -> Result<Option<Prefix>> {
    Ok(conn
        .query_row(
            "SELECT event_count, prefix_sha256 FROM protected_bridge_checkpoint WHERE singleton=1",
            [],
            |r| {
                Ok(Prefix {
                    count: r.get(0)?,
                    sha256: r.get(1)?,
                })
            },
        )
        .optional()?)
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Command {
    Plan {
        call: Call,
        now_us: i64,
    },
    Observe {
        primary: Value,
        outcome: Outcome,
    },
    Complete {
        lease_token: String,
        outcome: Outcome,
        divergence: f64,
        now_us: i64,
    },
    Fail {
        lease_token: String,
        now_us: i64,
    },
    Inspect {
        call_site_id: String,
    },
    Checkpoint,
    Verify {
        callback_id: String,
        request_sha256: String,
        reply_sha256: String,
    },
}

fn run(store: PathBuf, input: &str) -> Result<Value> {
    let request: Request = serde_json::from_str(input)?;
    ModelCatalog::from_json(&serde_json::to_string(&request.catalog)?)?;
    if !store.is_absolute() || request.binding["store"] != store.to_string_lossy().as_ref() {
        bail!("bridge store binding differs");
    }
    // These defaults and the exact three rules are a frozen experiment contract.
    // There is no environment/config route to changing a numerical guard here.
    let config = OptimizerConfig::default();
    config.validate()?;
    let request_sha256 = format!("{:x}", Sha256::digest(input.as_bytes()));
    let binding = canonical_json(&json!({"binding":request.binding,"catalog":request.catalog}));
    if matches!(request.command, Command::Checkpoint) && !store.exists() {
        return Ok(json!({"request_sha256":request_sha256,"result":null}));
    }
    if matches!(
        request.command,
        Command::Verify { .. } | Command::Inspect { .. } | Command::Checkpoint
    ) {
        let conn = Connection::open_with_flags(&store, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        let stored_binding: String = conn.query_row(
            "SELECT binding FROM protected_bridge_binding WHERE singleton=1",
            [],
            |r| r.get(0),
        )?;
        if stored_binding != binding {
            bail!("native store binding differs");
        }
        let result = match &request.command {
            Command::Verify {
                callback_id,
                request_sha256: expected_request,
                reply_sha256,
            } => {
                let saved: (String, Option<String>) = conn.query_row("SELECT request_sha256, reply_sha256 FROM protected_bridge_callbacks WHERE callback_id=?1", [callback_id], |r|Ok((r.get(0)?,r.get(1)?)))?;
                if saved.0 != *expected_request || saved.1.as_ref() != Some(reply_sha256) {
                    bail!("native callback acknowledgement differs or is ambiguous");
                }
                json!({"verified":true})
            }
            Command::Checkpoint => serde_json::to_value(checkpoint(&conn)?)?,
            Command::Inspect { call_site_id } => json!({
                "source_observations":conn.query_row("SELECT n_observations FROM call_site_profile WHERE call_site_id=?1",[call_site_id],|r|r.get::<_,u64>(0)).optional()?.unwrap_or(0),
                "completed_leases":conn.query_row("SELECT COUNT(*) FROM execution_plan_exploration WHERE status='completed'",[],|r|r.get::<_,u64>(0))?,
                "failed_leases":conn.query_row("SELECT COUNT(*) FROM execution_plan_exploration WHERE status='failed'",[],|r|r.get::<_,u64>(0))?,
            }),
            _ => unreachable!(),
        };
        return Ok(json!({"request_sha256":request_sha256,"result":result}));
    }
    let prefix = request
        .ledger_prefix
        .as_ref()
        .context("original journal prefix required")?;
    if prefix.count == 0 || prefix.sha256.len() != 64 {
        bail!("invalid original journal prefix");
    }
    if let Some(previous) = &request.previous_prefix {
        if prefix.count <= previous.count {
            bail!("journal checkpoint must advance");
        }
    }
    let mut conn = Connection::open(&store)?;
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;")?;
    conn.execute_batch("CREATE TABLE IF NOT EXISTS protected_bridge_binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), binding TEXT NOT NULL)")?;
    conn.execute_batch("CREATE TABLE IF NOT EXISTS protected_bridge_callbacks (callback_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, reply_sha256 TEXT);
        CREATE TABLE IF NOT EXISTS protected_bridge_checkpoint (singleton INTEGER PRIMARY KEY CHECK(singleton=1), event_count INTEGER NOT NULL, prefix_sha256 TEXT NOT NULL)")?;
    let transaction = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let existing: Option<String> = transaction
        .query_row(
            "SELECT binding FROM protected_bridge_binding WHERE singleton=1",
            [],
            |row| row.get(0),
        )
        .optional()?;
    if existing.as_ref().is_some_and(|value| value != &binding) {
        bail!("native store belongs to a different frozen contract");
    }
    transaction.execute(
        "INSERT OR IGNORE INTO protected_bridge_binding VALUES (1, ?1)",
        [&binding],
    )?;
    if checkpoint(&transaction)? != request.previous_prefix {
        bail!("native journal checkpoint changed concurrently");
    }
    let callback_id = request
        .callback_id
        .as_deref()
        .filter(|s| !s.is_empty())
        .context("callback identity required")?;
    // Enrollment, original-journal high-water mark and callback fence commit
    // together. This duplicates no accounting; older/missing prefixes stop.
    transaction.execute(
        "INSERT INTO protected_bridge_callbacks (callback_id,request_sha256) VALUES (?1,?2)",
        [callback_id, &request_sha256],
    )?;
    transaction.execute("INSERT INTO protected_bridge_checkpoint VALUES (1,?1,?2) ON CONFLICT(singleton) DO UPDATE SET event_count=excluded.event_count,prefix_sha256=excluded.prefix_sha256", rusqlite::params![prefix.count,prefix.sha256])?;
    transaction.commit()?;
    ensure_cost_model_schema(&conn)?;
    let catalog = Arc::new(request.catalog);
    let cost_model = Arc::new(CostModel::with_window(config.cost_model_window));
    let profiles = PlanProfiles::with_window(config.plan_profile_window);
    let guard = PlanGuard::new();
    let budget = Arc::new(Budget::with_window(config.divergence_window));
    cost_model.warm_from_db(&conn)?;
    profiles.warm_from_db(&conn)?;
    guard.warm_from_db(&conn)?;
    budget.warm_from_db(&conn)?;
    budget.warm_divergence_from_db(&conn)?;
    let controller = ExplorationController::with_policy(config.exploration_policy())?;
    let optimizer = Optimizer::with_budget(
        cost_model.clone(),
        vec![
            Box::new(ContextCompressRule::default()),
            Box::new(StateDropRule::default()),
            Box::new(ModelDowngradeRule::from_catalog(
                catalog.clone(),
                budget.clone(),
            )),
        ],
        config,
        budget.clone(),
    );
    let result = match request.command {
        Command::Plan { call, now_us } => {
            let call = serde_json::to_string(&call)?;
            let primary = ffi::optimize_profiled_plan(
                &optimizer,
                Some(&catalog),
                &profiles,
                &guard,
                &call,
                now_us,
            );
            let planned = ffi::reserve_profiled_exploration(
                &optimizer,
                Some(&catalog),
                &profiles,
                &guard,
                &controller,
                &mut conn,
                &call,
                &primary,
                now_us,
            );
            serde_json::from_str(&planned)?
        }
        Command::Observe { primary, outcome } => {
            if primary["kind"] != "pass_through"
                || primary.get("agentc_exploration_context").is_some()
            {
                bail!("source observation requires only the actual reference plan");
            }
            let token = ffi::optimize_observe(
                &cost_model,
                &profiles,
                &serde_json::to_string(&primary)?,
                &serde_json::to_string(&outcome)?,
            )
            .map_err(|e| anyhow!(e))?;
            json!({"observation_token":token})
        }
        Command::Complete {
            lease_token,
            outcome,
            divergence,
            now_us,
        } => {
            let attribution = ffi::complete_profiled_exploration(
                &profiles,
                &controller,
                &mut conn,
                &lease_token,
                &serde_json::to_string(&outcome)?,
                divergence,
                now_us,
            )
            .map_err(|e| anyhow!(e))?;
            if let Some(attribution) = attribution {
                let observation = attribution
                    .plan_observation
                    .as_ref()
                    .context("missing exact observation")?;
                let threshold = attribution
                    .divergence_threshold
                    .context("missing risk threshold")?;
                guard.record_sample(observation, divergence, threshold, Some(now_us))?;
                // Match the production adapter: solo evidence may update its
                // legacy rule guard; composed evidence is never split apart.
                if let Some(rule) = attribution.solo_rule.as_deref() {
                    let threshold = optimizer
                        .accuracy_budget_for(rule)
                        .context("unknown rule")?;
                    if let SampleOutcome::Disable {
                        disabled_at_us,
                        reenable_at_us,
                    } = budget.record_sample(
                        &attribution.call_site_id,
                        rule,
                        divergence as f32,
                        threshold,
                        now_us,
                    ) {
                        budget.persist_disable(
                            &conn,
                            &attribution.call_site_id,
                            rule,
                            "shadow_divergence",
                            disabled_at_us,
                            reenable_at_us,
                        )?;
                    }
                }
                json!({"newly_recorded":true,"observation":observation,"divergence_threshold":threshold})
            } else {
                json!({"newly_recorded":false})
            }
        }
        Command::Fail {
            lease_token,
            now_us,
        } => {
            ffi::fail_profiled_exploration(&controller, &mut conn, &lease_token, now_us)
                .map_err(|e| anyhow!(e))?;
            json!({"failed":true})
        }
        Command::Inspect { .. } | Command::Verify { .. } | Command::Checkpoint => {
            unreachable!("read-only operation returned above")
        }
    };
    // Acknowledgement follows durable native persistence. A process crash
    // between these writes may lose evidence, never authorize callback replay.
    cost_model.flush_dirty(&mut conn)?;
    profiles.flush_dirty(&mut conn)?;
    guard.flush_dirty(&mut conn)?;
    budget.flush_divergence(&mut conn)?;
    let reply = json!({"request_sha256":request_sha256,"result":result});
    if let Some(callback_id) = request.callback_id {
        let reply_sha256 = format!("{:x}", Sha256::digest(serde_json::to_vec(&reply)?));
        conn.execute(
            "UPDATE protected_bridge_callbacks SET reply_sha256=?1 WHERE callback_id=?2",
            [reply_sha256, callback_id],
        )?;
    }
    Ok(reply)
}

fn main() -> Result<()> {
    let store = PathBuf::from(std::env::args().nth(1).context("explicit store required")?);
    let mut input = String::new();
    io::stdin().take(2_000_001).read_to_string(&mut input)?;
    if input.len() > 2_000_000 {
        bail!("bridge request too large");
    }
    // No request/response/provider content is printed on failure.
    let result = run(store, &input).map_err(|_| anyhow!("protected native callback rejected"))?;
    println!("{}", serde_json::to_string(&result)?);
    Ok(())
}
