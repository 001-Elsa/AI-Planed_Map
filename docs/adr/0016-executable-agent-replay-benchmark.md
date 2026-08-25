# ADR 0016: Executable Multi-Agent Replay Benchmark

## Status

Accepted.

## Decision

Retain the 60-case static dataset gate for schema, tag and task-graph quality,
and add a separate 100-case executable replay benchmark. Both the Single-Agent
baseline and Multi-Agent runner receive the same structured intents, map data,
deterministic route tools and fault scripts.

The offline benchmark executes production Agent classes and the real
`PlanningService` workflow. It covers clarification, safety-sensitive planning,
transient search failure, route-matrix failure, infeasible time windows, missing
POI evidence, weather change, POI closure, off-route replanning, duplicate
events, worker crash recovery and tool escalation.

The Single-Agent baseline is deliberately strong: one controller uses the same
Search and Planner implementations, gets the same bounded search retry budget,
and may replay dynamic events. It omits Supervisor task decomposition, Safety,
Critic, role hand-offs and recoverable worker transport. It does not use weaker
route algorithms.

## Metrics

Scores are computed from execution results and traces rather than expected JSON
declarations:

- task completion and hard-constraint satisfaction;
- exact expected tool selection and illegal tool execution rate;
- successful protocol hand-offs;
- recovery and dynamic replanning success;
- Critic recall on deliberately invalid evidence;
- average active Agent roles and metered model calls/tokens/cost;
- measured end-to-end P50 and P95 latency.

The default profile makes no network or LLM calls. Therefore model calls, tokens
and token cost are truthfully zero, not estimated. The latency numbers measure
local deterministic replay and must not be presented as production latency. A
future live profile may add real model/provider measurements without replacing
the stable offline regression gate.

## Reproducibility

Cases are generated deterministically from 13 scenario templates. The report
contains a SHA-256 dataset hash. CI runs the 60-case static gate followed by the
100-case executable benchmark. A checked-in result snapshot documents the most
recent local run, but CI always recomputes the metrics.
