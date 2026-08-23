"""Generate deterministic fixture PDFs for the Phase 0 bbox-pipeline evidence harness.

All books are TEXT-LAYER PDFs (real word rectangles produced by MuPDF), so the
extractor takes the ``_words_from_fitz`` path and every coordinate is exact.
Two scanned (image-only) variants exercise the runtime Tesseract locate path
inside the api container. Content is original placeholder prose composed for
this test set.

Usage: python scripts/phase0/fixtures_gen.py
"""
from pathlib import Path

import fitz

OUT = Path(__file__).parent / "fixtures"
W, H = 595, 842
MARGIN = 56
LINE_H = 24

EN_LINES = [
    "The early jurists debated whether intention transforms a lawful act into",
    "an unlawful one, and their conclusions shaped centuries of legal thought.",
    "Ibn Rushd argued that custom carries weight equal to written statutes in",
    "matters of commerce, while stricter voices demanded explicit textual proof.",
    "A merchant in old Damascus kept three ledgers: one for debts, one for gifts,",
    "and one for the charity he distributed quietly before dawn each Friday.",
    "Scholars disagree about the mystery of the ancient manuscript found in the",
    "old library, because its margins contain notes from at least four hands.",
    "The first hand writes in a careful eastern script, the second corrects it,",
    "the third adds cross references, and the fourth merely counts the folios.",
    "Debate over water rights filled the autumn council sessions, and the final",
    "settlement relied on testimony from farmers rather than written decrees.",
    "Every settlement of dispute begins with listening, the judge reminded the",
    "clerks, because a claim without context is a lamp without oil in wind.",
    "Manuscript copyists introduced errors of omission more often than errors",
    "of invention, since fatigue erased whole clauses but rarely added them.",
    "Students memorised the core text first and only then approached commentary,",
    "for commentary without foundation drifts like a boat without an anchor.",
    "The marketplace inspector recorded prices twice daily, once after sunrise",
    "and once before sunset, so disputes could be checked against both entries.",
]

AR_LINES = [
    "ناقش الفقهاء الأوائل مسألة النية وأثرها في صحة التصرفات الشرعية على وجه التفصيل.",
    "وذكر جمهور الحنابلة أن العبرة في العقود بالمقاصد لا بالألفاظ التي ترد من المتعاقدين.",
    "واختلف أصحاب المذاهب في حكم بيع الوفاء وما إذا كان يرتب أثرا في ذمة الوائف له.",
    "قال المصنف في كتابه إن المسألة فيها نظر لأن الأدلة عليها مختلفة ومتقاربة.",
    "وأشار بعض المحققين إلى أن العرف يحمل دلالة قوية إذا لم يوجد نص خاص في الباب.",
    "وتتبع المحدثون طرق الرواية عن شيوخ البلد حتى استقر الحديث على وجهه الصحيح.",
    "وبيّن شراح الكتاب أن عموم اللفظ يجوز أن يخصص بدليل عقلي أو نقلي معتبر.",
    "وخلاصة القول أن المسألة ترجع إلى اعتبار المأمور به نية عند الاقتضاء كما مرّ.",
]

UR_LINES = [
    "محققین علومِ اسلامیہ نے نیّت کے مسئلے پر تفصیلی بحث کی ہے اور اپنی آراء پیش کیں۔",
    "اکابر علماء کا مختار یہ ہے کہ معاملات میں مقصد ہی نظر میں رکھا جاتا ہے ہمیشہ۔",
    "کتاب و سنت سے دلائل لیتے ہوئے مفتیان امت نے فتاویٰ میں اسی اصول کو ملحوظ رکھا۔",
    "عرف کی حجیت کے باب میں متاخرین کی تحقیقات قابلِ غور ہیں جن کو نظرانداز نہ کریں۔",
]

FOOTER_Y = H - 46


def _put(page, rect, text, rtl=False, size=13):
    """Insert a text block, preferring shaped HTML layout (handles RTL)."""
    try:
        align = "right" if rtl else "left"
        direction = "rtl" if rtl else "ltr"
        html = (
            f'<div style="direction:{direction};text-align:{align};'
            f'font-size:{size}px;line-height:1.4;">{text}</div>'
        )
        page.insert_htmlbox(rect, html)
        return True
    except Exception:
        fontpath = _find_font() if rtl else None
        if rtl and fontpath:
            page.insert_font(fontname="arabic", fontfile=fontpath)
            page.insert_textbox(
                rect, text, fontname="arabic", fontsize=size,
                align=fitz.TEXT_ALIGN_RIGHT if rtl else fitz.TEXT_ALIGN_LEFT,
            )
        else:
            page.insert_textbox(
                rect, text, fontname="helv", fontsize=size,
                align=fitz.TEXT_ALIGN_RIGHT if rtl else fitz.TEXT_ALIGN_LEFT,
            )
        return False


def _find_font():
    for cand in (r"C:\Windows\Fonts\tahoma.ttf", r"C:\Windows\Fonts\arial.ttf"):
        if Path(cand).exists():
            return cand
    return None


def flowing_book(name, lines, rtl=False, pages=4, footer_start_phys=1):
    """A continuously flowing book: paragraphs never break, so chunk boundaries
    fall wherever TARGET_MAX lands — producing both single-page and multi-page
    chunks deterministically."""
    doc = fitz.open()
    usable = FOOTER_Y - 20 - MARGIN
    per_page = max(1, int(usable // LINE_H))
    repeats = (pages * per_page // len(lines)) + 1
    sequence = (lines * repeats)[: pages * per_page]
    for i in range(pages):
        page = doc.new_page(width=W, height=H)
        y = MARGIN
        for line in sequence[i * per_page : (i + 1) * per_page]:
            _put(page, fitz.Rect(MARGIN, y, W - MARGIN, y + LINE_H), line, rtl=rtl)
            y += LINE_H
        _footer(page, footer_start_phys + i)
    doc.subset_fonts()
    doc.save(OUT / name, garbage=4, deflate=True)
    doc.close()


def _footer(page, printed_num):
    _put(page, fitz.Rect(W / 2 - 60, FOOTER_Y, W / 2 + 60, FOOTER_Y + 24),
         str(printed_num), size=11)


def cover_plus_frontmatter():
    """Physical page 1 = cover (no footer); physical 2..8 carry printed
    footers 1..7, i.e. printed = physical - 1. Reproduces the classic
    front-matter offset that mixes printed and physical page spaces."""
    doc = fitz.open()
    page = doc.new_page(width=W, height=H)
    _put(page, fitz.Rect(MARGIN, H / 3, W - MARGIN, H / 3 + 120),
         "THE COLLECTED OPINIONS OF THE EARLY JURISTS", size=26)
    _put(page, fitz.Rect(MARGIN, H / 3 + 60, W - MARGIN, H / 3 + 160),
         "Volume One — A Critical Edition With Commentary And Notes", size=16)
    for phys in range(2, 9):
        page = doc.new_page(width=W, height=H)
        y = MARGIN
        for line in EN_LINES[:14]:
            _put(page, fitz.Rect(MARGIN, y, W - MARGIN, y + LINE_H), line)
            y += LINE_H
        _put(page, fitz.Rect(W / 2 - 60, FOOTER_Y, W / 2 + 60, FOOTER_Y + 24),
             str(phys - 1), size=11)
    doc.subset_fonts()
    doc.save(OUT / "frontmatter.pdf", garbage=4, deflate=True)
    doc.close()


def scanned_from(src_name, dst_name, dpi=150, npages=2):
    """Rasterize a text-layer book into an image-only PDF (a 'scan')."""
    src = fitz.open(OUT / src_name)
    doc = fitz.open()
    for p in src[:npages]:
        pix = p.get_pixmap(dpi=dpi)
        page = doc.new_page(width=p.rect.width, height=p.rect.height)
        page.insert_image(page.rect, pixmap=pix)
    doc.subset_fonts()
    doc.save(OUT / dst_name, garbage=4, deflate=True)
    doc.close()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    flowing_book("en_book.pdf", EN_LINES)
    flowing_book("ar_book.pdf", AR_LINES, rtl=True)
    flowing_book("ur_book.pdf", UR_LINES, rtl=True)
    cover_plus_frontmatter()
    scanned_from("en_book.pdf", "en_scanned.pdf")
    scanned_from("ar_book.pdf", "ar_scanned.pdf")
    for f in sorted(OUT.iterdir()):
        print(f"{f.name}: {f.stat().st_size} bytes")


if __name__ == "__main__":
    main()
