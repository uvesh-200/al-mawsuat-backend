"""Paragraph-to-chunk assembly: overlap, hadith/ayah splits, ceilings."""
import logging
import re
from typing import Optional

from app.features.ingestion.geometry import (
    _annotate_offsets_with_printed,
    _compute_bbox,
    _compute_page_bboxes,
    _compute_page_offsets,
    _compute_text,
)
from app.features.ingestion.layout import (
    _group_paragraphs,
    _is_heading_line,
    _line_start_index,
    _line_text,
)
from app.features.ingestion.sizing import (
    AYAH_CLOSE,
    AYAH_OPEN,
    CHAPTER_WORDS,
    CROSS_PAGE_MERGE_THRESHOLD,
    HADITH_PATTERN,
    HARD_CEILING,
    MIN_TARGET,
    OVERLAP_FRACTION,
    SENTENCE_END,
    TARGET_MAX,
)

logger = logging.getLogger(__name__)


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


