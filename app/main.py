from fastapi import FastAPI

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.config import settings

app = FastAPI(title="Al-Mawsu'at al-Deobandiyyah API")

app.include_router(auth_router)
app.include_router(admin_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "environment": settings.ENVIRONMENT}

