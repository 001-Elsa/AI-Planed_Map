from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Generic, TypeVar

from backend.app.schemas.agent_artifacts import AgentSpec, ArtifactEnvelope

T = TypeVar("T")


def canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
