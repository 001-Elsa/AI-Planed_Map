import asyncio

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select

from backend.app.db.session import SessionLocal
from backend.app.main import app  # noqa: E402
from backend.app.models import AgentArtifact, AgentHandoff, AgentWorkflowTask


def test_auth_plan_and_ai_pipeline():
    with TestClient(app) as client:
        capabilities = client.get("/api/ai/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["data"]["persistence"] is True
        assert capabilities.json()["data"]["max_tasks"] == 24
        shared_capability = capabilities.json()["data"]["multi_agent"]["shared_state"]
        evaluation_capability = capabilities.json()["data"]["multi_agent"]["evaluation"]
        hitl_capability = capabilities.json()["data"]["multi_agent"]["human_in_the_loop"]
        role_contracts = capabilities.json()["data"]["multi_agent"]["role_contracts"]
        agent_runtime = capabilities.json()["data"]["multi_agent"]["agent_runtime"]
        tool_schemas = capabilities.json()["data"]["multi_agent"]["tool_argument_schemas"]
        model_router = capabilities.json()["data"]["multi_agent"]["model_router"]
        assert evaluation_capability["runtime_critic_scoring"] is True
        assert evaluation_capability["hard_fail_zero_score"] is True
        assert hitl_capability["enabled"] is True
        assert hitl_capability["reject_replans"] is True
        assert set(role_contracts) == {
            "requirement_clarification",
            "place_research",
            "itinerary_coordination",
            "plan_review",
            "runtime_companion",
        }
        assert agent_runtime["implementation"] == "framework_independent"
        assert agent_runtime["spec_driven"] is True
        assert agent_runtime["active_model_tool_loops"] == ["companion"]
        assert set(agent_runtime["runtime_eligible_roles"]) >= {"search", "planner", "critic"}
        assert "get_weather" in tool_schemas["companion"]
        assert "search_poi" in tool_schemas["search_internal"]
        assert "get_route_matrix" in tool_schemas["planner_internal"]
        assert model_router["role_policy"]["planner"] == "deterministic_route_optimizer"
        assert model_router["role_policy"]["critic"] == "rule_or_strong_hybrid"
        assert model_router["high_risk_action"] == "hitl"
        assert shared_capability["version"] == "1.0"
        assert shared_capability["optimistic_concurrency"] is True
        assert shared_capability["role_scoped_views"] is True

        registered = client.post(
            "/api/register",
            json={"username": "tester", "password": "secret12", "nickname": "测试者"},
        )
        assert registered.status_code == 200, registered.text
        token = registered.json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        ai = client.post(
            "/api/ai/plans",
            headers={**headers, "Idempotency-Key": "same-request"},
            json={
                "text": "明天下午两点从学校出发，先取快递，再买水果，五点前到医院",
                "origin": {"lng": 116.397, "lat": 39.908},
                "transport_mode": "walking",
            },
        )
        assert ai.status_code == 200, ai.text
        payload = ai.json()["data"]
        assert payload["status"] in ("success", "infeasible")
        assert len(payload["stops"]) == 3
        assert payload["algorithm"] == "joint-exact-enumeration"
        assert payload["plan_version"] == 1
        assert payload["confidence"] > 0
        assert all(stop["travel"]["source"] for stop in payload["stops"])
        assert len(payload["candidate_reviews"]) == 3
        assert all(item["considered_count"] > 0 for item in payload["candidate_reviews"])
        assert payload["execution"]["formal_plan_persisted"] is True
        assert payload["execution"]["map_provider"]
        assert payload["execution"]["stages"][-1]["key"] == "persist"
        assert payload["agent_workflow"]["workflow_id"] > 0
        assert [step["agent_type"] for step in payload["agent_workflow"]["steps"]] == [
            "supervisor",
            "intent",
            "supervisor",
            "search",
            "planner",
            "critic",
            "supervisor",
        ]
        assert [
            (message["sender"], message["receiver"])
            for message in payload["agent_workflow"]["messages"]
        ] == [
            ("user", "supervisor"),
            ("supervisor", "intent"),
            ("intent", "supervisor"),
            ("supervisor", "search"),
            ("search", "planner"),
            ("planner", "critic"),
            ("critic", "supervisor"),
            ("supervisor", "final_answer"),
        ]
        assert payload["agent_workflow"]["messages"][0]["content_summary"]["text"] == (
            "[REDACTED_TEXT]"
        )
        shared_state = payload["agent_workflow"]["shared_state"]
        assert shared_state["task_id"] == payload["agent_workflow"]["task_id"]
        assert shared_state["phase"] == "finalized"
        assert shared_state["revision"] >= 5
        assert shared_state["candidate_count"] >= 3
        assert shared_state["stop_count"] == 3
        assert len(shared_state["state_hash"]) == 64
        graph_counts = asyncio.run(_agent_graph_counts(payload["agent_workflow"]["workflow_id"]))
        assert graph_counts["tasks"] == len(payload["agent_workflow"]["steps"])
        assert graph_counts["handoffs"] == len(payload["agent_workflow"]["messages"])
        assert graph_counts["active_artifacts"] == len(payload["agent_workflow"]["steps"])
        assert payload["critic_review"]["verdict"] in {
            "approved",
            "approved_with_warnings",
            "retry_with_soft_adjustments",
        }

        replay = client.post(
            "/api/ai/plans",
            headers={**headers, "Idempotency-Key": "same-request"},
            json={
                "text": "明天下午两点从学校出发，先取快递，再买水果，五点前到医院",
                "origin": {"lng": 116.397, "lat": 39.908},
                "transport_mode": "walking",
            },
        )
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        assert replay.json()["data"]["planning_run_id"] == payload["planning_run_id"]

        patch = client.post(
            f"/api/ai/plans/{payload['planning_run_id']}/patches",
            headers=headers,
            json={
                "base_version": 1,
                "operations": [{"operation": "move_stop", "from_position": 0, "to_position": 1}],
                "reason": "用户希望先完成第二项任务",
                "impact": {"source": "integration_test"},
            },
        )
        assert patch.status_code == 200, patch.text
        patch_id = patch.json()["data"]["patch_id"]
        accepted = client.post(
            f"/api/ai/plans/{payload['planning_run_id']}/patches/{patch_id}/decision",
            headers=headers,
            json={"accept": True},
        )
        assert accepted.status_code == 409, accepted.text
        assert accepted.json()["code"] == "PATCH_INFEASIBLE"
        assert "任务先后顺序" in accepted.json()["details"]["conflicts"][0]

        valid_patch = client.post(
            f"/api/ai/plans/{payload['planning_run_id']}/patches",
            headers=headers,
            json={
                "base_version": 1,
                "operations": [{"operation": "change_transport_mode", "transport_mode": "driving"}],
                "reason": "用户确认改为驾车",
                "impact": {"source": "integration_test"},
            },
        )
        valid_patch_id = valid_patch.json()["data"]["patch_id"]
        accepted = client.post(
            f"/api/ai/plans/{payload['planning_run_id']}/patches/{valid_patch_id}/decision",
            headers=headers,
            json={"accept": True},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["data"]["plan_version"] == 2
        stale_counts = asyncio.run(_agent_graph_counts(payload["agent_workflow"]["workflow_id"]))
        assert stale_counts["stale_artifacts"] == graph_counts["active_artifacts"]

        versions = client.get(
            f"/api/ai/plans/{payload['planning_run_id']}/versions",
            headers=headers,
        )
        version_data = versions.json()["data"]
        assert [item["version"] for item in version_data] == [2, 1]
        assert version_data[1]["snapshot"]["execution"]["formal_plan_persisted"] is True

        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "mapgo_planning_results_total" in metrics.text
        assert "mapgo_route_fallback_edges_total" in metrics.text

        trip = client.post(
            "/api/companion/trips",
            headers=headers,
            json={"planning_run_id": payload["planning_run_id"]},
        )
        assert trip.status_code == 200, trip.text
        trip_id = trip.json()["data"]["trip_id"]
        active = client.post(
            f"/api/companion/trips/{trip_id}/transition",
            headers=headers,
            json={"target_state": "ACTIVE_TRIP", "reason": "用户确认出发"},
        )
        assert active.json()["data"]["state"] == "ACTIVE_TRIP"

        first_stop_id = payload["stops"][0]["poi"]["id"]
        second_stop_id = payload["stops"][1]["poi"]["id"]
        for event_id, event_type, stop_id in (
            ("stop-complete-first", "PlanStopCompleted", first_stop_id),
            ("stop-skip-first", "PlanStopSkipped", first_stop_id),
            ("stop-complete-second", "PlanStopCompleted", second_stop_id),
        ):
            outcome = client.post(
                f"/api/companion/trips/{trip_id}/events",
                headers=headers,
                json={
                    "event_id": event_id,
                    "type": event_type,
                    "occurred_at": "2026-07-29T14:00:00+08:00",
                    "payload": {
                        "stop_id": stop_id,
                        "planned_arrival": "2026-07-29T14:00:00+08:00",
                        "arrived_at": (
                            "2026-07-29T14:02:00+08:00"
                            if event_type == "PlanStopCompleted"
                            else None
                        ),
                    },
                },
            )
            assert outcome.status_code == 200, outcome.text
        progress = client.get(f"/api/companion/trips/{trip_id}/summary", headers=headers)
        assert progress.status_code == 200, progress.text
        progress_data = progress.json()["data"]
        assert progress_data["completed_stops"] == 1
        assert progress_data["skipped_stop_ids"] == [first_stop_id]
        first_progress = next(
            item for item in progress_data["stop_deviations"] if item["stop_id"] == first_stop_id
        )
        assert first_progress["completed"] is False
        assert first_progress["skipped"] is True

        location_body = {
            "event_id": "location-event-0001",
            "location": {"lng": 116.397, "lat": 39.908},
            "accuracy_meters": 12,
            "captured_at": "2026-07-29T14:00:00+08:00",
        }
        denied = client.post(
            f"/api/companion/trips/{trip_id}/location",
            headers=headers,
            json=location_body,
        )
        assert denied.status_code == 403
        consent = client.post(
            f"/api/companion/trips/{trip_id}/consents",
            headers=headers,
            json={"scope": "precise_location", "granted": True},
        )
        assert consent.status_code == 200
        location = client.post(
            f"/api/companion/trips/{trip_id}/location",
            headers=headers,
            json=location_body,
        )
        assert location.status_code == 200
        assert location.json()["data"]["deduplicated"] is False

        delay_body = {
            "event_id": "schedule-delay-0001",
            "type": "ScheduleDelayDetected",
            "occurred_at": "2026-07-29T15:00:00+08:00",
            "payload": {"delay_minutes": 35},
        }
        delay = client.post(
            f"/api/companion/trips/{trip_id}/events",
            headers=headers,
            json=delay_body,
        )
        assert delay.status_code == 200
        assert delay.json()["data"]["state"] == "AT_RISK"
        assert delay.json()["data"]["impact_level"] == "critical"
        duplicate = client.post(
            f"/api/companion/trips/{trip_id}/events",
            headers=headers,
            json=delay_body,
        )
        assert duplicate.json()["data"]["deduplicated"] is True

        replan = client.post(
            f"/api/companion/trips/{trip_id}/replan",
            headers=headers,
            json={
                "current_location": payload["stops"][-1]["poi"]["location"],
                "current_time": "2026-07-29T14:30:00+08:00",
                "completed_stop_ids": [payload["stops"][0]["poi"]["id"]],
                "reason": "延误后从当前位置重算剩余行程",
            },
        )
        assert replan.status_code == 200, replan.text
        assert replan.json()["data"]["status"] in {
            "patch_pending_confirmation",
            "current_plan_still_feasible",
            "no_feasible_replan",
        }

        tool = client.post(
            f"/api/companion/trips/{trip_id}/tools/execute",
            headers=headers,
            json={"tool": "get_trip_state", "arguments": {}},
        )
        assert tool.status_code == 200, tool.text
        assert tool.json()["data"]["audited"] is True

        disallowed_tool = client.post(
            f"/api/companion/trips/{trip_id}/tools/execute",
            headers=headers,
            json={
                "tool": "search_poi",
                "arguments": {"keyword": "museum", "origin": {"lng": 116.397, "lat": 39.908}},
            },
        )
        assert disallowed_tool.status_code == 403
        assert disallowed_tool.json()["code"] == "AGENT_TOOL_NOT_ALLOWED_FOR_ROLE"

        weather = client.post(
            f"/api/companion/trips/{trip_id}/pretrip-check",
            headers=headers,
            json={"location": {"lng": 116.397, "lat": 39.908}},
        )
        assert weather.status_code == 200
        assert weather.json()["data"]["original_plan_changed"] is False

        implicit_preference = client.post(
            "/api/companion/preferences",
            headers=headers,
            json={"key": "daily_walking_limit", "value": 6000, "confirmed": False},
        )
        assert implicit_preference.status_code == 409


async def _agent_graph_counts(workflow_id: int) -> dict[str, int]:
    async with SessionLocal() as db:
        tasks = (
            await db.scalars(
                select(AgentWorkflowTask).where(AgentWorkflowTask.workflow_run_id == workflow_id)
            )
        ).all()
        handoffs = (
            await db.scalars(
                select(AgentHandoff).where(AgentHandoff.workflow_run_id == workflow_id)
            )
        ).all()
        artifacts = (
            await db.scalars(
                select(AgentArtifact).where(AgentArtifact.workflow_run_id == workflow_id)
            )
        ).all()
        return {
            "tasks": len(tasks),
            "handoffs": len(handoffs),
            "active_artifacts": sum(1 for item in artifacts if item.status == "active"),
            "stale_artifacts": sum(1 for item in artifacts if item.status == "stale"),
        }


def test_confirmed_long_term_memory_is_applied_listed_and_revocable():
    with TestClient(app) as client:
        registered = client.post(
            "/api/register",
            json={"username": "memoryuser", "password": "secret12", "nickname": "记忆用户"},
        )
        token = registered.json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        invalid = client.post(
            "/api/companion/preferences",
            headers=headers,
            json={"key": "home_address", "value": "private", "confirmed": True},
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "LONG_TERM_MEMORY_INVALID"

        saved = client.post(
            "/api/companion/preferences",
            headers=headers,
            json={"key": "minimize_walking", "value": True, "confirmed": True},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["source"] == "explicit_user_confirmation"

        planned = client.post(
            "/api/ai/plans",
            headers=headers,
            json={
                "text": "从酒店去博物馆",
                "origin": {"lng": 120.62, "lat": 31.32},
            },
        )
        assert planned.status_code == 200, planned.text
        plan = planned.json()["data"]
        assert plan["intent"]["preferences"]["minimize_walking"] is True
        assert plan["memory"]["applied_keys"] == ["minimize_walking"]
        assert plan["memory"]["values_included"] is False

        overridden = client.post(
            "/api/ai/plans",
            headers=headers,
            json={
                "text": "从酒店去公园",
                "origin": {"lng": 120.62, "lat": 31.32},
                "preferences_answers": {"minimize_walking": False},
            },
        )
        assert overridden.status_code == 200, overridden.text
        override_data = overridden.json()["data"]
        assert override_data["intent"]["preferences"]["minimize_walking"] is False
        assert override_data["memory"]["applied_keys"] == []
        assert override_data["memory"]["skipped_explicit_keys"] == ["minimize_walking"]

        listed = client.get("/api/companion/preferences", headers=headers)
        assert listed.status_code == 200
        assert [(item["key"], item["value"]) for item in listed.json()["data"]] == [
            ("minimize_walking", True)
        ]

        deleted = client.delete(
            "/api/companion/preferences/minimize_walking", headers=headers
        )
        assert deleted.status_code == 200
        assert client.get("/api/companion/preferences", headers=headers).json()["data"] == []


def test_multi_turn_planning_conversation_is_versioned():
    with TestClient(app) as client:
        registered = client.post(
            "/api/register",
            json={"username": "dialog", "password": "secret12", "nickname": "多轮用户"},
        )
        token = registered.json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        draft = client.post(
            "/api/ai/conversations",
            headers=headers,
            json={
                "text": "明天下午带父母去逛园林，晚上七点前回来，尽量少走路",
                "transport_mode": "walking",
            },
        )
        assert draft.status_code == 200, draft.text
        first = draft.json()["data"]
        assert first["status"] == "need_clarification"
        assert first["planning_state"] == "NEED_CLARIFICATION"
        fields = {item["field"] for item in first["questions"]}
        assert "origin" in fields
        assert "constraints.hard.max_walking_meters" in fields

        completed = client.patch(
            f"/api/ai/conversations/{first['conversation_id']}",
            headers=headers,
            json={
                "base_revision": first["conversation_revision"],
                "answers": {
                    "origin": {"lng": 120.62, "lat": 31.32},
                    "constraints.hard.max_walking_meters": 3500,
                },
            },
        )
        assert completed.status_code == 200, completed.text
        final = completed.json()["data"]
        assert final["conversation_revision"] == 2
        assert final["status"] in {"success", "infeasible"}
        assert final["planning_run_id"] > 0
        assert final["plan_version"] == 1

        overview = client.get("/api/ai/plans/overview?limit=3", headers=headers)
        assert overview.status_code == 200, overview.text
        workspace = overview.json()["data"]
        assert workspace["total_runs"] >= 1
        assert workspace["formal_plans"] >= 1
        assert workspace["recent"][0]["planning_run_id"] == final["planning_run_id"]
        assert workspace["recent"][0]["plan_version"] == 1
        assert workspace["recent"][0]["snapshot"]["stops"]
