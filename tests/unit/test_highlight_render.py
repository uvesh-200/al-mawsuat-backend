"""Unit tests: render_highlight modes and cache-safety metadata (Phase 0).

The endpoint must know whether a render actually drew a highlight:
- "bbox": stored rectangle accepted (contained in the page) and drawn;
- "none": bbox rejected (outside page rect — synthetic OCR geometry) with no
  usable snippet; this render must NOT be cached under the request's key.
"""
import fitz

from app.features.highlights.router import render_highlight

W, H = 595.0, 842.0


def _page_with_text(text: str = "alpha beta gamma delta epsilon") -> fitz.Page:
    doc = fitz.open()
    page = doc.new_page(width=W, height=H)
    page.insert_text((72, 100), text, fontsize=12)
    return page


class TestRenderHighlight:
    def test_contained_bbox_drawn(self):
        page = _page_with_text()
        png, meta = render_highlight(page, [70, 90, 200, 110], None, rtl=False)
        assert meta["mode"] == "bbox"
        assert meta["rect"] == [70.0, 90.0, 200.0, 110.0]
        assert png.startswith(b"\x89PNG")

    def test_out_of_page_bbox_without_snippet_is_none(self):
        """Synthetic-style box far outside the page: nothing drawn, meta says
        "none" so the endpoint skips caching instead of poisoning the key."""
        page = _page_with_text()
        _, meta = render_highlight(page, [0.0, 0.0, 5000.0, 18.0], None, rtl=False)
        assert meta["mode"] == "none"
        assert "reason" in meta

    def test_no_request_is_full(self):
        page = _page_with_text()
        _, meta = render_highlight(page, None, None, rtl=False)
        assert meta["mode"] == "full"

    def test_zero_area_bbox_is_rejected(self):
        page = _page_with_text()
        _, meta = render_highlight(page, [10.0, 10.0, 10.0, 50.0], None, rtl=False)
        assert meta["mode"] == "none"
