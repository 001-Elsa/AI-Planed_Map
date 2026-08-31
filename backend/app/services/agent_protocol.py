"""Versioned, allow-listed communication protocol for Agent hand-offs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from backend.app.schemas.agent_artifacts import (
    AgentEndpoint,
    AgentMessage,
    AgentMessageAudit,
    AgentMessageType,
    CriticSoftAdjustments,
    ReviewReport,
    minimize_agent_payload,
)
from backend.app.schemas.ai_intent import AIPlanRequest, AIPlanResult, PlanningIntent, PoiCandidate
from backend.app.schemas.dynamic_replanning import (
    DynamicPatchReview,
    PlanPatchArtifact,
    ReplanDirective,
    TripEventArtifact,
)

MAX_AGENT_MESSAGE_BYTES = 256 * 1024
MAX_AGENT_AUDIT_BYTES = 8 * 1024


class AgentProtocolError(ValueError):
    """Raised when an Agent tries to cross a forbidden communication boundary."""


@dataclass(frozen=True)
class AgentRoute:
    sender: AgentEndpoint
    receiver: AgentEndpoint
    message_type: AgentMessageType
    artifact_types: frozenset[str]


ROUTES = (
    AgentRoute(
        AgentEndpoint.user,
        AgentEndpoint.supervisor,
        AgentMessageType.command,
        frozenset({"planning_request"}),
    ),
    AgentRoute(
        AgentEndpoint.supervisor,
        AgentEndpoint.intent,
        AgentMessageType.command,
        frozenset({"planning_request"}),
    ),
    AgentRoute(
        AgentEndpoint.intent,
        AgentEndpoint.supervisor,
        AgentMessageType.artifact,
        frozenset({"intent_artifact"}),
    ),
    AgentRoute(
        AgentEndpoint.intent,
        AgentEndpoint.supervisor,
        AgentMessageType.result,
        frozenset({"intent_artifact"}),
    ),
    AgentRoute(
        AgentEndpoint.supervisor,
        AgentEndpoint.search,
        AgentMessageType.command,
        frozenset({"intent_artifact", "retry_directive"}),
    ),
    AgentRoute(
        AgentEndpoint.search,
        AgentEndpoint.planner,
        AgentMessageType.artifact,
        frozenset({"search_artifact"}),
    ),
    AgentRoute(
        AgentEndpoint.search,
        AgentEndpoint.safety,
        AgentMessageType.artifact,
        frozenset({"search_artifact"}),
    ),
    AgentRoute(
        AgentEndpoint.safety,
        AgentEndpoint.planner,
        AgentMessageType.artifact,
        frozenset({"safety_report"}),
    ),
    AgentRoute(
        AgentEndpoint.planner,
        AgentEndpoint.critic,
        AgentMessageType.artifact,
        frozenset({"plan_candidate"}),
    ),
    AgentRoute(
        AgentEndpoint.planner,
        AgentEndpoint.supervisor,
        AgentMessageType.result,
        frozenset({"plan_candidate"}),
    ),
    AgentRoute(
        AgentEndpoint.critic,
        AgentEndpoint.supervisor,
        AgentMessageType.result,
        frozenset({"review_report"}),
    ),
    AgentRoute(
        AgentEndpoint.system,
        AgentEndpoint.supervisor,
        AgentMessageType.error,
        frozenset({"recovery_event"}),
    ),
    AgentRoute(
        AgentEndpoint.supervisor,
        AgentEndpoint.final_answer,
        AgentMessageType.result,
        frozenset({"final_answer"}),
    ),
    AgentRoute(
        AgentEndpoint.system,
        AgentEndpoint.companion,
        AgentMessageType.event,
        frozenset({"trip_observation"}),
    ),
    AgentRoute(
        AgentEndpoint.system,
        AgentEndpoint.companion,
        AgentMessageType.error,
        frozenset({"recovery_event"}),
    ),
    AgentRoute(
        AgentEndpoint.companion,
        AgentEndpoint.tool_runtime,
        AgentMessageType.tool_request,
        frozenset({"tool_request"}),
    ),
    AgentRoute(
        AgentEndpoint.tool_runtime,
        AgentEndpoint.companion,
        AgentMessageType.tool_result,
        frozenset({"tool_result"}),
    ),
    AgentRoute(
        AgentEndpoint.companion,
        AgentEndpoint.final_answer,
        AgentMessageType.result,
        frozenset({"companion_action_report"}),
    ),
    AgentRoute(
        AgentEndpoint.companion,
        AgentEndpoint.supervisor,
        AgentMessageType.artifact,
        frozenset({"trip_event_artifact"}),
    ),
    AgentRoute(
        AgentEndpoint.supervisor,
        AgentEndpoint.replanner,
        AgentMessageType.command,
        frozenset({"trip_event_artifact"}),
    ),
    AgentRoute(
        AgentEndpoint.replanner,
        AgentEndpoint.planner,
        AgentMessageType.artifact,
        frozenset({"replan_directive"}),
    ),
    AgentRoute(
        AgentEndpoint.replanner,
        AgentEndpoint.planner,
        AgentMessageType.result,
        frozenset({"replan_directive"}),
    ),
    AgentRoute(
        AgentEndpoint.planner,
        AgentEndpoint.critic,
        AgentMessageType.artifact,
        frozenset({"plan_patch_candidate"}),
    ),
    AgentRoute(
        AgentEndpoint.critic,
        AgentEndpoint.supervisor,
        AgentMessageType.result,
        frozenset({"dynamic_patch_review"}),
    ),
    AgentRoute(
        AgentEndpoint.supervisor,
        AgentEndpoint.final_answer,
        AgentMessageType.result,
        frozenset({"plan_patch_proposal"}),
    ),
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class AgentMessageRouter:
    """Protocol boundary with route authorization and a local delivery adapter.

    Durable delivery and cross-process deduplication live in Agent transports;
    ``deliver`` is retained for synchronous development workflows.
    """

    def __init__(self) -> None:
        self._delivered: dict[str, AgentMessage] = {}

    def build(
        self,
        *,
        task_id: str,
        sender: AgentEndpoint,
        receiver: AgentEndpoint,
        message_type: AgentMessageType,
        artifact_type: str,
        content: dict[str, Any],
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        attempt: int = 1,
    ) -> AgentMessage:
        serialized = _canonical_json(content).encode("utf-8")
        if len(serialized) > MAX_AGENT_MESSAGE_BYTES:
            raise AgentProtocolError("agent message exceeds 256 KiB")
        content_hash = hashlib.sha256(serialized).hexdigest()
        correlation = correlation_id or uuid4()
        idempotency_key = _sha256(
            {
                "protocol_version": "1.0",
                "task_id": task_id,
                "sender": sender.value,
                "receiver": receiver.value,
                "message_type": message_type.value,
                "artifact_type": artifact_type,
                "content_hash": content_hash,
                "correlation_id": str(correlation),
                "causation_id": str(causation_id) if causation_id else None,
            }
        )
        return AgentMessage(
            message_id=uuid4(),
            task_id=task_id,
            sender=sender,
            receiver=receiver,
            message_type=message_type,
            artifact_type=artifact_type,
            content=content,
            content_hash=content_hash,
            idempotency_key=idempotency_key,
            correlation_id=correlation,
            causation_id=causation_id,
            attempt=attempt,
        )

    def deliver(self, message: AgentMessage) -> tuple[AgentMessage, str]:
        self.validate(message)
        existing = self._delivered.get(message.idempotency_key)
        if existing is not None:
            return existing, "duplicate"
        self._delivered[message.idempotency_key] = message
        return message, "delivered"

    def validate(self, message: AgentMessage) -> None:
        """Validate an envelope without recording process-local delivery state.

        Durable transports use this boundary before publishing and again after
        claiming a message. ``deliver`` remains the synchronous development
        adapter and adds its historical in-process deduplication behavior.
        """
        self._authorize(message)
        self._validate_payload(message)
        serialized = _canonical_json(message.content).encode("utf-8")
        if len(serialized) > MAX_AGENT_MESSAGE_BYTES:
            raise AgentProtocolError("agent message exceeds 256 KiB")
        if hashlib.sha256(serialized).hexdigest() != message.content_hash:
            raise AgentProtocolError("agent message content hash mismatch")
        expected_idempotency_key = _sha256(
            {
                "protocol_version": message.protocol_version,
                "task_id": message.task_id,
                "sender": message.sender.value,
                "receiver": message.receiver.value,
                "message_type": message.message_type.value,
                "artifact_type": message.artifact_type,
                "content_hash": message.content_hash,
                "correlation_id": str(message.correlation_id),
                "causation_id": str(message.causation_id) if message.causation_id else None,
            }
        )
        if expected_idempotency_key != message.idempotency_key:
            raise AgentProtocolError("agent message idempotency key mismatch")
        if message.expires_at is not None and message.expires_at <= datetime.now(timezone.utc):
            raise AgentProtocolError("agent message has expired")

    @staticmethod
    def audit(message: AgentMessage, delivery_status: str = "delivered") -> AgentMessageAudit:
        minimized = minimize_agent_payload(message.content)
        serialized = _canonical_json(minimized).encode("utf-8")
        if len(serialized) > MAX_AGENT_AUDIT_BYTES:
            minimized = {
                "redacted": "oversized_agent_message",
                "original_byte_size": len(serialized),
                "top_level_keys": sorted(str(key) for key in message.content),
            }
        return AgentMessageAudit(
            protocol_version=message.protocol_version,
            message_id=message.message_id,
            task_id=message.task_id,
            sender=message.sender,
            receiver=message.receiver,
            message_type=message.message_type,
            artifact_type=message.artifact_type,
            content_summary=minimized,
            content_hash=message.content_hash,
            idempotency_key=message.idempotency_key,
            correlation_id=message.correlation_id,
            causation_id=message.causation_id,
            attempt=message.attempt,
            delivery_status=delivery_status,
            created_at=message.created_at,
        )

    @staticmethod
    def _authorize(message: AgentMessage) -> None:
        allowed = any(
            route.sender == message.sender
            and route.receiver == message.receiver
            and route.message_type == message.message_type
            and message.artifact_type in route.artifact_types
            for route in ROUTES
        )
        if not allowed:
            raise AgentProtocolError(
                "forbidden agent route: "
                f"{message.sender.value}->{message.receiver.value}/"
                f"{message.message_type.value}/{message.artifact_type}"
            )

    @staticmethod
    def _validate_payload(message: AgentMessage) -> None:
        """Validate security-sensitive artifacts again at the receiving boundary."""
        try:
            metadata_keys = {"shared_state_ref", "state_revision", "state_hash"}
            has_metadata = bool(metadata_keys & set(message.content))
            if has_metadata:
                if not metadata_keys.issubset(message.content):
                    raise ValueError("shared state metadata must be complete")
                if message.content["shared_state_ref"] != message.task_id:
                    raise ValueError("shared state reference must match task id")
                if int(message.content["state_revision"]) < 0:
                    raise ValueError("shared state revision must be non-negative")
                state_hash = str(message.content["state_hash"])
                if len(state_hash) != 64 or any(
                    character not in "0123456789abcdef" for character in state_hash
                ):
                    raise ValueError("shared state hash must be lowercase sha256")
            payload = {
                key: value for key, value in message.content.items() if key not in metadata_keys
            }
            if has_metadata and message.artifact_type == "intent_artifact":
                if set(payload) != {"artifact_hash", "question_count"}:
                    raise ValueError("state-backed intent message contains unexpected fields")
                if int(payload["question_count"]) < 0:
                    raise ValueError("question count must be non-negative")
                return
            if has_metadata and message.artifact_type == "search_artifact":
                if set(payload) != {"artifact_hash", "summary"} or not isinstance(
                    payload["summary"], dict
                ):
                    raise ValueError("state-backed search message requires a summary")
                return
            if has_metadata and message.artifact_type == "safety_report":
                if set(payload) != {
                    "artifact_hash",
                    "search_artifact_hash",
                    "summary",
                } or not isinstance(payload["summary"], dict):
                    raise ValueError("state-backed safety message requires a summary")
                return
            if has_metadata and message.artifact_type == "plan_candidate":
                allowed = {
                    "workflow_state",
                    "status",
                    "algorithm",
                    "candidate_count",
                    "stop_count",
                    "conflict_count",
                    "warning_count",
                    "tool_error_codes",
                    "plan_hash",
                }
                if set(payload) != allowed:
                    raise ValueError("state-backed plan message contains unexpected fields")
                return
            if has_metadata and message.artifact_type == "review_report":
                if set(payload) != {"verdict", "confidence", "finding_count"}:
                    raise ValueError("state-backed review message contains unexpected fields")
                if payload["verdict"] not in {
                    "approved",
                    "approved_with_warnings",
                    "needs_clarification",
                    "retry_with_soft_adjustments",
                }:
                    raise ValueError("unknown review verdict")
                return
            if has_metadata and message.artifact_type == "retry_directive":
                if set(payload) != {"soft_adjustments"}:
                    raise ValueError("state-backed retry message contains unexpected fields")
                CriticSoftAdjustments.model_validate(payload["soft_adjustments"])
                return
            if message.artifact_type == "planning_request":
                AIPlanRequest.model_validate(payload)
            elif message.artifact_type == "intent_artifact":
                PlanningIntent.model_validate(payload.get("intent"))
            elif message.artifact_type == "search_artifact":
                for group in payload.get("candidates", []):
                    for candidate in group:
                        PoiCandidate.model_validate(candidate)
            elif message.artifact_type == "plan_candidate":
                AIPlanResult.model_validate(payload)
            elif message.artifact_type == "review_report":
                ReviewReport.model_validate(payload)
            elif message.artifact_type == "retry_directive":
                if set(payload) != {"intent", "soft_adjustments"}:
                    raise ValueError("retry directive may contain only intent and soft adjustments")
                PlanningIntent.model_validate(payload["intent"])
                CriticSoftAdjustments.model_validate(payload["soft_adjustments"])
            elif message.artifact_type == "trip_event_artifact":
                TripEventArtifact.model_validate(payload)
            elif message.artifact_type == "replan_directive":
                ReplanDirective.model_validate(payload.get("directive", payload))
            elif message.artifact_type == "plan_patch_candidate":
                PlanPatchArtifact.model_validate(payload)
            elif message.artifact_type == "dynamic_patch_review":
                DynamicPatchReview.model_validate(payload)
            elif message.artifact_type == "plan_patch_proposal":
                PlanPatchArtifact.model_validate(payload)
        except (KeyError, TypeError, ValidationError, ValueError) as exc:
            raise AgentProtocolError(
                f"invalid {message.artifact_type} payload: {type(exc).__name__}"
            ) from exc
