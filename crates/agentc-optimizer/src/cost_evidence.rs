//! Provenance of execution costs, independent of observed quality and timing.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CostSource {
    #[default]
    Unknown,
    Estimated,
    ProviderReported,
    ProviderBillingVerified,
    NoInference,
}

impl CostSource {
    pub fn is_measured(self) -> bool {
        matches!(
            self,
            Self::ProviderReported | Self::ProviderBillingVerified | Self::NoInference
        )
    }

    pub fn combine(self, other: Self) -> Self {
        use CostSource::*;
        match (self, other) {
            (Unknown, _) | (_, Unknown) => Unknown,
            (Estimated, _) | (_, Estimated) => Estimated,
            (NoInference, other) | (other, NoInference) => other,
            (ProviderReported, _) | (_, ProviderReported) => ProviderReported,
            _ => ProviderBillingVerified,
        }
    }
}

impl From<&crate::multi_call::InferenceCostSource> for CostSource {
    fn from(source: &crate::multi_call::InferenceCostSource) -> Self {
        use crate::multi_call::InferenceCostSource as S;
        match source {
            S::Unknown => Self::Unknown,
            S::Estimated => Self::Estimated,
            S::ProviderReported => Self::ProviderReported,
            S::NoInference => Self::NoInference,
        }
    }
}

/// Identity of a physical receipt, when the SDK executed in an owned journal.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PhysicalInvocation {
    pub batch_id: String,
    pub invocation_id: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CostEvidence {
    pub source: CostSource,
    #[serde(default)]
    pub physical_invocations: Vec<PhysicalInvocation>,
}

impl CostEvidence {
    pub fn validate(&self, cost_usd: f64) -> bool {
        let mut identities = std::collections::BTreeSet::new();
        (self.source != CostSource::NoInference
            || cost_usd == 0.0 && self.physical_invocations.is_empty())
            && self.physical_invocations.iter().all(|identity| {
                [&identity.batch_id, &identity.invocation_id]
                    .into_iter()
                    .all(|id| {
                        id.len() == 32
                            && id
                                .bytes()
                                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                    })
                    && identities.insert(identity)
            })
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CostSourceCounts {
    pub unknown: u32,
    pub estimated: u32,
    pub provider_reported: u32,
    pub provider_billing_verified: u32,
    pub no_inference: u32,
}

impl CostSourceCounts {
    pub fn add(&mut self, source: CostSource) {
        *match source {
            CostSource::Unknown => &mut self.unknown,
            CostSource::Estimated => &mut self.estimated,
            CostSource::ProviderReported => &mut self.provider_reported,
            CostSource::ProviderBillingVerified => &mut self.provider_billing_verified,
            CostSource::NoInference => &mut self.no_inference,
        } += 1;
    }
}
