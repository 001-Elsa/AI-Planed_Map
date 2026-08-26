import json
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select

from backend.app.clients.weather_client import WeatherSnapshot
from backend.app.core.exceptions import AppError
from backend.app.db.session import SessionLocal
from backend.app.infrastructure.runtime_store import InMemoryRuntimeStore
from backend.app.main import app
from backend.app.models import (
    AgentRun,
    AgentSession,
    AgentToolCall,
    AgentWorkflowRun,
    AgentWorkflowTask,
    PlanPatch,
    PlanVersion,
    TripEvent,
    TripSession,
)
from backend.app.services.agent_controller import AgentController
from backend.app.services.agent_decider import AgentDecision, DecisionResult
from backend.app.services.plan_versioning import apply_plan_patch_cas
from backend.app.worker import process_trip_event


@dataclass
class ScriptedDecider:
    decisions: list[AgentDecision]
    index: int = 0

    async def decide(self, **_kwargs):
        decision = self.decisions[min(self.index, len(self.decisions) - 1)]
        self.index += 1
        return DecisionResult(decision=decision, model_name="integration-scripted-llm")


class HeavyRainWeather:
    name = "test-heavy-rain"

    async def current(self, _location):
        return WeatherSnapshot(
            temperature_c=20,
            precipitation_probability=90,
            weather_code=82,
            observed_at=datetime.now(timezone.utc),
            source=self.name,
            confidence=0.9,
        )


async def _active_trip(
    client: httpx.AsyncClient, username: str, text: str
) -> tuple[dict, dict, int]:
    registered = await client.post(
        "/api/register",
        json={"username": username, "password": "secret12", "nickname": "Agent 测试"},
    )
    headers = {"Authorization": f"Bearer {registered.json()['data']['token']}"}
    plan = await client.post(
        "/api/ai/plans",
        headers=headers,
        json={
            "text": text,
            "origin": {"lng": 120.62, "lat": 31.32},
            "transport_mode": "walking",
        },
    )
    assert plan.status_code == 200, plan.text
    plan_data = plan.json()["data"]
    trip = await client.post(
        "/api/companion/trips",
        headers=headers,
        json={"planning_run_id": plan_data["planning_run_id"]},
    )
    trip_id = trip.json()["data"]["trip_id"]
    assert (
        await client.post(
            f"/api/companion/trips/{trip_id}/transition",
            headers=headers,
            json={"target_state": "ACTIVE_TRIP", "reason": "开始演示"},
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/companion/trips/{trip_id}/consents",
            headers=headers,
            json={"scope": "precise_location", "granted": True},
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/companion/trips/{trip_id}/location",
            headers=headers,
            json={
                "event_id": f"location-{username}",
                "location": {"lng": 120.62, "lat": 31.32},
                "accuracy_meters": 10,
                "captured_at": "2026-07-29T14:00:00+08:00",
            },
        )
    ).status_code == 200
    return headers, plan_data, trip_id


async def _event_and_patch(trip_id: int) -> tuple[TripEvent, PlanPatch | None, int, int, str]:
    async with SessionLocal() as db:
        event = await db.scalar(
            select(TripEvent)
            .where(TripEvent.trip_session_id == trip_id)
            .order_by(TripEvent.id.desc())
        )
        trip = await db.get(TripSession, trip_id)
        patch = await db.scalar(
            select(PlanPatch)
            .where(PlanPatch.planning_run_id == trip.planning_run_id, PlanPatch.status == "pending")
            .order_by(PlanPatch.id.desc())
        )
        agent = await db.scalar(select(AgentSession).where(AgentSession.trip_session_id == trip_id))
        assert event is not None and trip is not None and agent is not None
        calls = int(
            await db.scalar(
                select(func.count(AgentToolCall.id))
                .join(AgentRun, AgentRun.id == AgentToolCall.agent_run_id)
                .where(AgentRun.agent_session_id == agent.id)
            )
            or 0
        )
        run = await db.scalar(
            select(AgentRun)
            .where(AgentRun.agent_session_id == agent.id)
            .order_by(AgentRun.id.desc())
        )
        assert run is not None
        return event, patch, trip.planning_run_id, calls, run.status


async def _latest_event(trip_id: int) -> TripEvent:
    async with SessionLocal() as db:
        event = await db.scalar(
            select(TripEvent)
            .where(TripEvent.trip_session_id == trip_id)
            .order_by(TripEvent.id.desc())
        )
        assert event is not None
        return event


async def _latest_tool_output(trip_id: int) -> str:
    async with SessionLocal() as db:
        agent = await db.scalar(select(AgentSession).where(AgentSession.trip_session_id == trip_id))
        assert agent is not None
        call = await db.scalar(
            select(AgentToolCall)
            .join(AgentRun, AgentRun.id == AgentToolCall.agent_run_id)
            .where(AgentRun.agent_session_id == agent.id)
            .order_by(AgentToolCall.id.desc())
        )
        assert call is not None
        return call.output_summary_json


async def _exercise_controller(trip_id: int, decider: ScriptedDecider, max_steps: int):
    async with SessionLocal() as db:
        trip = await db.get(TripSession, trip_id)
        agent = await db.scalar(select(AgentSession).where(AgentSession.trip_session_id == trip_id))
        assert trip is not None and agent is not None

        async def executor(_tool, _arguments):
            return {"ok": True}

        result = await AgentController(db, decider).run_once(
            trip=trip,
            agent=agent,
            observation={"trigger": "integration_test"},
            consents=set(),
            tool_executor=executor,
            max_steps=max_steps,
        )
        calls = (
            await db.scalars(
                select(AgentToolCall)
                .where(AgentToolCall.agent_run_id == result["run_id"])
                .order_by(AgentToolCall.id)
            )
        ).all()
        return result, calls


@pytest.mark.asyncio
async def test_worker_llm_tool_loop_creates_one_pending_patch_and_transport_switches(
    async_client: httpx.AsyncClient,
):
    headers, plan, trip_id = await _active_trip(
        async_client, "agentloop", "明天下午从酒店出发去博物馆，再去商场"
    )
    event_response = await async_client.post(
        f"/api/companion/trips/{trip_id}/events",
        headers=headers,
        json={
            "event_id": "delay-agent-loop-001",
            "type": "ScheduleDelayDetected",
            "occurred_at": "2026-07-29T14:30:00+08:00",
            "payload": {"delay_minutes": 45, "allow_transport_switch": True},
        },
    )
    assert event_response.status_code == 200
    event = await _latest_event(trip_id)
    store = InMemoryRuntimeStore()
    decider = ScriptedDecider(
        [
            AgentDecision(action="call_tool", tool="get_trip_state", reason="核对状态"),
            AgentDecision(action="call_tool", tool="propose_replan", reason="延误后重规划"),
            AgentDecision(action="finish", reason="已产生待确认方案"),
        ]
    )
    # Simulate a second worker holding the per-trip distributed lock.
    lock_name = f"agent-run:trip:{trip_id}"
    token = await store.acquire_lock(lock_name, 30)
    assert token is not None
    await process_trip_event(
        store,
        {"trip_id": trip_id, "event_id": event.id, "event_type": event.event_type},
        map_provider=app.state.map_provider,
        weather_provider=app.state.weather_provider,
        decider=decider,
    )
    assert await store.dequeue("mapgo:trip-events:retry", timeout_seconds=0) is not None
    assert await store.release_lock(lock_name, token)
    await process_trip_event(
        store,
        {"trip_id": trip_id, "event_id": event.id, "event_type": event.event_type},
        map_provider=app.state.map_provider,
        weather_provider=app.state.weather_provider,
        decider=decider,
    )
    event, patch, run_id, calls, run_status = await _event_and_patch(trip_id)
    assert patch is not None
    assert patch.status == "pending"
    operations = json.loads(patch.operations_json)
    assert any(item["operation"] == "change_transport_mode" for item in operations)
    assert calls >= 2
    assert run_status == "succeeded"
    async with SessionLocal() as db:
        workflow = await db.scalar(
            select(AgentWorkflowRun)
            .where(AgentWorkflowRun.trip_session_id == trip_id)
            .order_by(AgentWorkflowRun.id.desc())
        )
        assert workflow is not None
        tasks = (
            await db.scalars(
                select(AgentWorkflowTask)
                .where(AgentWorkflowTask.workflow_run_id == workflow.id)
                .order_by(AgentWorkflowTask.id)
            )
        ).all()
        assert [task.role for task in tasks] == [
            "companion",
            "supervisor",
            "replanner",
            "planner",
            "critic",
            "supervisor",
        ]
        assert all(task.status == "succeeded" for task in tasks)
    stream = await store.get_json(f"trip-stream:{trip_id}")
    assert stream["plan_patch"]["patch_id"] == patch.id
    assert any(
        item["operation"] == "change_transport_mode" for item in stream["plan_patch"]["operations"]
    )
    shared_state = await store.get_json(f"agent-shared-state:v1:trip-{trip_id}-state")
    assert shared_state["phase"] == "in_trip"
    assert shared_state["route_plan"]["plan_version"] == 1
    assert shared_state["execution_context"]["last_run_status"] == "succeeded"
    assert shared_state["execution_context"]["last_trigger"] == "worker_event"
    assert shared_state["revision"] == 1

    # Re-consuming the same durable event is idempotent: no second patch.
    await process_trip_event(
        store,
        {"trip_id": trip_id, "event_id": event.id, "event_type": event.event_type},
        map_provider=app.state.map_provider,
        weather_provider=app.state.weather_provider,
        decider=decider,
    )
    _, same_patch, _, _, _ = await _event_and_patch(trip_id)
    assert same_patch is not None and same_patch.id == patch.id

    accepted = await async_client.post(
        f"/api/ai/plans/{run_id}/patches/{patch.id}/decision",
        headers=headers,
        json={"accept": True},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["plan_version"] == 2
    assert accepted.json()["data"]["snapshot"]["intent"]["transport_mode"] == "driving"
    assert plan["plan_version"] == 1
    rejected_patch = await async_client.post(
        f"/api/ai/plans/{run_id}/patches",
        headers=headers,
        json={
            "base_version": 2,
            "operations": [{"operation": "move_stop", "from_position": 0, "to_position": 1}],
            "reason": "测试用户拒绝方案不改变正式计划",
        },
    )
    assert rejected_patch.status_code == 200, rejected_patch.text
    rejected = await async_client.post(
        f"/api/ai/plans/{run_id}/patches/{rejected_patch.json()['data']['patch_id']}/decision",
        headers=headers,
        json={"accept": False},
    )
    assert rejected.status_code == 200
    versions = (await async_client.get(f"/api/ai/plans/{run_id}/versions", headers=headers)).json()[
        "data"
    ]
    assert [version["version"] for version in versions] == [2, 1]


@pytest.mark.asyncio
async def test_weather_event_replaces_outdoor_poi_only_after_user_accepts_patch(
    async_client: httpx.AsyncClient,
):
    headers, plan, trip_id = await _active_trip(
        async_client, "weatherguard", "明天从酒店出发去户外公园"
    )
    original_id = plan["stops"][0]["poi"]["id"]
    event_response = await async_client.post(
        f"/api/companion/trips/{trip_id}/events",
        headers=headers,
        json={
            "event_id": "weather-agent-loop-001",
            "type": "WeatherAlertReceived",
            "occurred_at": "2026-07-29T14:30:00+08:00",
            "payload": {"severity": "severe", "outdoor_stop_ids": [original_id]},
        },
    )
    assert event_response.status_code == 200
    event = await _latest_event(trip_id)
    store = InMemoryRuntimeStore()
    decider = ScriptedDecider(
        [
            AgentDecision(action="call_tool", tool="get_weather", reason="查询暴雨风险"),
            AgentDecision(action="call_tool", tool="propose_replan", reason="切换室内备选"),
            AgentDecision(action="finish", reason="等待用户确认"),
        ]
    )
    await process_trip_event(
        store,
        {"trip_id": trip_id, "event_id": event.id, "event_type": event.event_type},
        map_provider=app.state.map_provider,
        weather_provider=HeavyRainWeather(),
        decider=decider,
    )
    processed_event, patch, run_id, _, _ = await _event_and_patch(trip_id)
    assert patch is not None, (
        f"{processed_event.decision_json}; {await _latest_tool_output(trip_id)}"
    )
    operations = json.loads(patch.operations_json)
    replacement = next(item for item in operations if item["operation"] == "replace_stop")
    assert replacement["stop_id"] == original_id
    assert replacement["replacement_stop"]["poi"]["id"] != original_id

    # Formal V1 remains intact until the explicit user decision.
    before = (await async_client.get(f"/api/ai/plans/{run_id}/versions", headers=headers)).json()[
        "data"
    ]
    assert before[0]["version"] == 1
    accepted = await async_client.post(
        f"/api/ai/plans/{run_id}/patches/{patch.id}/decision",
        headers=headers,
        json={"accept": True},
    )
    assert accepted.status_code == 200, accepted.text
    after = (await async_client.get(f"/api/ai/plans/{run_id}/versions", headers=headers)).json()[
        "data"
    ]
    assert [item["version"] for item in after] == [2, 1]
    assert after[0]["snapshot"]["stops"][0]["poi"]["id"] != original_id


@pytest.mark.asyncio
async def test_agent_rejects_illegal_tool_and_stops_at_step_limit(
    async_client: httpx.AsyncClient,
):
    _, _, trip_id = await _active_trip(async_client, "agentpolicy", "明天从酒店出发去博物馆")
    denied, denied_calls = await _exercise_controller(
        trip_id,
        ScriptedDecider(
            [
                AgentDecision(action="call_tool", tool="delete_plan", reason="非法工具"),
                AgentDecision(action="finish", reason="已拒绝"),
            ]
        ),
        max_steps=2,
    )
    assert denied["status"] == "succeeded"
    assert denied_calls[0].status == "policy_denied"
    assert denied_calls[0].error_type == "tool_not_registered"

    blocked, blocked_calls = await _exercise_controller(
        trip_id,
        ScriptedDecider(
            [
                AgentDecision(
                    action="call_tool",
                    tool="create_plan_patch",
                    reason="registered but outside Companion Agent boundary",
                ),
                AgentDecision(action="finish", reason="rejected by role boundary"),
            ]
        ),
        max_steps=2,
    )
    assert blocked["status"] == "succeeded"
    assert blocked_calls[0].status == "policy_denied"
    assert blocked_calls[0].error_type == "tool_not_allowed_for_agent"

    planning_tool, planning_tool_calls = await _exercise_controller(
        trip_id,
        ScriptedDecider(
            [
                AgentDecision(
                    action="call_tool",
                    tool="search_poi",
                    reason="planning-period tool must stay outside Companion",
                ),
                AgentDecision(action="finish", reason="role boundary held"),
            ]
        ),
        max_steps=2,
    )
    assert planning_tool["status"] == "succeeded"
    assert planning_tool_calls[0].status == "policy_denied"
    assert planning_tool_calls[0].error_type == "tool_not_allowed_for_agent"

    limited, calls = await _exercise_controller(
        trip_id,
        ScriptedDecider(
            [AgentDecision(action="call_tool", tool="get_trip_state", reason="继续观察")]
        ),
        max_steps=2,
    )
    assert limited["status"] == "step_limit_reached"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_plan_patch_cas_allows_only_one_writer_for_a_base_version(
    async_client: httpx.AsyncClient,
):
    _, _, trip_id = await _active_trip(async_client, "caswriter", "明天下午从酒店出发去博物馆")
    async with SessionLocal() as db:
        trip = await db.get(TripSession, trip_id)
        assert trip is not None
        patches = [
            PlanPatch(
                planning_run_id=trip.planning_run_id,
                user_id=trip.user_id,
                base_version=1,
                operations_json=json.dumps(
                    [{"operation": "change_transport_mode", "transport_mode": "driving"}]
                ),
                reason="single writer CAS test",
                impact_json="{}",
                status="pending",
            )
            for _ in range(2)
        ]
        db.add_all(patches)
        await db.commit()
        first_id, second_id = patches[0].id, patches[1].id

        applied = await apply_plan_patch_cas(
            db=db,
            patch_id=first_id,
            provider=app.state.map_provider,
            trace_id="cas-test",
            policy_result="integration_test",
        )
        assert applied["plan_version"] == 2
        with pytest.raises(AppError) as conflict:
            await apply_plan_patch_cas(
                db=db,
                patch_id=second_id,
                provider=app.state.map_provider,
                trace_id="cas-test",
                policy_result="integration_test",
            )
        assert conflict.value.code == "PLAN_VERSION_CONFLICT"
        versions = (
            await db.scalars(
                select(PlanVersion).where(PlanVersion.planning_run_id == trip.planning_run_id)
            )
        ).all()
        assert sorted(version.version for version in versions) == [1, 2]


@pytest.mark.asyncio
async def test_closed_poi_is_replaced_and_deadline_is_revalidated_before_v2(
    async_client: httpx.AsyncClient,
):
    headers, plan, trip_id = await _active_trip(
        async_client, "closedpoi", "明天下午两点从酒店出发去公园，晚上十一点前到医院"
    )
    closed_id = plan["stops"][0]["poi"]["id"]
    event_response = await async_client.post(
        f"/api/companion/trips/{trip_id}/events",
        headers=headers,
        json={
            "event_id": "closed-poi-agent-loop-001",
            "type": "PoiStatusChanged",
            "occurred_at": "2026-07-29T14:30:00+08:00",
            "payload": {"closed_poi_id": closed_id},
        },
    )
    assert event_response.status_code == 200
    event = await _latest_event(trip_id)
    await process_trip_event(
        InMemoryRuntimeStore(),
        {"trip_id": trip_id, "event_id": event.id, "event_type": event.event_type},
        map_provider=app.state.map_provider,
        weather_provider=app.state.weather_provider,
        decider=ScriptedDecider(
            [
                AgentDecision(action="call_tool", tool="propose_replan", reason="地点关闭"),
                AgentDecision(action="finish", reason="等待用户确认"),
            ]
        ),
    )
    processed_event, patch, run_id, _, _ = await _event_and_patch(trip_id)
    assert patch is not None, (
        f"{processed_event.decision_json}; {await _latest_tool_output(trip_id)}"
    )
    impact = json.loads(patch.impact_json)
    assert impact["after"]["constraint_conflicts"] == []
    assert any(item["operation"] == "replace_stop" for item in json.loads(patch.operations_json))
    accepted = await async_client.post(
        f"/api/ai/plans/{run_id}/patches/{patch.id}/decision",
        headers=headers,
        json={"accept": True},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["snapshot"]["stops"][-1]["constraint_satisfied"] is True
