from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.core.db import get_db
from app.models.tables import AdminSettings, User

router = APIRouter(prefix="/admin/settings", tags=["settings"])


class AdminSettingsOut(BaseModel):
    system_prompt: str
    product_name: str
    primary_color: str


class UpdateAdminSettingsRequest(BaseModel):
    system_prompt: str | None = None
    product_name: str | None = None
    primary_color: str | None = None


async def _get_or_create_settings(session: AsyncSession) -> AdminSettings:
    result = await session.execute(select(AdminSettings).where(AdminSettings.id == 1))
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = AdminSettings(id=1)
        session.add(settings)
        await session.commit()
        await session.refresh(settings)
    return settings


@router.get("")
async def get_admin_settings(
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> AdminSettingsOut:
    settings = await _get_or_create_settings(session)
    return AdminSettingsOut(
        system_prompt=settings.system_prompt or "",
        product_name=settings.product_name or "Al-Mawsu'at al-Deobandiyyah",
        primary_color=settings.primary_color or "#1D9E75",
    )


@router.put("", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def update_admin_settings(
    body: UpdateAdminSettingsRequest,
    user: Annotated[User, Depends(get_current_user)] = None,
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> None:
    settings = await _get_or_create_settings(session)

    dirty = False
    if body.system_prompt is not None:
        settings.system_prompt = body.system_prompt
        dirty = True
    if body.product_name is not None:
        settings.product_name = body.product_name
        dirty = True
    if body.primary_color is not None:
        settings.primary_color = body.primary_color
        dirty = True

    if dirty:
        await session.commit()
