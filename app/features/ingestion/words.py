"""Word extraction: fitz text layer, Tesseract OCR, Gemini text heuristics."""
import logging
import os
import re
import subprocess
import tempfile
import unicodedata

import fitz

from app.core.config import settings

logger = logging.getLogger(__name__)


TESSERACT_LANG = "ara+eng"
WORD_THRESHOLD = 10

# Synthetic line metrics for Gemini OCR text (see _text_to_words)
_LINE_HEIGHT = 18.0
_PARAGRAPH_GAP = 3.0 * _LINE_HEIGHT

_LIST_MARKER = re.compile(r"^[#>*\-]+\s*|\d+[\.\)]\s*")

# ---------------------------------------------------------------------------
# Footer page-number extraction
# ---------------------------------------------------------------------------

# Matches Arabic-Indic numerals (٠١٢٣٤٥٦٧٨٩) and Western digits, optionally
# surrounded by whitespace / dashes / brackets. We look for the number in the
# last few lines of the page, where most Islamic books place the footer.
_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# Patterns that identify a standalone page number in the footer region:

def _clean_gemini_text(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        line = _LIST_MARKER.sub("", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        line = line.replace("*", "").replace("#", "")
        lines.append(line)
    return "\n".join(lines)


def _words_from_fitz(page: fitz.Page) -> list[dict]:
    raw = page.get_text("words")
    # geom="real": these are true PDF-point rectangles measured by MuPDF.
    return [{"text": w[4], "bbox": list(w[:4]), "geom": "real"} for w in raw]


def _words_from_tesseract(page: fitz.Page) -> list[dict]:
    pix = page.get_pixmap(dpi=settings.OCR_DPI)
    scale = 72.0 / settings.OCR_DPI
    tmp_png = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    png_name = tmp_png.name
    tmp_png.close()
    try:
        pix.save(png_name)
        proc = subprocess.run(
            ["tesseract", png_name, "stdout", "-l", TESSERACT_LANG, "--psm", "3", "tsv"],
            capture_output=True, text=True,
        )
        import csv
        import io
        words = []
        reader = csv.DictReader(io.StringIO(proc.stdout), delimiter="\t")
        for row in reader:
            text = row.get("text", "").strip()
            conf = row.get("conf", "-1")
            w = int(row.get("width", "0"))
            h = int(row.get("height", "0"))
            try:
                conf_f = float(conf)
            except ValueError:
                conf_f = -1.0
            if text and conf_f > 0 and w > 0 and h > 0:
                if "\n" in text or "\t" in text or len(text) > 40:
                    continue
                x = int(row.get("left", "0")) * scale
                y = int(row.get("top", "0")) * scale
                line_key = f"{row.get('block_num', '0')}-{row.get('par_num', '0')}-{row.get('line_num', '0')}"
                words.append({"text": text, "bbox": [x, y, x + w * scale, y + h * scale],
                              "line": line_key, "geom": "real"})
        return words
    finally:
        try:
            if os.path.exists(png_name):
                os.unlink(png_name)
        except OSError:
            pass


def _text_to_words(text: str, page_num: int, printed_page_num: int | None = None) -> list[dict]:
    """Convert Gemini OCR text into word dicts with synthetic line bboxes.

    The Gemini path has no real layout data, so each OCR line is assigned a
    synthetic bounding box (line-height tall, full-width). This lets the
    chunker's line/paragraph grouping treat consecutive words on the same
    OCR line as one line instead of one word per line — the old behaviour
    caused the fiqh strategy to fire on every word ending with a colon and
    fragment books into sentence-level chunks.

    Blank OCR lines mark paragraph breaks; the first word of the line after
    a blank line is flagged ``para_start`` so the chunker can build
    paragraph-aware chunks.
    """
    words = []
    y = 0.0
    para_start = False
    for raw_line in _clean_gemini_text(text).split("\n"):
        line = raw_line.strip()
        if not line:
            y += _PARAGRAPH_GAP
            para_start = True
            continue
        tokens = line.split()
        line_width = sum(len(t) * 10 + 5 for t in tokens)
        x = 0.0
        line_words = []
        for token in tokens:
            tw = len(token) * 10
            entry = {
                "text": token,
                "page_num": page_num,
                "printed_page_num": printed_page_num,
                # geom="synthetic": this box is a char-width heuristic with no
                # relation to the real page layout (x can exceed the page width
                # many times over). The chunker uses it only for line/paragraph
                # grouping and MUST NOT emit it as highlightable geometry.
                "bbox": [x, y, x + tw, y + _LINE_HEIGHT],
                "geom": "synthetic",
            }
            if para_start:
                entry["para_start"] = True
                para_start = False
            line_words.append(entry)
            x += tw + 5
        words.extend(line_words)
        y += _LINE_HEIGHT
    if not words:
        return [{"text": "", "page_num": page_num, "printed_page_num": printed_page_num,
                 "bbox": None, "geom": "synthetic"}]
    return words


# ---------------------------------------------------------------------------
# Tesseract extraction
# ---------------------------------------------------------------------------

