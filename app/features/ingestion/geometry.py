"""Chunk geometry: joined text, per-page boxes, half-open offsets."""
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def _compute_text(words: list[dict]) -> str:
    return " ".join(w["text"] for w in words)


def _compute_page_bboxes(words: list[dict]) -> list[dict]:
    """Per-page minimal enclosing rectangles for a chunk.

    Word bboxes live in each page's own coordinate space (0..page_width,
    0..page_height). A single min/max union across pages produces a rectangle
    that exists on NO page (e.g. x1 or y1 in the tens of thousands), so the
    chunk stores one box per page it spans instead.

    Only words with REAL geometry (fitz text layer or Tesseract, tagged
    ``geom="real"`` by the extractor) contribute boxes. The Gemini OCR path
    emits synthetic char-width-heuristic boxes purely to support line/paragraph
    grouping; unioning those per page would store fictional rectangles that
    render as misplaced bands or near-full-page highlights.
    """
    per_page: dict[int, list[list[float]]] = {}
    for w in words:
        bbox = w.get("bbox")
        if bbox is None or w.get("geom", "real") != "real":
            continue
        per_page.setdefault(w["page_num"], []).append(bbox)

    result: list[dict] = []
    for page in sorted(per_page):
        boxes = per_page[page]
        result.append(
            {
                "page": page,
                "bbox": [
                    min(b[0] for b in boxes),
                    min(b[1] for b in boxes),
                    max(b[2] for b in boxes),
                    max(b[3] for b in boxes),
                ],
            }
        )
    return result


def _compute_bbox(words: list[dict], page_bboxes: list[dict]) -> list[float] | None:
    """The chunk's bbox expressed in page_start's coordinate space.

    For single-page chunks this is the whole chunk's rectangle (unchanged
    behaviour). For multi-page chunks it is the first page's own rectangle —
    the page the highlight endpoint renders by default — while the per-page
    boxes live in ``page_bboxes``.
    """
    if not page_bboxes:
        return None
    return page_bboxes[0]["bbox"]


def _compute_page_offsets(words: list[dict]) -> list[dict]:
    """Character ranges of the joined chunk text per page.

    Each entry maps a page to the half-open ``text[start_char:end_char]``
    slice of the chunk's joined text that came from that page (``end_char`` is
    EXCLUSIVE, matching Python/JavaScript slice semantics), plus the printed
    footer page number of that page (None when unknown). This survives into
    the stored payload so citation resolution can point at the actual page a
    cited sentence falls on instead of always using page_start.
    """
    offsets: list[dict] = []
    cur_page: Optional[int] = None
    start = 0
    pos = 0
    for w in words:
        page = w["page_num"]
        if page != cur_page:
            if cur_page is not None:
                # end_char exclusive: first char index of the next page's slice
                offsets.append({"page": cur_page, "start_char": start, "end_char": pos})
            cur_page = page
            start = pos
        pos += len(w["text"]) + 1  # +1 accounts for the join space
    if cur_page is not None:
        # final segment ends at the true end of the joined text (pos-1 because
        # the trailing word also counted its join space)
        offsets.append({"page": cur_page, "start_char": start, "end_char": pos - 1})
    return offsets


def _annotate_offsets_with_printed(offsets: list[dict], words: list[dict]) -> list[dict]:
    """Stamp each offset entry with the printed page number of its page.

    Word dicts carry per-page ``printed_page_num``; the first word of each
    page's slice determines the value for the whole segment.
    """
    word_iter = iter(words)
    for po in offsets:
        printed: Optional[int] = None
        for w in word_iter:
            if w["page_num"] == po["page"]:
                if w.get("printed_page_num") is not None:
                    printed = w["printed_page_num"]
                break
            if w["page_num"] > po["page"]:
                break
        po["printed_page_num"] = printed
    return offsets


