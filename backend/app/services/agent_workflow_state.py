"""Incremental durable state for Agent workflows and task DAGs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    AgentSharedStateSnapshot,
    AgentWorkflowRun,
    AgentWorkflowTask,
)
from backend.app.schemas.agent_artifacts import (
    AgentPlanStep,
    AgentWorkflowTrace,
    minimize_agent_payload,
)
from backend.app.schemas.agent_state import AgentSharedStateAudit

TERMINAL_WORKFLOW_STATUSES = {
    "succeeded",
    "success",
    "failed",
    "blocked",
    "infeasible",
    "need_clarification",
    "needs_clarification",
}
TaskStatus = Literal[
    "pending", "running", "retrying", "succeeded", "failed", "blocked", "skipped"
]


class DurableWorkflowCheckpointStore:
    """Idempotently commits workflow and DAG state at execution boundaries."""

    def __init__(self, db: AsyncSession, workflow: AgentWorkflowRun) -> None:
        self.db = db
        self.workflow = workflow

    @classmethod
    async def start(
        cls,
        db: AsyncSession,
        *,
        trace: AgentWorkflowTrace,
        user_id: int,
        trigger_type: str,
        trace_id: str | None,
        planning_conversation_id: int | None = None,
        planning_run_id: int | None = None,
        trip_session_id: int | None = None,
    ) -> DurableWorkflowCheckpointStore:
        workflow = AgentWorkflowRun(
            user_id=user_id,
            planning_conversation_id=planning_conversation_id,
            planning_run_id=planning_run_id,
            trip_session_id=trip_session_id,
            trigger_type=trigger_type,
            mode=trace.mode.value,
            execution_mode=trace.execution_mode.value,
            status="running",
            trace_id=trace_id,
        )
        db.add(workflow)
        await db.flush()
        trace.workflow_id = workflow.id
        await db.commit()
        return cls(db, workflow)

    @classmethod
    async def resume_or_start(
        cls,
        db: AsyncSession,
        *,
        trace: AgentWorkflowTrace,
        user_id: int,
        trigger_type: str,
        trace_id: str | None,
        planning_run_id: int,
        trip_session_id: int,
    ) -> tuple[DurableWorkflowCheckpointStore, str | None]:
        workflow = await db.scalar(
            select(AgentWorkflowRun)
            .where(
                AgentWorkflowRun.user_id == user_id,
                AgentWorkflowRun.trip_session_id == trip_session_id,
                AgentWorkflowRun.trigger_type == trigger_type,
                AgentWorkflowRun.status.in_(("running", "failed")),
            )
            .order_by(AgentWorkflowRun.id.desc())
        )
        if workflow is None:
            store = await cls.start(
                db,
                trace=trace,
                user_id=user_id,
                trigger_type=trigger_type,
                trace_id=trace_id,
                planning_run_id=planning_run_id,
                trip_session_id=trip_session_id,
            )
            return store, None
        if trace.execution_plan is None:
            raise RuntimeError("cannot resume a workflow without a task graph")
        tasks = {
            task.task_key: task
            for task in (
                await db.scalars(
                    select(AgentWorkflowTask).where(
                        AgentWorkflowTask.workflow_run_id == workflow.id
                    )
                )
            ).all()
        }
        resume_from: str | None = None
        for step in trace.execution_plan.steps:
            task = tasks.get(step.step_id)
            if task is not None:
                step.status = cast(TaskStatus, task.status)
                step.attempt_count = task.attempt_count
            if resume_from is None and step.status in {
                "pending",
                "running",
                "retrying",
                "failed",
                "blocked",
            }:
                resume_from = step.step_id
                step.status = "retrying"
            elif resume_from is not None and step.status in {"blocked", "failed", "retrying"}:
                step.status = "pending"
        trace.workflow_id = workflow.id
        trace.status = "running"
        workflow.status = "running"
        workflow.completed_at = None
        await db.commit()
        store = cls(db, workflow)
        await store.checkpoint(trace)
        return store, resume_from

    async def checkpoint(self, trace: AgentWorkflowTrace) -> None:
        self.workflow.status = trace.status
        self.workflow.mode = trace.mode.value
        self.workflow.execution_mode = trace.execution_mode.value
        self.workflow.handoff_count = trace.handoff_count
        self.workflow.retry_count = trace.retry_count
        self.workflow.estimated_cost_usd = trace.total_cost_usd
        self.workflow.completed_at = (
            datetime.now(timezone.utc)
            if trace.status in TERMINAL_WORKFLOW_STATUSES
            else None
        )
        if trace.execution_plan is not None:
            existing = {
                task.task_key: task
                for task in (
                    await self.db.scalars(
                        select(AgentWorkflowTask).where(
                            AgentWorkflowTask.workflow_run_id == self.workflow.id
                        )
                    )
                ).all()
            }
            stages = {stage.stage_key: stage for stage in trace.stages}
            steps: dict[str, list] = {}
            for step in trace.steps:
                if step.task_key:
                    steps.setdefault(step.task_key, []).append(step)
            for plan_step in trace.execution_plan.steps:
                task = existing.get(plan_step.step_id)
                if task is None:
                    task = AgentWorkflowTask(
                        workflow_run_id=self.workflow.id,
                        task_key=plan_step.step_id,
                        role=plan_step.agent_type.value,
                        output_artifact_type=plan_step.output_artifact_type,
                    )
                    self.db.add(task)
                self._apply_task(task, plan_step, stages.get(plan_step.step_id), steps)
        if trace.shared_state is not None:
            state = AgentSharedStateAudit.model_validate(trace.shared_state)
            snapshot = await self.db.scalar(
                select(AgentSharedStateSnapshot).where(
                    AgentSharedStateSnapshot.workflow_run_id == self.workflow.id
                )
            )
            if snapshot is None:
                snapshot = AgentSharedStateSnapshot(
                    workflow_run_id=self.workflow.id,
                    task_id=state.task_id,
                    revision=state.revision,
                    phase=state.phase.value,
                    state_hash=state.state_hash,
                    payload_json=state.model_dump_json(),
                )
                self.db.add(snapshot)
            else:
                snapshot.revision = state.revision
                snapshot.phase = state.phase.value
                snapshot.state_hash = state.state_hash
                snapshot.payload_json = state.model_dump_json()
        await self.db.commit()

    @staticmethod
    def _apply_task(task, plan_step: AgentPlanStep, stage, steps: dict[str, list]) -> None:
        agent_step = steps.get(plan_step.step_id, [])[-1] if steps.get(plan_step.step_id) else None
        summary = (
            stage.summary
            if stage is not None
            else (agent_step.output_artifact.payload if agent_step is not None else {})
        )
        task.role = plan_step.agent_type.value
        task.execution_kind = plan_step.execution_kind
        task.status = plan_step.status
        task.dependency_keys_json = json.dumps(plan_step.depends_on, ensure_ascii=False)
        task.attempt_count = plan_step.attempt_count
        task.output_artifact_type = (
            stage.output_artifact_type
            if stage is not None
            else (
                agent_step.output_artifact.artifact_type
                if agent_step is not None
                else plan_step.output_artifact_type
            )
        )
        task.budget_json = plan_step.budget.model_dump_json() if plan_step.budget else "{}"
        if stage is not None or agent_step is not None or not task.summary_json:
            task.summary_json = json.dumps(
                minimize_agent_payload(summary), ensure_ascii=False, default=str
            )[:4000]
        task.version = plan_step.version
        task.updated_at = datetime.now(timezone.utc)

    async def fail(self, trace: AgentWorkflowTrace, *, reason: str) -> None:
        trace.status = "failed"
        if trace.execution_plan is not None:
            for step in trace.execution_plan.steps:
                if step.status in {"running", "retrying"}:
                    step.status = "failed"
                elif step.status == "pending":
                    step.status = "blocked"
        await self.checkpoint(trace)
        failed_tasks = (
            await self.db.scalars(
                select(AgentWorkflowTask).where(
                    AgentWorkflowTask.workflow_run_id == self.workflow.id,
                    AgentWorkflowTask.status == "failed",
                )
            )
        ).all()
        for task in failed_tasks:
            task.summary_json = json.dumps({"error_code": reason}, ensure_ascii=False)
        await self.db.commit()
