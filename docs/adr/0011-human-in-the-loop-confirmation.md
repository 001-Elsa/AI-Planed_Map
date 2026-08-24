# ADR 0011: Human-in-the-loop confirmation gates

## Status

Accepted

## Context

MAPGO multi-agent planning can now parse intent, recall POIs, solve routes,
review with Critic, and recover from provider failures. Some feasible plans are
still too impactful to finalize automatically: long walking routes for older
travelers, high expected cost, or other strong user-constraint situations.

## Decision

Add Human-in-the-loop (HITL) as a confirmation gate after deterministic solving
and Critic review, before a formal `PlanVersion` is persisted.

HITL reuses the existing `need_clarification` conversation protocol:

- `ClarificationQuestion.kind="confirmation"` marks a yes/no confirmation.
- `AIPlanRequest.human_confirmations` stores accepted confirmations so the same
  gate is not asked again on a retry.
- A rejection is translated into structured planning input, then the same
  audited workflow runs again.

Initial gates:

- `walking_distance`: walking-mode plans above 8 km require confirmation; elderly,
  children, wheelchair, relaxed style, or minimize-walking requests use a 6 km
  threshold.
- `estimated_cost`: plans with estimated POI cost above 1000 yuan and no explicit
  budget require confirmation.

## Safety boundaries

- HITL does not grant tools to Intent, Critic, or Companion.
- HITL does not let Critic mutate hard constraints.
- Rejection is not applied as free text alone; it writes structured constraints
  such as `max_walking_meters` or `max_total_cost_yuan`.
- Formal plan persistence remains blocked while status is `need_clarification`.

## Consequences

Users can stop high-impact plans before they become executable artifacts. The
system keeps an auditable path from user confirmation to regenerated plan, while
preserving existing Agent isolation and versioning.
