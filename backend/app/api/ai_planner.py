import json

from fastapi import APIRouter, Header
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.api.deps import CurrentUser, Db
from backend.app.clients.amap_client import build_map_provider
from backend.app.core.config import get_settings
from backend.app.core.exceptions import AppError
from backend.app.models import IdempotencyRecord, PlanningRun
from backend.app.schemas.ai_intent import AIPlanRequest
from backend.app.services.intent_parser import build_intent_parser
from backend.app.services.planning_service import PlanningService, request_fingerprint


router = APIRouter(prefix="/ai", tags=["ai-planner"])


@router.post("/plans")
async def create_ai_plan(
    body: AIPlanRequest,
    user: CurrentUser,
    db: Db,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    settings = get_settings()
    parser = build_intent_parser(settings)
    fingerprint = request_fingerprint(str(user.id), body, parser.name)
    record = None
    if idempotency_key:
        if len(idempotency_key) > 200:
            raise AppError(400, "IDEMPOTENCY_KEY_INVALID", "Idempotency-Key 过长")
        record = await db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.owner_key == str(user.id),
            IdempotencyRecord.idempotency_key == idempotency_key,
        ))
        if record:
            if record.request_fingerprint != fingerprint:
                raise AppError(409, "IDEMPOTENCY_KEY_REUSED", "同一个幂等键不能用于不同请求")
            if record.status == "completed" and record.response_json:
                return {"ok": True, "data": json.loads(record.response_json), "replayed": True}
            raise AppError(409, "REQUEST_IN_PROGRESS", "相同请求正在处理中")
        record = IdempotencyRecord(
            owner_key=str(user.id),
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        db.add(record)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise AppError(409, "REQUEST_IN_PROGRESS", "相同请求正在处理中") from exc

    service = PlanningService(parser, build_map_provider(settings))
    try:
        result = await service.plan(body)
    except Exception:
        if record:
            await db.delete(record)
            await db.commit()
        raise
    response_data = result.model_dump(mode="json")
    run = PlanningRun(
        user_id=user.id,
        input_text=body.text,
        intent_json=json.dumps(response_data["intent"], ensure_ascii=False),
        result_json=json.dumps(response_data, ensure_ascii=False),
        status=result.status,
        model_name=parser.name,
    )
    db.add(run)
    await db.flush()
    response_data["planning_run_id"] = run.id
    if record:
        record.status = "completed"
        record.response_json = json.dumps(response_data, ensure_ascii=False)
    await db.commit()
    return {"ok": True, "data": response_data}
