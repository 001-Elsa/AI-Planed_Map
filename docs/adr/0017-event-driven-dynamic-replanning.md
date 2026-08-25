# ADR 0017: Event-driven dynamic replanning with versioned patches

## Status

Accepted.

## Context

MapGo already persisted trip events, ran a bounded Companion tool loop and
created pending plan patches. The missing boundary was that `propose_replan`
called one service which both selected recovery behavior and solved the route.
There was no durable Supervisor/Replanner/Planner/Critic workflow for the
in-trip phase, and concurrent patch acceptance relied mainly on a preflight
version comparison.

## Decision

Dynamic events execute this allow-listed protocol:

```text
TripEvent -> Companion -> Supervisor -> Replanner -> Planner -> Critic
                                                     |
                                          deterministic solver
                                                     v
Supervisor -> high risk or no opt-in -> HITL -> PlanPatch(base=N) -> CAS -> N+1
           -> low risk plus explicit opt-in -----------^
```

- `ReplannerAgent` has no tools. It converts the event into a typed recovery
  directive and selects a bounded strategy.
- `PlannerAgent` remains the only route-solving role. OR-Tools and route matrix
  providers remain deterministic tools, not Agents.
- `CriticAgent` cannot mutate a patch. Deterministic review blocks hard
  constraint conflicts, required-stop removal, stale evidence and replacement
  POIs without provider evidence.
- Companion never writes a formal plan. Every mutation is first persisted as a
  `PlanPatch` against an immutable base version.
- High-risk patches require HITL. Low-risk automatic application requires the
  user to opt in when creating the Trip Session; the default remains confirm.
- Patch acceptance locks both patch and latest version on databases that support
  row locks, revalidates the route and hard constraints, then inserts `N+1`.
  The unique `(planning_run_id, version)` constraint is the final fencing gate.
- Every dynamic handoff, task, artifact and workflow is persisted through the
  existing Agent workflow tables. Artifacts carry their actual base version and
  become stale after a successful version advance.

## Consequences

Worker retries are idempotent by TripEvent status and source event patch
deduplication. A crash can replay the event, but cannot create a second active
writer for the same plan version. Redis remains a delivery mechanism; the
database remains the source of truth.

The low-risk opt-in is deliberately narrow. Transport changes, POI replacement,
required-stop changes, material cost changes and any deterministic conflict are
never silently applied.
