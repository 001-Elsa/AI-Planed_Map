import asyncio
import uuid

import pytest

from backend.app.core.config import get_settings


@pytest.mark.asyncio
async def test_concurrent_registration_and_login_do_not_return_server_errors(async_client):
    prefix = f"concurrent{uuid.uuid4().hex[:6]}"
    usernames = [f"{prefix}{index}" for index in range(8)]

    registrations = await asyncio.gather(
        *(
            async_client.post(
                "/api/register",
                json={"username": username, "password": "secret12", "nickname": username},
                headers={"X-Device-Name": f"test-device-{index}"},
            )
            for index, username in enumerate(usernames)
        )
    )
    assert [response.status_code for response in registrations] == [200] * len(usernames)

    logins = await asyncio.gather(
        *(
            async_client.post(
                "/api/login",
                json={"username": f"  {username}  ", "password": "secret12"},
                headers={"X-Device-Name": f"login-device-{index}"},
            )
            for index, username in enumerate(usernames)
        )
    )
    assert [response.status_code for response in logins] == [200] * len(usernames)

    authenticated = await asyncio.gather(
        *(
            async_client.get(
                "/api/me",
                headers={"Authorization": f"Bearer {response.json()['data']['token']}"},
            )
            for response in logins
        )
    )
    assert [response.status_code for response in authenticated] == [200] * len(usernames)


@pytest.mark.asyncio
async def test_duplicate_registration_race_has_one_winner(async_client):
    username = f"duplicate{uuid.uuid4().hex[:8]}"
    responses = await asyncio.gather(
        *(
            async_client.post(
                "/api/register",
                json={"username": username, "password": "secret12", "nickname": username},
            )
            for _ in range(6)
        )
    )
    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 1
    assert statuses.count(409) == 5
    assert all(response.status_code < 500 for response in responses)


@pytest.mark.asyncio
async def test_authenticated_users_behind_one_ip_have_separate_rate_budgets(async_client):
    settings = get_settings()
    original_session_limit = settings.api_requests_per_minute
    original_ip_limit = settings.api_ip_requests_per_minute
    settings.api_requests_per_minute = 2
    settings.api_ip_requests_per_minute = 100
    try:
        prefix = f"sharedip{uuid.uuid4().hex[:6]}"
        registrations = []
        for index in range(2):
            response = await async_client.post(
                "/api/register",
                json={
                    "username": f"{prefix}{index}",
                    "password": "secret12",
                    "nickname": f"{prefix}{index}",
                },
                headers={"X-Device-Id": f"shared-ip-device-{index}"},
            )
            assert response.status_code == 200
            registrations.append(response)

        responses = await asyncio.gather(
            *(
                async_client.get(
                    "/api/me",
                    headers={"Authorization": f"Bearer {registration.json()['data']['token']}"},
                )
                for registration in registrations
                for _ in range(2)
            )
        )
        assert [response.status_code for response in responses] == [200] * 4
    finally:
        settings.api_requests_per_minute = original_session_limit
        settings.api_ip_requests_per_minute = original_ip_limit
