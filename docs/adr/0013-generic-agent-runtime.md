# ADR 0013: Generic Agent Runtime

## Status

Accepted

## Context

The former `AgentController` already provided a bounded, auditable model/tool
loop, but its constructor, context builder, TripState policy, message endpoints
and shared-state actions were hard-coded to Companion. Other roles could define
an `AgentSpec`, but could not execute through the same runtime boundary.

## Decision

Introduce a framework-independent `AgentRuntime` whose behavior is driven by
`AgentSpec` and injected ports:

- `load_context`: selects the role-scoped context view;
- `authorize_tool`: combines Tool Registry authorization with a domain policy;
- `call_model`: supports both legacy deciders and spec-aware model callers;
- `execute_tool`: validates typed arguments and emits stable result envelopes;
- `validate_artifact`: enforces producer and output-artifact contracts;
- `update_shared_state`: delegates role-specific state transitions;
- `emit_trace`: produces minimized lifecycle, decision and tool events.

The Runtime owns step, token, cost and tool-call budgets. A claimed tool in an
`AgentSpec` does not grant authority: Tool Registry authorization remains the
fail-closed source of truth.

`AgentController` remains as a compatibility facade. Its `run_once()` method is
the Companion adapter and supplies Companion context, TripState/consent policy,
shared-state transitions and durable SQL trace persistence. Its generic
`execute()` method delegates directly to `AgentRuntime`.

The LLM-driven tool loop is currently active only for Companion. Search,
Safety and Planner remain deterministic executable roles; in particular,
OR-Tools and Beam Search are not converted into model-driven Agents merely to
fit the Runtime abstraction.

## Consequences

New model-driven roles can reuse the same isolation and audit kernel without a
third-party multi-Agent framework. Role-specific business policy stays outside
the kernel, Companion behavior remains backward compatible, and deterministic
planning stages retain their stronger reliability boundary.
