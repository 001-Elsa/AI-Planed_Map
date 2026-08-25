from datetime import datetime, timezone

import pytest

from backend.app.schemas.agent_artifacts import (
    AgentEndpoint,
    AgentMessageType,
    AgentType,
)
from backend.app.schemas.ai_intent import Coordinate, PlanPatchOperation
from backend.app.schemas.dynamic_replanning import PlanPatchArtifact, TripEventArtifact
from backend.app.services.agent_protocol import AgentMessageRouter, AgentProtocolError
from backend.app.services.agents.replanner_agent import ReplannerAgent
from backend.app.services.dynamic_replanning import review_dynamic_patch


@pytest.mark.asyncio
async def test_replanner_selects_strategy_without_planning_tools():
    agent = ReplannerAgent()
    event = TripEventArtifact(
        trip_id=7,
        event_id=11,
        event_type="PoiStatusChanged",
        occurred_at=datetime.now(timezone.utc),
        impact_level="high",
        reason="poi closed",
        base_plan_version=12,
    )
    execution = await agent.run(
        event,
        current_location=Coordinate(lng=120.1, lat=30.2),
        completed_stop_ids=["done-1"],
        event_payload={"closed_poi_id": "poi-4"},
        weather=None,
    )
    assert execution.output.strategy == "replace_closed_poi"
    assert execution.output.base_plan_version == 12
    assert execution.spec.agent_type == AgentType.replanner
    assert not execution.spec.allowed_tools
    assert not execution.spec.allowed_internal_capabilities


def test_dynamic_critic_blocks_required_stop_removal():
    patch = PlanPatchArtifact(
        patch_id=5,
        base_version=12,
        status="patch_pending_confirmation",
        operations=[PlanPatchOperation(operation="remove_stop", stop_id="required")],
        impact={
            "before": {"plan_version": 12},
            "after": {"constraint_conflicts": []},
        },
    )
    review = review_dynamic_patch(
        base_snapshot={
            "stops": [
                {
                    "poi": {"id": "required"},
                    "task": {"required": True},
                }
            ]
        },
        patch=patch,
    )
    assert review.verdict == "blocked"
    assert review.risk_level == "critical"
    assert review.requires_confirmation is True
    assert "required_stop_removal_forbidden" in review.findings


def test_dynamic_protocol_allows_only_supervised_replanning_path():
    router = AgentMessageRouter()
    event = TripEventArtifact(
        trip_id=7,
        event_id=11,
        event_type="TrafficChanged",
        occurred_at=datetime.now(timezone.utc),
        impact_level="high",
        reason="traffic jam",
        base_plan_version=3,
    ).model_dump(mode="json")
    allowed = router.build(
        task_id="trip-7-event-11",
        sender=AgentEndpoint.companion,
        receiver=AgentEndpoint.supervisor,
        message_type=AgentMessageType.artifact,
        artifact_type="trip_event_artifact",
        content=event,
    )
    router.validate(allowed)
    denied = allowed.model_copy(
        update={
            "receiver": AgentEndpoint.planner,
        }
    )
    with pytest.raises(AgentProtocolError, match="forbidden agent route"):
        router.validate(denied)
