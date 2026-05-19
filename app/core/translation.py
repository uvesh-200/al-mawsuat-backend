import httpx

from app.config import settings

TRANSLATION_SERVER_URL = settings.NLLB_SERVER_URL


async def detect_language(text: str) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{TRANSLATION_SERVER_URL}/detect",
            json={"text": text},
        )
        resp.raise_for_status()
        return resp.json()["language"]


async def translate_for_retrieval(text: str, source_lang: str) -> str:
    if source_lang == "ar":
        return text
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{TRANSLATION_SERVER_URL}/translate",
            json={"text": text, "source_lang": source_lang, "target_lang": "ar"},
        )
        resp.raise_for_status()
        return resp.json()["translated"]
