"""Passage truncation/formatting and physical-page annotation."""
import logging
import re

from app.features.qa.prompts import MAX_PASSAGE_TOKENS

logger = logging.getLogger(__name__)

def _truncate_text(text: str, max_tokens: int = MAX_PASSAGE_TOKENS) -> str:
    words = text.split()
    if len(words) > max_tokens:
        return " ".join(words[:max_tokens]) + " ..."
    return text


def _annotate_pages(text: str, page_offsets: list[dict] | None) -> str:
    """Insert ⟨Page N⟩ markers into a multi-page passage's text.

    The chunk text itself is a single blob (per-word page info was collapsed
    at chunk build time); ``page_offsets`` records which character range of
    the text came from which page, so the markers tell the LLM where the page
    boundaries actually fall and let it cite the page a sentence is on.
    Truncation may have cut the tail, so segments are clamped to the visible
    text; if only one page remains visible there is nothing to annotate.

    Markers ALWAYS carry the PHYSICAL page index — the same space the viewer,
    the /highlight endpoint and SourceItem.page use. Mixing printed footer
    numbers into this space (the old behaviour) produced citations that could
    not be reconciled with the page actually rendered on screen.
    """
    if not page_offsets or len(page_offsets) <= 1:
        return text
    n = len(text)
    visible = [po for po in page_offsets if po["start_char"] < n]
    if len(visible) <= 1:
        return text
    return " ".join(
        f"\u27e8Page {po['page']}\u27e9 "
        + text[po["start_char"] : min(po["end_char"], n)]
        for po in visible
    )


def _format_passages(passages: list[dict]) -> str:
    """Format passages with stable [P1]…[PN] tags that the LLM must cite.

    Page numbers in both the ⟨Page N⟩ markers and the Source line are PHYSICAL
    pages — the same space the viewer, /highlight and SourceItem.page use.
    (Printed footer numbers remain on the payload for data completeness but
    are never mixed into citation text: two page spaces produced citations
    that could not be reconciled with the rendered page.) The Source line
    carries ONLY "<book>, Page X-Y". ``chapter`` and ``author`` metadata are
    deliberately excluded: for OCR'd Arabic books the chapter field is a
    garbage blob of the running header/footnote text ("AES AME REY", "A) LW",
    …) that leaked onto the citation line.
    """
    lines: list[str] = []
    for i, p in enumerate(passages, 1):
        text = _truncate_text(p.get("text", ""))
        text = _annotate_pages(text, p.get("page_offsets") or [])
        book = p.get("book_name", "")
        page = p.get("page_start", "")
        page_end = p.get("page_end", "")
        source = book
        if page:
            if page_end and page_end != page:
                source += f", Page {page}-{page_end}"
            else:
                source += f", Page {page}"
        lines.append(f"[P{i}] {text}\n    Source: {source}")
    return "\n\n".join(lines)


# Arabic function words carry no locating signal, so they are dropped when
# matching an answer sentence against the passage text for page attribution.

_MARKER_RE = re.compile(r"\u27e8Page (\d+)\u27e9")

def _annotation_for(p: dict) -> str:
    """The exact ⟨Page N⟩-marked passage text the LLM was shown."""
    return _annotate_pages(_truncate_text(p.get("text", "")), p.get("page_offsets") or [])


