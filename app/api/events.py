from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi_users.jwt import decode_jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.events import event_generator
from app.models.db import get_db
from app.models.tables import User

router = APIRouter(prefix="/admin", tags=["events"])


async def sse_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    token = request.query_params.get("token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        payload = decode_jwt(token, settings.JWT_SECRET_KEY, ["fastapi-users:auth"])
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    result = await session.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


@router.get("/events")
async def sse_events(
    request: Request,
    user: Annotated[User, Depends(sse_user)] = None,
) -> StreamingResponse:
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
