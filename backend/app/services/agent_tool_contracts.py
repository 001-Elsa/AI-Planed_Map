"""Strongly typed Agent tool contracts and stable tool result envelopes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import Field, ValidationError

from backend.app.schemas.ai_intent import Coordinate, TransportMode
from backend.app.schemas.common import StrictModel


class EmptyToolArgs(StrictModel):
    pass


class ParseRequirementArgs(StrictModel):
    text: str = Field(min_length=2, max_length=4000)


class SearchPoiArgs(StrictModel):
    keyword: str = Field(min_length=1, max_length=120)
    origin: Coordinate
    city: str | None = Field(default=None, max_length=50)


class SafetyCheckArgs(StrictModel):
    party: dict[str, Any] = Field(default_factory=dict)
    route_constraints: dict[str, Any] = Field(default_factory=dict)


class WeatherQueryArgs(StrictModel):
    location: Coordinate | None = None


class RouteMatrixArgs(StrictModel):
    points: list[Coordinate] = Field(min_length=2, max_length=25)
    transport_mode: TransportMode = TransportMode.walking


class OptimizeRouteArgs(StrictModel):
    candidate_group_count: int = Field(ge=1, le=24)
    transport_mode: TransportMode = TransportMode.walking
    hard_constraints: dict[str, Any] = Field(default_factory=dict)


class TripStateQueryArgs(EmptyToolArgs):
    pass


class CurrentLocationQueryArgs(EmptyToolArgs):
    pass


class ReplanProposalArgs(StrictModel):
    reason: str | None = Field(default=None, max_length=300)


class ToolResultEnvelope(StrictModel):
    success: bool
    error_code: str | None = None
    retryable: bool = False
    source: str = "mapgo"
    expires_at: datetime | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    artifact_ref: str | None = Field(default=None, max_length=128)
    data: dict[str, Any] = Field(default_factory=dict)


TOOL_ARGUMENT_MODELS: dict[str, type[StrictModel]] = {
    "parse_requirement": ParseRequirementArgs,
    "check_travel_safety": SafetyCheckArgs,
    "get_trip_state": TripStateQueryArgs,
    "get_current_location": CurrentLocationQueryArgs,
    "get_weather": WeatherQueryArgs,
    "propose_replan": ReplanProposalArgs,
    "search_poi": SearchPoiArgs,
    "get_route_matrix": RouteMatrixArgs,
    "verify_transit_edges": RouteMatrixArgs,
    "optimize_route": OptimizeRouteArgs,
}


def tool_argument_schema(tool: str) -> dict[str, Any] | None:
    model = TOOL_ARGUMENT_MODELS.get(tool)
    return model.model_json_schema() if model is not None else None


def tool_argument_schemas_for(tools: list[str] | set[str] | frozenset[str]) -> dict[str, Any]:
    return {
        tool: schema for tool in sorted(tools) if (schema := tool_argument_schema(tool)) is not None
    }


def validate_tool_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    model = TOOL_ARGUMENT_MODELS.get(tool)
    if model is None:
        return dict(arguments)
    return model.model_validate(arguments).model_dump(mode="json", exclude_none=True)


def tool_result_success(
    tool: str,
    data: dict[str, Any],
    *,
    source: str = "mapgo",
    expires_at: datetime | None = None,
    confidence: float = 1.0,
    artifact_ref: str | None = None,
) -> ToolResultEnvelope:
    return ToolResultEnvelope(
        success=True,
        source=source,
        expires_at=expires_at,
        confidence=confidence,
        artifact_ref=artifact_ref,
        data=data,
    )


def tool_result_error(
    error_code: str,
    *,
    retryable: bool,
    source: str = "mapgo",
    confidence: float = 0,
    data: dict[str, Any] | None = None,
) -> ToolResultEnvelope:
    return ToolResultEnvelope(
        success=False,
        error_code=error_code,
        retryable=retryable,
        source=source,
        confidence=confidence,
        data=data or {},
    )


def stable_tool_error(exc: Exception) -> str:
    """Map internal exceptions to model-safe error codes."""

    explicit = str(exc).strip().upper()
    if explicit in {
        "UPSTREAM_TIMEOUT",
        "QUOTA_EXHAUSTED",
        "DATA_EXPIRED",
        "INVALID_TOOL_ARGUMENTS",
        "UPSTREAM_ERROR",
    }:
        return explicit
    if isinstance(exc, TimeoutError):
        return "UPSTREAM_TIMEOUT"
    if isinstance(exc, ValidationError):
        return "INVALID_TOOL_ARGUMENTS"
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "UPSTREAM_TIMEOUT"
    if "quota" in name or "rate" in name:
        return "QUOTA_EXHAUSTED"
    if "expired" in name or "stale" in name:
        return "DATA_EXPIRED"
    return "UPSTREAM_ERROR"


def default_tool_expiry(seconds: int = 300) -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=seconds)
