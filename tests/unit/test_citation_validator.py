"""Unit tests for the citation validator and resolver (Bug 2 regression guard).

These tests run without any live services or LLM calls.
"""
import re
import pytest
from app.features.qa.agent import (
    _validate_citations,
    _resolve_citations,
    _CITATION_TAG_RE,
)


def _make_passages(n: int) -> list[dict]:
    return [
        {
            "text": f"نص المقطع {i}",
            "book_name": f"كتاب {i}",
            "page_start": i * 10,
            "author": "مؤلف",
            "chapter": None,
            "bbox": None,
            "score": 0.9,
            "book_id": f"book-{i}",
            "book_type": None,
        }
        for i in range(1, n + 1)
    ]


class TestValidateCitations:
    def test_valid_tags_no_errors(self):
        passages = _make_passages(3)
        answer = "هذا هو الجواب [P1, Page 10] وهذا مزيد [P3, Page 30]."
        bad = _validate_citations(answer, passages)
        assert bad == []

    def test_out_of_range_tag_detected(self):
        passages = _make_passages(3)
        answer = "إجابة تحتوي على [P99, Page 5] المخترع."
        bad = _validate_citations(answer, passages)
        assert 99 in bad

    def test_zero_index_tag_detected(self):
        passages = _make_passages(3)
        answer = "هذا [P0, Page 1] خطأ."
        bad = _validate_citations(answer, passages)
        assert 0 in bad

    def test_multiple_bad_tags(self):
        passages = _make_passages(2)
        answer = "[P5, Page 5] و [P10, Page 10] كلاهما خاطئ."
        bad = _validate_citations(answer, passages)
        assert 5 in bad
        assert 10 in bad

    def test_no_tags_no_errors(self):
        passages = _make_passages(5)
        answer = "إجابة بدون أي إشارات مرجعية."
        bad = _validate_citations(answer, passages)
        assert bad == []

    def test_boundary_tag_at_exactly_n(self):
        passages = _make_passages(5)
        answer = "الجواب [P5, Page 50]."  # 5 is the last valid index
        bad = _validate_citations(answer, passages)
        assert bad == []

    def test_tag_without_page_number_valid(self):
        passages = _make_passages(2)
        answer = "الإشارة [P2]."
        bad = _validate_citations(answer, passages)
        assert bad == []

    def test_combined_tags_detected(self):
        passages = _make_passages(3)
        answer = "مزيج [P2, Page 5; P1, Page 9]."
        bad = _validate_citations(answer, passages)
        assert bad == []

    def test_combined_tags_out_of_range_detected(self):
        passages = _make_passages(3)
        answer = "مزيج [P2, Page 5; P99, Page 9]."
        bad = _validate_citations(answer, passages)
        assert 99 in bad


class TestResolveCitations:
    def test_resolves_to_book_name_and_page(self):
        passages = _make_passages(3)
        answer = "الجواب هو [P2, Page 99]."
        resolved = _resolve_citations(answer, passages)
        # Should use chunk's actual page_start (20), not the LLM's "99"
        assert "[كتاب 2, Page 20]" in resolved
        assert "P2" not in resolved

    def test_out_of_range_tag_removed(self):
        passages = _make_passages(2)
        answer = "إجابة [P99, Page 5] مع مرجع خاطئ."
        resolved = _resolve_citations(answer, passages)
        assert "P99" not in resolved
        assert "[" not in resolved or "Page" not in resolved.split("P99")[0]

    def test_multiple_different_tags_resolved(self):
        passages = _make_passages(3)
        answer = "[P1, Page 1] وأيضاً [P3, Page 3]."
        resolved = _resolve_citations(answer, passages)
        assert "كتاب 1" in resolved
        assert "كتاب 3" in resolved
        # page_start values are 10, 20, 30 (i*10 in fixture)
        assert "Page 10" in resolved
        assert "Page 30" in resolved

    def test_tag_without_book_name_uses_page_only(self):
        passages = [{"text": "نص", "book_name": "", "page_start": 7,
                     "author": "", "chapter": None, "bbox": None,
                     "score": 0.5, "book_id": "x", "book_type": None}]
        answer = "[P1, Page 99] إجابة."
        resolved = _resolve_citations(answer, passages)
        assert "[Page 7]" in resolved

    def test_no_tags_unchanged(self):
        passages = _make_passages(2)
        answer = "إجابة بدون إشارات."
        assert _resolve_citations(answer, passages) == answer

    def test_combined_tags_in_one_bracket_resolved(self):
        passages = _make_passages(3)
        answer = "الجواب [P1, Page 2; P2, Page 1] نهاية."
        resolved = _resolve_citations(answer, passages)
        assert "[كتاب 1, Page 10]; [كتاب 2, Page 20]" in resolved
        assert "P1" not in resolved
        assert "P2" not in resolved

    def test_combined_tags_with_plain_index_only(self):
        passages = _make_passages(3)
        answer = "نص [P1; P3, Page 30]."
        resolved = _resolve_citations(answer, passages)
        assert "[كتاب 1, Page 10]; [كتاب 3, Page 30]" in resolved

    def test_combined_tags_out_of_range_removed(self):
        passages = _make_passages(2)
        answer = "إجابة [P1, Page 1; P99, Page 5]."
        resolved = _resolve_citations(answer, passages)
        assert "[كتاب 1, Page 10]" in resolved
        assert "P99" not in resolved


class TestCitationTagRegex:
    """Verify the regex correctly parses various tag formats."""

    def test_tag_with_page(self):
        m = _CITATION_TAG_RE.search("[P3, Page 12]")
        assert m is not None
        assert m.group(1) == "3"
        assert m.group(2) == "12"

    def test_tag_without_page(self):
        m = _CITATION_TAG_RE.search("[P7]")
        assert m is not None
        assert m.group(1) == "7"
        assert m.group(2) is None

    def test_no_match_for_plain_brackets(self):
        assert _CITATION_TAG_RE.search("[Book Name, Page 3]") is None

    def test_multiple_tags_found(self):
        text = "النص [P1, Page 5] ثم [P2, Page 8] وأخيراً [P3]."
        matches = list(_CITATION_TAG_RE.finditer(text))
        assert len(matches) == 3
        assert [m.group(1) for m in matches] == ["1", "2", "3"]
