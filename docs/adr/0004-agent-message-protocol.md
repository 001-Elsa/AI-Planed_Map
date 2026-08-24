# ADR-0004: Unified Agent Message Protocol

- Status: Accepted
- Date: 2026-08-21

## Context

Typed artifacts described outputs, but they did not define delivery semantics. Planning hand-offs were implicit function-call order, while Companion messages used a separate role/content table shape. This made routing authority, causality, deduplication, and cross-workflow debugging incomplete.

## Decision

All Agent and controlled-stage hand-offs use protocol version `1.0`. A message contains stable sender/receiver endpoints, task and causal identifiers, a typed artifact name, structured content, a content hash, and an idempotency key.

An in-process `AgentMessageRouter` authorizes exact sender/receiver/message/artifact tuples. Routes fail closed. The protocol permits Critic to return only a review to Supervisor and permits Companion to communicate only with its event source, tool runtime, and final-answer sink.

Runtime content is validated at the receiving boundary. Durable and API audit forms contain only minimized summaries; raw user text, secrets, and precise coordinates are not copied into Agent message audit records.

## Consequences

- Planning and in-trip workflows share one communication model and one audit table.
- Correlation and causation chains can reconstruct a task across Agent boundaries.
- Duplicate delivery is detectable without executing a second Agent hand-off.
- Adding a new Agent requires an explicit endpoint, message schema, and route policy.
- Schema revision `0011` is required.

