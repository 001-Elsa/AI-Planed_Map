"""CAS commit boundary for validated PlanPatch -> immutable PlanVersion."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.exceptions import AppError
from backend.app.models import (
    AgentArtifact,
    AgentWorkflowRun,
    DecisionAuditLog,
    PlanPatch,
    PlanVersion,
    TripEvent,
    TripSession,
)
from backend.app.services.plan_patch_validator import (
    apply_patch_structure,
    recalculate_and_validate_snapshot,
)


async def apply_plan_patch_cas(
    *,
    db: AsyncSession,
    patch_id: int,
    provider: Any,
    trace_id: str | None,
    policy_result: str,
) -> dict[str, Any]:
    """Validate and atomically advance N -> N+1; stale writers fail closed."""
    patch = await db.scalar(select(PlanPatch).where(PlanPatch.id == patch_id).with_for_update())
    if patch is None:
        raise AppError(404, "PLAN_PATCH_NOT_FOUND", "计划补丁不存在")
    if patch.status != "pending":
        raise AppError(409, "PLAN_PATCH_ALREADY_DECIDED", "计划补丁已经处理")
    current_row = await db.scalar(
        select(PlanVersion)
        .where(PlanVersion.planning_run_id == patch.planning_run_id)
        .order_by(PlanVersion.version.desc())
        .limit(1)
        .with_for_update()
    )
    current = current_row.version if current_row is not None else None
    if current != patch.base_version:
        raise AppError(
            409,
            "PLAN_VERSION_CONFLICT",
            "计划版本已经变化，旧补丁不能继续应用",
            {"current_version": current},
        )
    assert current_row is not None
    snapshot = json.loads(current_row.snapshot_json)
    operations = json.loads(patch.operations_json)
    stops = apply_patch_structure(snapshot, operations)
    impact = json.loads(patch.impact_json or "{}")
    snapshot, conflicts = await recalculate_and_validate_snapshot(
        snapshot, stops, provider, impact.get("replan_context")
    )
    if conflicts:
        patch.status = "blocked"
        patch.decided_at = datetime.now(timezone.utc)
        db.add(
            DecisionAuditLog(
                planning_run_id=patch.planning_run_id,
                user_id=patch.user_id,
                action="apply_plan_patch",
                reason=patch.reason,
                evidence_json=json.dumps({"conflicts": conflicts}, ensure_ascii=False),
                policy_result="blocked_by_constraint_validator",
                trace_id=trace_id,
            )
        )
        await db.commit()
        raise AppError(
            409,
            "PATCH_INFEASIBLE",
            "补丁违反硬约束，正式计划未修改",
            {"conflicts": conflicts},
        )

    decided_at = datetime.now(timezone.utc)
    next_version = current + 1
    db.add(
        PlanVersion(
            planning_run_id=patch.planning_run_id,
            user_id=patch.user_id,
            version=next_version,
            snapshot_json=json.dumps(snapshot, ensure_ascii=False),
            change_reason=patch.reason,
        )
    )
    patch.status = "accepted"
    patch.decided_at = decided_at
    trips = (
        await db.scalars(
            select(TripSession).where(
                TripSession.planning_run_id == patch.planning_run_id,
                TripSession.current_plan_version == current,
            )
        )
    ).all()
    for trip in trips:
        trip.current_plan_version = next_version
        if trip.state == "REPLANNING":
            trip.state = "ACTIVE_TRIP"
        db.add(
            TripEvent(
                trip_session_id=trip.id,
                event_id=f"patch-{patch.id}-accepted",
                event_type="PlanPatchAccepted",
                payload_json=json.dumps({"patch_id": patch.id, "plan_version": next_version}),
                occurred_at=decided_at,
                status="processed",
                impact_level="none",
                decision_json=json.dumps({"accepted": True, "policy": policy_result}),
                processed_at=decided_at,
            )
        )
    await db.execute(
        update(AgentArtifact)
        .where(
            AgentArtifact.workflow_run_id.in_(
                select(AgentWorkflowRun.id).where(
                    AgentWorkflowRun.planning_run_id == patch.planning_run_id
                )
            ),
            AgentArtifact.status == "active",
            (AgentArtifact.plan_version.is_(None)) | (AgentArtifact.plan_version < next_version),
        )
        .values(status="stale", invalidated_at=decided_at)
    )
    db.add(
        DecisionAuditLog(
            planning_run_id=patch.planning_run_id,
            user_id=patch.user_id,
            action="apply_plan_patch",
            reason=patch.reason,
            evidence_json=json.dumps(
                {"base_version": current, "operations": operations}, ensure_ascii=False
            ),
            policy_result=policy_result,
            trace_id=trace_id,
        )
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppError(
            409,
            "PLAN_VERSION_CONFLICT",
            "相同行程版本已存在有效写入者",
        ) from exc
    return {"status": "accepted", "plan_version": next_version, "snapshot": snapshot}
