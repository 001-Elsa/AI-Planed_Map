# ADR-0005: Versioned Agent Shared State

- Status: Accepted
- Date: 2026-08-21

## Context

AgentMessage defines delivery, but copying complete prior context through every message creates duplication and stale-data risk. Search must see effective user preferences, Planner must see verified candidates, Critic must see the exact plan, and Companion must retain context across trip events.

## Decision

Introduce `AgentSharedState` as the task-scoped source of current facts. The state contains user requirements, candidates, route plan, review result, bounded soft adjustments, execution context, and append-only change metadata.

The full state lives in `RuntimeStore` with a configurable TTL. Redis uses an atomic Lua compare-and-set on `revision`; the in-memory implementation provides the same behavior under a lock. Every Agent receives a role-scoped view and can update only its owned fields. Messages carry a state reference and revision so delivery and state are causally linked.

At workflow completion, PostgreSQL stores only an `AgentSharedStateSnapshot` audit summary and state hash. Existing `UserPreference`, `PlanningRun`, `PlanVersion`, and `TripEvent` remain the systems of record for confirmed long-term preferences, formal plans, and travel history.

## Consequences

- Agents reuse verified context instead of independently reconstructing it.
- Concurrent stale writes fail rather than silently losing an update.
- Exact coordinates and complete candidates remain temporary and expire from Redis.
- Adding a state field requires explicit read/write policy and transition tests.
- Schema revision `0012` is required.

