# TASK-07 — PDF Text and BBox Extraction

**Feature:** Extract text and coordinates from uploaded books  
**Repo:** al-mawsuat-backend  
**Week:** 3  
**Depends on:** TASK-06

---

## Description

Write `app/pipeline/extractor.py`. This module receives raw PDF bytes and returns structured text with word-level bounding box coordinates for every page. The bounding boxes are what make the source highlighting feature work — they tell the highlight renderer exactly where on the page to draw the yellow rectangle.

The extractor must handle two types of input:

**Digital PDFs** — PDFs where text is embedded (not scanned). Use PyMuPDF (`fitz`). Call `page.get_text("words")` on each page which returns tuples of `(x0, y0, x1, y1, word, block_no, line_no, word_no)`. Extract the word and its bbox from each tuple.

**Scanned PDFs / image-only PDFs** — PDFs where pages are images with no embedded text. Detect these by checking if PyMuPDF returns fewer than 10 words per page. If so, fall back to Tesseract OCR. Convert each page to a PIL image using PyMuPDF's `get_pixmap()`, then run `pytesseract.image_to_data()` with `lang="ara+urd+eng"` to get words and their bounding boxes.

The output format for both paths must be identical — a list of page objects:

```python
[
  {
    "page_num": 1,
    "words": [
      {
        "text": "الحمد",
        "bbox": [x0, y0, x1, y1]   # pixel coordinates on this page
      },
      ...
    ]
  },
  ...
]
```

The main function signature: `def extract(pdf_bytes: bytes) -> list[dict]`

Do not perform any chunking or embedding in this module — only raw text and coordinate extraction. Keep this module single-responsibility.

---

## Acceptance criteria

- [ ] Passing a digital Arabic PDF returns a list with one dict per page
- [ ] Each word dict contains `text` (string) and `bbox` (list of 4 floats)
- [ ] Passing a scanned PDF (image-only) returns the same structure via Tesseract
- [ ] Auto-detection works: digital PDF uses PyMuPDF, scanned uses Tesseract — no manual flag needed
- [ ] Arabic words are extracted with correct Unicode (not garbled characters)
- [ ] A 100-page PDF processes without running out of memory (processes page by page, not all at once)
- [ ] An empty page (no text) returns `{"page_num": N, "words": []}` — not an error
