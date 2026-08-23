"""Line grouping, heading detection and paragraph assembly."""
import logging
import re
from typing import Optional

from app.features.ingestion.sizing import (
    AYAH_CLOSE,
    AYAH_OPEN,
    CHAPTER_WORDS,
    HADITH_PATTERN,
    SENTENCE_END,
)

logger = logging.getLogger(__name__)


# Fiqh-style colon headings ("الخلاف:", "مسألة: ...") mark paragraph starts.
# A colon line is a heading only if it is short — long lines ending in a
# colon (e.g. a sentence ending "كالتالي:") are ordinary content.
HEADING_MAX_WORDS = 12
HEADING_MAX_CHARS = 60

# Visual paragraph gap heuristic for real (tesseract/fitz) bboxes: a gap
# between consecutive lines larger than this breaks the paragraph.
GAP_MULTIPLIER = 1.5
GAP_MIN_EXTRA = 6.0

HEADING_MARK = re.compile(r"^[\*\#\-\u2022\u00b7_\s]+")
NUMBER_MARK = re.compile(r"^\d+[\.\)،]?\s*")
TRAILING_PUNCT = re.compile(r"[.۔!?؟:,،;؛]$")
LEADING_BRACKET = re.compile(r"^[\(\[\"'\u201c\u201d\u00ab\u00bb]")
HAS_LETTER = re.compile(r"[A-Za-z\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


def _flatten(pages: list[dict]) -> list[dict]:
    result = []
    for page in pages:
        for w in page["words"]:
            w["page_num"] = page["page_num"]
            w["physical_page"] = page.get("physical_page", page["page_num"])
            w["printed_page_num"] = w.get("printed_page_num", page.get("printed_page_num"))
            w["_global_idx"] = len(result)
            result.append(w)
    return result


def _group_lines(words: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    current = [words[0]]
    for w in words[1:]:
        prev = current[-1]
        same_page = w["page_num"] == prev["page_num"]
        same_line = same_page and (
            w.get("bbox") is not None
            and prev.get("bbox") is not None
            and abs(w["bbox"][1] - prev["bbox"][1]) < 8
        )
        if same_line:
            current.append(w)
        else:
            lines.append(current)
            current = [w]
    if current:
        lines.append(current)
    return lines


def _line_text(line: list[dict]) -> str:
    return " ".join(w["text"] for w in line)


def _normalize_heading(text: str) -> Optional[str]:
    stripped = text.strip()
    stripped = HEADING_MARK.sub("", stripped).strip()
    stripped = NUMBER_MARK.sub("", stripped).strip()
    stripped = stripped.rstrip("*#").strip()
    if not stripped:
        return None
    if LEADING_BRACKET.search(stripped):
        return None
    if TRAILING_PUNCT.search(stripped):
        return None
    if not HAS_LETTER.search(stripped):
        return None
    word_list = stripped.split()
    if len(word_list) < 2 or len(word_list) > 8 or len(stripped) >= 50:
        return None
    return stripped


def _detect_chapters(lines: list[list[dict]]) -> dict[int, str]:
    indices: dict[int, str] = {}
    chapter: Optional[str] = None
    for i, line in enumerate(lines):
        candidate = _normalize_heading(_line_text(line))
        if candidate:
            first = candidate.split()[0]
            if first in CHAPTER_WORDS or (
                first
                and any(c.isalpha() for c in first)
                and all(c.isupper() for c in first if c.isalpha())
            ):
                chapter = candidate
        if chapter is not None:
            indices[i] = chapter
    return indices


def _detect_strategy(lines: list[list[dict]]) -> str:
    full_text = " ".join(_line_text(l) for l in lines)
    if HADITH_PATTERN.search(full_text):
        return "hadith"
    if AYAH_OPEN in full_text or AYAH_CLOSE in full_text:
        return "quran"
    colon_lines = sum(1 for l in lines if _line_text(l).strip().endswith(":"))
    if colon_lines >= 3:
        return "fiqh"
    return "fallback"


def _line_start_index(lines: list[list[dict]]) -> list[int]:
    counts = [0]
    for line in lines:
        counts.append(counts[-1] + len(line))
    return counts


def _line_end_index(lines: list[list[dict]], li: int) -> int:
    return _line_start_index(lines)[li + 1] - 1


def _is_heading_line(line: list[dict]) -> bool:
    """True for a short fiqh-style heading line ending with a colon."""
    text = _line_text(line).strip()
    if not text.endswith(":"):
        return False
    stripped = HEADING_MARK.sub("", text).rstrip(":").strip()
    stripped = NUMBER_MARK.sub("", stripped).strip()
    if not stripped or not HAS_LETTER.search(stripped):
        return False
    if len(stripped.split()) > HEADING_MAX_WORDS:
        return False
    if len(stripped) > HEADING_MAX_CHARS:
        return False
    return True


def _line_gap_height(lines: list[list[dict]]) -> Optional[float]:
    """Median line height across lines that carry bboxes, or None."""
    heights = []
    for line in lines:
        bboxes = [w["bbox"] for w in line if w.get("bbox") is not None]
        if len(bboxes) >= 2:
            heights.append(max(b[3] for b in bboxes) - min(b[1] for b in bboxes))
    if not heights:
        return None
    heights.sort()
    return heights[len(heights) // 2]


def _group_paragraphs(lines: list[list[dict]]) -> list[list[dict]]:
    """Group OCR lines into paragraph units (list of word-lists).

    A new paragraph starts when any of these is true:
    - the line's first word carries the ``para_start`` flag (blank line in
      the Gemini OCR text — see extractor._text_to_words),
    - the line is a short colon heading (fiqh masail style),
    - the vertical gap to the previous line is much larger than the median
      line height (real bbox data from tesseract/fitz).
    """
    paragraphs: list[list[dict]] = []
    current: list[dict] = []

    median_height = _line_gap_height(lines)
    prev_bottom: Optional[float] = None

    def _flush() -> None:
        nonlocal current
        if current:
            paragraphs.append(current)
            current = []

    for line in lines:
        if not line:
            continue
        first_bbox = line[0].get("bbox")
        gap: Optional[float] = None
        if prev_bottom is not None and first_bbox is not None:
            gap = first_bbox[1] - prev_bottom

        if current and (
            line[0].get("para_start")
            or _is_heading_line(line)
            or _is_large_gap(gap, median_height)
        ):
            _flush()

        current.extend(line)
        if first_bbox is not None:
            prev_bottom = max(
                (w["bbox"][3] for w in line if w.get("bbox") is not None),
                default=prev_bottom,
            )
        else:
            prev_bottom = None

    _flush()
    return paragraphs


def _is_large_gap(gap: Optional[float], median_height: Optional[float]) -> bool:
    if gap is None or median_height is None or median_height <= 0:
        return False
    return gap > median_height * GAP_MULTIPLIER + GAP_MIN_EXTRA


