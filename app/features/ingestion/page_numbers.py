"""Printed footer page-number extraction, imputation and sanity checks."""
import logging
import re
from collections import Counter

from app.features.ingestion.words import _ARABIC_INDIC

logger = logging.getLogger(__name__)


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


def _log_page_summary(pages: list[dict]) -> None:
    """Trace log one line per page: OCR output size, page-number findings."""
    for p in pages:
        text = " ".join(w["text"] for w in p.get("words", []))
        logger.info(
            "[TRACE] page physical=%d printed=%s footer_extracted=%s words=%d preview=%r",
            p.get("physical_page"), p.get("printed_page_num"),
            p.get("footer_extracted"), len(p.get("words", [])),
            text[:160],
        )


def _modal_print_offset(pages: list[dict]) -> int | None:
    """The most common printed-physical offset across footer-extracted pages.

    Used for option (b): pages whose footer could not be extracted are
    assigned ``printed = physical + offset`` when the offset is reliable.
    """
    from collections import Counter
    offsets = [
        p["printed_page_num"] - p["physical_page"]
        for p in pages
        if p.get("footer_extracted") and p.get("printed_page_num") is not None
        and p.get("physical_page") is not None
    ]
    if not offsets:
        return None
    counter = Counter(offsets)
    top_val, top_cnt = counter.most_common(1)[0]
    # Reliable only when one offset dominates the extraction results.
    if top_cnt < max(1, int(len(offsets) * 0.8)):
        return None
    return top_val


def _impute_printed_pages(pages: list[dict]) -> None:
    """Option (b): fill in missing printed page numbers via the modal offset.

    Pages where footer extraction failed get ``printed_page_num =
    physical_page + modal_offset``. When fewer than half the pages extracted
    a footer — or the offsets disagree — the printed numbers are unreliable
    for the whole book, so every page keeps ``printed_page_num = None`` and
    the rest of the pipeline falls back to physical page numbers.
    """
    extracted = sum(1 for p in pages if p.get("footer_extracted"))
    if extracted == 0 or extracted < max(1, len(pages) * 0.5):
        logger.info(
            "Footer page numbers unreliable (%d/%d extracted); using physical "
            "page numbers for the whole book", extracted, len(pages),
        )
        return
    offset = _modal_print_offset(pages)
    if offset is None:
        logger.info(
            "Footer offsets inconsistent; using physical page numbers for the whole book"
        )
        return
    imputed = 0
    for p in pages:
        if p.get("footer_extracted") and p.get("printed_page_num") is not None:
            continue
        printed = p["physical_page"] + offset
        for w in p.get("words", []):
            w["printed_page_num"] = printed
        p["printed_page_num"] = printed
        p["printed_imputed"] = True
        imputed += 1
    if imputed:
        logger.info(
            "Imputed printed page numbers for %d pages via modal offset %+d",
            imputed, offset,
        )


def _sanity_check_page_numbers(pages: list[dict]) -> None:
    """Log check when printed page numbers diverge suspiciously from
    the physical PDF index. This is a QA aid — it does not raise errors."""
    mismatches = 0
    skips = 0
    for p in pages:
        footer = p.get("printed_page_num")
        physical = p.get("physical_page")
        if footer is None:
            skips += 1
            continue
        # A constant offset (physical - printed) should be identical for all pages
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
