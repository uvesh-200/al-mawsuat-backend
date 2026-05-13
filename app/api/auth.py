import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Annotated

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth import UserManager, get_jwt_strategy, get_user_manager
from app.core.rate_limit import LoginRateLimiter, get_redis
from app.models.db import get_db
from app.models.tables import RefreshToken, User

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE_NAME = "refresh_token"


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=300)
    password: str = Field(min_length=1, max_length=500)


class AccessTokenBody(BaseModel):
    access_token: str
    token_type: str = "bearer"


def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _refresh_cookie_kwargs() -> dict:
    secure = settings.ENVIRONMENT == "production"
    return {
        "key": REFRESH_COOKIE_NAME,
        "httponly": True,
        "secure": secure,
        "samesite": "lax",
        "path": "/",
        "max_age": settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
    }


def _hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@router.post("/login", response_model=AccessTokenBody)
async def login(
    request: Request,
    body: LoginRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
    user_manager: Annotated[UserManager, Depends(get_user_manager)],
    redis_client: Annotated[redis.Redis, Depends(get_redis)],
) -> AccessTokenBody:
    ip = _client_ip(request)
    limiter = LoginRateLimiter(redis_client)

    if await limiter.is_blocked(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts")

    credentials = SimpleNamespace(username=body.email, password=body.password)
    user = await user_manager.authenticate(credentials)

    if user is None or not user.is_active:
        await limiter.record_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if user.tenant_id != settings.DEFAULT_TENANT_ID:
        await limiter.record_failure(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    await limiter.reset(ip)

    strategy = get_jwt_strategy()
    access_token = await strategy.write_token(user)

    raw_refresh = secrets.token_urlsafe(48)
    token_hash = _hash_refresh_token(raw_refresh)
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    session.add(
        RefreshToken(
            user_id=user.id,
            tenant_id=user.tenant_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    await session.commit()

    response.set_cookie(value=raw_refresh, **_refresh_cookie_kwargs())

    return AccessTokenBody(access_token=access_token)


@router.post("/refresh", response_model=AccessTokenBody)
async def refresh(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AccessTokenBody:
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing refresh token")

    token_hash = _hash_refresh_token(raw)
    now = datetime.now(timezone.utc)

    result = await session.execute(
        select(RefreshToken, User)
        .join(User, RefreshToken.user_id == User.id)
        .where(RefreshToken.token_hash == token_hash)
        .where(RefreshToken.expires_at > now)
    )
    row = result.first()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")

    _refresh_row, user = row
    if not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")

    strategy = get_jwt_strategy()
    access_token = await strategy.write_token(user)
    return AccessTokenBody(access_token=access_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw:
        token_hash = _hash_refresh_token(raw)
        await session.execute(delete(RefreshToken).where(RefreshToken.token_hash == token_hash))
        await session.commit()

    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=settings.ENVIRONMENT == "production",
        samesite="lax",
    )
