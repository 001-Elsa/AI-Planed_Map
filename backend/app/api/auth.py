from fastapi import APIRouter, Header
from sqlalchemy import func, select

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


async def issue_session(db: Db, user: User) -> str:
    raw = new_session_token()
    db.add(
        Session(
            token=token_hash(raw),
            user_id=user.id,
            expires_at=expires_at(get_settings().session_days),
        )
    )
    await db.commit()
    return raw


@router.post("/register")
async def register(body: RegisterRequest, db: Db):
    existing = await db.scalar(select(User).where(User.username == body.username))
    if existing:
        raise AppError(409, "USERNAME_EXISTS", "用户名已被注册")
    count = await db.scalar(select(func.count(User.id))) or 0
    settings = get_settings()
    is_admin = False
    if count == 0:
        if settings.environment == "production" and not settings.admin_init_token:
            raise AppError(503, "ADMIN_INIT_REQUIRED", "生产环境需要先配置 ADMIN_INIT_TOKEN")
        if settings.admin_init_token and body.adminInitToken != settings.admin_init_token:
            raise AppError(403, "ADMIN_INIT_INVALID", "管理员初始化令牌不正确")
        is_admin = True
    user = User(
        username=body.username,
        nickname=body.nickname or body.username,
        pass_hash=await hash_password(body.password),
        is_admin=is_admin,
    )
    db.add(user)
    await db.flush()
    token = await issue_session(db, user)
    return {"ok": True, "data": {"token": token, "user": public_user(user)}}


@router.post("/login")
async def login(body: LoginRequest, db: Db):
    user = await db.scalar(select(User).where(User.username == body.username))
    if user is None or not await verify_password(body.password, user.pass_hash):
        raise AppError(401, "INVALID_CREDENTIALS", "用户名或密码错误")
    token = await issue_session(db, user)
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

