# ADR-0009: Agent Failure Recovery

- Status: Accepted
- Date: 2026-08-21

## Context

A real multi-agent workflow cannot treat an upstream outage as a whole-task crash. In MapGo, POI recall is the clearest case: if the map API times out, Supervisor should record the failure, decide whether retry/fallback is safe, and allow the planning workflow to continue when evidence is still valid.

## Decision

Add a typed `AgentRecoveryDecision` emitted by Supervisor for recoverable stage failures. The recovery order is:

```text
stage failure
  -> Supervisor recovery_event
  -> retry while bounded attempts remain
  -> use provider-verified cache when available
  -> otherwise continue to explicit clarification
```

Search recovery is implemented first because POI recall is the highest-frequency upstream dependency. Provider-level retry/circuit-breaker behavior remains inside the map provider. Agent-level recovery wraps the stage with:

- `AGENT_STAGE_TIMEOUT_SECONDS`: stage budget enforced with `asyncio.wait_for`;
- `AGENT_SEARCH_MAX_ATTEMPTS`: bounded sequential retry after failed parallel recall;
- provider-verified POI recovery cache: only previous map-provider results are reused, with lowered confidence and `cache:` source prefixes.

Supervisor has no map/search/solver tool authority. It only emits the recovery decision and records it in Shared State `execution_context.last_recovery`. Search remains the only role that can write `poi_candidates`.

## Consequences

- A transient POI outage can recover to a valid route if verified cached candidates exist.
- If no safe fallback exists, the result becomes `need_clarification` instead of an uncaught workflow crash.
- Recovery events are visible in `AgentWorkflowTrace` and Prometheus through `mapgo_agent_recovery_total{stage,action}`.
- Cached POIs are never treated as real-time evidence: confidence is capped and the source is marked.
- This is an in-process recovery cache, not durable long-term memory. Redis-backed shared state still owns current task context; PostgreSQL remains reserved for durable, explicitly authorized records.
