# ADR-0007: Short-term and long-term Agent memory

## Status

Accepted.

## Context

The versioned Shared State already carries current planning facts, while `UserPreference` stores confirmed preferences. Neither lifecycle was complete: runtime planning state relied only on TTL instead of active deletion, and persisted preferences were not loaded into later plans or schema-restricted.

Memory must not become a covert profile-building channel. Inferred behavior, trip history, location, or Critic suggestions must not silently become durable preferences. A remembered preference must also not override the user's current request.

## Decision

Use two explicitly separated memory classes:

1. Short-term task memory uses the existing RuntimeStore/Redis Shared State. At every terminal planning return or exception, the state is actively deleted after its minimized audit summary has been copied into the trace. TTL remains a crash-recovery fallback. Companion trip state persists only for the active trip and is deleted when the trip completes.
2. Long-term memory uses PostgreSQL `UserPreference`. Only a fixed set of schema-validated soft preferences can be stored, and only through an endpoint requiring explicit confirmation. Users can list, delete one preference, disable memory for one request, export all preferences, or purge them all.

The API/Supervisor boundary loads confirmed preferences and injects normalized defaults into the planning request. Agents never receive database credentials or a user-profile query tool. Precedence is:

1. explicit structured values in the current request;
2. related intent stated in current free text;
3. confirmed long-term defaults;
4. parser/model defaults.

Discovery preferences such as preferred categories, quiet/indoor environments, or avoiding queues are used only as bounded recall hints for generic discovery requests. They do not create mandatory stops or claim facts that the Provider did not verify. The response identifies applied preference keys but never duplicates their values in memory audit metadata.

Multi-turn conversations freeze the effective memory at conversation start so a concurrent preference edit cannot change later turns invisibly.

## Consequences

- Normal completion does not leave full planning context in Redis.
- A new plan can reuse confirmed walking, cost, rating, dietary, optimization, and bounded discovery preferences.
- Current instructions always take priority and users have granular revocation.
- Existing unsupported/corrupt preference rows are ignored and counted, not injected into Agent context.
- Formal plans, minimized Agent audit snapshots, and versioned conversation records remain durable records rather than runtime memory.
