import logging
import re

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TRANSLATION_SERVER_URL = settings.NLLB_SERVER_URL

# Urdu uses Arabic script plus these extra characters not found in Arabic
_URDU_CHARS = re.compile(r"[\u0679-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
# Common Urdu words that don't appear in Arabic
_URDU_WORDS = re.compile(
    r"\b(?:ہے|کا|کی|کے|سے|میں|اور|یہ|اس|ان|تم|آپ|نے|کو|بھی|ہی|تو|کر|ہو|گی|گے)\b",
    re.UNICODE,
)


def _is_likely_urdu(text: str) -> bool:
    """Check if text is likely Urdu rather than Arabic by looking for Urdu-specific characters."""
    return bool(_URDU_CHARS.search(text) or _URDU_WORDS.search(text))


async def detect_language(text: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{TRANSLATION_SERVER_URL}/detect",
                json={"text": text},
            )
            resp.raise_for_status()
            detected = resp.json()["language"]
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
