"""Phase 0 evidence harness: dump bbox coordinates at every pipeline stage and
render the exact image KitabViewer would display, through the real endpoint.

Stages dumped per case:
  A extract   — extractor word bboxes per page
  B chunk     — target chunk geometry (bbox / page_bboxes / page_offsets)
  C payload   — dict exactly as Qdrant/Meilisearch store it
  D source    — ask._build_source result + plausibility verdicts + the query
                the frontend builds (mirror of components/KitabViewer.tsx)
  E render    — GET /highlight executed against stubbed DB/MinIO; highlight
                region measured by pixel-diffing against the clean page render

Usage: python scripts/phase0/dump_pipeline.py <output_dir>
Runs offline on host; run inside the fastapi container for Tesseract-backed
locate paths (docker cp this folder in first).
"""
import asyncio
import io
import json
import os
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

# Force the offline engine BEFORE app.config is imported: fixtures are
# text-layer PDFs handled by the fitz/tesseract code paths deterministically.
os.environ["OCR_ENGINE"] = "tesseract"

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import fitz  # noqa: E402

from app.features.qa.router import _build_source, _plausible_bbox  # noqa: E402
from app.features.highlights.router import get_highlight  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.features.ingestion.chunker import chunk  # noqa: E402
from app.features.ingestion.extractor import _text_to_words, extract  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
TENANT = settings.DEFAULT_TENANT_ID
RENDER_DPI = 150.0

GEMINI_PAGES_AR = [
    "قال المصنف في هذا الباب إن النية محل القول في صحة العقود وانعقادها عند من "
    "يعتمد المقاصد ويجعل الظاهر مرجعا إلى باطن في جميع الأبواب المذكورة كتابة.",
    "وذهب أصحابنا إلى أن العبرة في البيوع بالمقاصد والمعاني لا بالألفاظ المرادة "
    "لأن اللفظ لا يقوم مقام الإرادة إذا ثبت خلافه بالقرائن المعتبرة عند المحققين.",
]

# Short OCR lines: their synthetic x-extent stays UNDER the real page width,
# so the unioned chunk box passes the highlight endpoint's containment check
# and gets drawn as a giant band over unrelated content.
GEMINI_PAGES_AR_SHORT = [
    "قال المصنف في هذا الباب",
    "إن النية محل القول في صحة العقود",
    "وانعقادها عند من يعتمد المقاصد",
    "ويجعل الظاهر مرجعا إلى باطن",
]


def load_fixture_pages(name):
    pdf = (FIXTURES / name).read_bytes()

    async def run():
        return await extract(pdf)

    return asyncio.run(run()), pdf


# --------------------------------------------------------------------------
# Frontend query simulation — exact mirror of components/KitabViewer.tsx L53-73
# --------------------------------------------------------------------------

def viewer_request(source: dict, page: int | None):
    s = source
    cur = page if page is not None else (s.get("page") or 1)
    raw_boxes = s.get("page_bboxes")
    boxes = raw_boxes if isinstance(raw_boxes, list) else ([raw_boxes] if raw_boxes else [])
    page_boxes = [b for b in boxes if isinstance(b, dict)
                  and isinstance(b.get("page"), int) and isinstance(b.get("bbox"), list)]
    raw_offs = s.get("page_offsets")
    offs = raw_offs if isinstance(raw_offs, list) else ([raw_offs] if raw_offs else [])
    page_offsets = [o for o in offs if isinstance(o, dict) and isinstance(o.get("page"), int)]
    box = next((b["bbox"] for b in page_boxes if b["page"] == cur), None)
    if box is None and cur == s.get("page"):
        box = s.get("bbox")
    off = next((o for o in page_offsets if o["page"] == cur), None)
    if off is not None and s.get("text"):
        snippet = s["text"][off["start_char"]:off["end_char"]].strip()
    elif cur == s.get("page"):
        snippet = s.get("text")
    else:
        snippet = None
    params = {"book_id": str(s.get("book_id") or ""), "page": str(cur)}
    if box is not None:
        params["bbox"] = ",".join(repr(float(v)) for v in box)
    elif snippet:
        params["text"] = snippet[:600]
    return params, box, snippet


# --------------------------------------------------------------------------
# Stage E: real endpoint against stubbed DB + filesystem MinIO
# --------------------------------------------------------------------------

class _Result:
    def __init__(self, book):
        self._book = book

    def scalar_one_or_none(self):
        return self._book


class _Session:
    def __init__(self, book):
        self._book = book

    async def execute(self, *_a, **_k):
        return _Result(self._book)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _FSStorage:
    def __init__(self, root: Path):
        self.root = root

    async def get_file_safe(self, bucket, path):
        f = self.root / bucket / path
        return f.read_bytes() if f.exists() else None

    async def get_file(self, bucket, path):
        return (self.root / bucket / path).read_bytes()

    async def upload_file(self, bucket, path, data, *_a, **_k):
        f = self.root / bucket / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)


def install_endpoint_stubs(book, storage_root: Path):
    import app.features.highlights.router as H

    H.AsyncSessionLocal = lambda: _Session(book)
    H.storage = _FSStorage(storage_root)


async def render_request(book, params, storage_root: Path, cache_root: Path):
    install_endpoint_stubs(book, storage_root)
    try:
        resp = await get_highlight(
            book_id=params["book_id"], page=int(params["page"]),
            bbox=params.get("bbox"), text=params.get("text"), user=None,
        )
        return {"status": resp.status_code, "png": bytes(resp.body)}
    except Exception as exc:  # noqa: BLE001 — harness records every failure mode
        return {"status": getattr(exc, "status_code", None), "error": repr(exc),
                "png": None}


# --------------------------------------------------------------------------
# Highlight measurement: pixel diff of endpoint render vs clean render
# --------------------------------------------------------------------------

def _rgb_pixmap(png_bytes):
    try:
        pix = fitz.Pixmap(png_bytes)
    except TypeError:  # older PyMuPDF without bytes ctor
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(png_bytes)
            name = tmp.name
        pix = fitz.Pixmap(name)
        os.unlink(name)
    if pix.alpha or pix.colorspace.n != 3:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    return pix


def measure_highlight(before_png, after_png, dpi=RENDER_DPI):
    if before_png is None or after_png is None:
        return {"changed_px": 0, "region_pts": None}
    pa, pb = _rgb_pixmap(after_png), _rgb_pixmap(before_png)
    w, h, sa, sb = pa.width, pb.height and pa.height, pa.samples, pb.samples
    scale = 72.0 / dpi
    minx, miny, maxx, maxy, changed = 10 ** 9, 10 ** 9, -1, -1, 0
    for y in range(0, h, 2):
        ra, rb = y * w * 3, y * w * 3
        for x in range(0, w, 2):
            off = x * 3
            dr = abs(sa[ra + off] - sb[rb + off])
            dg = abs(sa[ra + off + 1] - sb[rb + off + 1])
            db = abs(sa[ra + off + 2] - sb[rb + off + 2])
            if max(dr, dg, db) > 14:
                changed += 1
                minx, miny = min(minx, x), min(miny, y)
                maxx, maxy = max(maxx, x), max(maxy, y)
    region = [round(minx * scale, 1), round(miny * scale, 1),
              round(maxx * scale, 1), round(maxy * scale, 1)] if changed else None
    return {"changed_px": changed * 4, "region_pts": region}


# --------------------------------------------------------------------------
# Citation probe: does claim→page resolution stay inside one number space?
# --------------------------------------------------------------------------

def citation_probe(payload: dict):
    from app.features.qa.agent import (_MARKER_RE, _annotate_pages, _claim_page,
                               _claim_sentence, _claim_tokens, _format_passages)
    passages = [payload]
    markers = [int(m.group(1)) for m in _MARKER_RE.finditer(_format_passages(passages))]
    offsets = payload.get("page_offsets") or []
    if len(offsets) < 2:
        return {"markers": markers, "skipped": "single-page chunk"}
    second = offsets[1]
    slice_text = payload["text"][second["start_char"]: second["end_char"]]
    # Pick a claim sentence UNIQUE to this page's slice: the fixtures reuse
    # sentence templates across pages, and a resolver cannot distinguish
    # identical text on two pages — measuring with a repeated sentence would
    # say nothing about coordinate-space correctness.
    sentences = [s.strip() for s in re.split(r"(?<=[.!؟?۔])\s+", slice_text) if s.strip()]
    full_text = " ".join(payload["text"].split())
    claim = next(
        (s for s in sentences if full_text.count(" ".join(s.split())) == 1),
        sentences[0] if sentences else slice_text[:100],
    )
    answer = f"{claim} [P1]"
    tokens = _claim_tokens(_claim_sentence(answer[: answer.index(" [P1")]))
    resolved = _claim_page(passages, 0, None, tokens)
    expected_markers = [o["page"] for o in offsets]
    return {
        "physical_pages_of_slices": expected_markers,
        "markers_emitted": markers,
        "marker_space_mixed": markers != expected_markers,
        "expected_physical_page": second["page"],
        "printed_of_expected": second.get("printed_page_num"),
        "resolved_citation_page": resolved,
        "resolved_equals_expected_physical": resolved == second["page"],
        "source_item_page_shown_in_ui": payload["page_start"],
        "ui_badge_differs_from_claim_page": payload["page_start"] != second["page"]
        or resolved != second["page"],
    }


# --------------------------------------------------------------------------
# Case runner
# --------------------------------------------------------------------------

def make_payload(chunk_dict: dict, book_id: str) -> dict:
    return {**chunk_dict, "book_id": book_id, "book_name": "Fixture Book",
            "author": "Fixture Author", "language": chunk_dict.get("_lang", "en"),
            "book_type": "treatise", "minio_path": chunk_dict["_pdf_name"],
            "score": 0.9}


def select_chunk(chunks, want_multi: bool):
    for c in chunks:
        spans = c["page_end"] > c["page_start"]
        if spans == want_multi:
            return c
    return chunks[len(chunks) // 2]


def dump_case(case: dict, outdir: Path):
    name = case["name"]
    cdir = outdir / name
    cdir.mkdir(parents=True, exist_ok=True)
    dump = {}

    if case.get("synthetic"):
        gemini_pages = (GEMINI_PAGES_AR_SHORT if case.get("short_lines")
                        else GEMINI_PAGES_AR)
        pages = []
        for i, txt in enumerate(gemini_pages * 2):
            words = _text_to_words(txt, page_num=i + 1, printed_page_num=i + 1)
            pages.append({"physical_page": i + 1, "page_num": i + 1,
                          "printed_page_num": i + 1, "footer_extracted": False,
                          "words": words})
        pdf_name, lang, pdf_bytes = "en_book.pdf", "ar", (FIXTURES / "en_book.pdf").read_bytes()
    else:
        pages, pdf_bytes = load_fixture_pages(case["pdf"])
        pdf_name, lang = case["pdf"], case.get("lang", "en")

    dump["stage_a_words"] = [
        {"physical_page": p["physical_page"], "n_words": len(p["words"]),
         "sample": [{"text": w["text"], "bbox": w.get("bbox")}
                    for w in p["words"][:3]]}
        for p in pages
    ]
    (cdir / "stage_a_words.json").write_text(json.dumps(dump["stage_a_words"], ensure_ascii=False, indent=1), encoding="utf-8")

    chunks = chunk(pages, TENANT)
    target = select_chunk(chunks, case.get("want_multi", False))
    target["_lang"], target["_pdf_name"] = lang, pdf_name
    (cdir / "stage_b_chunk.json").write_text(json.dumps(
        {k: v for k, v in target.items() if k != "_lang"}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    book_id = str(uuid.uuid4())
    payload = make_payload(target, book_id)
    payload.pop("_pdf_name", None)
    (cdir / "stage_c_payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    source = _build_source(dict(payload), rank=1, tenant_id=TENANT).model_dump()
    params, chosen_box, chosen_snippet = viewer_request(source, None)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_rect = list(doc[target["page_start"] - 1].rect)
    doc.close()
    dump["stage_d_source"] = {
        "bbox_after_gating": source["bbox"],
        "top_bbox_plausible": _plausible_bbox(payload["bbox"]) if payload.get("bbox") else None,
        "per_page_plausibility": [
            {"page": b["page"], "bbox": b["bbox"],
             "plausible": _plausible_bbox(b["bbox"]),
             "inside_page_rect": bool(
                 b["bbox"][0] >= 0 and b["bbox"][1] >= 0
                 and b["bbox"][2] <= page_rect[2] + 0.01
                 and b["bbox"][3] <= page_rect[3] + 0.01)}
            for b in (payload.get("page_bboxes") or [])],
        "page_rect": page_rect,
        "viewer_query_params": params,
        "viewer_chosen_box": chosen_box,
        "highlight_url_from_backend": source.get("highlight_url"),
    }
    (cdir / "stage_d_source.json").write_text(json.dumps(dump["stage_d_source"], ensure_ascii=False, indent=1), encoding="utf-8")

    if case.get("probe"):
        (cdir / "citation_probe.json").write_text(
            json.dumps(citation_probe(payload), ensure_ascii=False, indent=1),
            encoding="utf-8")

    storage_root = cdir / "minio"
    (storage_root / "books" / "fixtures").mkdir(parents=True, exist_ok=True)
    (storage_root / "books" / "fixtures" / pdf_name).write_bytes(pdf_bytes)
    cache_root = cdir / "cache"
    book = SimpleNamespace(id=uuid.UUID(book_id), tenant_id=TENANT, total_pages=None,
                           minio_path=f"fixtures/{pdf_name}", language=lang)

    async def stage_e():
        results = {}
        req = dict(params)
        if case.get("force_text_mode"):
            req.pop("bbox", None)
            req["text"] = (target["text"][:400])
        before = fitz.open(stream=pdf_bytes, filetype="pdf")
        clean = before[int(req["page"]) - 1].get_pixmap(dpi=int(RENDER_DPI)).tobytes("png")
        before.close()
        out = await render_request(book, req, storage_root, cache_root)
        m = measure_highlight(clean, out["png"])
        (cdir / "render.png").write_bytes(out["png"]) if out["png"] else None
        (cdir / "clean.png").write_bytes(clean)
        results["request"] = {k: (v[:120] if k == "text" else v) for k, v in req.items()}
        results["endpoint_status"] = out["status"]
        if out.get("error"):
            results["error"] = out["error"]
        results.update(m)
        page_area = page_rect[2] * page_rect[3]
        if m["region_pts"]:
            r = m["region_pts"]
            results["region_area_ratio_of_page"] = round(
                (r[2] - r[0]) * (r[3] - r[1]) / page_area, 3)
        return results

    dump["stage_e_render"] = asyncio.run(stage_e())
    (cdir / "stage_e_render.json").write_text(json.dumps(dump["stage_e_render"], indent=1), encoding="utf-8")
    return {"case": name, **{k: dump[k] for k in ("stage_d_source", "stage_e_render")}}


CASES = [
    {"name": "single_page_en", "pdf": "en_book.pdf", "want_multi": False},
    {"name": "multi_page_en", "pdf": "en_book.pdf", "want_multi": True},
    {"name": "single_page_ar", "pdf": "ar_book.pdf", "lang": "ar", "want_multi": False},
    {"name": "multi_page_ar", "pdf": "ar_book.pdf", "lang": "ar", "want_multi": True},
    {"name": "single_page_ur", "pdf": "ur_book.pdf", "lang": "ur", "want_multi": False},
    {"name": "edge_offset_frontmatter", "pdf": "frontmatter.pdf", "want_multi": True, "probe": True},
    {"name": "scanned_en_text_locate", "pdf": "en_scanned.pdf", "force_text_mode": True},
    {"name": "synthetic_gemini_geometry", "synthetic": True},
    {"name": "synthetic_gemini_shortlines", "synthetic": True, "short_lines": True},
]


def main():
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "refactor-evidence" / "phase0"
    outdir.mkdir(parents=True, exist_ok=True)
    summary = []
    for c in CASES:
        try:
            summary.append(dump_case(c, outdir))
        except Exception as exc:  # noqa: BLE001 — record and continue
            summary.append({"case": c["name"], "harness_error": repr(exc)})
    (outdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {len(summary)} cases to {outdir}")


if __name__ == "__main__":
    main()
