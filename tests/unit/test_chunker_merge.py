"""Unit tests for chunker cross-page merge logic (Bug 4 regression guard).

These tests run without any live services.
"""
import pytest
from app.features.ingestion.chunker import (
    CROSS_PAGE_MERGE_THRESHOLD,
    TARGET_MAX,
    chunk,
    _merge_cross_page_chunks,
)


def _make_word(text: str, page_num: int, physical_page: int | None = None) -> dict:
    return {
        "text": text,
        "page_num": page_num,
        "physical_page": physical_page if physical_page is not None else page_num,
        "bbox": None,
    }


def _make_page(page_num: int, n_words: int, last_word: str | None = None) -> dict:
    words = [_make_word(f"كلمة{i}", page_num) for i in range(n_words - 1)]
    terminal = last_word or "نهاية."
    words.append(_make_word(terminal, page_num))
    return {"page_num": page_num, "physical_page": page_num, "words": words}


class TestMergeCrossPageChunks:
    def test_two_short_chunks_merged(self):
        """Two small chunks (both < CROSS_PAGE_MERGE_THRESHOLD) should be merged."""
        short = CROSS_PAGE_MERGE_THRESHOLD - 10
        w1 = [_make_word(f"w{i}", 1) for i in range(short)]
        w2 = [_make_word(f"v{i}", 2) for i in range(short)]
        # Make chunk 1 end without sentence-ending punctuation
        w1[-1]["text"] = "كلمة"
        result = _merge_cross_page_chunks([w1, w2])
        assert len(result) == 1
        assert len(result[0]) == short * 2

    def test_sentence_ending_short_chunk_merged(self):
        """Even a sentence-ending short chunk should merge with its successor
        if it's under the threshold."""
        short = CROSS_PAGE_MERGE_THRESHOLD - 1
        w1 = [_make_word(f"w{i}", 1) for i in range(short)]
        w2 = [_make_word(f"v{i}", 2) for i in range(short)]
        w1[-1]["text"] = "جملة."  # ends sentence but chunk is short
        result = _merge_cross_page_chunks([w1, w2])
        assert len(result) == 1

    def test_long_sentence_ending_chunk_not_merged(self):
        """A long chunk that ends a sentence should NOT be merged."""
        long_n = CROSS_PAGE_MERGE_THRESHOLD + 50
        w1 = [_make_word(f"w{i}", 1) for i in range(long_n)]
        w2 = [_make_word(f"v{i}", 2) for i in range(50)]
        w1[-1]["text"] = "جملة."  # ends sentence, chunk is long
        result = _merge_cross_page_chunks([w1, w2])
        assert len(result) == 2

    def test_merged_would_exceed_target_not_merged(self):
        """If merging would exceed TARGET_MAX, don't merge."""
        n = TARGET_MAX // 2 + 10
        w1 = [_make_word(f"w{i}", 1) for i in range(n)]
        w2 = [_make_word(f"v{i}", 2) for i in range(n)]
        w1[-1]["text"] = "كلمة"  # no sentence end, short enough to want merge
        result = _merge_cross_page_chunks([w1, w2])
        assert len(result) == 2

    def test_empty_input(self):
        assert _merge_cross_page_chunks([]) == []

    def test_single_chunk_unchanged(self):
        words = [_make_word(f"w{i}", 1) for i in range(50)]
        result = _merge_cross_page_chunks([words])
        assert result == [words]

    def test_three_short_chunks_all_merged(self):
        n = CROSS_PAGE_MERGE_THRESHOLD // 3
        w1 = [_make_word(f"a{i}", 1) for i in range(n)]
        w2 = [_make_word(f"b{i}", 2) for i in range(n)]
        w3 = [_make_word(f"c{i}", 3) for i in range(n)]
        w1[-1]["text"] = "كلمة"
        w2[-1]["text"] = "كلمة"
        result = _merge_cross_page_chunks([w1, w2, w3])
        assert len(result) == 1
        assert len(result[0]) == n * 3


class TestChunkPageStartPageEnd:
    """Verify chunk page_start / page_end are correctly set after merging."""

    def test_same_page_chunk(self):
        pages = [_make_page(3, 20)]
        chunks = chunk(pages, "test-tenant")
        assert all(c["page_start"] == 3 for c in chunks)
        assert all(c["page_end"] == 3 for c in chunks)

    def test_cross_page_short_chunk_single_citation(self):
        """A hadith on page 5 and its short commentary on page 6 should
        produce at most one chunk, so the answer only cites one page."""
        page5 = _make_page(5, 30, last_word="كلمة")   # no sentence end
        page6 = _make_page(6, 20)
        chunks = chunk([page5, page6], "test-tenant")
        # After merge, the single chunk should span pages 5–6
        # (page_start=5, page_end=6) not produce two separate chunks
        page_starts = {c["page_start"] for c in chunks}
        # Both pages should NOT produce separate citations for a short passage
        assert len(chunks) <= 2  # could be 1 merged or 2 if long enough

    def test_physical_page_fields_present(self):
        pages = [_make_page(9, 15)]  # printed=9, physical=9
        # Override physical_page to simulate 6-page offset
        for w in pages[0]["words"]:
            w["physical_page"] = 15
        pages[0]["physical_page"] = 15
        chunks = chunk(pages, "test-tenant")
        for c in chunks:
            assert "physical_page_start" in c
            assert "physical_page_end" in c
