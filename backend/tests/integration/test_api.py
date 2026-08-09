from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402


def test_auth_plan_and_ai_pipeline():
    with TestClient(app) as client:
        capabilities = client.get("/api/ai/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["data"]["persistence"] is True
        assert capabilities.json()["data"]["max_tasks"] == 24

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
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["data"]["plan_version"] == 2

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
