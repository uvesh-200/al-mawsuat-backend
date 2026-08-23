"""Unit tests: per-page bboxes for multi-page chunks (Bug 1 regression guard).

A single min/max union of word bboxes across pages produces coordinates that
exist on no page; the chunk must store one bbox per page (page_bboxes), with
the top-level ``bbox`` expressed in page_start's coordinate space.
"""
from app.pipeline.chunker import chunk


def _word(text: str, page_num: int, bbox: list[float]) -> dict:
    return {
        "text": text,
        "page_num": page_num,
        "physical_page": page_num,
        "bbox": bbox,
    }


def _page(page_num: int, words: list[dict]) -> dict:
    return {"page_num": page_num, "physical_page": page_num, "words": words}


def _make_words(page_num: int, n: int, x0: float, y0: float) -> list[dict]:
    out = []
    for i in range(n):
        out.append(_word(f"كلمة{i}", page_num, [x0, y0 + i * 10, x0 + 60, y0 + i * 10 + 8]))
    return out


class TestPageBBoxes:
    def test_multi_page_chunk_has_per_page_bboxes(self):
        p1 = _page(1, _make_words(1, 10, 10.0, 20.0))
        p2 = _page(2, _make_words(2, 12, 30.0, 40.0))
        chunks = chunk([p1, p2], "test-tenant")
        for c in chunks:
            if c["page_end"] == c["page_start"]:
                continue  # only inspect chunks that actually span pages
            pages = {pb["page"] for pb in c["page_bboxes"]}
            assert pages == {c["page_start"], c["page_end"]}, "one bbox per spanned page"
            for pb in c["page_bboxes"]:
                b = pb["bbox"]
                assert len(b) == 4
                # every per-page box must be a valid rectangle in page-local space
                assert b[0] < b[2] and b[1] < b[3]
                assert b[0] >= 0 and b[1] >= 0
                assert b[2] <= 1000 and b[3] <= 1000, f"box {b} outside any real page"
                # the box for a page must only span that page's own y range
                if pb["page"] == 1:
                    assert b[1] >= 20.0 and b[3] <= 120.0
                if pb["page"] == 2:
                    assert b[1] >= 40.0 and b[3] <= 160.0

    def test_multi_page_bbox_is_page_start_box(self):
        """Top-level bbox must be the first page's box, not a cross-page union."""
        p1 = _page(1, _make_words(1, 5, 0.0, 0.0))
        p2 = _page(2, _make_words(2, 5, 8000.0, 9000.0))  # fake far-away coordinates
        chunks = chunk([p1, p2], "test-tenant")
        multi = [c for c in chunks if c["page_end"] > c["page_start"]]
        assert multi, "expected at least one cross-page chunk"
        for c in multi:
            assert c["bbox"][2] < 1000 and c["bbox"][3] < 1000, (
                f"bbox {c['bbox']} must stay in page_start's coordinate space"
            )

    def test_single_page_chunk_bbox_unchanged(self):
        p1 = _page(1, _make_words(1, 10, 5.0, 7.0))
        chunks = chunk([p1], "test-tenant")
        assert len(chunks) == 1
        c = chunks[0]
        # word i box: [5, 7+10i, 65, 7+10i+8] -> union y1 = 7+90+8 = 105
        assert c["bbox"] == [5.0, 7.0, 65.0, 105.0]
        assert c["page_bboxes"] == [{"page": 1, "bbox": [5.0, 7.0, 65.0, 105.0]}]
        # end_char is EXCLUSIVE (slice semantics shared with the JS viewer)
        assert c["page_offsets"] == [{"page": 1, "start_char": 0, "end_char": len(c["text"]), "printed_page_num": None}]

    def test_page_offsets_tile_the_chunk_text(self):
        p1 = _page(1, _make_words(1, 3, 0.0, 0.0))
        p2 = _page(2, _make_words(2, 3, 0.0, 0.0))
        chunks = chunk([p1, p2], "test-tenant")
        multi = [c for c in chunks if c["page_end"] > c["page_start"]]
        assert multi
        c = multi[0]
        offsets = c["page_offsets"]
        assert [o["page"] for o in offsets] == [c["page_start"], c["page_end"]]
        # segments are contiguous half-open ranges tiling the text exactly once
        assert offsets[0]["start_char"] == 0
        assert offsets[-1]["end_char"] == len(c["text"])
        for a, b in zip(offsets, offsets[1:]):
            assert b["start_char"] == a["end_char"]
        # reconstruct the text from the segments — must equal the chunk text
        joined = "".join(
            c["text"][o["start_char"] : o["end_char"]] for o in offsets
        )
        assert joined == c["text"]

    def test_synthetic_geometry_words_excluded_from_boxes(self):
        """Gemini text-only OCR words carry char-width-heuristic boxes tagged
        geom="synthetic". They support line/paragraph grouping but must never
        become highlightable geometry — a page whose only words are synthetic
        gets NO box (bbox None / empty page_bboxes) instead of a fictional
        rectangle that renders as a misplaced band."""
        real = _page(1, _make_words(1, 3, 10.0, 20.0))
        synth_page = {
            "page_num": 2,
            "physical_page": 2,
            "words": [
                {"text": f"كلمة{i}", "page_num": 2, "physical_page": 2,
                 "bbox": [i * 15.0, 0.0, i * 15.0 + 120.0, 18.0],
                 "geom": "synthetic"}
                for i in range(4)
            ],
        }
        chunks = chunk([real, synth_page], "test-tenant")
        for c in chunks:
            pages = {pb["page"] for pb in c["page_bboxes"]}
            assert 2 not in pages, "synthetic page must not produce a bbox"
            if c["page_start"] == 2 and c["page_end"] == 2:
                assert c["bbox"] is None
                assert c["page_bboxes"] == []

    def test_mixed_real_and_synthetic_pages_keep_real_box(self):
        """A chunk spanning one real-geometry page and one synthetic-only page
        keeps the real page's box; the synthetic page contributes nothing."""
        real = _page(1, _make_words(1, 3, 10.0, 20.0))
        synth_only = {
            "page_num": 2,
            "physical_page": 2,
            "words": [
                {"text": f"w{i}", "page_num": 2, "physical_page": 2,
                 "bbox": [float(i * 100), 0.0, float(i * 100 + 80), 18.0],
                 "geom": "synthetic"}
                for i in range(30)
            ],
        }
        chunks = chunk([real, synth_only], "test-tenant")
        boxes = [pb for c in chunks for pb in c["page_bboxes"]]
        assert all(pb["page"] == 1 for pb in boxes)
        for pb in boxes:
            assert pb["bbox"][2] < 1000, "real-page box must stay in page space"
