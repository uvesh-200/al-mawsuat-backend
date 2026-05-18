from collections.abc import Generator
from contextlib import contextmanager

import fitz
import pytesseract
from PIL import Image

TESSERACT_LANG = "ara+urd+eng"
WORD_THRESHOLD = 10


def _words_from_fitz(page: fitz.Page) -> list[dict]:
    raw = page.get_text("words")
    return [{"text": w[4], "bbox": list(w[:4])} for w in raw]


def _words_from_tesseract(page: fitz.Page) -> list[dict]:
    pix = page.get_pixmap()
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    data = pytesseract.image_to_data(img, lang=TESSERACT_LANG, output_type=pytesseract.Output.DICT)
    words = []
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        conf = data["conf"][i]
        w = data["width"][i]
        h = data["height"][i]
        if text and conf > 0 and w > 0 and h > 0:
            x = data["left"][i]
            y = data["top"][i]
            words.append({"text": text, "bbox": [x, y, x + w, y + h]})
    return words


def extract_page(page: fitz.Page, page_num: int) -> dict:
    words = _words_from_fitz(page)
    if len(words) < WORD_THRESHOLD:
        words = _words_from_tesseract(page)
    return {"page_num": page_num, "words": words}


@contextmanager
def open_pdf(pdf_bytes: bytes) -> Generator[fitz.Document, None, None]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        yield doc
    finally:
        doc.close()


def extract(pdf_bytes: bytes, progress_callback=None) -> list[dict]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    results = []
    total = len(doc)
    for page_num, page in enumerate(doc, start=1):
        results.append(extract_page(page, page_num))
        if progress_callback:
            progress_callback(page_num, total)
    doc.close()
    return results
