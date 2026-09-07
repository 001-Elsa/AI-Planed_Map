from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from backend.app.schemas.agent_artifacts import (
    AgentSpec,
    AgentToolCallAudit,
    ArtifactEnvelope,
    minimize_agent_payload,
)

T = TypeVar("T")


def canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def audited_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    success: bool,
    output: dict[str, Any] | None = None,
    provider: str | None = None,
    error_type: str | None = None,
    latency_ms: int | None = None,
    authorized: bool = True,
    arguments_valid: bool = True,
) -> AgentToolCallAudit:
    return AgentToolCallAudit(
        tool_name=tool_name,
        input_summary=minimize_agent_payload(arguments),
        output_summary=minimize_agent_payload(output or {}),
        upstream_provider=provider,
        status="succeeded" if success else ("failed" if authorized else "denied"),
        authorized=authorized,
        arguments_valid=arguments_valid,
        error_type=error_type,
        latency_ms=latency_ms,
    )


@dataclass(frozen=True)
class AgentExecution(Generic[T]):
    spec: AgentSpec
    output: T
    artifact: ArtifactEnvelope
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0
    fallback_used: bool = False
    reason: str | None = None
    tool_calls: tuple[AgentToolCallAudit, ...] = ()
    tool_audit_complete: bool = True
