import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.app.core.config import get_settings
from backend.app.core.observability import metrics
from backend.app.db.session import engine
from backend.app.main import app


def _register(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/register",
        json={"username": username, "password": "secret12", "nickname": username},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['token']}"}


def test_infeasible_plan_cannot_start_trip_and_conversation_is_idempotent():
    with TestClient(app) as client:
        headers = _register(client, "securityregression")
        request_body = {
            "text": "去附近超市买东西",
            "origin": {"lng": 116.397, "lat": 39.908},
            "transport_mode": "walking",
            "constraints": {"hard": {"max_walking_meters": 0}, "uncertain": []},
        }
        first = client.post(
            "/api/ai/conversations",
            headers={**headers, "Idempotency-Key": "conversation-security-regression"},
            json=request_body,
        )
        assert first.status_code == 200, first.text
        payload = first.json()["data"]
        assert payload["status"] == "infeasible"

        replay = client.post(
            "/api/ai/conversations",
            headers={**headers, "Idempotency-Key": "conversation-security-regression"},
            json=request_body,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["data"]["planning_run_id"] == payload["planning_run_id"]

        trip = client.post(
            "/api/companion/trips",
            headers=headers,
            json={"planning_run_id": payload["planning_run_id"]},
        )
        assert trip.status_code == 409
        assert trip.json()["code"] == "PLAN_NOT_EXECUTABLE"


def test_streaming_body_limit_and_amap_proxy_allowlist():
    with TestClient(app) as client:
        oversized = client.post(
            "/api/login",
            content=(chunk for chunk in (b"{" + b"x" * 600_000, b"x" * 600_000 + b"}")),
            headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
        )
        assert oversized.status_code == 413, oversized.text
        denied = client.get("/_AMapService/v9/unapproved")
        assert denied.status_code == 403
        assert denied.json()["code"] == "AMAP_PATH_DENIED"


def test_random_404_paths_do_not_create_unbounded_metric_labels():
    marker = f"missing-{uuid.uuid4().hex}"
    with TestClient(app) as client:
        response = client.get(f"/{marker}")
        assert response.status_code == 404
    rendered = metrics.render()
    assert marker not in rendered
    assert 'path="__unmatched__"' in rendered


def test_auth_rate_limit_cannot_be_bypassed_with_device_headers():
    settings = get_settings()
    original_identity_limit = settings.auth_device_requests_per_minute
    original_ip_limit = settings.auth_ip_requests_per_minute
    settings.auth_device_requests_per_minute = 1
    settings.auth_ip_requests_per_minute = 100
    try:
        with TestClient(app, client=("rate-limit-test", 50000)) as client:
            first = client.post(
                "/api/login",
                json={"username": "not-a-user", "password": "not-a-password"},
                headers={"X-Device-Id": "device-one"},
            )
            second = client.post(
                "/api/login",
                json={"username": "not-a-user", "password": "not-a-password"},
                headers={"X-Device-Id": "device-two"},
            )
        assert first.status_code == 401
        assert second.status_code == 429
        assert second.json()["code"] == "RATE_LIMITED"
    finally:
        settings.auth_device_requests_per_minute = original_identity_limit
        settings.auth_ip_requests_per_minute = original_ip_limit


def test_correlation_ids_are_sanitized_before_reflection():
    with TestClient(app) as client:
        response = client.get(
            "/api/health",
            headers={"X-Request-ID": "unsafe id", "X-Trace-ID": "<unsafe>"},
        )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"].startswith("req_")
    assert response.headers["X-Request-ID"] != "unsafe id"
    assert response.headers["X-Trace-ID"] != "<unsafe>"
    assert response.headers["Cache-Control"] == "no-store"


def test_new_credentials_and_public_share_tokens_have_safe_minimum_strength():
    username = f"strong{uuid.uuid4().hex[:8]}"
    with TestClient(app) as client:
        weak = client.post(
            "/api/register",
            json={"username": username, "password": "seven77"},
        )
        assert weak.status_code == 422

        headers = _register(client, username)
        shared = client.post(
            "/api/shares",
            headers=headers,
            json={"type": "plan", "payload": {"name": "private route"}},
        )
        assert shared.status_code == 200
        assert len(shared.json()["data"]["token"]) == 32


@pytest.mark.skipif(
    engine.dialect.name != "sqlite",
    reason="SQLite PRAGMA is only valid for the SQLite test database",
)
def test_sqlite_connections_enable_foreign_keys():
    async def check() -> int:
        async with engine.connect() as connection:
            return int(await connection.scalar(text("PRAGMA foreign_keys")) or 0)

    assert asyncio.run(check()) == 1
