import logging
import re

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Urdu uses Arabic script plus these extra characters not found in Arabic.
# NOTE: \uFB50-\uFDFF (Arabic Presentation Forms-A) is intentionally excluded —
# it contains common Arabic ligatures like ﷺ (U+FDFA) and ﷲ (U+FDF2) that appear
# in ordinary Arabic text and must not flag a question as Urdu.
_URDU_CHARS = re.compile(r"[\u0679-\u06FF\u0750-\u077F\uFE70-\uFEFF]")
# Common Urdu words that don't appear in Arabic
_URDU_WORDS = re.compile(
    r"\b(?:ہے|کا|کی|کے|سے|میں|اور|یہ|اس|ان|تم|آپ|نے|کو|بھی|ہی|تو|کر|ہو|گی|گے)\b",
    re.UNICODE,
)

TRANSLATE_BASE = settings.GOOGLE_TRANSLATE_BASE_URL

# Translation is deliberately provider-swappable: when a Google Cloud
# Translation API key is present it wins (preferred); otherwise the same
# calls fall back to Gemini with the existing GEMINI_API_KEY (no billing,
# free tier). Adding a Google key later only requires editing .env.
_LANG_NAMES = {"en": "English", "ur": "Urdu", "ar": "Arabic"}

_GEMINI_TRANSLATE_PROMPT = (
    "Translate the following {lang} text into {target}. Keep proper nouns and "
    "names as they are. Return ONLY the {target} translation with no "
    "explanations, notes, or commentary.\n\n{text}"
)


def _is_likely_urdu(text: str) -> bool:
    return bool(_URDU_CHARS.search(text) or _URDU_WORDS.search(text))


def _detect_by_script(text: str) -> str:
    if _is_likely_urdu(text):
        return "ur"
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    arabic = sum(1 for ch in text if "\u0600" <= ch <= "\u06FF")
    if latin > 0 and latin >= arabic:
        return "en"
    return "ar"


def _google_configured() -> bool:
    return bool(settings.GOOGLE_TRANSLATE_API_KEY)


async def detect_language(text: str) -> str:
    if not _google_configured():
        logger.warning("GOOGLE_TRANSLATE_API_KEY not set, using script heuristic")
        return _detect_by_script(text)
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
        logger.exception("Language detection failed, using script heuristic")
        return _detect_by_script(text)


async def translate_for_retrieval(text: str, source_lang: str) -> str:
    if source_lang == "ar":
        if _is_likely_urdu(text):
            source_lang = "ur"
        else:
            # Arabic chunks are matched by the Arabic question directly.
            return text
    if not _google_configured() and not settings.GEMINI_API_KEY:
        logger.warning("No translation provider configured, returning original text")
        return text
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            if _google_configured():
                return await _translate_google(client, text, source_lang)
            return await _translate_gemini(client, text, source_lang)
    except Exception:
        logger.exception("Translation failed, returning original text")
        return text


async def _translate_google(
    client: httpx.AsyncClient, text: str, source_lang: str, target: str = "ar"
) -> str:
    resp = await client.post(
        TRANSLATE_BASE,
        params={"key": settings.GOOGLE_TRANSLATE_API_KEY},
        json={"q": text, "source": source_lang, "target": target},
    )
    resp.raise_for_status()
    data = resp.json()
    return data["data"]["translations"][0]["translatedText"]


async def _translate_gemini(
    client: httpx.AsyncClient, text: str, source_lang: str, target: str = "ar"
) -> str:
    lang_name = _LANG_NAMES.get(source_lang, source_lang)
    model = f"models/{settings.GEMINI_TRANSLATE_MODEL}"
    url = f"{settings.GEMINI_API_BASE}/v1/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": _GEMINI_TRANSLATE_PROMPT.format(lang=lang_name, target=target, text=text)}]}],
        "generationConfig": {
            "maxOutputTokens": 2048,
            "temperature": 0.2,
        },
    }
    resp = await client.post(
        url,
        headers={"x-goog-api-key": settings.GEMINI_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError("No candidates in Gemini translation response")
    parts = candidates[0].get("content", {}).get("parts", [])
    translated = "".join(p.get("text", "") for p in parts).strip()
    if not translated:
        raise ValueError("Empty Gemini translation response")
    return translated


async def translate_to_english(text: str, source_lang: str) -> str:
    """Translate the query into English (for lexical overlap against the
    English-indexed chunks). Arabic questions need an English leg so the
    reranker's overlap component isn't 0 against English book text."""
    if source_lang == "en":
        return text
    if not _google_configured() and not settings.GEMINI_API_KEY:
        return text
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            if _google_configured():
                return await _translate_google(client, text, source_lang, "en")
            return await _translate_gemini(client, text, source_lang, "en")
    except Exception:
        logger.exception("Translation to English failed, returning original text")
        return text
