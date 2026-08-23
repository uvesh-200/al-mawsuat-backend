"""Unit tests: deterministic chunk IDs across OCR diacritic variance (Bug 4
regression guard).

The chunk ID is a uuid5 over the *normalised* chunk text + page span, so
reprocessing a page whose OCR output differs only in tashkeel/tatweel/hamza
spelling mints the same ID and upserts instead of duplicating vectors.
"""
from app.features.ingestion.indexer import _make_chunk_id, _normalise_for_id


def _chunk(text: str, page_start: int = 1, page_end: int = 1) -> dict:
    return {"text": text, "page_start": page_start, "page_end": page_end}


class TestChunkIdDeterminism:
    def test_identical_text_identical_id(self):
        a = _make_chunk_id(_chunk("هذا نص عربي"))
        b = _make_chunk_id(_chunk("هذا نص عربي"))
        assert a == b

    def test_diacritic_variance_same_id(self):
        raw = "الْمَوْسُوعَةُ الْفِقْهِيَّةُ"
        plain = "الموسوعة الفقهية"
        assert _make_chunk_id(_chunk(raw)) == _make_chunk_id(_chunk(plain))

    def test_tatweel_variance_same_id(self):
        a = _make_chunk_id(_chunk("كتابِـــهِ وَسُنَّتِهِ"))
        b = _make_chunk_id(_chunk("كتابه وسنته"))
        assert a == b

    def test_hamza_alef_form_variance_same_id(self):
        a = _make_chunk_id(_chunk("أحمد إبراهيم آدم"))
        b = _make_chunk_id(_chunk("احمد ابراهيم ادم"))
        assert a == b

    def test_latin_macron_variance_same_id(self):
        a = _make_chunk_id(_chunk("Kinānah al-Asqaʿ"))
        b = _make_chunk_id(_chunk("Kinanah al-Asqa"))
        assert a == b

    def test_page_span_participates_in_id(self):
        a = _make_chunk_id(_chunk("نص متطابق", page_start=1))
        b = _make_chunk_id(_chunk("نص متطابق", page_start=2))
        assert a != b

    def test_normalise_keeps_real_words(self):
        norm = _normalise_for_id("الموسوعة الفقهية الكويتية")
        # ta-marbuta merges into ha so OCR variance can't change the ID
        assert norm == "الموسوعه الفقهيه الكويتيه"
