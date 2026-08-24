# ADR-0006: Agent Tool Registry and capability isolation

## Status

Accepted.

## Context

Prompt separation and a list of tool names are not sufficient isolation. The existing trip-state policy table mixed two different questions: whether an operation is valid in the current trip state, and whether a specific Agent role may invoke it at all. It also listed planning operations next to Companion operations, which made “registered” easy to misread as “Agent-callable”.

Map search, route matrices, OR-Tools/Beam optimization, and transit verification must remain deterministic server capabilities. They are required by Search/Planner, but must never appear in an LLM tool schema. Planner must not gain access to user-profile storage merely because preferences are present in its typed input artifact.

## Decision

Introduce a fail-closed `AgentToolRegistry` as the single capability manifest. Every capability declares:

- the owning Agent role, if any;
- one invocation mode: `agent_callable`, `internal_stage`, or `workflow_only`;
- an exact set of permitted data scopes;
- a side-effect classification.

`AgentSpec.allowed_tools` contains only model-selectable tools. `AgentSpec.allowed_internal_capabilities` separately documents deterministic server capabilities and is derived from the registry. Intent owns only `parse_requirement`; Search owns only `search_poi`; Planner owns route-matrix, route-optimization, and transit-verification capabilities; Companion owns only the four in-trip model-selectable tools. Supervisor and Critic own none.

Authorization rejects unknown capabilities, wrong roles, invocation-mode confusion, and any data-scope mismatch. The actual Intent, Search, Planner, Companion Controller, and Companion HTTP execution boundaries call the registry immediately before execution. Companion operations must then also pass the existing trip state, consent, confirmation, and budget policy.

Operations such as persisting a Plan Patch, sharing trip status, or saving a long-term preference are `workflow_only` and have no Agent owner. They remain available only through dedicated server endpoints that perform their own validation and confirmation.

## Consequences

- Adding a capability requires an explicit role, mode, data footprint, and side-effect review.
- Internal planning functions cannot be selected by an LLM or reached by Companion through tool-name injection.
- Planner cannot request the precise-location or long-term-preference data domains.
- Authorization allow/deny outcomes are exported as bounded-cardinality metrics; denied attempts are logged without arguments or user data.
- This is an in-process capability boundary, not an OS security boundary. A future independently deployed Agent must additionally use service identity, network policy, and separate credentials.
