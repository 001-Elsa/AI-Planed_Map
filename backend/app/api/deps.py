from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.exceptions import AppError
from backend.app.core.security import token_hash
from backend.app.db.session import get_db
from backend.app.models import Session, User

Db = Annotated[AsyncSession, Depends(get_db)]


async def current_user(
    db: Db,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    raw = ""
    if authorization and authorization.startswith("Bearer "):
        raw = authorization[7:]
    if not raw:
        raise AppError(401, "AUTH_REQUIRED", "未登录")
    session = await db.get(Session, token_hash(raw))
    if (
        session is None
        or session.revoked_at is not None
        or session.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
    ):
        raise AppError(401, "SESSION_EXPIRED", "登录已过期，请重新登录")
    user = await db.get(User, session.user_id)
    if user is None:
        raise AppError(401, "AUTH_REQUIRED", "未登录")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
