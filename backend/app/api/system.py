import time

from fastapi import APIRouter, Request
from sqlalchemy import func, select, text

from backend.app.api.deps import CurrentUser, Db
from backend.app.clients.amap_client import build_map_provider
from backend.app.core.config import get_settings
from backend.app.core.exceptions import AppError
from backend.app.models import Checkin, Favorite, Plan, Setting, Share, Track, User
from backend.app.services.agent_readiness import evaluate_critic_enforcement_readiness

router = APIRouter(tags=["system"])
BOOT_TIME = time.monotonic()


@router.get("/health")
async def health(db: Db):
    await db.execute(text("SELECT 1"))
    settings = get_settings()
    return {
        "ok": True,
        "data": {
            "status": "ok",
            "version": settings.app_version,
            "uptimeSec": round(time.monotonic() - BOOT_TIME),
            "runtime": "Python / FastAPI",
            "database": db.get_bind().dialect.name,
            "databaseConnected": True,
        },
    }


async def setting(db: Db, key: str) -> str:
    row = await db.get(Setting, key)
    return row.value if row and row.value else ""


@router.get("/config")
async def config(db: Db):
    settings = get_settings()
    configured_key = "" if settings.disable_configured_map_credentials else settings.amap_key
    configured_jscode = "" if settings.disable_configured_map_credentials else settings.amap_jscode
    key = await setting(db, "amap_key") or configured_key
    jscode = await setting(db, "amap_jscode") or configured_jscode
    return {
        "ok": True,
        "data": {
            "amapKey": key or None,
            "proxy": bool(key and jscode),
            "registrationNeedsAdminToken": False,
            "adminAuthTokenRequired": bool(settings.admin_init_token),
        },
    }


def require_admin(user) -> None:
    if not user.is_admin:
        raise AppError(403, "ADMIN_REQUIRED", "需要管理员权限")


@router.get("/admin/amapkey")
async def get_amap_key(user: CurrentUser, db: Db):
    require_admin(user)
    settings = get_settings()
    configured_key = "" if settings.disable_configured_map_credentials else settings.amap_key
    configured_jscode = "" if settings.disable_configured_map_credentials else settings.amap_jscode
    key = await setting(db, "amap_key") or configured_key
    jscode = await setting(db, "amap_jscode") or configured_jscode
    return {
        "ok": True,
        "data": {
            "key": key,
            "jscodeMasked": f"{jscode[:4]}****" if jscode else "",
            "hasJscode": bool(jscode),
        },
    }


@router.post("/admin/amapkey")
async def set_amap_key(body: dict, request: Request, user: CurrentUser, db: Db):
    require_admin(user)
    key = str(body.get("key") or "").strip()
    jscode = str(body.get("jscode") or "").strip()
    settings = get_settings()
    configured_jscode = "" if settings.disable_configured_map_credentials else settings.amap_jscode
    previous_jscode = await setting(db, "amap_jscode") or configured_jscode
    updates = [("amap_key", key)]
    if not key:
        updates.append(("amap_jscode", ""))
    elif jscode:
        updates.append(("amap_jscode", jscode))
    for name, value in updates:
        row = await db.get(Setting, name)
        if row:
            row.value = value
        else:
            db.add(Setting(key=name, value=value))
    await db.commit()
    effective_jscode = jscode or previous_jscode if key else ""
    runtime_settings = settings.model_copy(
        update={
            "amap_key": key,
            "amap_jscode": effective_jscode,
            "disable_configured_map_credentials": False,
        }
    )
    request.app.state.map_provider = build_map_provider(
        runtime_settings, request.app.state.http_client
    )
    return {"ok": True, "data": None}


@router.get("/admin/overview")
async def admin_overview(user: CurrentUser, db: Db):
    require_admin(user)
    people = list((await db.scalars(select(User).order_by(User.id))).all())

    async def count(model, user_id: int | None = None):
        query = select(func.count(model.id))
        if user_id is not None:
            query = query.where(model.user_id == user_id)
        return await db.scalar(query) or 0

    users = []
    for person in people:
        distance = await db.scalar(
            select(func.coalesce(func.sum(Track.distance), 0)).where(Track.user_id == person.id)
        )
        users.append(
            {
                "id": person.id,
                "username": person.username,
                "nickname": person.nickname,
                "is_admin": int(person.is_admin),
                "created_at": person.created_at.isoformat(sep=" "),
                "tracks": await count(Track, person.id),
                "distance": distance or 0,
                "favorites": await count(Favorite, person.id),
                "checkins": await count(Checkin, person.id),
                "plans": await count(Plan, person.id),
            }
        )
    return {
        "ok": True,
        "data": {
            "users": users,
            "totals": {
                "users": len(users),
                "tracks": await count(Track),
                "distance": await db.scalar(select(func.coalesce(func.sum(Track.distance), 0)))
                or 0,
                "shares": await count(Share),
                "checkins": await count(Checkin),
            },
        },
    }


@router.get("/admin/agents/critic-readiness")
async def critic_enforcement_readiness(user: CurrentUser, db: Db):
    require_admin(user)
    return {
        "ok": True,
        "data": await evaluate_critic_enforcement_readiness(db, get_settings()),
    }


@router.delete("/admin/users/{user_id}")
async def delete_user(user_id: int, user: CurrentUser, db: Db):
    require_admin(user)
    if user_id == user.id:
        raise AppError(400, "CANNOT_DELETE_SELF", "不能删除自己")
    target = await db.get(User, user_id)
    if target:
        await db.delete(target)
        await db.commit()
    return {"ok": True, "data": None}
