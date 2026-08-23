"""Unit tests: citations resolve to the actual page of the cited text (Bug 3
regression guard).

Multi-page passages carry page_offsets (char ranges per page) through the
pipeline; the LLM sees ⟨Page N⟩ markers and cites the marker's page, which
citation resolution validates against the pages the passage really spans.
"""
import pytest

from app.features.qa.agent import _annotate_pages, _format_passages, _resolve_citations


def _multi_page_passage(text: str = "هذا نص في الصفحة الخامسة ثم نص في الصفحة السادسة") -> dict:
    mid = text.find("ثم")
    return {
        "book_name": "كتاب التجربة",
        "author": "",
        "text": text,
        "page_start": 5,
        "page_end": 6,
        "chapter": None,
        "page_offsets": [
            {"page": 5, "start_char": 0, "end_char": mid - 1},
            {"page": 6, "start_char": mid, "end_char": len(text) - 1},
        ],
    }


class TestAnnotatePages:
    def test_multi_page_text_gets_markers(self):
        p = _multi_page_passage()
        annotated = _annotate_pages(p["text"], p["page_offsets"])
        assert "\u27e8Page 5\u27e9" in annotated
        assert "\u27e8Page 6\u27e9" in annotated
        # the page-6 marker must sit before the page-6 text
        assert annotated.index("\u27e8Page 6\u27e9") < annotated.index("ثم")

    def test_single_page_no_markers(self):
        text = "نص عادي"
        assert _annotate_pages(text, [{"page": 3, "start_char": 0, "end_char": 10}]) == text
        assert _annotate_pages(text, []) == text
        assert _annotate_pages(text, None) == text

    def test_truncated_text_annotates_only_visible_pages(self):
        text = "أحد اثنان ثلاثة أربعة خمسة"
        offsets = [
            {"page": 1, "start_char": 0, "end_char": 10},
            {"page": 2, "start_char": 11, "end_char": len(text) - 1},
        ]
        # truncation kept only page 1's slice -> no markers needed
        assert "\u27e8Page 1\u27e9" not in _annotate_pages("أحد اثنان", offsets)
        # truncation kept part of page 2 as well -> both pages are marked
        annotated = _annotate_pages("أحد اثنان ثلاثة", offsets)
        assert "\u27e8Page 1\u27e9" in annotated
        assert "\u27e8Page 2\u27e9" in annotated

    def test_format_passages_shows_markers_and_range(self):
        p = _multi_page_passage()
        formatted = _format_passages([p])
        assert "[P1]" in formatted
        assert "\u27e8Page 5\u27e9" in formatted and "\u27e8Page 6\u27e9" in formatted
        assert "Page 5-6" in formatted


class TestResolveCitations:
    def test_citation_uses_cited_page_when_within_chunk_pages(self):
        answer = "الرأي الأول وارد في [P1, Page 6]."
        resolved = _resolve_citations(answer, [_multi_page_passage()])
        assert "[كتاب التجربة, Page 6]" in resolved

    def test_citation_on_first_page_still_resolves(self):
        answer = "الرأي الأول وارد في [P1, Page 5]."
        resolved = _resolve_citations(answer, [_multi_page_passage()])
        assert "[كتاب التجربة, Page 5]" in resolved

    def test_invalid_llm_page_falls_back_to_page_start(self):
        answer = "الرأي الأول وارد في [P1, Page 99]."
        resolved = _resolve_citations(answer, [_multi_page_passage()])
        assert "[كتاب التجربة, Page 5]" in resolved

    def test_single_page_chunk_unaffected(self):
        p = {
            "book_name": "كتاب التجربة",
            "text": "نص قصير",
            "page_start": 3,
            "page_end": 3,
            "chapter": None,
            "page_offsets": [{"page": 3, "start_char": 0, "end_char": 10}],
        }
        answer = "العبارة في [P1, Page 3]."
        assert "[كتاب التجربة, Page 3]" in _resolve_citations(answer, [p])

    def test_combined_tags_resolve_individually(self):
        p1 = _multi_page_passage()
        p2 = {
            "book_name": "كتاب ثان",
            "text": "نص آخر",
            "page_start": 2,
            "page_end": 2,
            "chapter": None,
            "page_offsets": [{"page": 2, "start_char": 0, "end_char": 10}],
        }
        answer = "انظر [P1, Page 6; P2, Page 2]."
        resolved = _resolve_citations(answer, [p1, p2])
        assert "[كتاب التجربة, Page 6]" in resolved
        assert "[كتاب ثان, Page 2]" in resolved
