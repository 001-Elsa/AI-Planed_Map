# ADR 0012: Explicit Agent task graph and typed tool contracts

## Status

Accepted

## Context

MAPGO already had isolated Agent roles, message envelopes, shared state,
memory, Critic review and HITL. The remaining gap was that execution still
looked like a single controller loop: tool arguments were untyped dictionaries,
handoffs were only implicit in messages, and long-running Worker events relied
on a fixed lock TTL.

## Decision

Add an explicit persisted task graph on top of existing workflow runs:

- `agent_workflow_runs`: workflow lifecycle and budget summary.
- `agent_workflow_tasks`: DAG task nodes with dependencies, role, status,
  attempts, input artifact refs, output artifact type, budget and version.
- `agent_handoffs`: durable edges generated from AgentMessage delivery.
- `agent_artifacts`: versioned artifacts with `active/stale` status and
  optional plan-version association.

OR-Tools and Beam Search remain deterministic tools owned by the Planner role.
They are not promoted to Agents.

Add typed tool contracts:

- each tool has a Pydantic argument model;
- Controller validates arguments before policy/execution;
- tool results use a stable envelope with success, error code, retryability,
  source, expiry, confidence and artifact reference;
- upstream exception text is not returned to the model.

Add Companion context building:

- model context is built from compact plan snapshot, current observation,
  confirmed preference keys and recent tool results;
- Agents exchange artifact refs and summaries rather than full transcripts.

Add Worker lock renewal:

- runtime stores support token-bound `renew_lock`;
- Worker keeps a lease heartbeat while processing trip events.

## Consequences

The system can now explain and persist both "what happened" and "why the next
Agent was allowed to run." It also has clearer reliability and safety gates:
role-specific tool schemas, separated budgets, stable tool errors, and stale
artifact invalidation after formal plan version changes.
