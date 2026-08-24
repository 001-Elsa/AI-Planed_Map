# ADR-0010: Supervisor Dynamic Agent Planning

- Status: Accepted
- Date: 2026-08-21

## Context

A mature multi-agent workflow should not hard-code the same route for every request. A normal sightseeing trip and an elderly/accessibility-sensitive trip need different checks, but the extra checks must not weaken isolation or give the Supervisor broad tool access.

## Decision

Intent now returns structured intent to Supervisor, not directly to Search. Supervisor then emits a typed `AgentExecutionPlan` and dispatches the next stage.

Standard trip:

```text
Supervisor -> Intent -> Supervisor Plan
  -> Search -> Planner -> Critic -> Final Answer
```

Safety-sensitive trip:

```text
Supervisor -> Intent -> Supervisor Plan
  -> Search -> Safety Check -> Planner -> Critic -> Final Answer
```

Safety-sensitive triggers include elderly travelers, wheelchair/accessibility requirements, explicit walking limits, `minimize_walking`, and `travel_style=relaxed`.

`SafetyAgent` is deterministic and has no model-selectable tools. It can only run the internal `check_travel_safety` capability, read structured intent plus provider-returned candidates, and output a `SafetyCheckReport`. It cannot generate POIs, call maps, optimize routes, mutate hard constraints, or write a formal plan.

## Consequences

- The old `Intent -> Search` protocol route is removed; Search can only receive an intent artifact from Supervisor after planning.
- Planner consumes either `search_artifact` for standard trips or `safety_report` for safety-sensitive trips.
- Shared State gets a `safety_ready` phase and a `safety_checked` action written only by `AgentType.safety`.
- `MAX_AGENT_HANDOFFS` defaults to 12 so the extra Supervisor planning step and one Critic soft retry still fit the normal budget.
- Future optional gates, such as budget review or opening-hours verification, can be added as plan steps without exposing cross-role tools.
