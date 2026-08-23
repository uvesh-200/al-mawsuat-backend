"""Ingestion-time page-number validation.

After extraction, call ``validate_ingestion_page_numbers`` to assert that the
footer-extracted page numbers form a plausible monotone sequence.  If too many
pages fail extraction or the sequence is non-monotone beyond the allowed
threshold, an ``IngestionPageNumberError`` is raised and the job is failed.

This catches the systematic-offset bug at ingest time so bad data never
reaches Qdrant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class IngestionPageNumberError(ValueError):
    """Raised when page-number quality does not meet the minimum threshold."""


@dataclass
class PageNumberReport:
    total_pages: int
    footer_extracted: int
    monotone_violations: int
    extraction_rate: float
    violation_rate: float
    offset: int | None  # Most common physical - printed offset, or None
    imputed_pages: int = 0  # pages whose printed number came from the modal offset


def _compute_offset(pages: list[dict]) -> int | None:
    """Return the modal (printed_page_num - physical_page) offset across pages
    where footer extraction succeeded.  None if no data."""
    from collections import Counter
    offsets = [
        p["printed_page_num"] - p["physical_page"]
        for p in pages
        if p.get("footer_extracted")
        and p.get("printed_page_num") is not None
        and p.get("physical_page") is not None
    ]
    if not offsets:
        return None
    return Counter(offsets).most_common(1)[0][0]


def _check_monotone(pages: list[dict]) -> int:
    """Count pages where the printed page_num decreases relative to the
    previous page that had footer extraction.  Isolated jumps (e.g., plates,
    inserts) are acceptable; a long descending run is not."""
    violations = 0
    prev: int | None = None
    for p in pages:
        printed = p.get("printed_page_num")
        if printed is None:
            continue
        if prev is not None and printed < prev:
            violations += 1
        prev = printed
    return violations


def validate_ingestion_page_numbers(
    pages: list[dict],
    *,
    min_extraction_rate: float = 0.5,
    max_violation_rate: float = 0.2,
) -> PageNumberReport:
    """Validate extracted page numbers against quality thresholds.

    Parameters
    ----------
    pages:
        Output of ``extract()``.  Each page dict must have keys
        ``physical_page``, ``page_num``, and ``footer_extracted``.
    min_extraction_rate:
        Fraction of pages that must have footer extraction succeed.
        Default 0.5 — fail if fewer than half the pages have a printed number.
    max_violation_rate:
        Maximum fraction of extracted pages with non-monotone page numbers.
        Default 0.2.

    Returns
    -------
    PageNumberReport

    Raises
    ------
    IngestionPageNumberError
        If either threshold is violated.
    """
    if not pages:
        raise IngestionPageNumberError("No pages returned from extraction")

    total = len(pages)
    extracted = sum(1 for p in pages if p.get("footer_extracted"))
    extraction_rate = extracted / total

    violations = _check_monotone(pages)
    violation_rate = violations / max(extracted, 1)

    offset = _compute_offset(pages)

    report = PageNumberReport(
        total_pages=total,
        footer_extracted=extracted,
        monotone_violations=violations,
        extraction_rate=extraction_rate,
        violation_rate=violation_rate,
        offset=offset,
        imputed_pages=sum(1 for p in pages if p.get("printed_imputed")),
    )

    logger.info(
        "Page-number validation: total=%d, footer_extracted=%d (%.0f%%), "
        "monotone_violations=%d (%.0f%%), modal_offset=%s, imputed=%d",
        total,
        extracted,
        extraction_rate * 100,
        violations,
        violation_rate * 100,
        offset,
        report.imputed_pages,
    )

    if extraction_rate < min_extraction_rate:
        raise IngestionPageNumberError(
            f"Footer page-number extraction rate too low: "
            f"{extracted}/{total} ({extraction_rate:.0%}). "
            f"Minimum required: {min_extraction_rate:.0%}. "
            "Check whether the book has printed page numbers in its footer/header."
        )

    if violation_rate > max_violation_rate:
        raise IngestionPageNumberError(
            f"Too many non-monotone page numbers: "
            f"{violations}/{extracted} extracted pages ({violation_rate:.0%}). "
            f"Maximum allowed: {max_violation_rate:.0%}. "
            "Footer OCR may be reading non-numeric content as page numbers."
        )

    return report
