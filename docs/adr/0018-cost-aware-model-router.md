# ADR 0018: Cost-aware Model Router

## Status

Accepted.

## Context

MapGo previously configured one `LLM_MODEL`. Intent parsing, Critic review and
Companion decisions therefore selected the same model even though their
accuracy, latency and cost requirements differ. Supervisor, Search, Safety,
Planner and Replanner were already deterministic; sending those roles through
an LLM would add cost and nondeterminism without adding authority.

## Decision

Introduce a framework-independent `ModelRouter` with four execution tiers:

- `rule`: local deterministic parsing or review;
- `small`: bounded structured output and Companion tool selection;
- `strong`: complex/uncertain Intent parsing and high-risk Critic review;
- `deterministic`: Supervisor scheduling, Search query construction, Safety
  rules, Planner route optimization and Replanner strategy selection.

Routing inputs are strongly typed and limited to task count, hard-constraint
count, uncertainty count, text length, risk, model availability, remaining
budget and failure count. The router never receives tool credentials, precise
location or full conversation history.

The production policy is:

| Role | Route |
|---|---|
| Intent | simple Rule; bounded Small; complex/uncertain Strong; Rule fallback |
| Supervisor | Deterministic |
| Search | Deterministic query builder + provider Tool |
| Safety | Deterministic rules |
| Planner | Deterministic route matrix + OR-Tools/Beam Search |
| Critic | Rule/Strong hybrid |
| Companion | Rule/Small only |
| Replanner | Deterministic strategy |

High risk sets `requires_hitl`; it does not grant a stronger model permission to
write a plan. Existing Tool Registry, Agent Runtime policy and PlanPatch CAS
remain the authority boundaries.

Each decision emits bounded-cardinality metrics for Agent role, tier, risk and
complexity. Intent and Critic traces include the route reason; Companion stores
the selected tier/model in `AgentRun.model_name`. Small and Strong tiers have
separate input/output prices so runtime budget enforcement reflects the actual
route.

`LLM_MODEL` remains a compatibility fallback. Deployments should configure
`LLM_SMALL_MODEL` and `LLM_STRONG_MODEL` explicitly. Missing credentials and
model failures fail closed to Rule execution.

## Consequences

Simple requests can avoid network model calls entirely. Complex requests spend
more only where structured reasoning can improve quality, while deterministic
solvers retain hard-constraint ownership. Search query expansion remains
deterministic until evaluation demonstrates that an LLM expansion improves
provider-backed recall enough to justify its cost and injection surface.
