import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import or_, select

from backend.app.api.deps import CurrentUser, Db
from backend.app.core.exceptions import AppError
from backend.app.models import Favorite, Friend, Share, Track, User

router = APIRouter(tags=["sharing"])


class ShareCreate(BaseModel):
    type: str
    payload: dict | list


class FriendRequest(BaseModel):
    username: str


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
    item = Share(token=secrets.token_hex(8), user_id=user.id, type=body.type, payload=encoded)
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
    db.add(Friend(user_id=user.id, friend_id=target.id))
    await db.commit()
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
    accepted, incoming, outgoing = [], [], []
    for row in rows:
        other_id = row.friend_id if row.user_id == user.id else row.user_id
        other = await db.get(User, other_id)
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


@router.get("/leaderboard")
async def leaderboard(user: CurrentUser, db: Db, days: int = 7):
    days = min(365, max(1, days))
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
    user_ids = [user.id] + [
        row.friend_id if row.user_id == user.id else row.user_id for row in relations
    ]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result: list[dict[str, Any]] = []
    for user_id in user_ids:
        person = await db.get(User, user_id)
        tracks = list(
            (
                await db.scalars(
                    select(Track).where(Track.user_id == user_id, Track.created_at >= cutoff)
                )
            ).all()
        )
        result.append(
            {
                "uid": user_id,
                "nickname": person.nickname if person else "",
                "distance": sum(item.distance for item in tracks),
                "count": len(tracks),
            }
        )
    result.sort(key=lambda item: item["distance"], reverse=True)
    return {"ok": True, "data": {"days": days, "rows": result, "me": user.id}}
