import asyncio
import logging
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import fitz
import httpx

from app.config import settings
from app.core.ocr import ocr_batch

logger = logging.getLogger(__name__)

TESSERACT_LANG = "ara"
WORD_THRESHOLD = 10

_LIST_MARKER = re.compile(r"^([#>*\-]+\s*|\d+[\.\)]\s*)")


def _clean_gemini_text(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        line = _LIST_MARKER.sub("", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        line = line.replace("*", "").replace("#", "")
        lines.append(line)
    return "\n".join(lines)


def _words_from_fitz(page: fitz.Page) -> list[dict]:
    raw = page.get_text("words")
    return [{"text": w[4], "bbox": list(w[:4])} for w in raw]


def _words_from_tesseract(page: fitz.Page) -> list[dict]:
    pix = page.get_pixmap(dpi=settings.OCR_DPI)
    scale = 72.0 / settings.OCR_DPI
    tmp_png = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    png_name = tmp_png.name
    tmp_png.close()
    try:
        pix.save(png_name)
        proc = subprocess.run(
            ["tesseract", png_name, "stdout", "-l", TESSERACT_LANG, "--psm", "3", "tsv"],
            capture_output=True, text=True,
        )
        import csv
        import io
        words = []
        reader = csv.DictReader(io.StringIO(proc.stdout), delimiter="\t")
        for row in reader:
            text = row.get("text", "").strip()
            conf = row.get("conf", "-1")
            w = int(row.get("width", "0"))
            h = int(row.get("height", "0"))
            try:
                conf_f = float(conf)
            except ValueError:
                conf_f = -1.0
            if text and conf_f > 0 and w > 0 and h > 0:
                x = int(row.get("left", "0")) * scale
                y = int(row.get("top", "0")) * scale
                words.append({"text": text, "bbox": [x, y, x + w * scale, y + h * scale]})
        return words
    finally:
        try:
            if os.path.exists(png_name):
                os.unlink(png_name)
        except OSError:
            pass


def _text_to_words(text: str, page_num: int) -> list[dict]:
    words = []
    y = 0
    for line in _clean_gemini_text(text).split("\n"):
        line = line.strip()
        if not line:
            y += 20
            continue
        x = 0
        for token in line.split():
            tw = len(token) * 10
            words.append({
                "text": token,
                "page_num": page_num,
                "bbox": None,
            })
            x += tw + 5
        y += 20
    if not words:
        return [{"text": "", "page_num": page_num, "bbox": None}]
    return words


def _process_page_tesseract(pdf_bytes: bytes, page_num: int) -> dict:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_num - 1]
    words = _words_from_fitz(page)
    if len(words) < WORD_THRESHOLD:
        words = _words_from_tesseract(page)
    doc.close()
    return {"page_num": page_num, "words": words}


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
    return results


async def _extract_gemini(pdf_bytes: bytes, progress_callback=None) -> list[dict]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = len(doc)

    logger.info("Rendering %d pages to PNG...", total)
    all_images: list[bytes] = []
    for i in range(total):
        pix = doc[i].get_pixmap(dpi=settings.OCR_DPI)
        all_images.append(pix.tobytes("png"))
    doc.close()
    logger.info("Rendered %d pages, starting OCR...", total)

    all_texts = [None] * total
    sem = asyncio.Semaphore(settings.OCR_CONCURRENT_BATCHES)

    async with httpx.AsyncClient(timeout=180.0) as client:
        async def _ocr_one(idx: int, img: bytes) -> tuple[int, str]:
            async with sem:
                texts = await ocr_batch(client, [img])
                return idx, texts[0] if texts else ""

        tasks = [_ocr_one(i, img) for i, img in enumerate(all_images)]
        for coro in asyncio.as_completed(tasks):
            idx, text = await coro
            all_texts[idx] = text
            if progress_callback:
                progress_callback(idx + 1, total)

    results = []
    for i, text in enumerate(all_texts):
        words = _text_to_words(text or "", i + 1)
        results.append({"page_num": i + 1, "words": words})
    return results


async def extract(pdf_bytes: bytes, progress_callback=None) -> list[dict]:
    if settings.OCR_ENGINE == "gemini":
        logger.info("Using Gemini OCR engine")
        return await _extract_gemini(pdf_bytes, progress_callback)
    logger.info("Using Tesseract OCR engine")
    return await _extract_tesseract(pdf_bytes, progress_callback)
