"""Minimal HTTP metrics endpoint for non-ASGI worker processes."""

from __future__ import annotations

import asyncio
import logging

from backend.app.core.observability import metrics

logger = logging.getLogger("mapgo.metrics-server")


async def start_metrics_server(
    *, service_name: str, host: str, port: int
) -> asyncio.Server:
    metrics.set_service_name(service_name)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=2)
            path = request_line.decode("latin-1", errors="replace").split(" ")[1]
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=2)
                if line in {b"\r\n", b"\n", b""}:
                    break
            if path == "/metrics":
                body = metrics.render().encode()
                status = b"200 OK"
                content_type = b"text/plain; version=0.0.4; charset=utf-8"
            else:
                body = b"not found\n"
                status = b"404 Not Found"
                content_type = b"text/plain; charset=utf-8"
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Type: "
                + content_type
                + f"\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (IndexError, TimeoutError, UnicodeError):
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, host, port)
    logger.info("Worker metrics listening service=%s address=%s:%s", service_name, host, port)
    return server
