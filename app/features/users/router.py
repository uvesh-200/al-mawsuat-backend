from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import get_current_user, get_user_manager, UserManager
from app.core.db import get_db
from app.models.tables import User

router = APIRouter(prefix="/admin/users", tags=["users"])


class UserItemOut(BaseModel):
    id: str
    email: str
    role: str
    is_active: bool
    created_at: str


class InviteRequest(BaseModel):
    email: EmailStr
    role: str = "user"


class UpdateUserRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None


@router.get("")
async def list_users(
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> list[UserItemOut]:
    result = await session.execute(
        select(User).where(User.tenant_id == settings.DEFAULT_TENANT_ID).order_by(User.created_at.desc())
    )
    users = result.scalars().all()
    return [
        UserItemOut(
            id=str(u.id),
            email=u.email,
            role=u.role,
            is_active=u.is_active,
            created_at=u.created_at.isoformat() if u.created_at else "",
        )
        for u in users
    ]


@router.post("/invite", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def invite_user(
    body: InviteRequest,
    current_user: Annotated[User, Depends(get_current_user)] = None,
    user_manager: Annotated[UserManager, Depends(get_user_manager)] = None,
) -> None:
    if current_user and current_user.role not in ("superadmin", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Only admins can invite users")

    temp_password = uuid.uuid4().hex[:16]
    _ = await user_manager.create(
        User(
            email=body.email,
            role=body.role,
            tenant_id=settings.DEFAULT_TENANT_ID,
        ),
        password=temp_password,
        safe=True,
    )
    return None


@router.put("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def update_user(
    user_id: str,
    body: UpdateUserRequest,
    current_user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> None:
    result = await session.execute(
        select(User).where(User.id == uuid.UUID(user_id))
    )
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.tenant_id != settings.DEFAULT_TENANT_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.role == "superadmin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cannot modify superadmin users")

    dirty = False
    if body.role is not None and body.role != target.role:
        target.role = body.role
        dirty = True
    if body.is_active is not None and body.is_active != target.is_active:
        target.is_active = body.is_active
        dirty = True

    if dirty:
        await session.commit()
