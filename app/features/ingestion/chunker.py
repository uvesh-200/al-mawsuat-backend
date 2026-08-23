"""Chunk orchestration: strategy selection, merging, payload build."""
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
    _detect_chapters,
    _detect_strategy,
    _flatten,
    _group_lines,
    _group_paragraphs,  # noqa: F401  (re-exported for tests)
    _is_heading_line,  # noqa: F401
)
from app.features.ingestion.paragraphs import (
    _build_chunks_from_paragraphs,
    _enforce_hard_ceiling,
    _merge_cross_page_chunks,
    _split_hadith,
    _split_quran,
)
from app.features.ingestion.sizing import (  # noqa: F401
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


def chunk(pages: list[dict], tenant_id: str) -> list[dict]:
    if not pages:
        return []

    words = _flatten(pages)
    if not words:
        return []
    lines = _group_lines(words)
    chapters = _detect_chapters(lines)
    strategy = _detect_strategy(lines)
    logger.info(
        "[TRACE] chunk input pages=%d words=%d lines=%d chapters=%d strategy=%s",
        len(pages), len(words), len(lines), len(chapters), strategy,
    )
    if chapters:
        logger.info(
            "[TRACE] chapters detected=%s",
            {idx: name for idx, name in sorted(chapters.items())},
        )
    raw = _split_chunks(words, lines, strategy)
    raw = _merge_cross_page_chunks(raw)
    chunks = [_build_chunk(c, tenant_id, lines, chapters) for c in raw]
    logger.info(
        "[TRACE] chunk done strategy=%s chunks=%d",
        strategy, len(chunks),
    )
    for i, c in enumerate(chunks):
        logger.info(
            "[TRACE] chunk idx=%d pages=%s..%s tokens=%d chapter=%s preview=%r",
            i, c["page_start"], c["page_end"], c["token_count"],
            c.get("chapter"), c["text"][:200],
        )
    return chunks


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
    page_bboxes = _compute_page_bboxes(words)
    offsets = _annotate_offsets_with_printed(_compute_page_offsets(words), words)
    printed_start = words[0].get("printed_page_num")
    printed_end = words[-1].get("printed_page_num")
    return {
        "text": text,
        "page_start": words[0]["page_num"],
        "page_end": words[-1]["page_num"],
        "physical_page_start": words[0].get("physical_page", words[0]["page_num"]),
        "physical_page_end": words[-1].get("physical_page", words[-1]["page_num"]),
        "printed_page_start": printed_start,
        "printed_page_end": printed_end,
        "bbox": _compute_bbox(words, page_bboxes),
        "page_bboxes": page_bboxes,
        "page_offsets": offsets,
        "chapter": _find_chapter(words[0], all_lines, chapters),
        "tenant_id": tenant_id,
        "token_count": len(text.split()),
    }
