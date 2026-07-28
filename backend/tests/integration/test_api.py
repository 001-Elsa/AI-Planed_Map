import os
import tempfile
import uuid
from pathlib import Path


database_file = Path(tempfile.gettempdir()) / f"mapgo-test-{uuid.uuid4().hex}.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database_file.as_posix()}"
os.environ["MOCK_MAP_PROVIDER"] = "true"
os.environ["ADMIN_INIT_TOKEN"] = ""
os.environ["ENVIRONMENT"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402


def test_auth_plan_and_ai_pipeline():
    with TestClient(app) as client:
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
        assert payload["algorithm"] == "exact-permutation"

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


def teardown_module():
    database_file.unlink(missing_ok=True)
