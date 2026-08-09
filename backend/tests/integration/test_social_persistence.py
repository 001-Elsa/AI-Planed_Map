import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from backend.app.main import app


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_personal_data_survives_app_restart_and_daily_leaderboard_is_complete():
    suffix = uuid.uuid4().hex[:7]
    first_username = f"persist{suffix}a"
    second_username = f"persist{suffix}b"

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/api/register",
                json={"username": first_username, "password": "secret12"},
            )
            assert first.status_code == 200
            first_headers = auth(first.json()["data"]["token"])
            assert (
                await client.post(
                    "/api/checkins",
                    headers=first_headers,
                    json={
                        "name": "持久足迹",
                        "note": "关闭网页后仍应存在",
                        "emoji": "📍",
                        "lng": 116.397,
                        "lat": 39.908,
                    },
                )
            ).status_code == 200
            assert (
                await client.post(
                    "/api/favorites",
                    headers=first_headers,
                    json={
                        "name": "持久收藏",
                        "address": "测试地址",
                        "lng": 116.398,
                        "lat": 39.909,
                        "mode": "food",
                    },
                )
            ).status_code == 200
            assert (
                await client.post(
                    "/api/tracks",
                    headers=first_headers,
                    json={
                        "kind": "run",
                        "name": "今日跑步",
                        "distance": 5200,
                        "duration": 1800,
                        "path": [[116.397, 39.908], [116.398, 39.909]],
                        "real": True,
                    },
                )
            ).status_code == 200

    # A fresh application lifespan simulates closing the page/service and
    # returning later. Authentication is recreated; records must come from DB.
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post(
                "/api/login",
                json={"username": first_username, "password": "secret12"},
            )
            assert login.status_code == 200
            first_headers = auth(login.json()["data"]["token"])
            checkins = await client.get("/api/checkins", headers=first_headers)
            favorites = await client.get("/api/favorites", headers=first_headers)
            tracks = await client.get("/api/tracks", headers=first_headers)
            assert checkins.json()["data"][0]["name"] == "持久足迹"
            assert favorites.json()["data"][0]["name"] == "持久收藏"
            assert tracks.json()["data"][0]["name"] == "今日跑步"

            stats = (await client.get("/api/stats", headers=first_headers)).json()["data"]
            assert len(stats["weekly"]) == 8
            assert len(stats["daily"]) == 30
            assert stats["recentCheckins"][0]["name"] == "持久足迹"
            assert stats["counts"]["favorites"] == 1
            assert stats["counts"]["checkins"] == 1
            assert stats["counts"]["tracks"] == 1

            second = await client.post(
                "/api/register",
                json={"username": second_username, "password": "secret12"},
            )
            assert second.status_code == 200
            second_headers = auth(second.json()["data"]["token"])
            assert (await client.get("/api/favorites", headers=second_headers)).json()["data"] == []

            requested = await client.post(
                "/api/friends/request",
                headers=first_headers,
                json={"username": second_username},
            )
            assert requested.status_code == 200
            incoming = (await client.get("/api/friends", headers=second_headers)).json()["data"]
            request_id = incoming["incoming"][0]["id"]
            assert (
                await client.post(
                    "/api/friends/respond",
                    headers=second_headers,
                    json={"id": request_id, "accept": True},
                )
            ).status_code == 200
            assert (
                await client.post(
                    "/api/tracks",
                    headers=second_headers,
                    json={
                        "kind": "ride",
                        "name": "今日骑行",
                        "distance": 8100,
                        "duration": 1600,
                        "path": [[116.4, 39.9], [116.41, 39.91]],
                        "real": True,
                    },
                )
            ).status_code == 200

            leaderboard = (
                await client.get("/api/leaderboard?days=1", headers=first_headers)
            ).json()["data"]
            assert leaderboard["periodStart"] == leaderboard["periodEnd"]
            assert leaderboard["timezone"] == "Asia/Shanghai"
            assert len(leaderboard["rows"]) == 2
            assert leaderboard["rows"][0]["distance"] == 8100
            assert leaderboard["rows"][0]["daily"][0]["count"] == 1
            assert leaderboard["nextDailyRefreshAt"] > leaderboard["updatedAt"]
