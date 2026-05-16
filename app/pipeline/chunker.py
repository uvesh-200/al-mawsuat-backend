import re
from typing import Optional

SENTENCE_END = re.compile(r"[.۔!?؟]$")
HADITH_PATTERN = re.compile(r"(?:حديث\s*رقم|باب\s+\d+|رقم\s*\d+)")
AYAH_OPEN = "\ufefd"
AYAH_CLOSE = "\ufefe"
CHAPTER_WORDS = ("باب", "كتاب", "الفصل")
HARD_CEILING = 900
TARGET_MAX = 800


def chunk(pages: list[dict], tenant_id: str) -> list[dict]:
    if not pages:
        return []

    words = _flatten(pages)
    lines = _group_lines(words)
    chapters = _detect_chapters(lines)
    strategy = _detect_strategy(lines)
    raw = _split_chunks(words, lines, strategy)
    return [_build_chunk(c, tenant_id, lines, chapters) for c in raw]


def _flatten(pages: list[dict]) -> list[dict]:
    result = []
    for page in pages:
        for w in page["words"]:
            w["page_num"] = page["page_num"]
            w["_global_idx"] = len(result)
            result.append(w)
    return result


def _group_lines(words: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    current = [words[0]]
    for w in words[1:]:
        prev = current[-1]
        same_page = w["page_num"] == prev["page_num"]
        same_line = same_page and abs(w["bbox"][1] - prev["bbox"][1]) < 8
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


def _detect_chapters(lines: list[list[dict]]) -> dict[int, str]:
    indices: dict[int, str] = {}
    chapter: Optional[str] = None
    for i, line in enumerate(lines):
        text = _line_text(line)
        stripped = text.strip()
        word_list = stripped.split()
        if len(word_list) <= 8 and len(stripped) < 50:
            first = word_list[0] if word_list else ""
            if first in CHAPTER_WORDS or (
                first and all(c.isupper() for c in first if c.isalpha())
            ):
                chapter = stripped
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
        return _split_hadith(words, lines)
    if strategy == "quran":
        return _split_quran(words, lines)
    if strategy == "fiqh":
        return _split_fiqh(words, lines)
    return _split_fallback(words)


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


def _split_fiqh(words: list[dict], lines: list[list[dict]]) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    start = 0
    starts = _line_start_index(lines)
    for i, line in enumerate(lines):
        text = _line_text(line).strip()
        if text.endswith(":") and i > 0:
            end = _line_end_index(lines, i - 1)
            chunk_words = words[start : end + 1]
            if chunk_words:
                chunks.append(chunk_words)
            start = starts[i]
    remaining = words[start:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _split_fallback(words: list[dict]) -> list[list[dict]]:
    sentence_ends = _find_sentence_ends(words)
    chunks: list[list[dict]] = []
    chunk_start = 0
    for i, w in enumerate(words):
        token_count = i - chunk_start + 1
        if token_count >= TARGET_MAX and i in sentence_ends:
            chunks.append(words[chunk_start : i + 1])
            chunk_start = i + 1
    remaining = words[chunk_start:]
    if remaining:
        chunks.append(remaining)
    return _enforce_hard_ceiling(chunks)


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


def _compute_text(words: list[dict]) -> str:
    return " ".join(w["text"] for w in words)


def _compute_bbox(words: list[dict]) -> list[float]:
    bboxes = [w["bbox"] for w in words]
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
        "bbox": _compute_bbox(words),
        "chapter": _find_chapter(words[0], all_lines, chapters),
        "tenant_id": tenant_id,
        "token_count": len(text.split()),
    }
