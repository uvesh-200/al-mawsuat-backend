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

TESSERACT_LANG = "ara+eng"
WORD_THRESHOLD = 10

# Synthetic line metrics for Gemini OCR text (see _text_to_words)
_LINE_HEIGHT = 18.0
_PARAGRAPH_GAP = 3.0 * _LINE_HEIGHT

_LIST_MARKER = re.compile(r"^[#>*\-]+\s*|\d+[\.\)]\s*")

# ---------------------------------------------------------------------------
# Footer page-number extraction
# ---------------------------------------------------------------------------

# Matches Arabic-Indic numerals (٠١٢٣٤٥٦٧٨٩) and Western digits, optionally
# surrounded by whitespace / dashes / brackets. We look for the number in the
# last few lines of the page, where most Islamic books place the footer.
_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# Patterns that identify a standalone page number in the footer region:
#   - A line that IS ONLY digits (possibly preceded/followed by whitespace)
#   - A line like "- 7 -" or "[ 7 ]" or "— ٧ —"
_FOOTER_PAGE_RE = re.compile(
    r"(?:^|[\s\-–—\[\(])([٠١٢٣٤٥٦٧٨٩\d]{1,4})(?:$|[\s\-–—\]\)])",
)


def _extract_footer_page_number(ocr_text: str) -> int | None:
    """Return the printed page number found in the footer region of a page.

    Strategy:
    1. Examine the last 5 lines of the OCR output (footer zone).
    2. Search for a short numeric token that looks like a page number (1–4
       digits, value ≤ 9999).
    3. Convert Arabic-Indic to Western digits and return as int.
    4. Return None if no convincing page number is found.
    """
    if not ocr_text:
        return None

    lines = [ln.strip() for ln in ocr_text.splitlines() if ln.strip()]
    # Check the last 5 lines (footer zone); also check the first 2 lines for
    # books that put page numbers in the header.
    footer_lines = lines[-5:] + lines[:2]

    candidates: list[int] = []
    for line in footer_lines:
        # Ignore lines that are long text (e.g. body text) or contain date words
        words = line.split()
        if len(words) > 4:
            continue
        if any(w in line for w in ("سنة", "عام", "طبع", "طبعة", "مـ")):
            continue

        # Normalise Arabic-Indic → Western before matching
        normalised = line.translate(_ARABIC_INDIC)
        for m in _FOOTER_PAGE_RE.finditer(normalised):
            raw = m.group(1)
            try:
                val = int(raw)
                if 1 <= val <= 9999:
                    candidates.append(val)
            except ValueError:
                pass

    if not candidates:
        return None

    # If multiple numbers found, prefer values that appear more than once
    # (e.g., page number printed in both header and footer), otherwise take
    # the last one found (footer takes precedence over header).
    from collections import Counter
    counts = Counter(candidates)
    most_common_val, most_common_cnt = counts.most_common(1)[0]
    if most_common_cnt > 1:
        return most_common_val
    return candidates[-1]


def _sanity_check_page_numbers(pages: list[dict]) -> None:
    """Log warnings when printed page numbers diverge suspiciously from
    the physical PDF index. This is a QA aid — it does not raise errors."""
    mismatches = 0
    skips = 0
    for p in pages:
        footer = p.get("page_num")
        physical = p.get("physical_page")
        if footer is None:
            skips += 1
            continue
        # A constant offset (physical - footer) should be identical for all pages
        # once we've established it. Flag if the offset swings wildly.
        p["_offset"] = physical - footer

    offsets = [p["_offset"] for p in pages if "_offset" in p]
    if not offsets:
        return

    from statistics import median, stdev as _stdev
    med = median(offsets)
    try:
        sd = _stdev(offsets) if len(offsets) > 1 else 0.0
    except Exception:
        sd = 0.0

    for p in pages:
        if "_offset" in p and abs(p["_offset"] - med) > 5:
            mismatches += 1

    logger.info(
        "Page-number sanity check: %d pages, footer-extraction skips=%d, "
        "median_offset=%d, stdev=%.1f, suspicious_pages=%d",
        len(pages), skips, int(med), sd, mismatches,
    )
    if mismatches > len(pages) * 0.1:
        logger.warning(
            "More than 10%% of pages have inconsistent page-number offsets. "
            "Check footer OCR quality. offset_median=%d, offset_stdev=%.1f",
            int(med), sd,
        )
    # Clean up temp field
    for p in pages:
        p.pop("_offset", None)


# ---------------------------------------------------------------------------
# Text cleaning helpers
# ---------------------------------------------------------------------------

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
                if "\n" in text or "\t" in text or len(text) > 40:
                    continue
                x = int(row.get("left", "0")) * scale
                y = int(row.get("top", "0")) * scale
                line_key = f"{row.get('block_num', '0')}-{row.get('par_num', '0')}-{row.get('line_num', '0')}"
                words.append({"text": text, "bbox": [x, y, x + w * scale, y + h * scale], "line": line_key})
        return words
    finally:
        try:
            if os.path.exists(png_name):
                os.unlink(png_name)
        except OSError:
            pass


def _text_to_words(text: str, page_num: int) -> list[dict]:
    """Convert Gemini OCR text into word dicts with synthetic line bboxes.

    The Gemini path has no real layout data, so each OCR line is assigned a
    synthetic bounding box (line-height tall, full-width). This lets the
    chunker's line/paragraph grouping treat consecutive words on the same
    OCR line as one line instead of one word per line — the old behaviour
    caused the fiqh strategy to fire on every word ending with a colon and
    fragment books into sentence-level chunks.

    Blank OCR lines mark paragraph breaks; the first word of the line after
    a blank line is flagged ``para_start`` so the chunker can build
    paragraph-aware chunks.
    """
    words = []
    y = 0.0
    para_start = False
    for raw_line in _clean_gemini_text(text).split("\n"):
        line = raw_line.strip()
        if not line:
            y += _PARAGRAPH_GAP
            para_start = True
            continue
        tokens = line.split()
        line_width = sum(len(t) * 10 + 5 for t in tokens)
        x = 0.0
        line_words = []
        for token in tokens:
            tw = len(token) * 10
            entry = {
                "text": token,
                "page_num": page_num,
                "bbox": [x, y, x + tw, y + _LINE_HEIGHT],
            }
            if para_start:
                entry["para_start"] = True
                para_start = False
            line_words.append(entry)
            x += tw + 5
        words.extend(line_words)
        y += _LINE_HEIGHT
    if not words:
        return [{"text": "", "page_num": page_num, "bbox": None}]
    return words


# ---------------------------------------------------------------------------
# Tesseract extraction
# ---------------------------------------------------------------------------

def _process_page_tesseract(pdf_bytes: bytes, physical_page: int) -> dict:
    """Extract text from a single page using fitz + Tesseract fallback.

    Returns a dict with:
      physical_page  — 1-based index in the PDF file
      page_num       — printed footer page number (or physical_page if not found)
      words          — list of word dicts
      footer_extracted — True if page_num came from footer OCR
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[physical_page - 1]
    words = _words_from_fitz(page)
    raw_text = page.get_text("text")
    doc.close()

    if len(words) < WORD_THRESHOLD:
        words = _words_from_tesseract(page)

    footer_num = _extract_footer_page_number(raw_text)
    if footer_num is None:
        logger.debug("No footer page number on physical page %d; using physical index", physical_page)
    else:
        logger.debug("Footer page number on physical page %d: %s", physical_page, footer_num)

    # Chunk page numbers must match the physical page index used by the
    # viewer/highlight endpoint (doc[page - 1]). Printed footer numbers are
    # unreliable (missing/OCR-noise), so page_num = physical_page.
    page_num = physical_page
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

        words = _text_to_words(ocr_text, page_num)
        results.append({
            "physical_page": physical_page,
            "page_num": page_num,
            "printed_page_num": footer_num,
            "footer_extracted": footer_num is not None,
            "words": words,
        })

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
