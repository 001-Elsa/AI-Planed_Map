# ADR 0015: Recoverable Agent Message Transport

## Status

Accepted.

## Context

`AgentMessageRouter` already defines the communication allow-list and validates
content hashes, idempotency keys, causal identifiers and Artifact payloads. Its
delivery record was a process-local dictionary, so a process restart lost
deduplication and an Agent hand-off could not be claimed by another worker.

Search and Planner are now executable Agents, making transport separation useful.
The existing synchronous planning API must not publish a second copy of the same
workflow to distributed workers until the distributed workflow runner owns that
workflow; otherwise API and Worker processes could both execute it.

## Decision

Separate protocol validation from transport:

- `AgentMessageRouter.validate()` remains the single authorization and schema
  boundary. Its existing `deliver()` method remains a synchronous development
  adapter with local deduplication.
- `InMemoryAgentMessageTransport` implements publish, claim, ACK, retry, pending
  recovery and DLQ semantics for local development and tests.
- `RedisStreamAgentMessageTransport` uses one Stream and consumer group per
  receiver role. Publishing uses an atomic Lua operation for durable idempotency
  (`SET NX EX`) and `XADD`. Workers use `XREADGROUP`, `XACK`, `XPENDING` and
  `XAUTOCLAIM`; retry exhaustion and excessive crash deliveries go to a role DLQ.
- `RecoverableAgentMessageBus` validates on both publish and receive.
- `AgentTaskWorker` runs one bounded task, publishes validated output hand-offs,
  ACKs only after successful handling, converts exceptions to stable error codes,
  and retries or dead-letters without storing upstream exception text.

Database workflow/task/artifact records and versioned Shared State remain the
facts of record. Redis Streams are reliable notification and hand-off delivery,
not a replacement for workflow state.

## Configuration

`AGENT_MESSAGE_TRANSPORT=auto` selects Redis Streams when the configured runtime
store is Redis and otherwise selects memory. Explicit `memory` and `redis_stream`
modes are available. Stream prefix, consumer-group prefix, retention length,
attempt limit and reclaim idle time are separately configurable.

The application creates the message bus at startup, but the current synchronous
planning endpoint does not mirror messages into it. Independently deployed role
workers must use the bus as the sole owner of a distributed workflow execution.

## Failure semantics

- Duplicate publish: returns `duplicate`; no second Stream entry is created.
- Handler failure: ACK and retry/DLQ insertion are executed in one Redis
  transaction.
- Worker crash before ACK: the entry remains in the Pending Entries List and is
  reclaimed after the idle threshold.
- Repeated worker crashes: Redis delivery count is checked and the poison message
  is dead-lettered after the configured limit.
- Invalid or tampered message: protocol validation fails before Agent code runs.
