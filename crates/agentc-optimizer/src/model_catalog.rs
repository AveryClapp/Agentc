//! Versioned model targets and provider-safe compatibility checks.
//!
//! The catalog is the only production source of routing candidates. It keeps
//! provider protocol, credential namespace, immutable model identity (where a
//! provider exposes one), capabilities, token limits, prices, and provenance
//! together. Candidate generation can therefore enumerate complete targets
//! without teaching individual rewrite rules about provider naming schemes.

use std::error::Error;
use std::fmt;

use serde::{Deserialize, Serialize};

use crate::dag::Call;

pub const OPENAI_CHAT_COMPLETIONS_PROTOCOL: &str = "openai.chat.completions.v1";
pub const ANTHROPIC_MESSAGES_PROTOCOL: &str = "anthropic.messages.v1";
pub const LITELLM_COMPLETION_PROTOCOL: &str = "litellm.completion.v1";

pub const ROUTE_CONTEXT_KEY: &str = "agentc_route_context";
pub const ROUTED_TARGET_KEY: &str = "agentc_routed_target";

const INPUT_BOUND_BASIS_KEY: &str = "input_tokens_upper_bound_basis";
const JSON_UTF8_BYTES_BOUND_V1: &str = "json_utf8_bytes_v1";

pub const DEFAULT_MODEL_CATALOG_VERSION: &str = "agentc-model-catalog-2026-09-04-r2";
pub const DEFAULT_PRICE_TABLE_VERSION: &str = "agentc-price-table-2026-09-03-r1";
pub const DEFAULT_CATALOG_OBSERVED_AT_UTC: &str = "2026-09-04T00:00:00Z";

const OPENAI_GPT_54_SOURCE: &str = "https://developers.openai.com/api/docs/models/gpt-5.4";
const OPENAI_GPT_54_MINI_SOURCE: &str =
    "https://developers.openai.com/api/docs/models/gpt-5.4-mini";
const OPENAI_GPT_4O_SOURCE: &str = "https://developers.openai.com/api/docs/models/gpt-4o";
const OPENAI_GPT_4O_MINI_SOURCE: &str = "https://developers.openai.com/api/docs/models/gpt-4o-mini";
const ANTHROPIC_MODELS_SOURCE: &str =
    "https://platform.claude.com/docs/en/about-claude/models/overview";
const ANTHROPIC_PRICING_SOURCE: &str = "https://platform.claude.com/docs/en/about-claude/pricing";
const TOGETHER_MODELS_SOURCE: &str = "https://docs.together.ai/docs/serverless/models";

/// Parameter name an adapter must use when it installs an output-token cap.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OutputTokenParameter {
    MaxTokens,
    MaxCompletionTokens,
}

/// Whether the target revision is provider-pinned or only catalog-observed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelRevisionKind {
    ImmutableSnapshot,
    CatalogObservation,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelCapabilities {
    pub text_input: bool,
    pub image_input: bool,
    pub tool_calling: bool,
    pub structured_outputs: bool,
    pub streaming: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModelPrice {
    pub input_per_million_tokens_usd: f64,
    pub output_per_million_tokens_usd: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cached_input_per_million_tokens_usd: Option<f64>,
    pub table_version: String,
    pub source_url: String,
    pub observed_at_utc: String,
}

impl ModelPrice {
    fn conservative_ratio_to(&self, source: &Self) -> Option<f32> {
        if source.input_per_million_tokens_usd <= 0.0 || source.output_per_million_tokens_usd <= 0.0
        {
            return None;
        }
        let input_ratio = self.input_per_million_tokens_usd / source.input_per_million_tokens_usd;
        let output_ratio =
            self.output_per_million_tokens_usd / source.output_per_million_tokens_usd;
        let ratio = input_ratio.max(output_ratio);
        (ratio.is_finite() && ratio >= 0.0).then_some(ratio as f32)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelProvenance {
    pub catalog_version: String,
    pub source_url: String,
    pub observed_at_utc: String,
}

/// One dispatchable model under one concrete adapter and credential namespace.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModelTarget {
    pub adapter_protocol: String,
    pub provider_namespace: String,
    /// Exact string the provider adapter receives on dispatch.
    pub model_id: String,
    /// Stable runtime-version token. For providers without immutable snapshots,
    /// this is an explicit catalog-observation cohort rather than a fake pin.
    pub model_version: String,
    pub revision_kind: ModelRevisionKind,
    #[serde(default)]
    pub aliases: Vec<String>,
    /// Only targets in the same declared group are downgrade alternatives.
    pub routing_group: String,
    pub context_window_tokens: u32,
    pub max_output_tokens: u32,
    pub output_token_parameter: OutputTokenParameter,
    pub capabilities: ModelCapabilities,
    pub price: ModelPrice,
    pub provenance: ModelProvenance,
}

impl ModelTarget {
    fn matches_id(&self, model_id: &str) -> bool {
        self.model_id == model_id || self.aliases.iter().any(|alias| alias == model_id)
    }

    fn supports(&self, requirements: &RequestRequirements) -> bool {
        if !self.capabilities.text_input
            || (requirements.image_input && !self.capabilities.image_input)
            || (requirements.tool_calling && !self.capabilities.tool_calling)
            || (requirements.structured_outputs && !self.capabilities.structured_outputs)
            || (requirements.streaming && !self.capabilities.streaming)
            || requirements.max_output_tokens > self.max_output_tokens
        {
            return false;
        }

        requirements
            .input_tokens_upper_bound
            .saturating_add(requirements.max_output_tokens)
            <= self.context_window_tokens
    }
}

/// Adapter-declared requirements used to conservatively filter targets.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RequestRequirements {
    pub provider_protocol: String,
    pub provider_namespace: String,
    pub input_tokens_upper_bound: u32,
    pub max_output_tokens: u32,
    pub image_input: bool,
    pub tool_calling: bool,
    pub structured_outputs: bool,
    pub streaming: bool,
}

impl RequestRequirements {
    /// Read the provider-owned request facts attached by a Python adapter.
    /// Missing or malformed context means the catalog abstains.
    pub fn from_call(call: &Call) -> Option<Self> {
        let context = call
            .parameters
            .extra
            .as_object()?
            .get(ROUTE_CONTEXT_KEY)?
            .as_object()?;
        let provider_protocol = context.get("provider_protocol")?.as_str()?.trim();
        let provider_namespace = context.get("provider_namespace")?.as_str()?.trim();
        if provider_protocol.is_empty() || provider_namespace.is_empty() {
            return None;
        }

        Some(Self {
            provider_protocol: provider_protocol.to_string(),
            provider_namespace: provider_namespace.to_string(),
            input_tokens_upper_bound: context
                .get("input_tokens_upper_bound")?
                .as_u64()?
                .min(u64::from(u32::MAX)) as u32,
            max_output_tokens: call.parameters.max_output_tokens.unwrap_or(0),
            image_input: context.get("image_input")?.as_bool()?,
            tool_calling: context.get("tool_calling")?.as_bool()?,
            structured_outputs: context.get("structured_outputs")?.as_bool()?,
            streaming: context.get("streaming")?.as_bool()?,
        })
    }

    /// Lower only the adapter's original input bound by bytes that a lossless,
    /// ordered message deletion is guaranteed to remove. Content-changing or
    /// opaque-native rewrites retain the original bound and therefore cannot
    /// unlock a smaller context window from an unverifiable projection.
    pub(crate) fn apply_transformed_input_bound(reference: &Call, transformed: &mut Call) {
        let Some(reference_requirements) = Self::from_call(reference) else {
            return;
        };
        let Some(transformed_requirements) = Self::from_call(transformed) else {
            return;
        };
        if reference.has_opaque_native_messages()
            || transformed.has_opaque_native_messages()
            || !has_json_byte_input_bound(reference)
            || !has_json_byte_input_bound(transformed)
            || transformed.model != reference.model
            || transformed_requirements.provider_protocol
                != reference_requirements.provider_protocol
            || transformed_requirements.provider_namespace
                != reference_requirements.provider_namespace
            || transformed_requirements.input_tokens_upper_bound
                != reference_requirements.input_tokens_upper_bound
            || transformed_requirements.image_input != reference_requirements.image_input
            || transformed_requirements.tool_calling != reference_requirements.tool_calling
            || transformed_requirements.structured_outputs
                != reference_requirements.structured_outputs
            || transformed_requirements.streaming != reference_requirements.streaming
        {
            return;
        }

        // The Anthropic adapter lifts its top-level `system` value into the
        // unified message list, but deliberately retains the native value when
        // no transformed system message is present. Do not claim those bytes
        // were removed.
        if reference_requirements.provider_protocol == ANTHROPIC_MESSAGES_PROTOCOL
            && reference
                .messages
                .iter()
                .any(|message| message.role == "system")
            && !transformed
                .messages
                .iter()
                .any(|message| message.role == "system")
        {
            return;
        }

        let Some(removed_content_bytes) = ordered_deletion_bytes(
            reference,
            transformed,
            reference_requirements.input_tokens_upper_bound,
        ) else {
            return;
        };
        let Ok(removed_content_bytes) = u32::try_from(removed_content_bytes) else {
            return;
        };
        let Some(route_context) = transformed
            .parameters
            .extra
            .as_object_mut()
            .and_then(|extra| extra.get_mut(ROUTE_CONTEXT_KEY))
            .and_then(serde_json::Value::as_object_mut)
        else {
            return;
        };
        route_context.insert(
            "input_tokens_upper_bound".to_string(),
            serde_json::Value::from(
                reference_requirements
                    .input_tokens_upper_bound
                    .saturating_sub(removed_content_bytes),
            ),
        );
    }
}

fn has_json_byte_input_bound(call: &Call) -> bool {
    call.parameters
        .extra
        .as_object()
        .and_then(|extra| extra.get(ROUTE_CONTEXT_KEY))
        .and_then(serde_json::Value::as_object)
        .and_then(|context| context.get(INPUT_BOUND_BASIS_KEY))
        .and_then(serde_json::Value::as_str)
        == Some(JSON_UTF8_BYTES_BOUND_V1)
}

fn ordered_deletion_bytes(
    reference: &Call,
    transformed: &Call,
    reference_upper_bound: u32,
) -> Option<usize> {
    if !transformed.messages_are_ordered_subsequence_of(reference) {
        return None;
    }

    let reference_bytes = reference
        .messages
        .iter()
        .try_fold(0usize, |total, message| {
            total.checked_add(message.content.len())
        })?;
    let transformed_bytes = transformed
        .messages
        .iter()
        .try_fold(0usize, |total, message| {
            total.checked_add(message.content.len())
        })?;
    if reference_bytes > reference_upper_bound as usize {
        return None;
    }
    reference_bytes.checked_sub(transformed_bytes)
}

/// Dispatch contract attached to a routed call and verified by Python.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RoutedModelTarget {
    pub catalog_version: String,
    pub price_table_version: String,
    pub provider_protocol: String,
    pub provider_namespace: String,
    pub requested_model_id: String,
    pub resolved_requested_model_id: String,
    pub target_model_id: String,
    pub target_model_version: String,
    pub target_revision_kind: ModelRevisionKind,
    pub output_token_parameter: OutputTokenParameter,
}

impl RoutedModelTarget {
    /// Attach metadata without discarding request facts already in `extra`.
    pub fn annotate_call(&self, call: &mut Call) -> Result<(), CatalogError> {
        if call.parameters.extra.is_null() {
            call.parameters.extra = serde_json::json!({});
        }
        let Some(extra) = call.parameters.extra.as_object_mut() else {
            return Err(CatalogError::ExtraParametersNotObject);
        };
        let value = serde_json::to_value(self)
            .map_err(|error| CatalogError::Serialization(error.to_string()))?;
        extra.insert(ROUTED_TARGET_KEY.to_string(), value);
        Ok(())
    }
}

/// Validated, immutable-in-use catalog snapshot.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModelCatalog {
    pub catalog_version: String,
    pub price_table_version: String,
    pub observed_at_utc: String,
    pub targets: Vec<ModelTarget>,
}

impl ModelCatalog {
    /// Decode an explicit snapshot without allowing serde to bypass validation.
    pub fn from_json(json: &str) -> Result<Self, CatalogError> {
        let catalog: Self = serde_json::from_str(json)
            .map_err(|error| CatalogError::Serialization(error.to_string()))?;
        catalog.validate()?;
        Ok(catalog)
    }

    pub fn new(
        catalog_version: impl Into<String>,
        price_table_version: impl Into<String>,
        observed_at_utc: impl Into<String>,
        targets: Vec<ModelTarget>,
    ) -> Result<Self, CatalogError> {
        let catalog = Self {
            catalog_version: catalog_version.into(),
            price_table_version: price_table_version.into(),
            observed_at_utc: observed_at_utc.into(),
            targets,
        };
        catalog.validate()?;
        Ok(catalog)
    }

    pub fn targets_for_protocol(&self, provider_protocol: &str) -> Vec<&ModelTarget> {
        self.targets
            .iter()
            .filter(|target| target.adapter_protocol == provider_protocol)
            .collect()
    }

    pub fn resolve(
        &self,
        provider_protocol: &str,
        provider_namespace: &str,
        model_id: &str,
    ) -> Option<&ModelTarget> {
        self.targets.iter().find(|target| {
            target.adapter_protocol == provider_protocol
                && target.provider_namespace == provider_namespace
                && target.matches_id(model_id)
        })
    }

    /// Enumerate request-compatible models in the requested model's declared
    /// routing group. Cross-protocol and cross-credential routes are impossible.
    pub fn compatible_targets(&self, call: &Call) -> Vec<&ModelTarget> {
        let Some(requirements) = RequestRequirements::from_call(call) else {
            return Vec::new();
        };
        let Some(source) = self.resolve(
            &requirements.provider_protocol,
            &requirements.provider_namespace,
            &call.model,
        ) else {
            return Vec::new();
        };

        let mut targets: Vec<_> = self
            .targets
            .iter()
            .filter(|target| {
                target.adapter_protocol == source.adapter_protocol
                    && target.provider_namespace == source.provider_namespace
                    && target.routing_group == source.routing_group
                    && target.supports(&requirements)
            })
            .collect();
        targets.sort_by(|left, right| left.model_id.cmp(&right.model_id));
        targets
    }

    /// Return cheaper compatible alternatives ordered by conservative price
    /// ratio, then stable model ID. Both input and output prices must not rise.
    pub fn cheaper_targets(&self, call: &Call) -> Vec<(&ModelTarget, f32)> {
        let Some(requirements) = RequestRequirements::from_call(call) else {
            return Vec::new();
        };
        let Some(source) = self.resolve(
            &requirements.provider_protocol,
            &requirements.provider_namespace,
            &call.model,
        ) else {
            return Vec::new();
        };

        let mut targets: Vec<_> = self
            .compatible_targets(call)
            .into_iter()
            .filter(|target| target.model_id != source.model_id)
            .filter(|target| {
                target.price.input_per_million_tokens_usd
                    <= source.price.input_per_million_tokens_usd
                    && target.price.output_per_million_tokens_usd
                        <= source.price.output_per_million_tokens_usd
                    && (target.price.input_per_million_tokens_usd
                        < source.price.input_per_million_tokens_usd
                        || target.price.output_per_million_tokens_usd
                            < source.price.output_per_million_tokens_usd)
            })
            .filter_map(|target| {
                target
                    .price
                    .conservative_ratio_to(&source.price)
                    .map(|ratio| (target, ratio))
            })
            .collect();
        targets.sort_by(|(left, left_ratio), (right, right_ratio)| {
            left_ratio
                .total_cmp(right_ratio)
                .then_with(|| left.model_id.cmp(&right.model_id))
        });
        targets
    }

    pub fn routed_target(
        &self,
        call: &Call,
        target: &ModelTarget,
    ) -> Result<RoutedModelTarget, CatalogError> {
        let requirements =
            RequestRequirements::from_call(call).ok_or(CatalogError::MissingRouteContext)?;
        let source = self
            .resolve(
                &requirements.provider_protocol,
                &requirements.provider_namespace,
                &call.model,
            )
            .ok_or(CatalogError::UnknownRequestedModel)?;
        let admitted = self.compatible_targets(call).into_iter().any(|candidate| {
            candidate.adapter_protocol == target.adapter_protocol
                && candidate.provider_namespace == target.provider_namespace
                && candidate.model_id == target.model_id
        });
        if !admitted {
            return Err(CatalogError::IncompatibleTarget);
        }

        Ok(RoutedModelTarget {
            catalog_version: self.catalog_version.clone(),
            price_table_version: self.price_table_version.clone(),
            provider_protocol: target.adapter_protocol.clone(),
            provider_namespace: target.provider_namespace.clone(),
            requested_model_id: call.model.clone(),
            resolved_requested_model_id: source.model_id.clone(),
            target_model_id: target.model_id.clone(),
            target_model_version: target.model_version.clone(),
            target_revision_kind: target.revision_kind,
            output_token_parameter: target.output_token_parameter,
        })
    }

    pub(crate) fn validate(&self) -> Result<(), CatalogError> {
        for (field, value) in [
            ("catalog_version", self.catalog_version.as_str()),
            ("price_table_version", self.price_table_version.as_str()),
            ("observed_at_utc", self.observed_at_utc.as_str()),
        ] {
            require_nonempty(field, value)?;
        }
        if self.targets.is_empty() {
            return Err(CatalogError::EmptyCatalog);
        }

        let mut identities = std::collections::HashSet::new();
        let mut aliases = std::collections::HashSet::new();
        for target in &self.targets {
            for (field, value) in [
                ("adapter_protocol", target.adapter_protocol.as_str()),
                ("provider_namespace", target.provider_namespace.as_str()),
                ("model_id", target.model_id.as_str()),
                ("model_version", target.model_version.as_str()),
                ("routing_group", target.routing_group.as_str()),
                ("price.table_version", target.price.table_version.as_str()),
                ("price.source_url", target.price.source_url.as_str()),
                (
                    "price.observed_at_utc",
                    target.price.observed_at_utc.as_str(),
                ),
                (
                    "provenance.catalog_version",
                    target.provenance.catalog_version.as_str(),
                ),
                (
                    "provenance.source_url",
                    target.provenance.source_url.as_str(),
                ),
                (
                    "provenance.observed_at_utc",
                    target.provenance.observed_at_utc.as_str(),
                ),
            ] {
                require_nonempty(field, value)?;
            }
            if target.price.table_version != self.price_table_version
                || target.provenance.catalog_version != self.catalog_version
            {
                return Err(CatalogError::VersionMismatch(target.model_id.clone()));
            }
            if !target.price.input_per_million_tokens_usd.is_finite()
                || target.price.input_per_million_tokens_usd < 0.0
                || !target.price.output_per_million_tokens_usd.is_finite()
                || target.price.output_per_million_tokens_usd < 0.0
                || target
                    .price
                    .cached_input_per_million_tokens_usd
                    .is_some_and(|price| !price.is_finite() || price < 0.0)
            {
                return Err(CatalogError::InvalidPrice(target.model_id.clone()));
            }
            if target.context_window_tokens == 0
                || target.max_output_tokens == 0
                || target.max_output_tokens > target.context_window_tokens
            {
                return Err(CatalogError::InvalidTokenLimit(target.model_id.clone()));
            }

            let identity = (
                target.adapter_protocol.clone(),
                target.provider_namespace.clone(),
                target.model_id.clone(),
            );
            if !identities.insert(identity.clone()) || !aliases.insert(identity) {
                return Err(CatalogError::DuplicateModelId(target.model_id.clone()));
            }
            for alias in &target.aliases {
                require_nonempty("alias", alias)?;
                let alias_key = (
                    target.adapter_protocol.clone(),
                    target.provider_namespace.clone(),
                    alias.clone(),
                );
                if !aliases.insert(alias_key) {
                    return Err(CatalogError::DuplicateAlias(alias.clone()));
                }
            }
        }
        Ok(())
    }
}

fn require_nonempty(field: &'static str, value: &str) -> Result<(), CatalogError> {
    if value.trim().is_empty() {
        Err(CatalogError::EmptyField(field))
    } else {
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CatalogError {
    EmptyCatalog,
    EmptyField(&'static str),
    DuplicateModelId(String),
    DuplicateAlias(String),
    InvalidPrice(String),
    InvalidTokenLimit(String),
    VersionMismatch(String),
    MissingRouteContext,
    UnknownRequestedModel,
    IncompatibleTarget,
    ExtraParametersNotObject,
    Serialization(String),
}

impl fmt::Display for CatalogError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyCatalog => formatter.write_str("model catalog is empty"),
            Self::EmptyField(field) => write!(formatter, "model catalog field {field} is empty"),
            Self::DuplicateModelId(model) => write!(formatter, "duplicate model ID {model}"),
            Self::DuplicateAlias(alias) => write!(formatter, "duplicate model alias {alias}"),
            Self::InvalidPrice(model) => write!(formatter, "invalid price for model {model}"),
            Self::InvalidTokenLimit(model) => {
                write!(formatter, "invalid token limit for model {model}")
            }
            Self::VersionMismatch(model) => {
                write!(
                    formatter,
                    "catalog or price version mismatch for model {model}"
                )
            }
            Self::MissingRouteContext => formatter.write_str("request has no route context"),
            Self::UnknownRequestedModel => formatter.write_str("requested model is not cataloged"),
            Self::IncompatibleTarget => formatter.write_str("target is incompatible with request"),
            Self::ExtraParametersNotObject => {
                formatter.write_str("call parameters.extra is not an object")
            }
            Self::Serialization(message) => {
                write!(formatter, "catalog serialization failed: {message}")
            }
        }
    }
}

impl Error for CatalogError {}

pub fn default_model_catalog() -> Result<ModelCatalog, CatalogError> {
    let targets = vec![
        target(
            OPENAI_CHAT_COMPLETIONS_PROTOCOL,
            "openai",
            "gpt-5.4-2026-03-05",
            ModelRevisionKind::ImmutableSnapshot,
            &["gpt-5.4"],
            "openai-gpt-5.4",
            1_050_000,
            128_000,
            OutputTokenParameter::MaxCompletionTokens,
            all_capabilities(true),
            price(2.50, 15.00, Some(0.25), OPENAI_GPT_54_SOURCE),
            OPENAI_GPT_54_SOURCE,
        ),
        target(
            OPENAI_CHAT_COMPLETIONS_PROTOCOL,
            "openai",
            "gpt-5.4-mini-2026-03-17",
            ModelRevisionKind::ImmutableSnapshot,
            &["gpt-5.4-mini"],
            "openai-gpt-5.4",
            400_000,
            128_000,
            OutputTokenParameter::MaxCompletionTokens,
            all_capabilities(true),
            price(0.75, 4.50, Some(0.075), OPENAI_GPT_54_MINI_SOURCE),
            OPENAI_GPT_54_MINI_SOURCE,
        ),
        target(
            OPENAI_CHAT_COMPLETIONS_PROTOCOL,
            "openai",
            "gpt-4o-2024-11-20",
            ModelRevisionKind::ImmutableSnapshot,
            &["gpt-4o"],
            "openai-gpt-4o",
            128_000,
            16_384,
            OutputTokenParameter::MaxTokens,
            all_capabilities(true),
            price(2.50, 10.00, Some(1.25), OPENAI_GPT_4O_SOURCE),
            OPENAI_GPT_4O_SOURCE,
        ),
        target(
            OPENAI_CHAT_COMPLETIONS_PROTOCOL,
            "openai",
            "gpt-4o-mini-2024-07-18",
            ModelRevisionKind::ImmutableSnapshot,
            &["gpt-4o-mini"],
            "openai-gpt-4o",
            128_000,
            16_384,
            OutputTokenParameter::MaxTokens,
            all_capabilities(true),
            price(0.15, 0.60, Some(0.075), OPENAI_GPT_4O_MINI_SOURCE),
            OPENAI_GPT_4O_MINI_SOURCE,
        ),
        target_with_distinct_price_source(
            ANTHROPIC_MESSAGES_PROTOCOL,
            "anthropic",
            "claude-sonnet-4-5-20250929",
            ModelRevisionKind::ImmutableSnapshot,
            &["claude-sonnet-4-5"],
            "anthropic-claude-4.5",
            200_000,
            64_000,
            OutputTokenParameter::MaxTokens,
            all_capabilities(true),
            price(3.00, 15.00, Some(0.30), ANTHROPIC_PRICING_SOURCE),
            ANTHROPIC_MODELS_SOURCE,
        ),
        target_with_distinct_price_source(
            ANTHROPIC_MESSAGES_PROTOCOL,
            "anthropic",
            "claude-haiku-4-5-20251001",
            ModelRevisionKind::ImmutableSnapshot,
            &["claude-haiku-4-5"],
            "anthropic-claude-4.5",
            200_000,
            64_000,
            OutputTokenParameter::MaxTokens,
            all_capabilities(true),
            price(1.00, 5.00, Some(0.10), ANTHROPIC_PRICING_SOURCE),
            ANTHROPIC_MODELS_SOURCE,
        ),
        // LiteLLM preserves provider prefixes on both requested and routed model
        // IDs. These duplicate the underlying provider facts under LiteLLM's
        // distinct adapter protocol and credential namespace; they are not
        // cross-provider routes.
        target(
            LITELLM_COMPLETION_PROTOCOL,
            "openai",
            "openai/gpt-5.4-2026-03-05",
            ModelRevisionKind::ImmutableSnapshot,
            &["openai/gpt-5.4"],
            "litellm-openai-gpt-5.4",
            1_050_000,
            128_000,
            OutputTokenParameter::MaxCompletionTokens,
            all_capabilities(true),
            price(2.50, 15.00, Some(0.25), OPENAI_GPT_54_SOURCE),
            OPENAI_GPT_54_SOURCE,
        ),
        target(
            LITELLM_COMPLETION_PROTOCOL,
            "openai",
            "openai/gpt-5.4-mini-2026-03-17",
            ModelRevisionKind::ImmutableSnapshot,
            &["openai/gpt-5.4-mini"],
            "litellm-openai-gpt-5.4",
            400_000,
            128_000,
            OutputTokenParameter::MaxCompletionTokens,
            all_capabilities(true),
            price(0.75, 4.50, Some(0.075), OPENAI_GPT_54_MINI_SOURCE),
            OPENAI_GPT_54_MINI_SOURCE,
        ),
        target_with_distinct_price_source(
            LITELLM_COMPLETION_PROTOCOL,
            "anthropic",
            "anthropic/claude-sonnet-4-5-20250929",
            ModelRevisionKind::ImmutableSnapshot,
            &["anthropic/claude-sonnet-4-5"],
            "litellm-anthropic-claude-4.5",
            200_000,
            64_000,
            OutputTokenParameter::MaxTokens,
            all_capabilities(true),
            price(3.00, 15.00, Some(0.30), ANTHROPIC_PRICING_SOURCE),
            ANTHROPIC_MODELS_SOURCE,
        ),
        target_with_distinct_price_source(
            LITELLM_COMPLETION_PROTOCOL,
            "anthropic",
            "anthropic/claude-haiku-4-5-20251001",
            ModelRevisionKind::ImmutableSnapshot,
            &["anthropic/claude-haiku-4-5"],
            "litellm-anthropic-claude-4.5",
            200_000,
            64_000,
            OutputTokenParameter::MaxTokens,
            all_capabilities(true),
            price(1.00, 5.00, Some(0.10), ANTHROPIC_PRICING_SOURCE),
            ANTHROPIC_MODELS_SOURCE,
        ),
        // Together exposes no immutable served binary revision, so these
        // versions identify the observed catalog cohort; a catalog refresh
        // starts a cold profile.
        target(
            LITELLM_COMPLETION_PROTOCOL,
            "together_ai",
            "together_ai/zai-org/GLM-5.3",
            ModelRevisionKind::CatalogObservation,
            &[],
            "together-glm-5.3",
            1_048_575,
            128_000,
            OutputTokenParameter::MaxTokens,
            text_tool_capabilities(),
            price(1.40, 4.40, Some(0.26), TOGETHER_MODELS_SOURCE),
            TOGETHER_MODELS_SOURCE,
        ),
        target(
            LITELLM_COMPLETION_PROTOCOL,
            "together_ai",
            "together_ai/zai-org/GLM-5.3-Flash",
            ModelRevisionKind::CatalogObservation,
            &[],
            "together-glm-5.3",
            1_048_575,
            128_000,
            OutputTokenParameter::MaxTokens,
            text_tool_capabilities(),
            price(0.15, 0.50, Some(0.03), TOGETHER_MODELS_SOURCE),
            TOGETHER_MODELS_SOURCE,
        ),
    ];

    ModelCatalog::new(
        DEFAULT_MODEL_CATALOG_VERSION,
        DEFAULT_PRICE_TABLE_VERSION,
        DEFAULT_CATALOG_OBSERVED_AT_UTC,
        targets,
    )
}

#[allow(clippy::too_many_arguments)]
fn target(
    adapter_protocol: &str,
    provider_namespace: &str,
    model_id: &str,
    revision_kind: ModelRevisionKind,
    aliases: &[&str],
    routing_group: &str,
    context_window_tokens: u32,
    max_output_tokens: u32,
    output_token_parameter: OutputTokenParameter,
    capabilities: ModelCapabilities,
    price: ModelPrice,
    source_url: &str,
) -> ModelTarget {
    target_with_distinct_price_source(
        adapter_protocol,
        provider_namespace,
        model_id,
        revision_kind,
        aliases,
        routing_group,
        context_window_tokens,
        max_output_tokens,
        output_token_parameter,
        capabilities,
        price,
        source_url,
    )
}

#[allow(clippy::too_many_arguments)]
fn target_with_distinct_price_source(
    adapter_protocol: &str,
    provider_namespace: &str,
    model_id: &str,
    revision_kind: ModelRevisionKind,
    aliases: &[&str],
    routing_group: &str,
    context_window_tokens: u32,
    max_output_tokens: u32,
    output_token_parameter: OutputTokenParameter,
    capabilities: ModelCapabilities,
    price: ModelPrice,
    source_url: &str,
) -> ModelTarget {
    ModelTarget {
        adapter_protocol: adapter_protocol.to_string(),
        provider_namespace: provider_namespace.to_string(),
        model_id: model_id.to_string(),
        model_version: match revision_kind {
            ModelRevisionKind::ImmutableSnapshot => model_id.to_string(),
            ModelRevisionKind::CatalogObservation => {
                format!("{model_id}@{DEFAULT_MODEL_CATALOG_VERSION}")
            }
        },
        revision_kind,
        aliases: aliases.iter().map(|alias| (*alias).to_string()).collect(),
        routing_group: routing_group.to_string(),
        context_window_tokens,
        max_output_tokens,
        output_token_parameter,
        capabilities,
        price,
        provenance: ModelProvenance {
            catalog_version: DEFAULT_MODEL_CATALOG_VERSION.to_string(),
            source_url: source_url.to_string(),
            observed_at_utc: DEFAULT_CATALOG_OBSERVED_AT_UTC.to_string(),
        },
    }
}

fn price(input: f64, output: f64, cached_input: Option<f64>, source_url: &str) -> ModelPrice {
    ModelPrice {
        input_per_million_tokens_usd: input,
        output_per_million_tokens_usd: output,
        cached_input_per_million_tokens_usd: cached_input,
        table_version: DEFAULT_PRICE_TABLE_VERSION.to_string(),
        source_url: source_url.to_string(),
        observed_at_utc: DEFAULT_CATALOG_OBSERVED_AT_UTC.to_string(),
    }
}

fn all_capabilities(image_input: bool) -> ModelCapabilities {
    ModelCapabilities {
        text_input: true,
        image_input,
        tool_calling: true,
        structured_outputs: true,
        streaming: true,
    }
}

fn text_tool_capabilities() -> ModelCapabilities {
    all_capabilities(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dag::{Message, Parameters, NATIVE_MESSAGES_OPAQUE_KEY};

    fn call(protocol: &str, namespace: &str, model: &str) -> Call {
        Call {
            call_site_id: "site".into(),
            trace_id: [0; 16],
            span_id: [0; 8],
            model: model.into(),
            messages: vec![Message {
                role: "user".into(),
                content: "hello".into(),
            }],
            parameters: Parameters {
                max_output_tokens: Some(512),
                extra: serde_json::json!({
                    ROUTE_CONTEXT_KEY: {
                        "provider_protocol": protocol,
                        "provider_namespace": namespace,
                        "input_tokens_upper_bound": 10,
                        "input_tokens_upper_bound_basis": JSON_UTF8_BYTES_BOUND_V1,
                        "image_input": false,
                        "tool_calling": false,
                        "structured_outputs": false,
                        "streaming": false
                    }
                }),
                ..Parameters::default()
            },
            tools: vec![],
            input_deps: vec![],
            occurrence_ix: 0,
        }
    }

    #[test]
    fn default_catalog_is_valid_and_versioned() {
        let catalog = default_model_catalog().unwrap();
        assert_eq!(catalog.catalog_version, DEFAULT_MODEL_CATALOG_VERSION);
        assert_eq!(catalog.price_table_version, DEFAULT_PRICE_TABLE_VERSION);
        assert!(catalog.targets.iter().all(|target| {
            target.price.source_url.starts_with("https://")
                && target.provenance.source_url.starts_with("https://")
                && target.price.table_version == catalog.price_table_version
                && target.provenance.catalog_version == catalog.catalog_version
        }));
    }

    #[test]
    fn explicit_catalog_json_must_pass_all_catalog_invariants() {
        let catalog = default_model_catalog().unwrap();
        let json = serde_json::to_string(&catalog).unwrap();
        assert_eq!(ModelCatalog::from_json(&json).unwrap(), catalog);
        for invalid in ["{}", "{", r#"{"targets":[]}"#] {
            assert!(ModelCatalog::from_json(invalid).is_err());
        }
        let mut bad = catalog.clone();
        bad.targets[0].price.input_per_million_tokens_usd = -1.0;
        assert!(ModelCatalog::from_json(&serde_json::to_string(&bad).unwrap()).is_err());
        bad = catalog.clone();
        bad.targets.push(bad.targets[0].clone());
        assert!(ModelCatalog::from_json(&serde_json::to_string(&bad).unwrap()).is_err());
        bad = catalog;
        bad.catalog_version = "changed-without-target-provenance".into();
        assert!(ModelCatalog::from_json(&serde_json::to_string(&bad).unwrap()).is_err());
    }

    #[test]
    fn every_supported_adapter_enumerates_two_real_targets() {
        let catalog = default_model_catalog().unwrap();
        for protocol in [
            OPENAI_CHAT_COMPLETIONS_PROTOCOL,
            ANTHROPIC_MESSAGES_PROTOCOL,
            LITELLM_COMPLETION_PROTOCOL,
        ] {
            assert!(
                catalog.targets_for_protocol(protocol).len() >= 2,
                "{protocol}"
            );
        }
    }

    #[test]
    fn aliases_resolve_to_pinned_snapshots() {
        let catalog = default_model_catalog().unwrap();
        assert_eq!(
            catalog
                .resolve(OPENAI_CHAT_COMPLETIONS_PROTOCOL, "openai", "gpt-5.4")
                .unwrap()
                .model_id,
            "gpt-5.4-2026-03-05"
        );
        assert_eq!(
            catalog
                .resolve(ANTHROPIC_MESSAGES_PROTOCOL, "anthropic", "claude-haiku-4-5")
                .unwrap()
                .model_id,
            "claude-haiku-4-5-20251001"
        );
        assert!(catalog
            .resolve(
                OPENAI_CHAT_COMPLETIONS_PROTOCOL,
                "openai",
                "gpt-4o-2024-08-06",
            )
            .is_none());
    }

    #[test]
    fn cheaper_target_is_same_protocol_namespace_and_routing_group() {
        let catalog = default_model_catalog().unwrap();
        let request = call(
            OPENAI_CHAT_COMPLETIONS_PROTOCOL,
            "openai",
            "gpt-5.4-2026-03-05",
        );
        let cheaper = catalog.cheaper_targets(&request);
        assert_eq!(cheaper.len(), 1);
        assert_eq!(cheaper[0].0.model_id, "gpt-5.4-mini-2026-03-17");
        assert!((cheaper[0].1 - 0.30).abs() < f32::EPSILON);
    }

    #[test]
    fn every_supported_adapter_resolves_its_expected_cheaper_target() {
        let catalog = default_model_catalog().unwrap();
        for (protocol, namespace, source, expected) in [
            (
                OPENAI_CHAT_COMPLETIONS_PROTOCOL,
                "openai",
                "gpt-5.4-2026-03-05",
                "gpt-5.4-mini-2026-03-17",
            ),
            (
                ANTHROPIC_MESSAGES_PROTOCOL,
                "anthropic",
                "claude-sonnet-4-5-20250929",
                "claude-haiku-4-5-20251001",
            ),
            (
                LITELLM_COMPLETION_PROTOCOL,
                "openai",
                "openai/gpt-5.4-2026-03-05",
                "openai/gpt-5.4-mini-2026-03-17",
            ),
            (
                LITELLM_COMPLETION_PROTOCOL,
                "anthropic",
                "anthropic/claude-sonnet-4-5-20250929",
                "anthropic/claude-haiku-4-5-20251001",
            ),
            (
                LITELLM_COMPLETION_PROTOCOL,
                "together_ai",
                "together_ai/zai-org/GLM-5.3",
                "together_ai/zai-org/GLM-5.3-Flash",
            ),
        ] {
            let request = call(protocol, namespace, source);
            let cheaper = catalog.cheaper_targets(&request);
            assert_eq!(cheaper.len(), 1, "{protocol}");
            assert_eq!(cheaper[0].0.model_id, expected, "{protocol}");
        }
    }

    #[test]
    fn litellm_routing_stays_within_provider_namespace() {
        let catalog = default_model_catalog().unwrap();
        let request = call(LITELLM_COMPLETION_PROTOCOL, "openai", "openai/gpt-5.4");
        let compatible = catalog.compatible_targets(&request);
        assert_eq!(compatible.len(), 2);
        assert!(compatible
            .iter()
            .all(|target| target.provider_namespace == "openai"));
        assert_eq!(
            catalog.cheaper_targets(&request)[0].0.model_id,
            "openai/gpt-5.4-mini-2026-03-17"
        );
        assert!(compatible
            .iter()
            .all(|target| !target.model_id.starts_with("anthropic/")));
    }

    #[test]
    fn missing_adapter_context_abstains() {
        let mut request = call(OPENAI_CHAT_COMPLETIONS_PROTOCOL, "openai", "gpt-5.4");
        request.parameters.extra = serde_json::Value::Null;
        assert!(default_model_catalog()
            .unwrap()
            .compatible_targets(&request)
            .is_empty());
    }

    #[test]
    fn incomplete_adapter_context_abstains() {
        let mut request = call(OPENAI_CHAT_COMPLETIONS_PROTOCOL, "openai", "gpt-5.4");
        request.parameters.extra[ROUTE_CONTEXT_KEY]
            .as_object_mut()
            .unwrap()
            .remove("structured_outputs");
        assert!(default_model_catalog()
            .unwrap()
            .compatible_targets(&request)
            .is_empty());
    }

    #[test]
    fn target_capabilities_and_context_are_hard_constraints() {
        let catalog = default_model_catalog().unwrap();
        let mut request = call(
            LITELLM_COMPLETION_PROTOCOL,
            "together_ai",
            "together_ai/zai-org/GLM-5.3",
        );
        request.parameters.extra[ROUTE_CONTEXT_KEY]["image_input"] = serde_json::json!(true);
        assert!(catalog.compatible_targets(&request).is_empty());

        let mut huge = call(OPENAI_CHAT_COMPLETIONS_PROTOCOL, "openai", "gpt-5.4");
        huge.parameters.extra[ROUTE_CONTEXT_KEY]["input_tokens_upper_bound"] =
            serde_json::json!(400_000);
        assert!(catalog.cheaper_targets(&huge).is_empty());
    }

    #[test]
    fn transformed_bound_uses_only_verified_ordered_message_deletions() {
        let mut reference = call(OPENAI_CHAT_COMPLETIONS_PROTOCOL, "openai", "gpt-5.4");
        reference.messages = vec![
            Message {
                role: "system".into(),
                content: "x".repeat(150),
            },
            Message {
                role: "user".into(),
                content: "y".repeat(100),
            },
        ];
        reference.parameters.extra[ROUTE_CONTEXT_KEY]["input_tokens_upper_bound"] =
            serde_json::json!(300);

        let mut deletion = reference.clone();
        deletion.messages.remove(0);
        RequestRequirements::apply_transformed_input_bound(&reference, &mut deletion);
        assert_eq!(
            RequestRequirements::from_call(&deletion)
                .unwrap()
                .input_tokens_upper_bound,
            150
        );

        let mut content_change = reference.clone();
        content_change.messages[0].content = "shorter but not extractive".into();
        RequestRequirements::apply_transformed_input_bound(&reference, &mut content_change);
        assert_eq!(
            RequestRequirements::from_call(&content_change)
                .unwrap()
                .input_tokens_upper_bound,
            300
        );

        let mut unknown_basis = reference.clone();
        unknown_basis.parameters.extra[ROUTE_CONTEXT_KEY]
            .as_object_mut()
            .unwrap()
            .remove(INPUT_BOUND_BASIS_KEY);
        unknown_basis.messages.remove(0);
        RequestRequirements::apply_transformed_input_bound(&reference, &mut unknown_basis);
        assert_eq!(
            RequestRequirements::from_call(&unknown_basis)
                .unwrap()
                .input_tokens_upper_bound,
            300
        );
    }

    #[test]
    fn transformed_bound_does_not_shrink_opaque_or_retained_native_content() {
        let mut opaque = call(OPENAI_CHAT_COMPLETIONS_PROTOCOL, "openai", "gpt-5.4");
        opaque.messages = vec![
            Message {
                role: "system".into(),
                content: "x".repeat(150),
            },
            Message {
                role: "user".into(),
                content: "y".repeat(100),
            },
        ];
        opaque.parameters.extra[ROUTE_CONTEXT_KEY]["input_tokens_upper_bound"] =
            serde_json::json!(300);
        opaque.parameters.extra[NATIVE_MESSAGES_OPAQUE_KEY] = serde_json::json!(true);
        let mut opaque_deletion = opaque.clone();
        opaque_deletion.messages.remove(0);
        RequestRequirements::apply_transformed_input_bound(&opaque, &mut opaque_deletion);
        assert_eq!(
            RequestRequirements::from_call(&opaque_deletion)
                .unwrap()
                .input_tokens_upper_bound,
            300
        );

        let mut anthropic = call(
            ANTHROPIC_MESSAGES_PROTOCOL,
            "anthropic",
            "claude-sonnet-4-5",
        );
        anthropic.messages = vec![
            Message {
                role: "system".into(),
                content: "x".repeat(150),
            },
            Message {
                role: "user".into(),
                content: "y".repeat(100),
            },
        ];
        anthropic.parameters.extra[ROUTE_CONTEXT_KEY]["input_tokens_upper_bound"] =
            serde_json::json!(300);
        let mut missing_system = anthropic.clone();
        missing_system.messages.remove(0);
        RequestRequirements::apply_transformed_input_bound(&anthropic, &mut missing_system);
        assert_eq!(
            RequestRequirements::from_call(&missing_system)
                .unwrap()
                .input_tokens_upper_bound,
            300
        );
    }

    #[test]
    fn routed_metadata_preserves_versions_and_output_convention() {
        let catalog = default_model_catalog().unwrap();
        let request = call(OPENAI_CHAT_COMPLETIONS_PROTOCOL, "openai", "gpt-5.4");
        let target = catalog.cheaper_targets(&request)[0].0;
        let metadata = catalog.routed_target(&request, target).unwrap();
        assert_eq!(metadata.requested_model_id, "gpt-5.4");
        assert_eq!(metadata.target_model_id, "gpt-5.4-mini-2026-03-17");
        assert_eq!(
            metadata.output_token_parameter,
            OutputTokenParameter::MaxCompletionTokens
        );
        assert_eq!(metadata.catalog_version, DEFAULT_MODEL_CATALOG_VERSION);
        assert_eq!(metadata.price_table_version, DEFAULT_PRICE_TABLE_VERSION);
    }

    #[test]
    fn retired_together_llama_pair_is_not_dispatchable() {
        let catalog = default_model_catalog().unwrap();
        assert!(catalog
            .targets
            .iter()
            .all(|target| !target.model_id.contains("Meta-Llama-3.1")));
    }
}
