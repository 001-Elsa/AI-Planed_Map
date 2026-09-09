# Agent Runtime Observability

MapGo exposes process-local Prometheus metrics and propagates OpenTelemetry context across the
distributed Agent boundary. Metrics are not proxied through the API process because that would
hide crashed or partitioned workers.

## Process endpoints

| Process | Compose endpoint | Prometheus job |
|---|---|---|
| API | `api:3000/metrics` | `mapgo-api` |
| Event worker | `worker:9100/metrics` | `mapgo-event-worker` |
| Planner worker | `agent-planner:9100/metrics` | `mapgo-agent-workers` |
| Critic worker | `agent-critic:9100/metrics` | `mapgo-agent-workers` |
| Replanner worker | `agent-replanner:9100/metrics` | `mapgo-agent-workers` |

Every container has its own network namespace, so role workers can all bind port `9100`. Set
`METRICS_PORT` separately when running multiple workers directly on one host.

## Agent metrics

- `mapgo_agent_task_duration_ms`: task handler latency by role.
- `mapgo_agent_task_queue_delay_ms`: message creation-to-claim delay by role.
- `mapgo_agent_task_attempt_count`: observed Redis delivery attempt count.
- `mapgo_agent_task_results_total`: ACK, retry, and DLQ outcomes by role.
- `mapgo_agent_pending_messages`: current Redis PEL size by role.
- `mapgo_agent_oldest_pending_age_ms`: age of the oldest pending delivery by role.
- `mapgo_agent_dlq_messages`: current dead-letter stream size by role.
- `mapgo_agent_reclaim_age_ms`: idle age when a worker reclaims a delivery.
- `mapgo_agent_workflow_node_transitions_total`: planning DAG node status transitions.

Prometheus loads `infrastructure/agent-alerts.yml`. The rules cover missing worker targets,
sustained pending backlog, stale pending messages, non-empty DLQs, and elevated reclaim rate.
Grafana provisions the Agent task panels from `mapgo-ai-planned.json`.

## Distributed traces

The API creates a server span and injects W3C `traceparent`, `tracestate`, and `baggage` into the
typed Agent message envelope or event payload. Redis commands are instrumented. A worker extracts
the carrier and creates a consumer span before invoking the role handler; SQLAlchemy and HTTPX
spans remain children of that consumer span. Message attributes contain bounded IDs and role
names, never prompts, coordinates, tool arguments, credentials, or raw payloads.

Configure an OTLP HTTP destination with the full trace path:

```powershell
$env:OTEL_EXPORTER_OTLP_ENDPOINT="http://jaeger:4318/v1/traces"
$env:OTEL_TRACES_SAMPLER="parentbased_traceidratio"
$env:OTEL_TRACES_SAMPLER_ARG="0.25"
docker compose --profile observability up --build
```

Jaeger is available at `http://localhost:16686`. With no exporter endpoint, context propagation
still works but completed spans are not sent to an external backend.

## Architecture statement

MapGo is a **deterministic-core, LLM-assisted, policy-governed Multi-Agent workflow**. The
Supervisor, Search, Safety, Planner, and Replanner roles are deterministic. Intent, Critic, and
Companion are LLM-eligible, and every model output remains behind schemas, capability policy,
hard validators, budgets, and human confirmation where required.
