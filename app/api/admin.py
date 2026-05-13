from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.auth import get_current_user
from app.models.tables import User

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/books")
async def list_books_placeholder(
    _: Annotated[User, Depends(get_current_user)],
) -> dict:
    return {"items": []}
