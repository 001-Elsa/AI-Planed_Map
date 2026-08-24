import json
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.app.api.deps import CurrentUser, Db
from backend.app.core.exceptions import AppError
from backend.app.models import Favorite, Friend, Share, Track, User

router = APIRouter(tags=["sharing"])
SHANGHAI = ZoneInfo("Asia/Shanghai")


class ShareCreate(BaseModel):
    type: str
    payload: dict | list


class FriendRequest(BaseModel):
    username: str = Field(min_length=2, max_length=20)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return str(value).strip()


class FriendResponse(BaseModel):
    id: int
    accept: bool


@router.post("/shares")
async def create_share(body: ShareCreate, user: CurrentUser, db: Db):
    if body.type not in ("track", "plan"):
        raise AppError(400, "SHARE_TYPE_INVALID", "缺少分享内容")
    encoded = json.dumps(body.payload, ensure_ascii=False)
    if len(encoded.encode()) > 500_000:
        raise AppError(413, "PAYLOAD_TOO_LARGE", "分享内容过大")
    # Public links may expose precise routes, so use a full 128-bit capability
    # token. Existing shorter tokens remain readable for backward compatibility.
    item = Share(token=secrets.token_hex(16), user_id=user.id, type=body.type, payload=encoded)
    db.add(item)
    await db.commit()
    return {"ok": True, "data": {"token": item.token}}


@router.get("/shares")
async def list_shares(user: CurrentUser, db: Db):
    rows = list(
        (
            await db.scalars(
                select(Share).where(Share.user_id == user.id).order_by(Share.id.desc())
            )
        ).all()
    )
    return {
        "ok": True,
        "data": [
            {
                "id": row.id,
                "token": row.token,
                "type": row.type,
                "created_at": row.created_at.isoformat(sep=" "),
            }
            for row in rows
        ],
    }


@router.delete("/shares/{item_id}")
async def delete_share(item_id: int, user: CurrentUser, db: Db):
    item = await db.get(Share, item_id)
    if item and item.user_id == user.id:
        await db.delete(item)
        await db.commit()
    return {"ok": True, "data": None}


@router.get("/share/{token}")
async def public_share(token: str, db: Db):
    item = await db.scalar(select(Share).where(Share.token == token))
    if item is None:
        raise AppError(404, "SHARE_NOT_FOUND", "分享不存在或已过期")
    created = item.created_at.replace(tzinfo=timezone.utc)
    if created < datetime.now(timezone.utc) - timedelta(days=180):
        raise AppError(404, "SHARE_EXPIRED", "分享不存在或已过期")
    owner = await db.get(User, item.user_id)
    return {
        "ok": True,
        "data": {
            "type": item.type,
            "payload": json.loads(item.payload),
            "nickname": owner.nickname if owner else "",
            "created_at": item.created_at.isoformat(sep=" "),
        },
    }


@router.post("/friends/request")
async def request_friend(body: FriendRequest, user: CurrentUser, db: Db):
    target = await db.scalar(select(User).where(User.username == body.username))
    if target is None:
        raise AppError(404, "USER_NOT_FOUND", "没有这个用户")
    if target.id == user.id:
        raise AppError(400, "FRIEND_SELF", "不能加自己为好友")
    existing = await db.scalar(
        select(Friend).where(
            or_(
                (Friend.user_id == user.id) & (Friend.friend_id == target.id),
                (Friend.user_id == target.id) & (Friend.friend_id == user.id),
            )
        )
    )
    if existing:
        raise AppError(409, "FRIEND_REQUEST_EXISTS", "已有好友关系或待处理请求")
    db.add(
        Friend(
            user_id=user.id,
            friend_id=target.id,
            pair_key=f"{min(user.id, target.id)}:{max(user.id, target.id)}",
        )
    )
    try:
        await db.commit()
    except (IntegrityError, OperationalError) as exc:
        await db.rollback()
        raced = await db.scalar(
            select(Friend).where(
                or_(
                    (Friend.user_id == user.id) & (Friend.friend_id == target.id),
                    (Friend.user_id == target.id) & (Friend.friend_id == user.id),
                )
            )
        )
        if raced:
            raise AppError(409, "FRIEND_REQUEST_EXISTS", "已有好友关系或待处理请求") from exc
        raise AppError(503, "SOCIAL_DATABASE_BUSY", "好友服务繁忙，请稍后重试") from exc
    return {"ok": True, "data": {"nickname": target.nickname}}


@router.get("/friends")
async def list_friends(user: CurrentUser, db: Db):
    rows = list(
        (
            await db.scalars(
                select(Friend).where(or_(Friend.user_id == user.id, Friend.friend_id == user.id))
            )
        ).all()
    )
    other_ids = {row.friend_id if row.user_id == user.id else row.user_id for row in rows}
    people = (
        {
            person.id: person
            for person in (await db.scalars(select(User).where(User.id.in_(other_ids)))).all()
        }
        if other_ids
        else {}
    )
    accepted, incoming, outgoing = [], [], []
    for row in rows:
        other_id = row.friend_id if row.user_id == user.id else row.user_id
        other = people.get(other_id)
        if other is None:
            continue
        item = {
            "id": row.id,
            "uid": other_id,
            "username": other.username,
            "nickname": other.nickname,
        }
        if row.status == "accepted":
            accepted.append(item)
        elif row.friend_id == user.id:
            incoming.append(item)
        else:
            outgoing.append(item)
    return {"ok": True, "data": {"accepted": accepted, "incoming": incoming, "outgoing": outgoing}}


@router.post("/friends/respond")
async def respond_friend(body: FriendResponse, user: CurrentUser, db: Db):
    row = await db.get(Friend, body.id)
    if row is None or row.friend_id != user.id or row.status != "pending":
        raise AppError(404, "FRIEND_REQUEST_NOT_FOUND", "请求不存在")
    if body.accept:
        row.status = "accepted"
    else:
        await db.delete(row)
    await db.commit()
    return {"ok": True, "data": None}


@router.delete("/friends/{item_id}")
async def delete_friend(item_id: int, user: CurrentUser, db: Db):
    row = await db.get(Friend, item_id)
    if row and user.id in (row.user_id, row.friend_id):
        await db.delete(row)
        await db.commit()
    return {"ok": True, "data": None}


async def accepted_friend(db: Db, first: int, second: int) -> bool:
    return bool(
        await db.scalar(
            select(Friend.id).where(
                Friend.status == "accepted",
                or_(
                    (Friend.user_id == first) & (Friend.friend_id == second),
                    (Friend.user_id == second) & (Friend.friend_id == first),
                ),
            )
        )
    )


@router.get("/friends/{friend_id}/favorites")
async def friend_favorites(friend_id: int, user: CurrentUser, db: Db):
    if not await accepted_friend(db, user.id, friend_id):
        raise AppError(403, "NOT_FRIENDS", "你们还不是好友")
    other = await db.get(User, friend_id)
    rows = list(
        (
            await db.scalars(
                select(Favorite).where(Favorite.user_id == friend_id).order_by(Favorite.id.desc())
            )
        ).all()
    )
    return {
        "ok": True,
        "data": {
            "nickname": other.nickname if other else "",
            "favorites": [
                {
                    "name": row.name,
                    "address": row.address,
                    "lng": row.lng,
                    "lat": row.lat,
                    "mode": row.mode,
                }
                for row in rows
            ],
        },
    }


def _local_day(value: datetime) -> date:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(SHANGHAI).date()


@router.get("/leaderboard")
async def leaderboard(
    user: CurrentUser,
    db: Db,
    days: int = Query(7, ge=1, le=90),
):
    relations = list(
        (
            await db.scalars(
                select(Friend).where(
                    Friend.status == "accepted",
                    or_(Friend.user_id == user.id, Friend.friend_id == user.id),
                )
            )
        ).all()
    )
    user_ids = list(
        dict.fromkeys(
            [user.id]
            + [row.friend_id if row.user_id == user.id else row.user_id for row in relations]
        )
    )
    now_local = datetime.now(SHANGHAI)
    period_start_date = now_local.date() - timedelta(days=days - 1)
    period_start = datetime.combine(period_start_date, datetime.min.time(), SHANGHAI).astimezone(
        timezone.utc
    )
    period_end = datetime.combine(
        now_local.date() + timedelta(days=1), datetime.min.time(), SHANGHAI
    ).astimezone(timezone.utc)
    people = {
        person.id: person
        for person in (await db.scalars(select(User).where(User.id.in_(user_ids)))).all()
    }
    track_rows = (
        await db.execute(
            select(
                Track.user_id,
                Track.distance,
                Track.duration,
                Track.created_at,
            ).where(
                Track.user_id.in_(user_ids),
                Track.created_at >= period_start,
                Track.created_at < period_end,
            )
        )
    ).all()
    totals: dict[int, dict[str, Any]] = {
        user_id: {"distance": 0.0, "duration": 0.0, "count": 0, "daily": {}} for user_id in user_ids
    }
    for user_id, distance, duration, created_at in track_rows:
        total = totals[user_id]
        total["distance"] += float(distance)
        total["duration"] += float(duration or 0)
        total["count"] += 1
        day = _local_day(created_at).isoformat()
        daily = total["daily"].setdefault(day, {"distance": 0.0, "duration": 0.0, "count": 0})
        daily["distance"] += float(distance)
        daily["duration"] += float(duration or 0)
        daily["count"] += 1

    result = []
    for user_id in user_ids:
        person = people.get(user_id)
        total = totals[user_id]
        result.append(
            {
                "uid": user_id,
                "nickname": person.nickname if person else "",
                "distance": total["distance"],
                "duration": total["duration"],
                "count": total["count"],
                "daily": [
                    {"date": day, **values} for day, values in sorted(total["daily"].items())
                ],
            }
        )
    result.sort(key=lambda item: (-item["distance"], -item["count"], item["uid"]))
    for rank, item in enumerate(result, 1):
        item["rank"] = rank
    next_update = datetime.combine(
        now_local.date() + timedelta(days=1), datetime.min.time(), SHANGHAI
    )
    return {
        "ok": True,
        "data": {
            "days": days,
            "rows": result,
            "me": user.id,
            "periodStart": period_start_date.isoformat(),
            "periodEnd": now_local.date().isoformat(),
            "updatedAt": now_local.isoformat(),
            "nextDailyRefreshAt": next_update.isoformat(),
            "timezone": "Asia/Shanghai",
        },
    }
