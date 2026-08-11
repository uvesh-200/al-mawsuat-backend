"""Unit tests: keyword leg queries in the indexed (original) language (Bug 2
regression guard).

Previously the Meilisearch keyword query was built only from the English-
translated leg, guaranteeing 0 hits against non-English (e.g. Arabic) indexed
text. The query must combine the original-language terms with the English
terms.
"""
from app.rag.agent import _build_keyword_query, _build_keyword_queries


class TestBuildKeywordQuery:
    def test_arabic_question_keeps_arabic_terms(self):
        # original-language (Arabic) query + its English translation
        normalised = "ما حكم الربا في المذهب الحنفي؟"
        english = "What is the ruling on riba in the Hanafi school?"
        q = _build_keyword_query(normalised, english, "ignored")
        assert "حكم" in q.split() or "الربا" in q.split() or "المذهب" in q.split()
        # Arabic terms must not have been dropped by the English-only path
        assert any(_is_arabic(t) for t in q.split()), f"keyword query lost Arabic terms: {q!r}"
        # English terms are also present
        assert any(t in q.split() for t in ("ruling", "riba", "hanafi", "school"))

    def test_english_question_still_works(self):
        q = _build_keyword_query(
            "What is the ruling on riba?", "What is the ruling on riba?", "ignored"
        )
        assert "riba" in q.split()
        assert "ruling" in q.split()

    def test_terms_deduplicated(self):
        normalised = "riba riba ruling"
        english = "riba ruling"
        q = _build_keyword_query(normalised, english, "ignored")
        terms = q.split()
        assert len(terms) == len(set(terms))

    def test_falls_back_when_no_terms(self):
        assert _build_keyword_query("", "", "الاستفسار عن الحكم") == "الاستفسار عن الحكم"


class TestBuildKeywordQueries:
    """Split per-script legs so Meilisearch's AND-over-matched-words behaviour
    cannot zero the whole keyword leg (see keyword_search's alt_query)."""

    def test_arabic_question_splits_into_two_legs(self):
        normalised = "من هو صدر الشريعة؟"
        english = "Who is Sadr al-Sharia?"
        primary, secondary = _build_keyword_queries(normalised, english, "ignored")
        # primary = original-language terms only
        assert "صدر" in primary.split() or "الشريعه" in primary.split()
        assert not any(_is_arabic(t) for t in secondary.split())
        # secondary = English terms only
        assert any(t in secondary.split() for t in ("sadr", "al", "sharia"))

    def test_english_question_primary_leg_has_no_secondary(self):
        english = "What is the ruling on riba?"
        primary, secondary = _build_keyword_queries(english, english, "ignored")
        assert "riba" in primary.split()
        assert secondary is None

    def test_secondary_dropped_when_identical_to_primary(self):
        primary, secondary = _build_keyword_queries("riba ruling", "riba ruling", "ignored")
        assert primary == "riba ruling"
        assert secondary is None

    def test_primary_falls_back_to_english_when_no_original_terms(self):
        primary, secondary = _build_keyword_queries("???", "Who is Sadr al-Sharia?", "x")
        assert "sadr" in primary.split() or "sharia" in primary.split()


def _is_arabic(token: str) -> bool:
    return any("\u0600" <= ch <= "\u06FF" for ch in token)
