import logging
import re

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Urdu uses Arabic script plus these extra characters not found in Arabic
_URDU_CHARS = re.compile(r"[\u0679-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
# Common Urdu words that don't appear in Arabic
_URDU_WORDS = re.compile(
    r"\b(?:ہے|کا|کی|کے|سے|میں|اور|یہ|اس|ان|تم|آپ|نے|کو|بھی|ہی|تو|کر|ہو|گی|گے)\b",
    re.UNICODE,
)

TRANSLATE_BASE = settings.GOOGLE_TRANSLATE_BASE_URL


def _is_likely_urdu(text: str) -> bool:
    return bool(_URDU_CHARS.search(text) or _URDU_WORDS.search(text))


async def detect_language(text: str) -> str:
    if not settings.GOOGLE_TRANSLATE_API_KEY:
        logger.warning("GOOGLE_TRANSLATE_API_KEY not set, defaulting to 'ar'")
        return "ur" if _is_likely_urdu(text) else "ar"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{TRANSLATE_BASE}/detect",
                params={"key": settings.GOOGLE_TRANSLATE_API_KEY},
                json={"q": [text]},
            )
            resp.raise_for_status()
            data = resp.json()
            detected = data["data"]["detections"][0][0]["language"]
            if detected == "ar" and _is_likely_urdu(text):
                return "ur"
            return detected
    except Exception:
        logger.exception("Language detection failed, defaulting to 'ar'")
        if _is_likely_urdu(text):
            return "ur"
        return "ar"


async def translate_for_retrieval(text: str, source_lang: str) -> str:
    if source_lang == "ar":
        if _is_likely_urdu(text):
            source_lang = "ur"
        else:
            return text
    if not settings.GOOGLE_TRANSLATE_API_KEY:
        logger.warning("GOOGLE_TRANSLATE_API_KEY not set, returning original text")
        return text
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                TRANSLATE_BASE,
                params={"key": settings.GOOGLE_TRANSLATE_API_KEY},
                json={"q": text, "source": source_lang, "target": "ar"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data["data"]["translations"][0]["translatedText"]
    except Exception:
        logger.exception("Translation failed, returning original text")
        return text
