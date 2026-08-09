from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import select
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
    now = datetime.now(timezone.utc)
    user = await db.scalar(
        select(User)
        .join(Session, Session.user_id == User.id)
        .where(
            Session.token == token_hash(raw),
            Session.revoked_at.is_(None),
            Session.expires_at > now,
        )
    )
    if user is None:
        raise AppError(401, "SESSION_EXPIRED", "登录已过期，请重新登录")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
