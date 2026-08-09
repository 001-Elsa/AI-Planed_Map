import uuid

import pytest

from backend.app.core.config import get_settings


@pytest.mark.asyncio
async def test_user_auth_never_requires_admin_token(async_client):
    username = f"userrole{uuid.uuid4().hex[:8]}"
    registered = await async_client.post(
        "/api/register",
        json={
            "username": username,
            "password": "secret12",
            "nickname": "普通用户",
            "accountType": "user",
        },
    )
    assert registered.status_code == 200, registered.text
    assert registered.json()["data"]["user"]["is_admin"] == 0

    logged_in = await async_client.post(
        "/api/login",
        json={
            "username": username,
            "password": "secret12",
            "accountType": "user",
        },
    )
    assert logged_in.status_code == 200, logged_in.text


@pytest.mark.asyncio
async def test_admin_auth_requires_role_selection_and_token(async_client):
    settings = get_settings()
    original_token = settings.admin_init_token
    settings.admin_init_token = "test-admin-token"
    username = f"adminrole{uuid.uuid4().hex[:8]}"
    try:
        rejected_registration = await async_client.post(
            "/api/register",
            json={
                "username": username,
                "password": "secret12",
                "accountType": "admin",
                "adminInitToken": "wrong-token",
            },
        )
        assert rejected_registration.status_code == 403
        assert rejected_registration.json()["code"] == "ADMIN_INIT_INVALID"

        registered = await async_client.post(
            "/api/register",
            json={
                "username": username,
                "password": "secret12",
                "accountType": "admin",
                "adminInitToken": "test-admin-token",
            },
        )
        assert registered.status_code == 200, registered.text
        assert registered.json()["data"]["user"]["is_admin"] == 1

        user_login = await async_client.post(
            "/api/login",
            json={
                "username": username,
                "password": "secret12",
                "accountType": "user",
            },
        )
        assert user_login.status_code == 403
        assert user_login.json()["code"] == "ADMIN_LOGIN_REQUIRED"

        missing_token = await async_client.post(
            "/api/login",
            json={
                "username": username,
                "password": "secret12",
                "accountType": "admin",
            },
        )
        assert missing_token.status_code == 403
        assert missing_token.json()["code"] == "ADMIN_INIT_INVALID"

        admin_login = await async_client.post(
            "/api/login",
            json={
                "username": username,
                "password": "secret12",
                "accountType": "admin",
                "adminInitToken": "test-admin-token",
            },
        )
        assert admin_login.status_code == 200, admin_login.text
    finally:
        settings.admin_init_token = original_token

