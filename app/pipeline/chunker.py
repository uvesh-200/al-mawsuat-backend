import re
from typing import Optional

SENTENCE_END = re.compile(r"[.۔!?؟]$")
HADITH_PATTERN = re.compile(r"(?:حديث\s*رقم|باب\s+\d+|رقم\s*\d+)")
AYAH_OPEN = "\ufefd"
AYAH_CLOSE = "\ufefe"
CHAPTER_WORDS = ("باب", "كتاب", "الفصل")
HARD_CEILING = 900
TARGET_MAX = 420  # soft upper bound; complete paragraphs grouped into ~420-word chunks
MIN_TARGET = 200  # don't close a chunk below this size unless it's the tail
OVERLAP_FRACTION = 0.15  # next chunk re-includes ~15% of the previous chunk's tail
CROSS_PAGE_MERGE_THRESHOLD = 150  # merge consecutive chunks if both are this short

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


def chunk(pages: list[dict], tenant_id: str) -> list[dict]:
    if not pages:
        return []

    words = _flatten(pages)
    if not words:
        return []
    lines = _group_lines(words)
    chapters = _detect_chapters(lines)
    strategy = _detect_strategy(lines)
    raw = _split_chunks(words, lines, strategy)
    raw = _merge_cross_page_chunks(raw)
    return [_build_chunk(c, tenant_id, lines, chapters) for c in raw]


def _flatten(pages: list[dict]) -> list[dict]:
    result = []
    for page in pages:
        for w in page["words"]:
            w["page_num"] = page["page_num"]
            w["physical_page"] = page.get("physical_page", page["page_num"])
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


def _split_chunks(
    words: list[dict],
    lines: list[list[dict]],
    strategy: str,
) -> list[list[dict]]:
    if strategy == "hadith":
        chunks = _split_hadith(words, lines)
    elif strategy == "quran":
        chunks = _split_quran(words, lines)
    else:
        # Fiqh and fallback books are chunked paragraph-aware: lines are
        # grouped into paragraphs (visual gaps, blank-line para_start flags,
        # or short colon-heading lines), then paragraphs are grouped into
        # chunks of TARGET_MAX words with ~OVERLAP_FRACTION overlap so no
        # sentence or logical unit (claim + supporting quote) is split.
        paragraphs = _group_paragraphs(lines)
        chunks = _build_chunks_from_paragraphs(paragraphs)
    return _enforce_hard_ceiling(chunks)


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


def _build_chunks_from_paragraphs(
    paragraphs: list[list[dict]],
) -> list[list[dict]]:
    """Group paragraphs into chunks of up to TARGET_MAX words.

    - Consecutive paragraphs are absorbed greedily; a chunk is closed only
      when adding the next paragraph would exceed TARGET_MAX and the chunk
      has at least MIN_TARGET words.
    - The next chunk starts ~OVERLAP_FRACTION x TARGET_MAX words back inside
      the previous chunk's tail, at a paragraph boundary, so overlapping
      regions never cut a sentence in half.
    - A single paragraph larger than TARGET_MAX is split at sentence ends.
    """
    if not paragraphs:
        return []

    chunks: list[list[dict]] = []
    overlap_budget = max(1, int(round(TARGET_MAX * OVERLAP_FRACTION)))
    i = 0
    n = len(paragraphs)
    while i < n:
        if len(paragraphs[i]) > TARGET_MAX:
            chunks.extend(_split_oversized_paragraph(paragraphs[i]))
            i += 1
            continue

        chunk_words: list[dict] = []
        j = i
        while (
            j < n
            and len(chunk_words) + len(paragraphs[j]) <= TARGET_MAX
        ):
            chunk_words.extend(paragraphs[j])
            j += 1

        if j >= n:
            chunks.append(chunk_words)
            break

        if len(chunk_words) >= MIN_TARGET:
            chunks.append(chunk_words)
            i = _overlap_start(paragraphs, i, j, overlap_budget)
        else:
            # Rare: everything so far is under MIN_TARGET but the next
            # paragraph would overshoot. Close as-is and move on.
            chunks.append(chunk_words)
            i = j
    return chunks


def _overlap_start(
    paragraphs: list[list[dict]],
    start: int,
    end: int,
    budget: int,
) -> int:
    """Paragraph index where the next chunk should begin so that it overlaps
    the chunk ``paragraphs[start:end]`` by roughly ``budget`` words.

    Walks backwards from the last paragraph of the previous chunk and never
    returns before ``start``; the result always includes at least one
    paragraph of overlap when the chunk has more than one paragraph.
    """
    words = 0
    idx = end - 1
    while idx > start and words < budget:
        words += len(paragraphs[idx])
        idx -= 1
    # Never restart at the same index (would loop forever for a
    # single-paragraph chunk); at least one paragraph of progress.
    return max(idx, start + 1)


def _split_oversized_paragraph(paragraph: list[dict]) -> list[list[dict]]:
    """Split a paragraph larger than TARGET_MAX at sentence ends."""
    sentence_ends = _find_sentence_ends(paragraph)
    pieces: list[list[dict]] = []
    start = 0
    for i, w in enumerate(paragraph):
        if i - start + 1 >= TARGET_MAX and i in sentence_ends:
            pieces.append(paragraph[start : i + 1])
            start = i + 1
    remaining = paragraph[start:]
    if remaining:
        pieces.append(remaining)
    return pieces if pieces else [paragraph]


def _split_hadith(words: list[dict], lines: list[list[dict]]) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    start = 0
    hadith_count = 0
    starts = _line_start_index(lines)
    for i, line in enumerate(lines):
        text = _line_text(line)
        if HADITH_PATTERN.search(text):
            if hadith_count > 0:
                end = _line_end_index(lines, i - 1)
                chunk_words = words[start : end + 1]
                if chunk_words:
                    chunks.append(chunk_words)
                start = starts[i]
            hadith_count += 1
    remaining = words[start:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _split_quran(words: list[dict], lines: list[list[dict]]) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    start = 0
    starts = _line_start_index(lines)
    for i, line in enumerate(lines):
        text = _line_text(line)
        if AYAH_CLOSE in text:
            end = _line_end_index(lines, i)
            chunk_words = words[start : end + 1]
            if chunk_words:
                chunks.append(chunk_words)
            start = starts[i + 1] if i + 1 < len(lines) else len(words)
    remaining = words[start:]
    if remaining:
        chunks.append(remaining)
    return chunks if chunks else [words[:]]


def _find_sentence_ends(words: list[dict]) -> set[int]:
    indices: set[int] = set()
    for i, w in enumerate(words):
        if SENTENCE_END.search(w["text"]):
            indices.add(i)
    return indices


def _enforce_hard_ceiling(chunks: list[list[dict]]) -> list[list[dict]]:
    result: list[list[dict]] = []
    for chunk in chunks:
        if len(chunk) <= HARD_CEILING:
            result.append(chunk)
        else:
            for start in range(0, len(chunk), HARD_CEILING):
                result.append(chunk[start : start + HARD_CEILING])
    return result


def _merge_cross_page_chunks(chunks: list[list[dict]]) -> list[list[dict]]:
    """Merge consecutive short chunks that straddle page boundaries.

    A chunk is a candidate for merging with its successor when:
    - It contains fewer than CROSS_PAGE_MERGE_THRESHOLD words, OR
    - Its last word does not end a sentence (the chunk is mid-sentence).
    - The merged result would not exceed TARGET_MAX words.

    This prevents a single logical passage (e.g., a hadith and its
    short commentary) from being cited with two different page numbers.
    """
    if len(chunks) < 2:
        return chunks

    merged: list[list[dict]] = []
    i = 0
    while i < len(chunks):
        current = chunks[i]
        while i + 1 < len(chunks):
            nxt = chunks[i + 1]
            combined_len = len(current) + len(nxt)
            current_ends_sentence = bool(
                SENTENCE_END.search(current[-1]["text"]) if current else False
            )
            current_is_short = len(current) < CROSS_PAGE_MERGE_THRESHOLD
            if combined_len <= TARGET_MAX and (current_is_short or not current_ends_sentence):
                current = current + nxt
                i += 1
            else:
                break
        merged.append(current)
        i += 1
    return merged


def _compute_text(words: list[dict]) -> str:
    return " ".join(w["text"] for w in words)


def _compute_bbox(words: list[dict]) -> list[float] | None:
    bboxes = [w["bbox"] for w in words if w.get("bbox") is not None]
    if not bboxes:
        return None
    return [
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    ]


def _find_chapter(
    word: dict, all_lines: list[list[dict]], chapters: dict[int, str]
) -> Optional[str]:
    word_idx = word["_global_idx"]
    count = 0
    for li, line in enumerate(all_lines):
        count += len(line)
        if word_idx < count:
            line_idx = li
            break
    else:
        line_idx = len(all_lines) - 1
    candidates = [chapters[k] for k in sorted(chapters) if k <= line_idx]
    return candidates[-1] if candidates else None


def _build_chunk(
    words: list[dict],
    tenant_id: str,
    all_lines: list[list[dict]],
    chapters: dict[int, str],
) -> dict:
    text = _compute_text(words)
    return {
        "text": text,
        "page_start": words[0]["page_num"],
        "page_end": words[-1]["page_num"],
        "physical_page_start": words[0].get("physical_page", words[0]["page_num"]),
        "physical_page_end": words[-1].get("physical_page", words[-1]["page_num"]),
        "bbox": _compute_bbox(words),
        "chapter": _find_chapter(words[0], all_lines, chapters),
        "tenant_id": tenant_id,
        "token_count": len(text.split()),
    }
