"""Book extraction pipelines (Tesseract / Gemini) - public entry point."""
import logging
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import fitz
import httpx

from app.core.config import settings
from app.core.ocr import ocr_batch

logger = logging.getLogger(__name__)

from app.features.ingestion.page_numbers import (  # noqa: F401
    _ARABIC_INDIC,
    _extract_footer_page_number,
    _impute_printed_pages,
    _log_page_summary,
    _modal_print_offset,
    _sanity_check_page_numbers,
)
from app.features.ingestion.words import (  # noqa: F401
    TESSERACT_LANG,
    WORD_THRESHOLD,
    _clean_gemini_text,
    _text_to_words,
    _words_from_fitz,
    _words_from_tesseract,
)


def _process_page_tesseract(pdf_bytes: bytes, physical_page: int) -> dict:
    """Extract text from a single page using fitz + Tesseract fallback.

    Returns a dict with:
      physical_page  — 1-based index in the PDF file
      page_num       — printed footer page number (or physical_page if not found)
      words          — list of word dicts
      footer_extracted — True if page_num came from footer OCR
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[physical_page - 1]
        words = _words_from_fitz(page)
        raw_text = page.get_text("text")

        if len(words) < WORD_THRESHOLD:
            # Must run while the document is still open: page objects become
            # invalid after doc.close().
            words = _words_from_tesseract(page)
    finally:
        doc.close()

    footer_num = _extract_footer_page_number(raw_text)
    if footer_num is None:
        logger.debug("No footer page number on physical page %d; using physical index", physical_page)
    else:
        logger.debug("Footer page number on physical page %d: %s", physical_page, footer_num)

    # Chunk page numbers must match the physical page index used by the
    # viewer/highlight endpoint (doc[page - 1]). Printed footer numbers are
    # unreliable (missing/OCR-noise), so page_num = physical_page; the
    # printed number is carried separately for citation display.
    page_num = physical_page
    for w in words:
        w["printed_page_num"] = footer_num
    return {
        "physical_page": physical_page,
        "page_num": page_num,
        "printed_page_num": footer_num,
        "footer_extracted": footer_num is not None,
        "words": words,
    }


async def _extract_tesseract(pdf_bytes: bytes, progress_callback=None) -> list[dict]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = len(doc)
    doc.close()

    max_workers = settings.OCR_MAX_WORKERS or os.cpu_count() or 4
    results = [None] * total
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_process_page_tesseract, pdf_bytes, i + 1): i
            for i in range(total)
        }
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()
            if progress_callback:
                progress_callback(idx + 1, total)

    _impute_printed_pages(results)
    _log_page_summary(results)
    _sanity_check_page_numbers(results)
    return results


# ---------------------------------------------------------------------------
# Gemini extraction
# ---------------------------------------------------------------------------

async def _extract_gemini(pdf_bytes: bytes, progress_callback=None) -> list[dict]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = len(doc)

    logger.info("Rendering %d pages to PNG...", total)
    all_images: list[bytes] = []
    # Also grab raw fitz text per page for footer extraction (fast, no OCR cost)
    fitz_texts: list[str] = []
    for i in range(total):
        pix = doc[i].get_pixmap(dpi=settings.OCR_DPI)
        all_images.append(pix.tobytes("png"))
        fitz_texts.append(doc[i].get_text("text"))
    doc.close()
    logger.info("Rendered %d pages, starting OCR...", total)

    all_texts = [None] * total
    sem = asyncio.Semaphore(settings.OCR_CONCURRENT_BATCHES)

    async with httpx.AsyncClient(timeout=180.0) as client:
        async def _ocr_one(idx: int, img: bytes) -> tuple[int, str]:
            async with sem:
                try:
                    texts = await ocr_batch(client, [img])
                    return idx, texts[0] if texts else ""
                except Exception:
                    logger.warning(
                        "OCR failed for physical page %d after retries; keeping it empty",
                        idx + 1,
                    )
                    return idx, ""

        tasks = [_ocr_one(i, img) for i, img in enumerate(all_images)]
        for coro in asyncio.as_completed(tasks):
            idx, text = await coro
            all_texts[idx] = text
            if progress_callback:
                progress_callback(idx + 1, total)

    results = []
    for i, text in enumerate(all_texts):
        physical_page = i + 1
        ocr_text = text or ""

        # Gemini sometimes returns empty text for scripture-heavy pages
        # (safety "RECITATION" block). Fall back to local tesseract OCR.
        if not ocr_text.strip():
            with fitz.open(stream=pdf_bytes, filetype="pdf") as fallback_doc:
                fallback_words = _words_from_tesseract(fallback_doc[i])
            if fallback_words:
                ocr_text = " ".join(w["text"] for w in fallback_words)
                logger.warning(
                    "Gemini OCR empty for physical page %d; used tesseract fallback (%d words)",
                    physical_page, len(fallback_words),
                )

        # Try footer extraction from Gemini OCR text first; fall back to
        # fitz raw text (which is available for text-layer PDFs at zero cost).
        footer_num = _extract_footer_page_number(ocr_text)
        if footer_num is None:
            footer_num = _extract_footer_page_number(fitz_texts[i])

        # Canonical page number = physical page index (see _process_page_tesseract).
        page_num = physical_page

        words = _text_to_words(ocr_text, page_num, footer_num)
        results.append({
            "physical_page": physical_page,
            "page_num": page_num,
            "printed_page_num": footer_num,
            "footer_extracted": footer_num is not None,
            "words": words,
        })

    # Option (b): impute missing printed page numbers from the modal offset
    # BEFORE _log_page_summary so the trace shows resolved values.
    _impute_printed_pages(results)
    _log_page_summary(results)
    _sanity_check_page_numbers(results)
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def extract(pdf_bytes: bytes, progress_callback=None) -> list[dict]:
    if settings.OCR_ENGINE == "gemini":
        logger.info("Using Gemini OCR engine")
        return await _extract_gemini(pdf_bytes, progress_callback)
    logger.info("Using Tesseract OCR engine")
    return await _extract_tesseract(pdf_bytes, progress_callback)
