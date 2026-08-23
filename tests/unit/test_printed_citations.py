"""Unit tests: citation resolution in a SINGLE page space (physical pages).

The old behaviour mixed printed footer numbers into ⟨Page N⟩ markers, Source
lines and _claim_page fallbacks (with a falsy-zero bug: printed 0 fell back to
the physical page). Everything user-facing now resolves in PHYSICAL page
space — the same space the viewer and /highlight endpoint use — so a cited
page number always matches the page actually rendered on screen.

Also guards the dense-page anchoring: ``_claim_page`` ranks passages by
matched-token count first (an anchor-only match must not beat a long exact
match) and anchors on the page that contains MOST matched tokens.
"""
import pytest

from app.rag.agent import (
    _annotate_pages,
    _claim_page,
    _dense_page,
    _format_passages,
    _is_citation_question,
    _claim_tokens,
    _resolve_citations,
)


def _passage(
    book="How to Love Prophet",
    text="www.alqurans.com one narration is from Abu Hurayrah where the Prophet says the people would go around it",
    page_start=5,
    page_end=7,
):
    return {
        "book_name": book,
        "author": "",
        "text": text,
        "page_start": page_start,
        "page_end": page_end,
        "chapter": None,
        "page_offsets": [
            {"page": page_start, "start_char": 0, "end_char": len(text), "printed_page_num": None}
        ],
    }


class TestPhysicalMarkers:
    def test_markers_always_physical_even_when_printed_known(self):
        """printed_page_num must never leak into markers (old code preferred
        it; printed 0 additionally collided with the falsy fallback)."""
        offsets = [
            {"page": 5, "start_char": 0, "end_char": 10, "printed_page_num": 11},
            {"page": 6, "start_char": 11, "end_char": 20, "printed_page_num": 12},
        ]
        annotated = _annotate_pages("aaaaaaaaaa bbbbbbbbbb", offsets)
        assert "\u27e8Page 5\u27e9" in annotated
        assert "\u27e8Page 6\u27e9" in annotated
        assert "\u27e8Page 11\u27e9" not in annotated
        assert "\u27e8Page 12\u27e9" not in annotated

    def test_marker_physical_when_printed_is_zero(self):
        """Regression for the `printed or physical` falsy-zero bug: printed 0
        used to silently become physical 1 while sibling offsets stayed in
        printed space, labelling two different pages as the same number."""
        offsets = [
            {"page": 1, "start_char": 0, "end_char": 10, "printed_page_num": 0},
            {"page": 2, "start_char": 11, "end_char": 20, "printed_page_num": 1},
        ]
        annotated = _annotate_pages("aaaaaaaaaa bbbbbbbbbb", offsets)
        assert annotated.index("\u27e8Page 1\u27e9") < annotated.index("\u27e8Page 2\u27e9")

    def test_annotate_slices_are_end_exclusive(self):
        """Offsets are half-open ranges: text[start:end] with end EXCLUSIVE —
        the character AT end_char belongs to the next page's slice."""
        offsets = [
            {"page": 5, "start_char": 0, "end_char": 10},
            {"page": 6, "start_char": 10, "end_char": 11},
        ]
        annotated = _annotate_pages("aaaaaaaaaab", offsets)
        # first slice covers chars [0,10): ten 'a', and must NOT contain the
        # trailing 'b' (char index 10 = start of page 6's range)
        assert annotated == "\u27e8Page 5\u27e9 aaaaaaaaaa \u27e8Page 6\u27e9 b"
        assert "aaaaaaaaaab" not in annotated

    def test_format_passages_source_line_physical(self):
        p = {
            "book_name": "كتاب التجربة",
            "author": "",
            "text": "aaaaaaaaaa bbbbbbbbbb",
            "page_start": 5,
            "page_end": 6,
            "printed_page_start": 11,
            "printed_page_end": 12,
            "chapter": None,
            "page_offsets": [
                {"page": 5, "start_char": 0, "end_char": 10, "printed_page_num": 11},
                {"page": 6, "start_char": 11, "end_char": 20, "printed_page_num": 12},
            ],
        }
        formatted = _format_passages([p])
        assert "Page 5-6" in formatted
        assert "Page 11" not in formatted
        assert "\u27e8Page 5\u27e9" in formatted

    def test_format_passages_physical_fallback(self):
        p = {
            "book_name": "كتاب التجربة",
            "author": "",
            "text": "نص قصير",
            "page_start": 3,
            "page_end": 3,
            "printed_page_start": None,
            "printed_page_end": None,
            "chapter": None,
            "page_offsets": [{"page": 3, "start_char": 0, "end_char": 10}],
        }
        assert "Page 3" in _format_passages([p])


class TestDensePage:
    def test_dense_page_wins_over_last_token(self):
        """Tokens mostly on page 6; the trailing token sits on page 7. The
        dense page (6) must win — the old end-anchor returned 7."""
        annotated = (
            "\u27e8Page 5\u27e9 a b \u27e8Page 6\u27e9 abu hurayrah the people "
            "\u27e8Page 7\u27e9 people"
        )
        matched = [
            {"claim": 0, "exact": True, "start": annotated.index("abu")},
            {"claim": 1, "exact": True, "start": annotated.index("hurayrah")},
            {"claim": 2, "exact": True, "start": annotated.index("the")},
            {"claim": 3, "exact": True, "start": annotated.index("people")},
            {"claim": 4, "exact": True, "start": annotated.rindex("people")},
        ]
        assert _dense_page(annotated, matched) == 6

    def test_dense_page_single_page(self):
        annotated = "\u27e8Page 5\u27e9 one two"
        matched = [
            {"claim": 0, "exact": True, "start": annotated.index("one")},
            {"claim": 1, "exact": True, "start": annotated.index("two")},
        ]
        assert _dense_page(annotated, matched) == 5

    def test_dense_page_claim_crossing_boundary_uses_page_local_lcs(self):
        """Regression: the global LCS matched 'am the brick and' on page 12
        but collected 'am the seal of the prophets' from page 13 (the same
        sentence tail repeated on the next page); the weighted-count rule
        then picked 13. With the full claim available, page-local LCS must
        choose page 12 whose own text holds the entire claim."""
        page12 = "am the brick and I am the seal of the prophets"
        page13 = "am the seal of the prophets"
        annotated = f"\u27e8Page 12\u27e9 {page12} \u27e8Page 13\u27e9 {page13}"
        o12 = annotated.index(page12)
        o13 = annotated.index(page13, annotated.index("\u27e8Page 13\u27e9"))
        matched = [
            {"claim": 0, "exact": True, "start": o12 + page12.index("am")},
            {"claim": 1, "exact": True, "start": o12 + page12.index("the")},
            {"claim": 2, "exact": True, "start": o12 + page12.index("brick")},
            {"claim": 3, "exact": True, "start": o12 + page12.index("and")},
            {"claim": 4, "exact": True, "start": o13 + page13.index("am")},
            {"claim": 5, "exact": True, "start": o13 + page13.index("the")},
            {"claim": 6, "exact": True, "start": o13 + page13.index("seal")},
            {"claim": 7, "exact": True, "start": o13 + page13.index("of")},
            {"claim": 8, "exact": True, "start": o13 + page13.rindex("the")},
            {"claim": 9, "exact": True, "start": o13 + page13.index("prophets")},
        ]
        claim = ["am", "the", "brick", "and", "am", "the", "seal", "of", "the", "prophets"]
        # without the claim list the weighted-count rule still prefers page 13
        assert _dense_page(annotated, matched) == 13
        assert _dense_page(annotated, matched, claim_tokens=claim) == 12


class TestClaimPageScoring:
    """Regression: the (1 anchor, 12 exact) passage must NOT outrank the
    (0 anchors, 17 exact) passage — score by matched count first."""

    def _passages(self):
        cited_text = (
            "www first and the prophecy said verily i am the seal of the "
            "prophets before allah in the mother of the book and adam is put "
            "down on the ground in his clay"
        )
        wrong_text = (
            "www page two the god who sent down the book for the guidance and "
            "the prophet said the noble book and the mother of a boy"
        )
        msg = [
            {
                "book_name": "How to Love Prophet",
                "author": "",
                "text": wrong_text,
                "page_start": 1,
                "page_end": 2,
                "chapter": None,
                "page_offsets": [
                    {"page": 1, "start_char": 0, "end_char": len(wrong_text), "printed_page_num": 7},
                ],
            },
            {
                "book_name": "How to Love Prophet",
                "author": "",
                "text": cited_text,
                "page_start": 5,
                "page_end": 7,
                "chapter": None,
                "page_offsets": [
                    {"page": 5, "start_char": 0, "end_char": len(cited_text), "printed_page_num": 11},
                ],
            },
        ]
        return msg

    def test_long_exact_match_beats_single_anchor(self):
        passages = self._passages()
        cited_idx = 1  # [P2] — the richer matching chunk
        tokens = _claim_tokens(
            "additionally the prophet says verily i am the seal of the prophets "
            "before allah in the mother of the book and adam is put down"
        )
        page = _claim_page(passages, cited_idx, "5", tokens)
        # PHYSICAL space: the long claim lives on the chunk's start page 5
        assert page == 5

    def test_tiny_generic_claim_cannot_hijack_page(self):
        """A 2-token generic lead-in ("according to") that also occurs in an
        unrelated chunk must not drag the citation away; resolution falls back
        to the cited chunk's own (physical) start page."""
        cited_text = "the scholar judge iyad mentioned in his book the rules of belief"
        other_text = "and according to the scholars the prophets before allah"
        cited = {
            "book_name": "How to Love Prophet",
            "author": "",
            "text": cited_text,
            "page_start": 2,
            "page_end": 3,
            "chapter": None,
            "page_offsets": [
                {"page": 2, "start_char": 0, "end_char": len(cited_text), "printed_page_num": 8},
            ],
        }
        other = {
            "book_name": "How to Love Prophet",
            "author": "",
            "text": other_text,
            "page_start": 7,
            "page_end": 8,
            "chapter": None,
            "page_offsets": [
                {"page": 7, "start_char": 0, "end_char": len(other_text), "printed_page_num": 14},
            ],
        }
        # the model tagged [P1, Page 14]; the claim "according to" is generic
        assert _claim_page([cited, other], 0, "14", ["according", "to"]) == 2

    def test_citation_resolution_uses_physical_page(self):
        """[P1, Page N] tags resolve to PHYSICAL pages even when printed
        numbers exist on the payload — citations must match the rendered
        page the viewer shows."""
        text = "www one narration is from the prophet says the people of the book page seven end"
        p = {
            "book_name": "How to Love Prophet",
            "author": "",
            "text": text,
            "page_start": 5,
            "page_end": 8,
            "printed_page_start": 12,
            "printed_page_end": 15,
            "chapter": None,
            "page_offsets": [
                {"page": 5, "start_char": 0, "end_char": 21, "printed_page_num": 12},
                {"page": 6, "start_char": 21, "end_char": len(text), "printed_page_num": 13},
            ],
        }
        # claim text "one narration is from" lives in the page-5 slice
        resolved = _resolve_citations(
            "هذا القول ورد [P1, Page 5]. واحد آخر [P1, Page 6].",
            [p],
        )
        assert "[How to Love Prophet, Page 5]" in resolved
        assert "[How to Love Prophet, Page 6]" in resolved


class TestCitationQuestion:
    @pytest.mark.parametrize(
        "q",
        [
            "What is the source reference for Ibn Hajar's comment on the Qiblah?",
            "Who reported the hadith about the brick?",
            "ما هو المصدر لتعليق ابن حجر؟",
            "Who narrated this statement from Abu Hurayrah?",
        ],
    )
    def test_citation_intent_detected(self, q):
        assert _is_citation_question(q)

    @pytest.mark.parametrize(
        "q",
        [
            "Which pizza topping is the most popular in Naples?",
            "أين تقع مكة؟",
            "What is the meaning of the seal of the Prophets?",
        ],
    )
    def test_non_citation_not_detected(self, q):
        assert not _is_citation_question(q)
