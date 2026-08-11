import asyncio
import secrets

from fastapi import APIRouter, Header
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.app.api.deps import CurrentUser, Db
from backend.app.core.config import get_settings
from backend.app.core.exceptions import AppError
from backend.app.core.security import (
    expires_at,
    hash_password,
    new_session_token,
    token_hash,
    verify_password,
)
from backend.app.models import Session, User
from backend.app.schemas.auth import LoginRequest, RegisterRequest

router = APIRouter(tags=["auth"])


def public_user(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "nickname": user.nickname,
        "is_admin": int(user.is_admin),
    }


async def issue_session(db: Db, user: User, device_name: str = "unknown") -> str:
    for attempt in range(3):
        raw = new_session_token()
        db.add(
            Session(
                token=token_hash(raw),
                user_id=user.id,
                expires_at=expires_at(get_settings().session_days),
                device_name=device_name[:100],
            )
        )
        try:
            await db.commit()
            return raw
        except IntegrityError:
            await db.rollback()
            if attempt >= 2:
                raise AppError(
                    503,
                    "SESSION_CREATE_FAILED",
                    "登录会话创建失败，请重试",
                ) from None
        except OperationalError as exc:
            await db.rollback()
            if attempt >= 2:
                raise AppError(
                    503,
                    "AUTH_DATABASE_BUSY",
                    "当前登录人数较多，请稍后重试",
                    {"retry_after_seconds": 1},
                ) from exc
        await asyncio.sleep(0.05 * (2**attempt))
    raise AppError(503, "SESSION_CREATE_FAILED", "登录会话创建失败，请重试")


@router.post("/register")
async def register(
    body: RegisterRequest,
    db: Db,
    device_name: str = Header(default="unknown", alias="X-Device-Name"),
):
    password_hash = await hash_password(body.password)
    try:
        existing = await db.scalar(select(User).where(User.username == body.username))
        if existing:
            raise AppError(409, "USERNAME_EXISTS", "用户名已被注册")
        settings = get_settings()
        is_admin = body.accountType == "admin"
        if is_admin:
            if not settings.admin_init_token:
                raise AppError(
                    503,
                    "ADMIN_INIT_REQUIRED",
                    "管理员注册前必须配置 ADMIN_INIT_TOKEN",
                )
            if not body.adminInitToken or not secrets.compare_digest(
                body.adminInitToken, settings.admin_init_token
            ):
                raise AppError(403, "ADMIN_INIT_INVALID", "管理员初始化令牌不正确")
            is_admin = True
        user = User(
            username=body.username,
            nickname=body.nickname or body.username,
            pass_hash=password_hash,
            is_admin=is_admin,
        )
        db.add(user)
        await db.flush()
        token = new_session_token()
        db.add(
            Session(
                token=token_hash(token),
                user_id=user.id,
                expires_at=expires_at(settings.session_days),
                device_name=device_name[:100],
            )
        )
        await db.commit()
        return {"ok": True, "data": {"token": token, "user": public_user(user)}}
    except AppError:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        raise AppError(409, "USERNAME_EXISTS", "用户名已被注册") from exc
    except OperationalError as exc:
        await db.rollback()
        raise AppError(
            503,
            "AUTH_DATABASE_BUSY",
            "当前注册人数较多，请稍后重试",
            {"retry_after_seconds": 1},
        ) from exc


@router.post("/login")
async def login(
    body: LoginRequest,
    db: Db,
    device_name: str = Header(default="unknown", alias="X-Device-Name"),
):
    user = await db.scalar(select(User).where(User.username == body.username))
    if user is None or not await verify_password(body.password, user.pass_hash):
        raise AppError(401, "INVALID_CREDENTIALS", "用户名或密码错误")
    settings = get_settings()
    if body.accountType == "admin":
        if not user.is_admin:
            raise AppError(403, "ADMIN_ACCOUNT_REQUIRED", "该账号不是管理员账号")
        if (
            not settings.admin_init_token
            or not body.adminInitToken
            or not secrets.compare_digest(body.adminInitToken, settings.admin_init_token)
        ):
            raise AppError(403, "ADMIN_INIT_INVALID", "管理员初始化令牌不正确")
    elif user.is_admin:
        raise AppError(403, "ADMIN_LOGIN_REQUIRED", "管理员账号请使用管理员登录")
    token = await issue_session(db, user, device_name)
    return {"ok": True, "data": {"token": token, "user": public_user(user)}}


@router.get("/me")
async def me(user: CurrentUser):
    return {"ok": True, "data": public_user(user)}


@router.post("/logout")
async def logout(
    user: CurrentUser,
    db: Db,
    authorization: str = Header(default=""),
):
    raw = authorization[7:] if authorization.startswith("Bearer ") else ""
    session = await db.get(Session, token_hash(raw))
    if session:
        await db.delete(session)
        await db.commit()
    return {"ok": True, "data": None}


@router.get("/sessions")
async def list_sessions(user: CurrentUser, db: Db):
    sessions = (
        await db.scalars(
            select(Session)
            .where(Session.user_id == user.id, Session.revoked_at.is_(None))
            .order_by(Session.created_at.desc())
        )
    ).all()
    return {
        "ok": True,
        "data": [
            {
                "token_id": item.token,
                "device_name": item.device_name,
                "created_at": item.created_at,
                "last_seen_at": item.last_seen_at,
                "expires_at": item.expires_at,
            }
            for item in sessions
        ],
    }


@router.delete("/sessions/{token_id}")
async def revoke_session(token_id: str, user: CurrentUser, db: Db):
    if len(token_id) != 64:
        raise AppError(422, "SESSION_ID_INVALID", "Session ID 格式错误")
    session = await db.scalar(
        select(Session).where(Session.token == token_id, Session.user_id == user.id)
    )
    if session:
        from datetime import datetime, timezone

        session.revoked_at = datetime.now(timezone.utc)
        await db.commit()
    return {"ok": True, "data": None}
