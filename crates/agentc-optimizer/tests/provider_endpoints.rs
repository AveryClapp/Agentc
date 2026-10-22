use agentc_optimizer::dag::Call;
use agentc_optimizer::model_catalog::{default_model_catalog, RequestRequirements};
use serde_json::{json, Value};

fn request(namespace: &str, endpoint: &str, model: &str) -> Call {
    serde_json::from_value(json!({"call_site_id":"endpoint-fixture","trace_id":"0".repeat(32),"span_id":"0".repeat(16),"model":model,
        "messages":[{"role":"user","content":"private"}],"tools":[],"input_deps":[],"occurrence_ix":0,
        "parameters":{"max_output_tokens":16,"extra":{"agentc_route_context":{
            "provider_protocol":"openai.chat.completions.v1","provider_namespace":namespace,
            "input_tokens_upper_bound":10,"image_input":false,"tool_calling":false,"structured_outputs":false,"streaming":false,
            "provider_endpoint":{"version":"provider-endpoint-v1","namespace":namespace,"supported":true,
                "sha256":agentc_core::storage::content_hash(&format!("billing-scope-v1\0{endpoint}"))}
        }}}})).unwrap()
}

#[test]
fn router_routes_use_the_router_namespace_and_never_direct_openai_targets() {
    let catalog = default_model_catalog().unwrap();
    let source = request(
        "openrouter",
        "https://openrouter.ai/api/v1",
        "openai/gpt-4o",
    );
    let candidates = catalog.cheaper_targets(&source);
    assert_eq!(candidates.len(), 1);
    assert_eq!(candidates[0].0.model_id, "openai/gpt-4o-mini");
    assert!(candidates
        .iter()
        .all(|(target, _)| target.provider_namespace == "openrouter"));
    assert!(catalog
        .cheaper_targets(&request(
            "openai",
            "https://api.openai.com/v1",
            "openai/gpt-4o"
        ))
        .is_empty());
    assert!(catalog
        .cheaper_targets(&request(
            "openrouter",
            "https://openrouter.ai/api/v1",
            "gpt-4o"
        ))
        .is_empty());
}

#[test]
fn router_namespace_requires_the_actual_endpoint_digest_and_protocol() {
    let mut source = request("openrouter", "https://api.openai.com/v1", "openai/gpt-4o");
    assert!(RequestRequirements::from_call(&source).is_none());
    source = request(
        "openrouter",
        "https://openrouter.ai/api/v1",
        "openai/gpt-4o",
    );
    assert!(RequestRequirements::from_call(&source).is_some());
    source.parameters.extra["agentc_route_context"]["provider_protocol"] =
        json!("anthropic.messages.v1");
    assert!(RequestRequirements::from_call(&source).is_none());
}

#[test]
fn router_declaration_is_required_and_malformed_endpoint_is_not_legacy_absence() {
    let mut source = request(
        "openrouter",
        "https://openrouter.ai/api/v1",
        "openai/gpt-4o",
    );
    source.parameters.extra["agentc_route_context"]
        .as_object_mut()
        .unwrap()
        .remove("provider_endpoint");
    assert!(RequestRequirements::from_call(&source).is_none());
    source.parameters.extra["agentc_route_context"]["provider_endpoint"] = Value::Null;
    assert!(RequestRequirements::from_call(&source).is_none());
}

#[test]
fn unknown_endpoint_is_retained_as_a_separate_accounting_namespace() {
    let endpoint = "https://private.invalid/v1";
    let digest = agentc_core::storage::content_hash(&format!("billing-scope-v1\0{endpoint}"));
    let namespace = format!("unqualified:{digest}");
    let mut source = request(&namespace, endpoint, "gpt-4o");
    source.parameters.extra["agentc_route_context"]["provider_endpoint"]["supported"] =
        json!(false);
    let requirements = RequestRequirements::from_call(&source).unwrap();
    assert_eq!(requirements.provider_namespace, namespace);
    assert!(default_model_catalog()
        .unwrap()
        .compatible_targets(&source)
        .is_empty());
    source.parameters.extra["agentc_route_context"]["provider_endpoint"]["namespace"] =
        json!("openai");
    assert!(RequestRequirements::from_call(&source).is_none());
}
