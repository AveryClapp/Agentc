//! Original SDK token categories for scoped tariff evidence, never a bill.

use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};

pub const VERSION: &str = "provider-billing-usage-v1";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BillingUsage {
    pub version: String,
    pub usage: Usage,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "provider_protocol", deny_unknown_fields)]
pub enum Usage {
    #[serde(rename = "openai.chat.completions.v1")]
    Openai {
        input_tokens: u32,
        output_tokens: u32,
        total_tokens: Option<u32>,
        input_details: Option<InputDetails>,
        output_details: Option<OutputDetails>,
        service_tier: Option<String>,
        unrecognized_fields: bool,
    },
    #[serde(rename = "anthropic.messages.v1")]
    Anthropic {
        input_tokens: u32,
        output_tokens: u32,
        cache_creation_input_tokens: Option<u32>,
        cache_read_input_tokens: Option<u32>,
        cache_creation: Option<CacheCreation>,
        server_tool_use: Option<ServerToolUse>,
        service_tier: Option<String>,
        inference_geo: Option<String>,
        unrecognized_fields: bool,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InputDetails {
    pub cached_tokens: Option<u32>,
    pub cache_write_tokens: Option<u32>,
    pub audio_tokens: Option<u32>,
    pub image_tokens: Option<u32>,
    pub text_tokens: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutputDetails {
    pub accepted_prediction_tokens: Option<u32>,
    pub rejected_prediction_tokens: Option<u32>,
    pub audio_tokens: Option<u32>,
    pub reasoning_tokens: Option<u32>,
    pub text_tokens: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CacheCreation {
    pub ephemeral_5m_input_tokens: Option<u32>,
    pub ephemeral_1h_input_tokens: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerToolUse {
    pub web_search_requests: Option<u32>,
    pub web_fetch_requests: Option<u32>,
}

impl BillingUsage {
    /// Bind projection counts and protocol to the original terminal receipt.
    /// Missing and unrecognized categories remain evidence of uncertainty.
    pub fn validate(&self, protocol: &str, input: Option<u32>, output: Option<u32>) -> Result<()> {
        if self.version != VERSION {
            bail!("provider billing usage version is unsupported");
        }
        let (expected_protocol, observed_input, observed_output) = match &self.usage {
            Usage::Openai {
                input_tokens,
                output_tokens,
                total_tokens,
                input_details,
                output_details,
                service_tier,
                ..
            } => {
                if total_tokens.is_some_and(|total| {
                    u64::from(total) != u64::from(*input_tokens) + u64::from(*output_tokens)
                }) {
                    bail!("provider billing usage total conflicts with token counts");
                }
                if let Some(details) = input_details {
                    within(
                        *input_tokens,
                        &[
                            details.cached_tokens,
                            details.cache_write_tokens,
                            details.audio_tokens,
                            details.image_tokens,
                            details.text_tokens,
                        ],
                    )?;
                }
                if let Some(details) = output_details {
                    within(
                        *output_tokens,
                        &[
                            details.accepted_prediction_tokens,
                            details.rejected_prediction_tokens,
                            details.audio_tokens,
                            details.reasoning_tokens,
                            details.text_tokens,
                        ],
                    )?;
                }
                label(
                    service_tier,
                    &["auto", "default", "flex", "scale", "priority", "fast"],
                )?;
                ("openai.chat.completions.v1", *input_tokens, *output_tokens)
            }
            Usage::Anthropic {
                input_tokens,
                output_tokens,
                cache_creation_input_tokens,
                cache_creation,
                service_tier,
                inference_geo,
                ..
            } => {
                if let (Some(total), Some(details)) = (cache_creation_input_tokens, cache_creation)
                {
                    let counts = [
                        details.ephemeral_5m_input_tokens,
                        details.ephemeral_1h_input_tokens,
                    ];
                    let sum: u64 = counts.iter().flatten().map(|n| u64::from(*n)).sum();
                    if sum > u64::from(*total)
                        || counts.iter().all(Option::is_some) && sum != u64::from(*total)
                    {
                        bail!("provider billing cache creation split conflicts with total");
                    }
                }
                label(service_tier, &["standard", "priority", "batch"])?;
                label(inference_geo, &["global", "us"])?;
                ("anthropic.messages.v1", *input_tokens, *output_tokens)
            }
        };
        if protocol != expected_protocol
            || input != Some(observed_input)
            || output != Some(observed_output)
        {
            bail!("provider billing usage conflicts with original SDK receipt");
        }
        Ok(())
    }
}

fn within(total: u32, parts: &[Option<u32>]) -> Result<()> {
    if parts.iter().flatten().any(|n| *n > total) {
        bail!("provider billing token category exceeds its containing total");
    }
    Ok(())
}

fn label(value: &Option<String>, allowed: &[&str]) -> Result<()> {
    if value
        .as_ref()
        .is_some_and(|v| !allowed.contains(&v.as_str()))
    {
        bail!("provider billing usage label is unsupported");
    }
    Ok(())
}
