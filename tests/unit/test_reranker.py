"""Unit tests for reranker confidence scores (Bug 6: confidence threshold).

The confidence threshold (RAG_MIN_CONFIDENCE_SCORE=0.30) is only meaningful
if the overlap component of the rerank score behaves: an unrelated English
question must score well below 0.30 (otherwise the quality gate never
refuses), while the real Ash-Shifa' query must score well above it.
"""
import pytest
from app.rag.reranker import _query_terms, _term_overlap, normalise_transliteration, rerank

ASH_SHIFA_QUESTION = (
    "What did the author of Ash-Shifa' say about the order of honoring vs. "
    "blaming the Prophet (pbuh)?"
)

ASH_SHIFA_CHUNK = (
    "(Al-Hijr: Before blaming him, He mentioned first His forgiving him: "
    "May Allah forgive you (O Muhammad). Why did you grant them leave...? "
    "(At-Tawbah: The author of Ash-Shifa' said, \"Allah began with honoring "
    "before blaming and made him feel at ease by mentioning forgiveness "
    "before mentioning the sin - if he originally committed a sin.\""
    "(These are only some examples from among many others in this respect. "
    "Another fact that reflects the Prophet's superiority and honor is that "
    "he is referred to with many names and titles in the Qur'an and that "
    "Allah has especially honored him with part of the meanings of His "
    "Fairest Names. (pbuh)"
)

PIZZA_QUESTION = "Which pizza topping is the most popular in Naples?"

BRICK_CHUNK = (
    "\"The people would go around it and say, 'May the brick be put?' I am "
    "the brick and I am the seal of the Prophets.\"( Abu Hurayrah (may Allah "
    "be pleased with him) reported that the Prophet (pbuh) said, \"There is "
    "no Prophet but was given proofs with the like of which people would "
    "believe, but what I have been given is a revelation from Allah."
)

ARABIC_QUESTION = "أين تقع مكة؟"
ARABIC_CHUNK = "مكة المكرمة هي مدينة مقدسة عند المسلمين، وهي قبلة الصلاة."

MACRON_QUESTION = (
    "According to Wathilah ibn al-Asqaʿ's report, whom did Allah choose from "
    "among Kinānah, and whom did He choose from Quraysh?"
)
MACRON_CHUNK = (
    "Wathilah ibn al-Asqa reported that Allah chose the Prophet from Banu "
    "Hashim, chose Banu Hashim from Quraysh, and chose Quraysh from Kinanah."
)


class TestTransliterationNormalisation:
    def test_macron_stripped(self):
        from app.rag.reranker import normalise_transliteration

        assert normalise_transliteration("Kinānah") == "Kinanah"
        assert normalise_transliteration("al-Asqaʿ") == "al-Asqa"

    def test_macron_question_overlaps_ascii_chunk(self):
        assert _term_overlap(MACRON_QUESTION, MACRON_CHUNK) > 0.5


class TestQueryTerms:
    def test_english_stopwords_dropped(self):
        terms = _query_terms(PIZZA_QUESTION)
        assert terms == ["pizza", "topping", "popular", "naples"]

    def test_short_noise_dropped(self):
        terms = _query_terms("vs pp 3 in the")
        assert terms == []

    def test_arabic_terms_kept_short(self):
        # note: "ة" normalises to "ه" so the term is "مكه"
        terms = _query_terms(ARABIC_QUESTION)
        assert "مكه" in terms
        assert "تقع" in terms

    def test_arabic_stopwords_dropped(self):
        terms = _query_terms("ما هي")
        assert terms == []


class TestTermOverlap:
    def test_pizza_vs_brick_chunk_low(self):
        """An unrelated question must score low so the 0.30 quality gate
        can refuse it instead of letting the LLM invent an answer."""
        overlap = _term_overlap(PIZZA_QUESTION, BRICK_CHUNK)
        assert overlap < 0.3

    def test_ash_shifa_question_high_overlap(self):
        overlap = _term_overlap(ASH_SHIFA_QUESTION, ASH_SHIFA_CHUNK)
        assert overlap > 0.5

    def test_arabic_question_matches_arabic_chunk(self):
        overlap = _term_overlap(ARABIC_QUESTION, ARABIC_CHUNK)
        assert overlap > 0.3


class TestRerankThreshold:
    def test_pizza_rerank_dropped_by_floor(self):
        """An unrelated question must be dropped by the relevance floor, so
        the quality gate refuses instead of letting the LLM invent an answer."""
        res = [
            {"text": BRICK_CHUNK, "score": 0.8},
            {"text": ASH_SHIFA_CHUNK, "score": 0.6},
        ]
        rr = __import__("asyncio").run(rerank(PIZZA_QUESTION, res, top_k=2))
        assert rr == []

    def test_ash_shifa_rerank_above_threshold(self):
        res = [
            {"text": BRICK_CHUNK, "score": 0.8},
            {"text": ASH_SHIFA_CHUNK, "score": 0.6},
        ]
        rr = __import__("asyncio").run(
            rerank(ASH_SHIFA_QUESTION, res, top_k=2)
        )
        assert rr[0]["score"] >= 0.30
        # the chunk containing the claim ranks first
        assert ASH_SHIFA_CHUNK in rr[0]["text"]


class TestOverlapChunkSuppression:
    """Chunk windows overlap by design (~15%), so a quote near a chunk
    boundary appears verbatim in two consecutive chunks; the reranker must
    drop the lower-ranked duplicate so the answer cites one page only."""

    TAIL_CHUNK = (
        "Another fact referring to the Prophet's superiority and honor with "
        "his Lord is that He used to give him without asking. "
        "(Al-Hijr: Verily, by thy life (O Prophet), in their wild "
        "intoxication, they wander in distraction, to and fro. "
        "(Al-Hijr: Before blaming him, He mentioned first His forgiving him: "
        "May Allah forgive you (O Muhammad). Why did you grant them leave...? "
        "(At-Tawbah: The author of Ash-Shifa' said, \"Allah began with honoring "
        "before blaming and made him feel at ease by mentioning forgiveness "
        "before mentioning the sin - if he originally committed a sin.\""
        "(These are only some examples from among many others in this respect."
    )

    HEAD_CHUNK = (
        "(Al-Hijr: Before blaming him, He mentioned first His forgiving him: "
        "May Allah forgive you (O Muhammad). Why did you grant them leave...? "
        "(At-Tawbah: The author of Ash-Shifa' said, \"Allah began with honoring "
        "before blaming and made him feel at ease by mentioning forgiveness "
        "before mentioning the sin - if he originally committed a sin.\""
        "(These are only some examples from among many others in this respect. "
        "Another fact that reflects the Prophet's superiority and honor is "
        "that he is referred to with many names and titles in the Qur'an and "
        "that Allah has especially honored him."
    )

    def test_lower_scored_overlapping_chunk_suppressed(self):
        from app.rag.reranker import _suppress_overlap_chunks

        ranked = [
            {
                "book_id": "book-1",
                "page_start": 2,
                "page_end": 3,
                "text": self.HEAD_CHUNK,
                "score": 0.9,
            },
            {
                "book_id": "book-1",
                "page_start": 1,
                "page_end": 2,
                "text": self.TAIL_CHUNK,
                "score": 0.5,
            },
        ]
        out = _suppress_overlap_chunks(ranked)
        assert len(out) == 1
        assert out[0]["page_start"] == 2

    def test_different_books_never_suppressed(self):
        from app.rag.reranker import _suppress_overlap_chunks

        ranked = [
            {
                "book_id": "book-1",
                "page_start": 2,
                "page_end": 3,
                "text": self.HEAD_CHUNK,
                "score": 0.9,
            },
            {
                "book_id": "book-2",
                "page_start": 1,
                "page_end": 2,
                "text": self.TAIL_CHUNK,
                "score": 0.5,
            },
        ]
        out = _suppress_overlap_chunks(ranked)
        assert len(out) == 2

    def test_non_overlapping_pages_kept(self):
        from app.rag.reranker import _suppress_overlap_chunks

        ranked = [
            {
                "book_id": "book-1",
                "page_start": 5,
                "page_end": 6,
                "text": self.HEAD_CHUNK,
                "score": 0.9,
            },
            {
                "book_id": "book-1",
                "page_start": 1,
                "page_end": 2,
                "text": self.TAIL_CHUNK,
                "score": 0.5,
            },
        ]
        out = _suppress_overlap_chunks(ranked)
        assert len(out) == 2
