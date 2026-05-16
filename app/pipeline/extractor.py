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


def extract(pdf_bytes: bytes) -> list[dict]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    results = []
    for page_num, page in enumerate(doc, start=1):
        words = _words_from_fitz(page)
        if len(words) < WORD_THRESHOLD:
            words = _words_from_tesseract(page)
        results.append({"page_num": page_num, "words": words})
    doc.close()
    return results
