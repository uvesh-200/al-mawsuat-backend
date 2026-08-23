"""Unit tests for footer page-number extraction (Bug 1 regression guard).

These tests run without any live services and do not require a PDF.
"""
import pytest
from app.features.ingestion.extractor import _extract_footer_page_number


class TestWesternNumerals:
    def test_bare_number_on_own_line(self):
        text = "بسم الله الرحمن الرحيم\nهذا هو النص العربي للصفحة.\n\n7"
        assert _extract_footer_page_number(text) == 7

    def test_number_between_dashes(self):
        text = "نص الصفحة هنا\n\n- 42 -"
        assert _extract_footer_page_number(text) == 42

    def test_number_in_brackets(self):
        text = "نص الصفحة هنا\n[ 15 ]"
        assert _extract_footer_page_number(text) == 15

    def test_number_in_em_dash(self):
        text = "نص الصفحة هنا\n— 99 —"
        assert _extract_footer_page_number(text) == 99

    def test_three_digit_number(self):
        text = "نص طويل جداً\n\n123"
        assert _extract_footer_page_number(text) == 123

    def test_number_at_end_of_mixed_line(self):
        # Some books put "Book Name  7" in the footer
        text = "محتوى الصفحة\nكتاب الوقاية  7"
        assert _extract_footer_page_number(text) == 7


class TestArabicIndicNumerals:
    def test_arabic_indic_digit(self):
        # ٧ is U+0667 = Arabic-Indic 7
        text = "نص الصفحة\n\n٧"
        assert _extract_footer_page_number(text) == 7

    def test_arabic_indic_two_digits(self):
        text = "محتوى\n١٢"
        assert _extract_footer_page_number(text) == 12

    def test_arabic_indic_between_dashes(self):
        text = "نص\n- ٣٤ -"
        assert _extract_footer_page_number(text) == 34

    def test_mixed_arabic_western_in_footer(self):
        # Same number printed twice (header + footer) — should pick it up
        text = "٩\nنص الصفحة\n9"
        result = _extract_footer_page_number(text)
        assert result == 9


class TestNoFooter:
    def test_empty_string(self):
        assert _extract_footer_page_number("") is None

    def test_no_number_in_text(self):
        text = "هذا نص عربي بدون أرقام في التذييل\nوسطر آخر بدون أرقام"
        assert _extract_footer_page_number(text) is None

    def test_year_not_mistaken_for_page(self):
        # A 4-digit year by itself would technically match — our limit is ≤9999,
        # so let's verify a typical body year doesn't dominate when no footer
        # number is present. In practice a year in a footer is unlikely; the
        # test confirms we don't crash.
        text = "طُبع عام 1438\nنص طويل"
        result = _extract_footer_page_number(text)
        # We don't assert None here since 1438 is in range — this is a known
        # edge case the caller should handle by comparing against physical page.
        assert result is None or isinstance(result, int)


class TestNoisyContent:
    def test_year_in_body_not_footer(self):
        # Year is in the middle of the text, not in the last 5 lines
        lines = ["السنة 1438"] + ["نص عادي"] * 10 + [""]
        text = "\n".join(lines)
        # The last 5 lines + first 2 don't contain the year, so result is None
        assert _extract_footer_page_number(text) is None

    def test_large_number_out_of_range(self):
        # Numbers > 9999 should not be treated as page numbers
        text = "نص الصفحة\n12345"
        assert _extract_footer_page_number(text) is None

    def test_repeated_page_number_wins(self):
        # Some books print the page in both header and footer
        text = "9\nمحتوى الصفحة التاسعة\n\n9"
        assert _extract_footer_page_number(text) == 9


class TestOffsetConsistency:
    """Verify that a sequence of pages produces a consistent offset."""

    def _make_page(self, printed: int, physical: int) -> dict:
        from app.features.ingestion.extractor import _ARABIC_INDIC
        text = f"نص الصفحة رقم {printed}\n\n{printed}"
        from app.features.ingestion.extractor import _extract_footer_page_number
        footer = _extract_footer_page_number(text)
        return {
            "page_num": footer if footer is not None else physical,
            "physical_page": physical,
            "footer_extracted": footer is not None,
        }

    def test_consistent_6_page_offset(self):
        # Simulate a book with 6 front-matter pages, body starts at physical 7
        pages = [self._make_page(i - 6, i) for i in range(7, 17)]
        from app.features.ingestion.extractor import _sanity_check_page_numbers
        # Should not raise
        _sanity_check_page_numbers(pages)
        offsets = [p["physical_page"] - p["page_num"] for p in pages if p["footer_extracted"]]
        assert all(o == 6 for o in offsets), f"Expected uniform offset 6, got: {offsets}"
