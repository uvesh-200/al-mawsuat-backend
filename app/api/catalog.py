from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.models.tables import Book

router = APIRouter(prefix="/books", tags=["books"])


class PublicBook(BaseModel):
    id: str
    title: str
    author: str | None
    language: str


class PublicBookList(BaseModel):
    items: list[PublicBook]


@router.get("", response_model=PublicBookList)
async def list_public_books(
    session: Annotated[AsyncSession, Depends(get_db)] = None,
) -> PublicBookList:
    """Publicly list ready books (default tenant) so the chat UI can scope
    questions to a single document."""
    result = await session.execute(
        select(Book)
        .where(Book.tenant_id == settings.DEFAULT_TENANT_ID, Book.status == "ready")
        .order_by(Book.title.asc())
    )
    books = result.scalars().all()
    return PublicBookList(
        items=[
            PublicBook(
                id=str(b.id),
                title=b.title,
                author=b.author,
                language=b.language,
            )
            for b in books
        ]
    )
