# Agentc Optimization Runtime

Agentc observes repeated LLM call sites and chooses guarded execution plans that preserve the application's request contract while reducing billed cost or latency.

## Language

**Call site**:
A stable semantic location in an agent program that issues an LLM request. Repeated invocations at that location may carry different user content.
_Avoid_: Endpoint, prompt

**Call-site version**:
The content-free identity of a call site under one request-template shape, provider protocol, tool schema, and relevant application configuration. Any constituent change creates a cold version with no inherited evidence.
_Avoid_: Call-site ID, prompt hash

**Reference plan**:
The unchanged provider request supplied by the application. It is the result returned whenever no alternative is admissible or optimized dispatch fails.
_Avoid_: Default model, control rule

**Execution plan**:
A complete choice of target model, ordered semantic rewrites, cache behavior, output budget, and validation policy for one request.
_Avoid_: Rule, route

**Semantic rewrite**:
A declared transformation of an LLM request whose safety must be evaluated as part of its complete execution plan.
_Avoid_: Prompt tweak

**Plan profile**:
A bounded empirical history for exactly one call-site version and execution plan. Evidence from constituent routes or rewrites is never combined to fabricate a joint plan profile.
_Avoid_: Capability score, call-site aggregate

**Paired observation**:
A complete-plan/reference comparison linked to the exact plan execution for the same logical request. Paired observations occupy a safety window independent of ordinary execution outcomes.
_Avoid_: Ordinary observation

**Divergence exposure**:
The accumulated amount by which sampled output divergence exceeds a plan's calibrated threshold. It is an online safety proxy, not observed task damage.
_Avoid_: Accuracy loss, task damage
