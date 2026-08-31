import pytest

from backend.app.core.config import get_settings


@pytest.mark.asyncio
async def test_mcp_server_is_opt_in_authenticated_and_read_only(async_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mcp_server_enabled", True)
    monkeypatch.setattr(settings, "mcp_internal_token", "test-mcp-token")
    monkeypatch.setattr(settings, "mcp_allowed_origins", "https://trusted.example")
    headers = {
        "Authorization": "Bearer test-mcp-token",
        "Origin": "https://trusted.example",
        "MCP-Protocol-Version": "2025-11-25",
    }

    unauthorized = await async_client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
    )
    assert unauthorized.status_code == 401
    forbidden = await async_client.post(
        "/mcp",
        headers={**headers, "Origin": "https://evil.example"},
        json={"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
    )
    assert forbidden.status_code == 403

    listed = await async_client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200
    names = {item["name"] for item in listed.json()["result"]["tools"]}
    assert names == {"search_poi", "get_route_matrix", "verify_transit_edges", "get_weather"}
    assert "propose_replan" not in names
    assert "optimize_route" not in names

    called = await async_client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "search_poi",
                "arguments": {
                    "keyword": "museum",
                    "origin": {"lng": 120.1, "lat": 30.2},
                    "city": "Hangzhou",
                },
            },
        },
    )
    result = called.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["success"] is True
    assert result["structuredContent"]["data"]["candidates"]

    denied = await async_client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "propose_replan", "arguments": {}},
        },
    )
    assert denied.json()["error"]["code"] == -32602
