from fastapi import FastAPI

app = FastAPI(title="Al-Mawsu'at al-Deobandiyyah API")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}

