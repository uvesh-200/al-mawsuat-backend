import asyncio
import base64
import logging
import random

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# One image per request: Gemini does not reliably return per-image
# transcriptions for multi-image batches (it may return a single merged
# text, which the line-based fallback splitter then chops up incorrectly).
OCR_BATCH_SIZE = 1
MAX_RETRIES = settings.OCR_MAX_RETRIES
BASE_DELAY = 1.0
MAX_DELAY = settings.OCR_RETRY_MAX_DELAY
CONCURRENT_BATCHES = settings.OCR_CONCURRENT_BATCHES

_ocr_semaphore = asyncio.Semaphore(CONCURRENT_BATCHES)

_PROMPT = """Extract the exact raw text from this book page image. The page may be
in Arabic, Urdu, or English.
Return ONLY the original text characters exactly as they appear, in the original
language and script. Preserve line breaks. Do not summarize, explain, translate,
or add any commentary."""


def _encode_image(png_bytes: bytes) -> dict:
    b64 = base64.b64encode(png_bytes).decode("utf-8")
    return {"inline_data": {"mime_type": "image/png", "data": b64}}


async def _ocr_batch(client: httpx.AsyncClient, page_images: list[bytes]) -> list[str]:
    model = f"models/{settings.GEMINI_OCR_MODEL}"
    url = f"{settings.GEMINI_API_BASE}/v1/{model}:generateContent"

    parts = [_encode_image(img) for img in page_images]
    contents = [{"parts": parts}]
    payload = {
        "systemInstruction": {"parts": [{"text": _PROMPT}]},
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": 4096,
            "temperature": 0.1,
        },
    }

    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.post(
                url,
                headers={"x-goog-api-key": settings.GEMINI_API_KEY, "Content-Type": "application/json"},
                json=payload,
                timeout=180.0,
            )
            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                wait = min(retry_after or BASE_DELAY * (2 ** min(attempt, 4)) + random.uniform(0, 1), MAX_DELAY)
                logger.warning("OCR 429 rate limited, retrying in %.1fs (attempt %d/%d)", wait, attempt + 1, MAX_RETRIES)
                await asyncio.sleep(wait)
                last_exc = httpx.HTTPStatusError("429 Too Many Requests", request=resp.request, response=resp)
                continue
            resp.raise_for_status()
            data = resp.json()
            texts = _parse_batch_response(data, len(page_images))
            if any(not (t or "").strip() for t in texts):
                wait = min(BASE_DELAY * (2 ** min(attempt, 4)) + random.uniform(0, 1), MAX_DELAY)
                logger.warning(
                    "OCR attempt %d/%d returned empty text for %d/%d page(s), retrying in %.1fs",
                    attempt + 1, MAX_RETRIES,
                    sum(1 for t in texts if not (t or "").strip()), len(page_images), wait,
                )
                await asyncio.sleep(wait)
                last_exc = ValueError("Empty OCR response")
                continue
            return texts
        except httpx.TimeoutException as e:
            last_exc = e
            wait = min(BASE_DELAY * (2 ** min(attempt, 4)) + random.uniform(0, 1), MAX_DELAY)
            logger.warning("OCR attempt %d/%d timed out, retrying in %.1fs", attempt + 1, MAX_RETRIES, wait)
            await asyncio.sleep(wait)
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                last_exc = e
                wait = min(BASE_DELAY * (2 ** min(attempt, 4)) + random.uniform(0, 1), MAX_DELAY)
                logger.warning("OCR attempt %d/%d failed (%d), retrying in %.1fs", attempt + 1, MAX_RETRIES, e.response.status_code, wait)
                await asyncio.sleep(wait)
            else:
                raise
        except Exception as e:
            last_exc = e
            logger.warning("OCR attempt %d/%d failed: %s", attempt + 1, MAX_RETRIES, e)
            await asyncio.sleep(BASE_DELAY * (attempt + 1))
    logger.exception("All OCR retries exhausted")
    raise last_exc


async def ocr_batch(client: httpx.AsyncClient, page_images: list[bytes]) -> list[str]:
    async with _ocr_semaphore:
        return await _ocr_batch(client, page_images)


async def ocr_pages(client: httpx.AsyncClient, page_images: list[bytes]) -> list[str]:
    texts = await ocr_batch(client, page_images)
    return texts


def _parse_batch_response(data: dict, expected: int) -> list[str]:
    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError("No candidates in Gemini response")

    parts = []
    for c in candidates:
        content = c.get("content", {})
        for part in content.get("parts", []):
            text = part.get("text", "")
            if text:
                parts.append(text)

    full_text = "\n".join(parts)
    separators = _find_image_separators(full_text)
    if separators:
        texts = []
        start = 0
        for s in separators:
            texts.append(full_text[start:s].strip())
            start = s + 1
        texts.append(full_text[start:].strip())
        while len(texts) < expected:
            texts.append("")
        return texts[:expected]

    lines = full_text.strip().split("\n")
    per_page = max(1, len(lines) // max(expected, 1))
    texts = []
    for i in range(expected):
        start = i * per_page
        end = start + per_page if i < expected - 1 else len(lines)
        texts.append("\n".join(lines[start:end]).strip())
    return texts


def _find_image_separators(text: str) -> list[int]:
    import re
    indices = []
    for m in re.finditer(r"(?:\*\*Page\s+\d+\*\*|\[Page\s+\d+\]|---+)", text):
        indices.append(m.start())
    return indices


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
