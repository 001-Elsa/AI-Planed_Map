"""Optional OpenTelemetry setup and W3C context propagation helpers."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("mapgo.telemetry")

otel_propagate: Any
otel_trace: Any
OtelSpanKind: Any
try:
    from opentelemetry import propagate as otel_propagate
    from opentelemetry import trace as otel_trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import SpanKind as OtelSpanKind
except ImportError:  # Local development remains usable before optional packages are installed.
    otel_propagate = otel_trace = None
    OtelSpanKind = None

_configured = False


def configure_telemetry(
    service_name: str,
    *,
    endpoint: str,
    environment: str,
    sqlalchemy_engine: Any | None = None,
) -> bool:
    global _configured
    if _configured or otel_trace is None:
        return bool(otel_trace)
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service_name, "deployment.environment": environment}
        )
    )
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    otel_trace.set_tracer_provider(provider)
    if sqlalchemy_engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=sqlalchemy_engine)
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    _configured = True
    logger.info("OpenTelemetry configured service=%s exporter=%s", service_name, bool(endpoint))
    return True


def inject_trace_context() -> dict[str, str]:
    carrier: dict[str, str] = {}
    if otel_propagate is not None:
        otel_propagate.inject(carrier)
    return carrier


@contextmanager
def traced(
    name: str,
    *,
    carrier: Mapping[str, str] | None = None,
    kind: str = "internal",
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    if otel_trace is None:
        yield None
        return
    parent = (
        otel_propagate.extract(dict(carrier))
        if carrier and otel_propagate is not None
        else None
    )
    span_kind = {
        "server": OtelSpanKind.SERVER,
        "producer": OtelSpanKind.PRODUCER,
        "consumer": OtelSpanKind.CONSUMER,
    }.get(kind, OtelSpanKind.INTERNAL)
    tracer = otel_trace.get_tracer("mapgo")
    with tracer.start_as_current_span(
        name, context=parent, kind=span_kind, attributes=dict(attributes or {})
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(
                otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc)[:200])
            )
            raise
