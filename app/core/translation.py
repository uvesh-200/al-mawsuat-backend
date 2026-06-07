import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TRANSLATION_SERVER_URL = settings.NLLB_SERVER_URL


async def detect_language(text: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{TRANSLATION_SERVER_URL}/detect",
                json={"text": text},
            )
            resp.raise_for_status()
            return resp.json()["language"]
    except Exception:
        logger.exception("Language detection failed, defaulting to 'ar'")
        return "ar"


async def translate_for_retrieval(text: str, source_lang: str) -> str:
    if source_lang == "ar":
        return text
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{TRANSLATION_SERVER_URL}/translate",
                json={"text": text, "source_lang": source_lang, "target_lang": "ar"},
            )
            resp.raise_for_status()
            return resp.json()["translated"]
    except Exception:
        logger.exception("Translation failed, returning original text")
        return text
