from fastapi import FastAPI
from app.config import settings

app = FastAPI(title="Al-Mawsu'at al-Deobandiyyah API")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "environment": settings.ENVIRONMENT}

