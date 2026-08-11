import json
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select

from backend.app.api.deps import CurrentUser, Db
from backend.app.core.exceptions import AppError
from backend.app.models import Checkin, Favorite, Plan, PlanStop, Track

router = APIRouter(tags=["user-data"])
SHANGHAI = ZoneInfo("Asia/Shanghai")


def local_date(value: datetime) -> date:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(SHANGHAI).date()


def row_dict(row) -> dict:
    result = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        result[column.name] = value.isoformat(sep=" ") if hasattr(value, "isoformat") else value
    return result


class PlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    data: dict | list


class FavoriteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    address: str = Field(default="", max_length=200)
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    mode: str = Field(default="", max_length=30)


class TrackCreate(BaseModel):
    kind: Literal["run", "ride"]
    name: str = Field(min_length=1, max_length=50)
    distance: float = Field(ge=0, le=10_000_000)
    duration: float | None = Field(default=None, ge=0, le=31_536_000)
    path: list[list[float]] = Field(min_length=1, max_length=100_000)
    real: bool = False


class CheckinCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=300)
    emoji: str = Field(default="📍", max_length=8)
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


async def paged(db: Db, model, user_id: int, limit: int, offset: int, extra=None):
    condition = model.user_id == user_id
    if extra is not None:
        condition = condition & extra
    rows = list(
        (
            await db.scalars(
                select(model).where(condition).order_by(model.id.desc()).limit(limit).offset(offset)
            )
        ).all()
    )
    return [row_dict(row) for row in rows]


@router.get("/plans")
async def list_plans(
    user: CurrentUser, db: Db, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)
):
    return {"ok": True, "data": await paged(db, Plan, user.id, limit, offset)}


@router.post("/plans")
async def create_plan(body: PlanCreate, user: CurrentUser, db: Db):
    encoded = json.dumps(body.data, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 200_000:
        raise AppError(413, "PAYLOAD_TOO_LARGE", "计划内容过大")
    plan = Plan(user_id=user.id, name=body.name.strip(), data=encoded)
    db.add(plan)
    await db.flush()
    if isinstance(body.data, dict):
        ai_result = body.data.get("aiResult")
        if isinstance(ai_result, dict):
            for position, stop in enumerate(ai_result.get("stops") or []):
                try:
                    task = stop["task"]
                    poi = stop["poi"]
                    location = poi["location"]
                    db.add(
                        PlanStop(
                            plan_id=plan.id,
                            position=position,
                            task_description=str(task["description"])[:300],
                            poi_id=str(poi.get("id") or "")[:80] or None,
                            poi_name=str(poi.get("name") or "")[:200] or None,
                            longitude=float(location["lng"]),
                            latitude=float(location["lat"]),
                            eta=datetime.fromisoformat(stop["arrival_time"]),
                            service_duration_minutes=int(task.get("service_duration_minutes") or 0),
                            deadline=datetime.fromisoformat(task["deadline"])
                            if task.get("deadline")
                            else None,
                            constraint_satisfied=bool(stop.get("constraint_satisfied", True)),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
    await db.commit()
    return {"ok": True, "data": {"id": plan.id}}


@router.delete("/plans/{item_id}")
async def delete_plan(item_id: int, user: CurrentUser, db: Db):
    item = await db.get(Plan, item_id)
    if item and item.user_id == user.id:
        await db.delete(item)
        await db.commit()
    return {"ok": True, "data": None}


@router.get("/favorites")
async def list_favorites(
    user: CurrentUser, db: Db, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)
):
    return {"ok": True, "data": await paged(db, Favorite, user.id, limit, offset)}


@router.post("/favorites")
async def create_favorite(body: FavoriteCreate, user: CurrentUser, db: Db):
    duplicate = await db.scalar(
        select(Favorite).where(
            Favorite.user_id == user.id,
            Favorite.name == body.name,
            func.abs(Favorite.lng - body.lng) < 0.000001,
            func.abs(Favorite.lat - body.lat) < 0.000001,
        )
    )
    if duplicate:
        raise AppError(409, "FAVORITE_EXISTS", "已经收藏过啦")
    item = Favorite(user_id=user.id, **body.model_dump())
    db.add(item)
    await db.commit()
    return {"ok": True, "data": {"id": item.id}}


@router.delete("/favorites/{item_id}")
async def delete_favorite(item_id: int, user: CurrentUser, db: Db):
    item = await db.get(Favorite, item_id)
    if item and item.user_id == user.id:
        await db.delete(item)
        await db.commit()
    return {"ok": True, "data": None}


@router.get("/tracks")
async def list_tracks(
    user: CurrentUser,
    db: Db,
    kind: str | None = None,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    extra = Track.kind == kind if kind else None
    return {"ok": True, "data": await paged(db, Track, user.id, limit, offset, extra)}


@router.post("/tracks")
async def create_track(body: TrackCreate, user: CurrentUser, db: Db):
    item = Track(
        user_id=user.id,
        kind=body.kind,
        name=body.name,
        distance=body.distance,
        duration=body.duration,
        path=json.dumps(body.path),
        is_real=body.real,
    )
    db.add(item)
    await db.commit()
    return {"ok": True, "data": {"id": item.id}}


@router.delete("/tracks/{item_id}")
async def delete_track(item_id: int, user: CurrentUser, db: Db):
    item = await db.get(Track, item_id)
    if item and item.user_id == user.id:
        await db.delete(item)
        await db.commit()
    return {"ok": True, "data": None}


@router.get("/checkins")
async def list_checkins(
    user: CurrentUser, db: Db, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)
):
    return {"ok": True, "data": await paged(db, Checkin, user.id, limit, offset)}


@router.post("/checkins")
async def create_checkin(body: CheckinCreate, user: CurrentUser, db: Db):
    item = Checkin(user_id=user.id, **body.model_dump())
    db.add(item)
    await db.commit()
    return {"ok": True, "data": {"id": item.id}}


@router.delete("/checkins/{item_id}")
async def delete_checkin(item_id: int, user: CurrentUser, db: Db):
    item = await db.get(Checkin, item_id)
    if item and item.user_id == user.id:
        await db.delete(item)
        await db.commit()
    return {"ok": True, "data": None}


@router.get("/stats")
async def stats(user: CurrentUser, db: Db):
    aggregate_rows = (
        await db.execute(
            select(
                Track.kind,
                func.count(Track.id),
                func.coalesce(func.sum(Track.distance), 0),
                func.coalesce(func.sum(Track.duration), 0),
                func.coalesce(func.sum(case((Track.is_real.is_(True), 1), else_=0)), 0),
            )
            .where(Track.user_id == user.id)
            .group_by(Track.kind)
        )
    ).all()
    by_kind = [
        {
            "kind": kind,
            "count": int(count),
            "distance": float(distance),
            "duration": float(duration),
            "realCount": int(real_count),
        }
        for kind, count, distance, duration, real_count in aggregate_rows
    ]

    now_local = datetime.now(SHANGHAI)
    today = now_local.date()
    this_week = today - timedelta(days=today.weekday())
    week_starts = [this_week - timedelta(weeks=offset) for offset in range(7, -1, -1)]
    activity_cutoff = datetime.combine(week_starts[0], datetime.min.time(), SHANGHAI).astimezone(
        timezone.utc
    )
    activity_rows = (
        await db.execute(
            select(Track.distance, Track.duration, Track.created_at).where(
                Track.user_id == user.id,
                Track.created_at >= activity_cutoff,
            )
        )
    ).all()
    weekly_buckets = {
        start: {"distance": 0.0, "duration": 0.0, "count": 0} for start in week_starts
    }
    daily_buckets: dict[date, dict[str, float | int]] = {}
    for distance, duration, created_at in activity_rows:
        day = local_date(created_at)
        week = day - timedelta(days=day.weekday())
        if week in weekly_buckets:
            weekly_buckets[week]["distance"] += float(distance)
            weekly_buckets[week]["duration"] += float(duration or 0)
            weekly_buckets[week]["count"] += 1
        bucket = daily_buckets.setdefault(day, {"distance": 0.0, "duration": 0.0, "count": 0})
        bucket["distance"] += float(distance)
        bucket["duration"] += float(duration or 0)
        bucket["count"] += 1

    weekly = [{"week": start.isoformat(), **values} for start, values in weekly_buckets.items()]
    last_30_days = [today - timedelta(days=offset) for offset in range(29, -1, -1)]
    daily = [
        {
            "date": day.isoformat(),
            **daily_buckets.get(day, {"distance": 0, "duration": 0, "count": 0}),
        }
        for day in last_30_days
    ]
    activity_days = set(daily_buckets)
    streak_cursor = today if today in activity_days else today - timedelta(days=1)
    current_streak = 0
    while streak_cursor in activity_days:
        current_streak += 1
        streak_cursor -= timedelta(days=1)

    recent_checkins = list(
        (
            await db.scalars(
                select(Checkin)
                .where(Checkin.user_id == user.id)
                .order_by(Checkin.created_at.desc(), Checkin.id.desc())
                .limit(5)
            )
        ).all()
    )

    favorite_mode_rows = (
        await db.execute(
            select(Favorite.mode, func.count(Favorite.id))
            .where(Favorite.user_id == user.id)
            .group_by(Favorite.mode)
        )
    ).all()

    async def count(model):
        return await db.scalar(select(func.count(model.id)).where(model.user_id == user.id)) or 0

    return {
        "ok": True,
        "data": {
            "byKind": by_kind,
            "weekly": weekly,
            "daily": daily,
            "currentStreakDays": current_streak,
            "counts": {
                "favorites": await count(Favorite),
                "plans": await count(Plan),
                "checkins": await count(Checkin),
                "tracks": sum(item["count"] for item in by_kind),
            },
            "favoritesByMode": [
                {"mode": mode or "other", "count": int(total)} for mode, total in favorite_mode_rows
            ],
            "recentCheckins": [row_dict(row) for row in recent_checkins],
            "since": user.created_at.isoformat(sep=" "),
            "generatedAt": now_local.isoformat(),
            "timezone": "Asia/Shanghai",
        },
    }
