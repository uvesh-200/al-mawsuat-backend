"""Unit tests for paragraph-aware chunking (Bug 6 regression guard).

These tests run without any live services. They cover the two failure
modes found in QA:
- Gemini-OCR'd summary books being split into sentence-level fragments
  by the fiqh colon-heading strategy,
- chunks with no overlap splitting a claim from its supporting quote.
"""
import pytest
from app.features.ingestion.chunker import (
    TARGET_MAX,
    chunk,
    _group_paragraphs,
    _build_chunks_from_paragraphs,
    _is_heading_line,
)


def _word(text: str, page: int = 1, para_start: bool = False) -> dict:
    w = {"text": text, "page_num": page, "bbox": None}
    if para_start:
        w["para_start"] = True
    return w


def _line(*texts: str, para_start: bool = False) -> list[dict]:
    out = []
    for i, t in enumerate(texts):
        out.append(_word(t, para_start=(para_start and i == 0)))
    return out


def _page(words: list[dict], page_num: int = 1) -> dict:
    for w in words:
        w["page_num"] = page_num
    return {"page_num": page_num, "physical_page": page_num, "words": words}


class TestHeadingLine:
    def test_short_colon_heading(self):
        assert _is_heading_line(_line("الزبيدي:"))

    def test_label_with_content_on_same_line_not_heading(self):
        # "الزبيدي: محمد بن محمد..." does not END with a colon
        assert not _is_heading_line(_line("الزبيدي:", "محمد", "بن", "محمد."))

    def test_long_colon_line_not_heading(self):
        words = [f"w{i}" for i in range(15)]
        assert not _is_heading_line(_line(*words, "الجملة:"))

    def test_plain_line_not_heading(self):
        assert not _is_heading_line(_line("هذا", "نص", "عادي."))


class TestGroupParagraphs:
    def test_blank_line_separates_paragraphs(self):
        p1 = _line("أول", "فقرة.")
        p2 = _line("ثاني", "فقرة.", para_start=True)
        lines = [p1, p2]
        paragraphs = _group_paragraphs(lines)
        assert len(paragraphs) == 2

    def test_colon_heading_starts_new_paragraph(self):
        p1 = _line("نص", "قبل", "العنوان.")
        p2 = _line("الزبيدي:")
        p3 = _line("محمد", "بن", "محمد.")
        lines = [p1, p2, p3]
        paragraphs = _group_paragraphs(lines)
        assert len(paragraphs) == 2
        # the heading line and its following content form ONE paragraph
        assert paragraphs[1][0]["text"] == "الزبيدي:"

    def test_first_para_start_does_not_create_empty_paragraph(self):
        p1 = _line("أول", "فقرة.", para_start=True)
        paragraphs = _group_paragraphs([p1])
        assert len(paragraphs) == 1


class TestBuildChunksFromParagraphs:
    def test_short_fragments_group_into_one_chunk(self):
        """Sentence-level fragments of one discussion must NOT each become
        their own chunk — they group into a single ~TARGET_MAX chunk."""
        bullets = []
        for i in range(200):
            words = [f"k{i}", "بعض", "الكلمات", "العادية", f"الرقم{i}."]
            bullets.append(_line(*words, para_start=(i > 0)))
        chunks = _build_chunks_from_paragraphs(bullets)
        assert 2 <= len(chunks) <= 10
        # every bullet is inside a chunk of at least a few bullets
        assert all(len(c) >= 20 for c in chunks[:-1])
        assert all(len(c) <= TARGET_MAX for c in chunks)

    def test_chunks_overlap_share_tail(self):
        """Consecutive chunks re-include the tail of the previous chunk so
        a claim at a boundary is never lost."""
        paragraphs = []
        for i in range(300):
            paragraphs.append(_line(f"s{i}", "جملة", f"كاملة{i}.", para_start=(i > 0)))
        chunks = _build_chunks_from_paragraphs(paragraphs)
        assert len(chunks) >= 3
        # the first word of chunk[i+1] must appear in chunk[i] (overlap)
        for a, b in zip(chunks, chunks[1:]):
            tail_words = {w["text"] for w in a}
            assert b[0]["text"] in tail_words

    def test_no_mid_sentence_fragments(self):
        paragraphs = []
        for i in range(200):
            paragraphs.append(_line(f"فقرة{i}", "كلمات", "متعددة", f"نقطة{i}.", para_start=(i > 0)))
        chunks = _build_chunks_from_paragraphs(paragraphs)
        # paragraph boundaries are chunk boundaries: every chunk starts with
        # the first word of one of the paragraphs
        first_words = {p[0]["text"] for p in paragraphs}
        for c in chunks[1:]:
            assert c[0]["text"] in first_words

    def test_empty_input(self):
        assert _build_chunks_from_paragraphs([]) == []


class TestChunkEndToEnd:
    def test_gemini_style_summary_book_not_fragmented(self):
        """A Gemini-OCR'd summary page with colon labels (fiqh detection)
        must NOT be split into one chunk per label."""
        page_words: list[dict] = []
        for i in range(30):
            label = f"عنوان{i}:"
            page_words += _line(label, para_start=(i == 0))
            for j in range(3):
                page_words += _line(f"كلمة", f"رقم", f"{j}.")
        pages = [_page(page_words)]
        chunks = chunk(pages, "test-tenant")
        # 30 labels -> previously 30+ tiny chunks; now a handful of sizable ones
        assert len(chunks) <= 6
        assert all(c["token_count"] >= 10 for c in chunks)

    def test_short_chunk_still_single_paragraph(self):
        pages = [_page(_line("جملة", "واحدة", "قصيرة."))]
        chunks = chunk(pages, "test-tenant")
        assert len(chunks) == 1
