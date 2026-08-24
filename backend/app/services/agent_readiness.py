"""Readiness checks for moving Critic Agent from shadow to enforce."""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import Settings
from backend.app.models import AgentArtifact, AgentRun, AgentWorkflowRun
from backend.app.schemas.agent_artifacts import AgentWorkflowMode


def _rate(count: int, total: int) -> float:
    return count / total if total else 0.0


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def _verdict(payload_json: str) -> str | None:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    verdict = payload.get("verdict")
    return verdict if isinstance(verdict, str) else None


def _thresholds(settings: Settings) -> dict[str, float | int]:
    return {
        "min_shadow_samples": settings.critic_enforce_min_shadow_samples,
        "max_fallback_rate": settings.critic_enforce_max_fallback_rate,
        "max_blocking_rate": settings.critic_enforce_max_blocking_rate,
        "max_budget_exceeded_rate": settings.critic_enforce_max_budget_exceeded_rate,
        "max_p95_latency_ms": settings.critic_enforce_max_p95_latency_ms,
    }


def build_critic_readiness_report(
    *,
    settings: Settings,
    shadow_workflows: list[AgentWorkflowRun],
    critic_runs: list[AgentRun],
    critic_artifacts: list[AgentArtifact],
) -> dict[str, Any]:
    verdicts = [item for item in (_verdict(row.payload_json) for row in critic_artifacts) if item]
    verdict_counts = Counter(verdicts)
    sample_count = len(verdicts)
    fallback_rate = _rate(sum(1 for run in critic_runs if run.fallback_used), len(critic_runs))
    blocking_rate = _rate(verdict_counts["needs_clarification"], sample_count)
    budget_rate = _rate(
        sum(1 for workflow in shadow_workflows if workflow.status == "budget_exceeded"),
        len(shadow_workflows),
    )
    p95_latency_ms = _p95(
        [int(run.latency_ms or 0) for run in critic_runs if run.latency_ms is not None]
    )
    thresholds = _thresholds(settings)
    checks = {
        "sample_count": sample_count >= thresholds["min_shadow_samples"],
        "fallback_rate": fallback_rate <= thresholds["max_fallback_rate"],
        "blocking_rate": blocking_rate <= thresholds["max_blocking_rate"],
        "budget_exceeded_rate": budget_rate <= thresholds["max_budget_exceeded_rate"],
        "p95_latency_ms": p95_latency_ms <= thresholds["max_p95_latency_ms"],
    }
    ready = all(checks.values())
    return {
        "mode_observed": AgentWorkflowMode.shadow.value,
        "recommendation": "ready_for_enforce" if ready else "keep_shadow",
        "ready": ready,
        "checks": checks,
        "thresholds": thresholds,
        "stats": {
            "shadow_workflow_count": len(shadow_workflows),
            "critic_review_count": sample_count,
            "critic_run_count": len(critic_runs),
            "fallback_rate": round(fallback_rate, 4),
            "blocking_rate": round(blocking_rate, 4),
            "budget_exceeded_rate": round(budget_rate, 4),
            "p95_latency_ms": p95_latency_ms,
            "retry_rate": round(
                _rate(verdict_counts["retry_with_soft_adjustments"], sample_count), 4
            ),
            "verdict_counts": dict(verdict_counts),
        },
        "next_step": (
            "PLAN_CRITIC_MODE=enforce can be tested on a controlled deployment slice."
            if ready
            else "Keep PLAN_CRITIC_MODE=shadow until all readiness checks pass."
        ),
    }


async def evaluate_critic_enforcement_readiness(
    db: AsyncSession,
    settings: Settings,
    *,
    sample_limit: int | None = None,
) -> dict[str, Any]:
    limit = sample_limit or max(settings.critic_enforce_min_shadow_samples * 2, 50)
    shadow_workflows = (
        await db.scalars(
            select(AgentWorkflowRun)
            .where(AgentWorkflowRun.mode == AgentWorkflowMode.shadow.value)
            .order_by(AgentWorkflowRun.id.desc())
            .limit(limit)
        )
    ).all()
    workflow_ids = [row.id for row in shadow_workflows]
    if not workflow_ids:
        return build_critic_readiness_report(
            settings=settings,
            shadow_workflows=[],
            critic_runs=[],
            critic_artifacts=[],
        )

    critic_runs = (
        await db.scalars(
            select(AgentRun)
            .where(
                AgentRun.workflow_run_id.in_(workflow_ids),
                AgentRun.agent_type == "critic",
            )
            .order_by(AgentRun.id.desc())
        )
    ).all()
    critic_artifacts = (
        await db.scalars(
            select(AgentArtifact)
            .where(
                AgentArtifact.workflow_run_id.in_(workflow_ids),
                AgentArtifact.producer_agent == "critic",
                AgentArtifact.artifact_type == "review_report",
            )
            .order_by(AgentArtifact.id.desc())
        )
    ).all()
    return build_critic_readiness_report(
        settings=settings,
        shadow_workflows=list(shadow_workflows),
        critic_runs=list(critic_runs),
        critic_artifacts=list(critic_artifacts),
    )
