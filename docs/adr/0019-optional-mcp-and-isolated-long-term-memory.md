# ADR 0019: Optional MCP tools and isolated long-term memory

## Status

Accepted.

## Context

MAPGO already has executable Agents, a fail-closed capability registry,
strongly typed tool contracts, task-scoped Shared State, and explicitly
confirmed PostgreSQL preferences. Replacing this architecture with MCP would
blur orchestration, authorization and deterministic solver boundaries. Direct
preference ORM use in API handlers also made the long-term memory boundary less
visible than its policy intended.

## Decision

MCP is a late-bound transport adapter under the existing ToolRegistry.
`AgentToolRuntime` authorizes the Agent, invocation mode and exact data scopes,
validates arguments locally, then selects an operator-configured Local, HTTP or
MCP adapter. MCP endpoints and allowlists never come from model output. The MCP
client performs initialization, sends the initialized notification, discovers
tools, and requires each remote input schema to equal the locally pinned schema
before `tools/call`.

MAPGO also exposes an opt-in, stateless JSON Streamable HTTP endpoint. It is
disabled by default, uses Bearer authentication and an Origin allowlist, and
exports only read-only fact acquisition tools. Route optimization remains an
in-process deterministic capability. Replan proposals and plan writes remain
workflow-only because they require authenticated trip context, PlanVersion CAS,
Critic review and possibly human confirmation.

Long-term preferences are accessed through `UserPreferenceMemory`. It accepts
only the existing bounded schema and explicit-confirmation write path. Agents
receive applied keys or context projections, never a database connection or
the complete preference store. Existing helper functions delegate to this
service for backwards compatibility.

## Consequences

- Local operation has no MCP dependency or latency.
- Remote tools cannot widen Agent permissions or silently change their schema.
- MCP failures use stable error envelopes and do not leak upstream exceptions.
- State, memory and conversation history retain distinct ownership and
  lifecycles.
- Stateful SSE, OAuth discovery, durable MCP Tasks and mutating MCP tools are
  deliberately out of scope until a concrete deployment requires them.
