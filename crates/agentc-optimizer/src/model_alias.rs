//! Provider-observed alias revisions shared by exact request cohorts.
//!
//! Resolution changes lookup metadata only. Dispatch still uses the original
//! requested alias, and each completed response supplies its own observed version.

use std::collections::HashMap;

use anyhow::{Context, Result};
use parking_lot::RwLock;
use rusqlite::{params, Connection, Transaction};

use crate::plan_profile::{CallSiteVersion, PlanProfileKey, PlanRuntimeVersion};

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub(crate) struct AliasKey {
    call_site_version: CallSiteVersion,
    protocol: String,
    model_id: String,
    declared_version: String,
    price_version: String,
}

#[derive(Clone, Debug)]
pub(crate) struct Resolution {
    version: Option<String>,
    generation: u64,
    dirty: bool,
}

pub(crate) struct ModelAliases {
    entries: RwLock<HashMap<AliasKey, Resolution>>,
}

fn alias_key(site: &CallSiteVersion, runtime: &PlanRuntimeVersion) -> Option<AliasKey> {
    let (model, catalog) = runtime.target_model_version.split_once('@')?;
    if model != runtime.target_model_id || catalog.is_empty() {
        return None;
    }
    Some(AliasKey {
        call_site_version: site.clone(),
        protocol: runtime.provider_protocol.clone(),
        model_id: runtime.target_model_id.clone(),
        declared_version: runtime.target_model_version.clone(),
        price_version: runtime.price_table_version.clone(),
    })
}

impl ModelAliases {
    pub fn new() -> Self {
        Self {
            entries: RwLock::new(HashMap::new()),
        }
    }

    pub fn observe(
        &self,
        key: &PlanProfileKey,
        planned: &PlanRuntimeVersion,
        observed: &PlanRuntimeVersion,
    ) {
        let Some(key) = alias_key(&key.call_site_version, planned) else {
            return;
        };
        let version = (observed.target_model_version != planned.target_model_version)
            .then(|| observed.target_model_version.clone());
        let mut entries = self.entries.write();
        // Preserve the older unresolved native-only cohort until an actual
        // provider expansion is seen. Returning to an opaque alias afterwards
        // invalidates that expansion, including across a restart.
        if version.is_none() && !entries.contains_key(&key) {
            return;
        }
        let entry = entries.entry(key).or_insert(Resolution {
            version: None,
            generation: 0,
            dirty: false,
        });
        entry.version = version;
        entry.generation = entry.generation.saturating_add(1);
        entry.dirty = true;
    }

    pub fn for_planning(
        &self,
        key: &PlanProfileKey,
        planned: &PlanRuntimeVersion,
    ) -> Option<PlanRuntimeVersion> {
        let Some(key) = alias_key(&key.call_site_version, planned) else {
            return Some(planned.clone());
        };
        let entries = self.entries.read();
        let Some(resolution) = entries.get(&key) else {
            return Some(planned.clone());
        };
        let mut runtime = planned.clone();
        runtime.target_model_version = resolution.version.clone()?;
        Some(runtime)
    }

    pub fn warm(&self, connection: &Connection) -> Result<()> {
        let mut statement = connection.prepare(
            "SELECT call_site_version, provider_protocol, target_model_id, declared_model_version, \
             price_table_version, resolved_model_version, generation FROM execution_plan_model_alias"
        ).context("read alias resolutions")?;
        let rows = statement.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, Option<String>>(5)?,
                row.get::<_, u64>(6)?,
            ))
        })?;
        let mut entries = self.entries.write();
        for row in rows {
            let (site, protocol, model_id, declared_version, price_version, version, generation) =
                row?;
            let site = CallSiteVersion::parse(site).context("decode alias call-site version")?;
            let runtime = PlanRuntimeVersion {
                provider_protocol: protocol,
                target_model_id: model_id,
                target_model_version: declared_version,
                price_table_version: price_version,
            };
            let key = alias_key(&site, &runtime).context("invalid persisted alias declaration")?;
            if generation == 0
                || version
                    .as_ref()
                    .is_some_and(|value| value.trim().is_empty())
            {
                anyhow::bail!("invalid persisted alias resolution");
            }
            entries.insert(
                key,
                Resolution {
                    version,
                    generation,
                    dirty: false,
                },
            );
        }
        Ok(())
    }

    pub fn dirty_snapshot(&self) -> Vec<(AliasKey, Resolution)> {
        self.entries
            .read()
            .iter()
            .filter(|(_, value)| value.dirty)
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect()
    }

    pub fn persist(transaction: &Transaction<'_>, rows: &[(AliasKey, Resolution)]) -> Result<()> {
        for (key, value) in rows {
            transaction.execute(
                "INSERT INTO execution_plan_model_alias (call_site_version, provider_protocol, \
                 target_model_id, declared_model_version, price_table_version, resolved_model_version, generation) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7) \
                 ON CONFLICT(call_site_version, provider_protocol, target_model_id, declared_model_version, price_table_version) \
                 DO UPDATE SET resolved_model_version=excluded.resolved_model_version, generation=excluded.generation \
                 WHERE excluded.generation >= execution_plan_model_alias.generation",
                params![key.call_site_version.as_str(), key.protocol, key.model_id,
                        key.declared_version, key.price_version, value.version, value.generation],
            )?;
        }
        Ok(())
    }

    pub fn mark_persisted(&self, rows: &[(AliasKey, Resolution)]) {
        let mut entries = self.entries.write();
        for (key, persisted) in rows {
            if let Some(entry) = entries.get_mut(key) {
                if entry.generation == persisted.generation {
                    entry.dirty = false;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::execution_plan::ExecutionPlanId;
    use crate::schema::ensure_cost_model_schema;

    fn key(plan: char) -> PlanProfileKey {
        PlanProfileKey {
            call_site_version: CallSiteVersion::parse("a".repeat(64)).unwrap(),
            execution_plan_id: ExecutionPlanId::parse(plan.to_string().repeat(64)).unwrap(),
        }
    }

    fn planned() -> PlanRuntimeVersion {
        PlanRuntimeVersion {
            provider_protocol: "openai.chat.completions.v1".into(),
            target_model_id: "alias".into(),
            target_model_version: "alias@catalog-v1".into(),
            price_table_version: "prices-v1".into(),
        }
    }

    #[test]
    fn resolution_is_shared_by_plans_but_not_other_request_cohorts() {
        let aliases = ModelAliases::new();
        let planned = planned();
        let observed = PlanRuntimeVersion {
            target_model_version: "revision-A".into(),
            ..planned.clone()
        };
        aliases.observe(&key('b'), &planned, &observed);
        assert_eq!(
            aliases.for_planning(&key('c'), &planned),
            Some(observed.clone())
        );
        for other in [
            PlanRuntimeVersion {
                provider_protocol: "other-protocol".into(),
                ..planned.clone()
            },
            PlanRuntimeVersion {
                price_table_version: "prices-v2".into(),
                ..planned.clone()
            },
            PlanRuntimeVersion {
                target_model_version: "alias@catalog-v2".into(),
                ..planned.clone()
            },
        ] {
            assert_eq!(aliases.for_planning(&key('b'), &other), Some(other));
        }
        let other_site = PlanProfileKey {
            call_site_version: CallSiteVersion::parse("d".repeat(64)).unwrap(),
            ..key('b')
        };
        assert_eq!(aliases.for_planning(&other_site, &planned), Some(planned));
    }

    #[test]
    fn an_opaque_response_invalidates_the_previous_resolution_after_restart() {
        let aliases = ModelAliases::new();
        let planned = planned();
        let observed = PlanRuntimeVersion {
            target_model_version: "revision-A".into(),
            ..planned.clone()
        };
        aliases.observe(&key('b'), &planned, &observed);
        aliases.observe(&key('c'), &planned, &planned);
        assert!(aliases.for_planning(&key('b'), &planned).is_none());
        let mut connection = Connection::open_in_memory().unwrap();
        ensure_cost_model_schema(&connection).unwrap();
        let rows = aliases.dirty_snapshot();
        let transaction = connection.transaction().unwrap();
        ModelAliases::persist(&transaction, &rows).unwrap();
        transaction.commit().unwrap();
        let restarted = ModelAliases::new();
        restarted.warm(&connection).unwrap();
        assert!(restarted.for_planning(&key('b'), &planned).is_none());
    }

    #[test]
    fn a_newer_resolution_cannot_be_cleared_by_an_older_flush() {
        let aliases = ModelAliases::new();
        let planned = planned();
        let first = PlanRuntimeVersion {
            target_model_version: "revision-A".into(),
            ..planned.clone()
        };
        let second = PlanRuntimeVersion {
            target_model_version: "revision-B".into(),
            ..planned.clone()
        };
        aliases.observe(&key('b'), &planned, &first);
        let old = aliases.dirty_snapshot();
        aliases.observe(&key('c'), &planned, &second);
        aliases.mark_persisted(&old);
        assert_eq!(aliases.dirty_snapshot().len(), 1);
        assert_eq!(aliases.for_planning(&key('b'), &planned), Some(second));
    }
}
