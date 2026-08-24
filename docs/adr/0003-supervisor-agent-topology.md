# ADR-0003: Supervisor Agent Topology

- Status: Accepted
- Date: 2026-08-21

## Context

MapGo v7 already isolated Intent, Critic, and Companion roles. The next architecture step is to make workflow ownership explicit: task decomposition, role scheduling, state tracking, and recovery should not be hidden inside the planning service.

## Decision

Add a `Supervisor Agent` with no tool permissions. The planning workflow is now dynamically scheduled by Supervisor after Intent returns structured requirements. The standard workflow is:

```text
Supervisor
  -> Intent
  -> Supervisor Plan
  -> Search
  -> Planner
  -> Critic
  -> Final Answer
```

`Search` and `Planner` are supervised deterministic stages, not free-form LLM tool users. Search is the server-side Provider recall stage; Planner is the deterministic route/candidate solver stage. Both emit typed artifacts and audit records, but do not receive arbitrary tool-call authority.

Companion remains the separate in-trip event agent for off-route, delay, closure, and weather changes.

Safety-sensitive requests, such as elderly or accessibility-constrained trips, insert a deterministic `Safety Check` stage between Search and Planner. The Safety stage cannot call maps, generate POIs, optimize routes, or mutate hard constraints.

## Consequences

- Agent workflow traces now include `supervisor`, `intent`, `search`, `planner`, `critic`, and final supervisor handoff records.
- `MAX_AGENT_HANDOFFS` defaults to 12 so the dynamic Supervisor planning step and one Critic soft retry can fit the full topology.
- The Supervisor records schedule, final answer, and recovery artifacts.
- Search/Planner evidence is visible in Agent audit tables without expanding the LLM permission surface.
- Failure recovery is handled by Supervisor decisions, not by cross-agent tool access. Search now supports bounded retry, stage timeout, and provider-verified cache fallback before returning clarification.
- Intent can no longer hand off directly to Search; every post-intent planning route must pass through Supervisor's typed execution plan.
