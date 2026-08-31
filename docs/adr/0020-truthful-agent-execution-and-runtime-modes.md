# ADR-0020: Truthful Agent Execution and Runtime Modes

## Status

Accepted

## Context

The dynamic replanning trace used to label orchestration and deterministic work as Agent executions. That made the system look more multi-agent than the runtime actually was, and it made the persisted workflow impossible to replay from the Supervisor's original dependency graph.

The repository already has a validated message protocol and a recoverable Redis Streams transport. The missing link was an explicit execution contract that says which work is an Agent role and which work is a deterministic stage.

## Decision

1. `AgentPlanStep.execution_kind` is either `agent` or `stage`.
2. Dynamic replanning persists the exact `AgentExecutionPlan`, including stable `step_id`, `depends_on`, status, attempt count, budget and compact summaries.
3. Deterministic event ingestion, patch solving, patch review and finalization are stages. They do not create `AgentRun` records.
4. Replanner is the only dynamic role that creates an `AgentRun`. In `sync` mode the workflow owner invokes it directly. In `distributed` mode the owner publishes a typed command to the Replanner stream and a real `AgentTaskWorker` returns a causally linked typed result.
5. The Planner and Critic responsibilities remain deterministic stages in this workflow until they have an independent model-backed role owner. This is intentional: the trace must describe deployed behavior, not aspirational architecture.

## Runtime contract

```text
sync:
  workflow owner -> ReplannerAgent.run()

distributed:
  workflow owner -> Redis Stream(replanner)
  Replanner AgentTaskWorker -> Redis Stream(planner)
  workflow owner -> deterministic stages -> pending PlanPatch
```

Distributed mode requires `AGENT_EXECUTION_MODE=distributed`, `AGENT_MESSAGE_TRANSPORT=redis_stream`, and a reachable `REDIS_URL`. The default remains `sync` for local development and SQLite tests.

## Consequences

- Workflow audit rows can reconstruct the Supervisor DAG without guessing dependencies from trace order.
- Stage latency and hashes remain observable without inflating Agent counts.
- Redis delivery semantics are testable independently from database persistence.
- Adding another true Agent requires a role handler, protocol route, worker ownership, and a focused integration test; adding a solver or validator does not require pretending it is an Agent.
