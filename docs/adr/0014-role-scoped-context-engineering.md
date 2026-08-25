# ADR 0014: Role-scoped Context Engineering

## Status

Accepted.

## Decision

Keep versioned Shared State as the workflow source of truth, but never expose it
or a conversation transcript directly to an Agent. A centralized Context
Builder projects the authorized Shared State view and validated hand-off
artifacts into strict, role-specific models.

- `PlanningContext` contains structured intent, verified candidate groups, an
  optional safety report, hard constraints, soft preferences, origin, and the
  current state revision/hash. Search retry internals and conversation history
  are excluded.
- `CriticContext` contains the structured original requirement, formal plan,
  constraint evidence, stable tool summaries, and the current state
  revision/hash. It cannot see candidate search state or mutable execution
  state.
- Companion retains a bounded trip snapshot and recent minimized tool results;
  it does not receive planning history or long-term memory values.

Every context build validates the task id, Shared State revision/hash, role
field allow-list, and relevant Artifact content hash. A stale or tampered
handoff fails before Agent execution. Provider-controlled POI text is marked as
untrusted data; instruction-like text is redacted only in model-facing Critic
payloads, while the immutable formal plan remains unchanged for deterministic
validation and audit.

## Boundary model

Isolation is enforced at four independent layers:

1. Tool: the registry authorizes capabilities by role and invocation mode.
2. State: role-scoped read/write matrices plus revisioned compare-and-set.
3. Context: typed minimal projections with a bounded model serialization.
4. Message: allow-listed routes, schemas, hashes, idempotency, and causality.

## Consequences

Deterministic route solving still receives the coordinates and time-window data
it requires. This is intentionally different from token minimization for an LLM:
structured algorithm input is not conversation context. Critic model input is
bounded to 16,000 characters and drops lower-priority candidate reviews before
rejecting an oversized context; hard constraints and the selected plan are
never silently truncated.
