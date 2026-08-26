"""Optimistic, role-scoped Shared State over Redis or the in-memory runtime store."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from backend.app.core.config import Settings
from backend.app.core.observability import metrics
from backend.app.infrastructure.runtime_store import RuntimeStore
from backend.app.schemas.agent_artifacts import AgentType
from backend.app.schemas.agent_state import (
    AgentSharedState,
    AgentSharedStateAudit,
    AgentSharedStateEvent,
    AgentSharedStatePhase,
    AgentSharedStateView,
)


class SharedStateError(RuntimeError):
    pass


class SharedStateNotFoundError(SharedStateError):
    pass


class SharedStateConflictError(SharedStateError):
    pass


class SharedStateAccessError(SharedStateError):
    pass


WRITE_FIELDS: dict[AgentType, frozenset[str]] = {
    AgentType.supervisor: frozenset({"soft_adjustments", "execution_context"}),
    AgentType.intent: frozenset({"user_requirement", "clarification_questions"}),
    AgentType.search: frozenset({"poi_candidates"}),
    AgentType.safety: frozenset({"execution_context"}),
    AgentType.planner: frozenset({"route_plan"}),
    AgentType.critic: frozenset({"evaluation_result"}),
    AgentType.companion: frozenset({"execution_context"}),
}

READ_FIELDS: dict[AgentType, frozenset[str]] = {
    AgentType.supervisor: frozenset(
        {
            "user_requirement",
            "clarification_questions",
            "poi_candidates",
            "route_plan",
            "evaluation_result",
            "soft_adjustments",
            "execution_context",
            "execution_history",
        }
    ),
    AgentType.intent: frozenset({"user_requirement", "clarification_questions"}),
    AgentType.search: frozenset({"user_requirement", "soft_adjustments"}),
    AgentType.safety: frozenset({"user_requirement", "poi_candidates", "execution_context"}),
    AgentType.planner: frozenset(
        {"user_requirement", "poi_candidates", "soft_adjustments", "execution_context"}
    ),
    AgentType.critic: frozenset({"user_requirement", "route_plan"}),
    AgentType.companion: frozenset(
        {"route_plan", "evaluation_result", "execution_context", "execution_history"}
    ),
}

ACTION_PHASE: dict[str, AgentSharedStatePhase] = {
    "intent_analyzed": AgentSharedStatePhase.intent_ready,
    "search_completed": AgentSharedStatePhase.search_ready,
    "safety_checked": AgentSharedStatePhase.safety_ready,
    "plan_completed": AgentSharedStatePhase.plan_ready,
    "critic_reviewed": AgentSharedStatePhase.reviewed,
    "soft_retry_scheduled": AgentSharedStatePhase.retrying,
    "recovery_applied": AgentSharedStatePhase.retrying,
    "workflow_finalized": AgentSharedStatePhase.finalized,
    "workflow_failed": AgentSharedStatePhase.failed,
    "trip_started": AgentSharedStatePhase.in_trip,
    "trip_event_processed": AgentSharedStatePhase.in_trip,
    "trip_completed": AgentSharedStatePhase.completed,
}

ACTION_ACTORS: dict[str, frozenset[AgentType]] = {
    "intent_analyzed": frozenset({AgentType.intent}),
    "search_completed": frozenset({AgentType.search}),
    "safety_checked": frozenset({AgentType.safety}),
    "plan_completed": frozenset({AgentType.planner}),
    "critic_reviewed": frozenset({AgentType.critic}),
    "soft_retry_scheduled": frozenset({AgentType.supervisor}),
    "recovery_applied": frozenset({AgentType.supervisor}),
    "workflow_finalized": frozenset({AgentType.supervisor}),
    "workflow_failed": frozenset({AgentType.supervisor}),
    "trip_started": frozenset({AgentType.companion}),
    "trip_event_processed": frozenset({AgentType.companion}),
    "trip_completed": frozenset({AgentType.companion}),
}

ALLOWED_TRANSITIONS: dict[AgentSharedStatePhase, frozenset[AgentSharedStatePhase]] = {
    AgentSharedStatePhase.initialized: frozenset(
        {
            AgentSharedStatePhase.intent_ready,
            AgentSharedStatePhase.plan_ready,
            AgentSharedStatePhase.retrying,
            AgentSharedStatePhase.failed,
        }
    ),
    AgentSharedStatePhase.intent_ready: frozenset(
        {
            AgentSharedStatePhase.search_ready,
            AgentSharedStatePhase.retrying,
            AgentSharedStatePhase.finalized,
            AgentSharedStatePhase.failed,
        }
    ),
    AgentSharedStatePhase.search_ready: frozenset(
        {
            AgentSharedStatePhase.safety_ready,
            AgentSharedStatePhase.plan_ready,
            AgentSharedStatePhase.retrying,
            AgentSharedStatePhase.failed,
        }
    ),
    AgentSharedStatePhase.safety_ready: frozenset(
        {
            AgentSharedStatePhase.plan_ready,
            AgentSharedStatePhase.retrying,
            AgentSharedStatePhase.failed,
        }
    ),
    AgentSharedStatePhase.plan_ready: frozenset(
        {
            AgentSharedStatePhase.reviewed,
            AgentSharedStatePhase.retrying,
            AgentSharedStatePhase.finalized,
            AgentSharedStatePhase.in_trip,
            AgentSharedStatePhase.failed,
        }
    ),
    AgentSharedStatePhase.reviewed: frozenset(
        {
            AgentSharedStatePhase.retrying,
            AgentSharedStatePhase.finalized,
            AgentSharedStatePhase.failed,
        }
    ),
    AgentSharedStatePhase.retrying: frozenset(
        {
            AgentSharedStatePhase.retrying,
            AgentSharedStatePhase.search_ready,
            AgentSharedStatePhase.plan_ready,
            AgentSharedStatePhase.failed,
        }
    ),
    AgentSharedStatePhase.finalized: frozenset(
        {AgentSharedStatePhase.in_trip, AgentSharedStatePhase.completed}
    ),
    AgentSharedStatePhase.in_trip: frozenset(
        {
            AgentSharedStatePhase.in_trip,
            AgentSharedStatePhase.completed,
            AgentSharedStatePhase.failed,
        }
    ),
    AgentSharedStatePhase.completed: frozenset(),
    AgentSharedStatePhase.failed: frozenset(),
}


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AgentSharedStateManager:
    def __init__(self, store: RuntimeStore, settings: Settings) -> None:
        self.store = store
        self.ttl_seconds = max(60, settings.agent_shared_state_ttl_seconds)
        self.max_history = min(200, max(1, settings.agent_shared_state_max_history))
        self.max_bytes = max(16_384, settings.agent_shared_state_max_bytes)

    def _serialized(self, state: AgentSharedState) -> dict[str, Any]:
        payload = state.model_dump(mode="json")
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > self.max_bytes:
            raise SharedStateError("shared state exceeds configured byte limit")
        return payload

    @staticmethod
    def _state_hash(state: AgentSharedState) -> str:
        return _hash(state.model_dump(mode="json", exclude={"state_hash"}))

    def _seal(self, state: AgentSharedState) -> AgentSharedState:
        return state.model_copy(update={"state_hash": self._state_hash(state)})

    @staticmethod
    def key(task_id: str) -> str:
        return f"agent-shared-state:v1:{task_id}"

    async def initialize(
        self,
        task_id: str,
        *,
        route_plan: dict[str, Any] | None = None,
    ) -> AgentSharedState:
        existing = await self.store.get_json(self.key(task_id))
        if existing is not None:
            state = AgentSharedState.model_validate(existing)
            if state.state_hash != self._state_hash(state):
                raise SharedStateError("shared state content hash mismatch")
            if route_plan is not None and _hash(state.route_plan) != _hash(route_plan):
                return await self.sync_formal_route_plan(
                    task_id,
                    expected_revision=state.revision,
                    route_plan=route_plan,
                )
            return state
        if route_plan is not None and route_plan.get("status") != "success":
            raise SharedStateAccessError("only a successful formal plan can seed trip state")
        now = datetime.now(timezone.utc)
        phase = (
            AgentSharedStatePhase.plan_ready
            if route_plan is not None
            else AgentSharedStatePhase.initialized
        )
        initial_change = {"route_plan": route_plan} if route_plan is not None else {}
        event = AgentSharedStateEvent(
            revision=0,
            actor=AgentType.supervisor,
            action="state_initialized",
            changed_fields=sorted(initial_change),
            change_hash=_hash(initial_change),
            created_at=now,
        )
        state = AgentSharedState(
            task_id=task_id,
            phase=phase,
            route_plan=route_plan,
            execution_history=[event],
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
        )
        state = self._seal(state)
        created = await self.store.compare_set_json(
            self.key(task_id), -1, self._serialized(state), self.ttl_seconds
        )
        if not created:
            try:
                return await self.read(task_id)
            except SharedStateNotFoundError as exc:
                raise SharedStateConflictError(
                    "shared state initialization was not stored"
                ) from exc
        metrics.increment(
            "mapgo_agent_shared_state_updates_total",
            {"actor": "supervisor", "action": "state_initialized"},
        )
        return state

    async def sync_formal_route_plan(
        self,
        task_id: str,
        *,
        expected_revision: int,
        route_plan: dict[str, Any],
    ) -> AgentSharedState:
        """Refresh Companion state from an already-authorized immutable PlanVersion."""
        if route_plan.get("status") != "success":
            raise SharedStateAccessError("only a successful formal plan can refresh trip state")
        current = await self.read(task_id)
        if current.revision != expected_revision:
            raise SharedStateConflictError(
                f"shared state revision is {current.revision}, expected {expected_revision}"
            )
        old_version = int((current.route_plan or {}).get("plan_version") or 0)
        new_version = int(route_plan.get("plan_version") or 0)
        if new_version < old_version:
            raise SharedStateAccessError("formal plan version cannot move backwards")
        if _hash(current.route_plan) == _hash(route_plan):
            return current
        now = datetime.now(timezone.utc)
        next_revision = current.revision + 1
        event = AgentSharedStateEvent(
            revision=next_revision,
            actor=AgentType.supervisor,
            action="formal_plan_refreshed",
            changed_fields=["route_plan"],
            change_hash=_hash(route_plan),
            created_at=now,
        )
        history = [*current.execution_history, event][-self.max_history :]
        payload = current.model_dump(mode="json")
        payload.update(
            {
                "revision": next_revision,
                "route_plan": route_plan,
                "execution_history": [item.model_dump(mode="json") for item in history],
                "updated_at": now,
                "expires_at": now + timedelta(seconds=self.ttl_seconds),
            }
        )
        updated = AgentSharedState.model_validate(payload)
        updated = self._seal(updated)
        stored = await self.store.compare_set_json(
            self.key(task_id),
            expected_revision,
            self._serialized(updated),
            self.ttl_seconds,
        )
        if not stored:
            metrics.increment("mapgo_agent_shared_state_conflicts_total", {"actor": "supervisor"})
            raise SharedStateConflictError("shared state changed during formal plan refresh")
        metrics.increment(
            "mapgo_agent_shared_state_updates_total",
            {"actor": "supervisor", "action": "formal_plan_refreshed"},
        )
        return updated

    async def read(self, task_id: str) -> AgentSharedState:
        raw = await self.store.get_json(self.key(task_id))
        if raw is None:
            raise SharedStateNotFoundError(f"shared state not found: {task_id}")
        state = AgentSharedState.model_validate(raw)
        if state.state_hash != self._state_hash(state):
            raise SharedStateError("shared state content hash mismatch")
        return state

    async def read_for_agent(self, task_id: str, actor: AgentType) -> AgentSharedStateView:
        state = await self.read(task_id)
        fields = READ_FIELDS[actor]
        payload: dict[str, Any] = {
            "task_id": state.task_id,
            "revision": state.revision,
            "state_hash": state.state_hash,
            "phase": state.phase,
            "visible_fields": sorted(fields),
        }
        for field in fields:
            payload[field] = getattr(state, field)
        return AgentSharedStateView.model_validate(payload)

    async def update(
        self,
        task_id: str,
        *,
        actor: AgentType,
        expected_revision: int,
        action: str,
        changes: dict[str, Any],
        message_id: UUID | None = None,
    ) -> AgentSharedState:
        if action not in ACTION_PHASE or actor not in ACTION_ACTORS[action]:
            raise SharedStateAccessError(
                f"{actor.value} cannot perform shared-state action {action}"
            )
        forbidden = set(changes) - WRITE_FIELDS[actor]
        if forbidden:
            raise SharedStateAccessError(
                f"{actor.value} cannot write shared-state fields {sorted(forbidden)}"
            )
        current = await self.read(task_id)
        if current.revision != expected_revision:
            metrics.increment("mapgo_agent_shared_state_conflicts_total", {"actor": actor.value})
            raise SharedStateConflictError(
                f"shared state revision is {current.revision}, expected {expected_revision}"
            )
        next_phase = ACTION_PHASE[action]
        if next_phase not in ALLOWED_TRANSITIONS[current.phase]:
            raise SharedStateAccessError(
                f"invalid shared-state transition {current.phase.value}->{next_phase.value}"
            )
        now = datetime.now(timezone.utc)
        next_revision = current.revision + 1
        event = AgentSharedStateEvent(
            revision=next_revision,
            actor=actor,
            action=action,
            changed_fields=sorted(changes),
            message_id=message_id,
            change_hash=_hash(changes),
            created_at=now,
        )
        history = [*current.execution_history, event][-self.max_history :]
        payload = current.model_dump(mode="json")
        payload.update(changes)
        payload.update(
            {
                "revision": next_revision,
                "phase": next_phase,
                "execution_history": [item.model_dump(mode="json") for item in history],
                "updated_at": now,
                "expires_at": now + timedelta(seconds=self.ttl_seconds),
            }
        )
        updated = AgentSharedState.model_validate(payload)
        updated = self._seal(updated)
        stored = await self.store.compare_set_json(
            self.key(task_id),
            expected_revision,
            self._serialized(updated),
            self.ttl_seconds,
        )
        if not stored:
            metrics.increment("mapgo_agent_shared_state_conflicts_total", {"actor": actor.value})
            raise SharedStateConflictError("shared state changed concurrently")
        metrics.increment(
            "mapgo_agent_shared_state_updates_total",
            {"actor": actor.value, "action": action},
        )
        return updated

    async def audit(self, task_id: str) -> AgentSharedStateAudit:
        state = await self.read(task_id)
        preference_flags: list[str] = []
        if state.user_requirement is not None:
            preferences = state.user_requirement.preferences
            for field in (
                "minimize_distance",
                "minimize_walking",
                "minimize_cost",
                "prefer_high_rating",
            ):
                if getattr(preferences, field):
                    preference_flags.append(field)
            if preferences.dietary_restrictions:
                preference_flags.append("dietary_restrictions")
            if preferences.optimization_goal != "balanced":
                preference_flags.append(f"optimization:{preferences.optimization_goal}")
        route = state.route_plan or {}
        return AgentSharedStateAudit(
            task_id=task_id,
            revision=state.revision,
            phase=state.phase,
            preference_flags=preference_flags,
            candidate_group_count=len(state.poi_candidates),
            candidate_count=sum(len(group) for group in state.poi_candidates),
            route_status=str(route.get("status")) if route.get("status") else None,
            stop_count=len(route.get("stops") or []),
            evaluation_verdict=(
                state.evaluation_result.verdict if state.evaluation_result else None
            ),
            history_count=len(state.execution_history),
            state_hash=state.state_hash,
        )

    async def delete(self, task_id: str, *, reason: str = "task_completed") -> bool:
        """Actively erase runtime state; TTL remains only a crash-recovery fallback."""
        deleted = await self.store.delete_json(self.key(task_id))
        metrics.increment(
            "mapgo_short_term_memory_deletions_total",
            {"reason": reason, "result": "deleted" if deleted else "already_absent"},
        )
        return deleted
