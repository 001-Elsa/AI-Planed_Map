import asyncio

import pytest

from backend.app.core.metrics_server import start_metrics_server
from backend.app.core.observability import MetricsRegistry, metrics


def test_metrics_registry_exports_gauges_and_valid_cumulative_histogram_buckets():
    registry = MetricsRegistry()
    registry.set_service_name("mapgo-agent-test")
    registry.set_gauge("mapgo_agent_pending_messages", 3, {"role": "planner"})
    registry.observe("mapgo_agent_task_duration_ms", 20, {"role": "planner"})

    rendered = registry.render()

    assert 'mapgo_build_info{service="mapgo-agent-test"} 1' in rendered
    assert 'mapgo_agent_pending_messages{role="planner"} 3' in rendered
    assert 'mapgo_agent_task_duration_ms_histogram_bucket{role="planner",le="10"} 0' in rendered
    assert 'mapgo_agent_task_duration_ms_histogram_bucket{role="planner",le="25"} 1' in rendered
    assert 'mapgo_agent_task_duration_ms_histogram_bucket{role="planner",le="100"} 1' in rendered


@pytest.mark.asyncio
async def test_worker_metrics_server_exposes_process_registry():
    metrics.set_gauge("mapgo_agent_pending_messages", 2, {"role": "critic"})
    server = await start_metrics_server(
        service_name="mapgo-agent-critic",
        host="127.0.0.1",
        port=0,
    )
    try:
        socket = server.sockets[0]
        port = int(socket.getsockname()[1])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()

        assert b"HTTP/1.1 200 OK" in response
        assert b'mapgo_build_info{service="mapgo-agent-critic"} 1' in response
        assert b'mapgo_agent_pending_messages{role="critic"} 2' in response
    finally:
        server.close()
        await server.wait_closed()
