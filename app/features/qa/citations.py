"""Answer-side citation tags: parsing, validation, resolution."""
import logging
import re
import unicodedata

from app.features.qa.claim_matching import _claim_sentence, _claim_tokens
from app.features.qa.page_resolution import _claim_page

logger = logging.getLogger(__name__)

_URDU_SPECIFIC = set("\u0679\u067A\u067B\u067C\u067D\u067E\u067F\u0680\u0688\u0689\u068A\u068B\u068C\u068D\u068E\u068F\u0691\u0692\u0693\u0694\u0695\u0696\u0697\u0698\u0699\u06A0\u06A1\u06A2\u06A3\u06A4\u06A5\u06A6\u06A7\u06A8\u06A9\u06AA\u06AB\u06AC\u06AD\u06AE\u06AF\u06B0\u06B1\u06B2\u06B3\u06B4\u06B5\u06B6\u06B7\u06B8\u06B9\u06BA\u06BB\u06BC\u06BD\u06BE\u06BF\u06C0\u06C1\u06C2\u06C3\u06C4\u06C5\u06C6\u06C7\u06C8\u06C9\u06CA\u06CB\u06CC\u06CD\u06CE\u06CF\u06D0\u06D1\u06D2\u06D3\u06D4\u06D5\u06D6\u06D7\u06D8\u06D9\u06DA\u06DB\u06DC\u06DD\u06DE\u06DF\u06E0\u06E1\u06E2\u06E3\u06E4\u06E5\u06E6\u06E7\u06E8\u06E9\u06EA\u06EB\u06EC\u06ED\u06EE\u06EF\u06F0\u06F1\u06F2\u06F3\u06F4\u06F5\u06F6\u06F7\u06F8\u06F9\u06FA\u06FB\u06FC\u06FD\u06FE\u06FF\u0640\u0626\u0624\u0671\u06C0")

# Regex to match structured citation tags produced by the LLM
_CITATION_TAG_RE = re.compile(r"\[P(\d+)(?:,\s*Page\s+(\d+))?\]")

# Individual tag inside a bracket group, e.g. "P1, Page 2" within "[P1, Page 2; P2, Page 1]"
_TAG_RE = re.compile(r"P(\d+)(?:\s*,\s*Page\s+(\d+))?")

# A bracket group that contains one or more citation tags (single or "; "-combined)
_CITATION_BRACKET_RE = re.compile(r"\[[^\]]*P\d[^\]]*\]")


def _iter_tag_indices(answer: str):
    """Yield every passage index cited in the answer, including tags that are
    combined inside a single bracket group like [P1, Page 2; P2, Page 1]."""
    for bracket in _CITATION_BRACKET_RE.finditer(answer):
        for m in _TAG_RE.finditer(bracket.group(0)):
            yield int(m.group(1))

# Arabic honorific / nisba suffixes used to detect named-entity queries
_ARABIC_ENTITY_RE = re.compile(
    r"[\u0600-\u06FF]+(?:الشريعة|الدين|الإسلام|الملة|الله|الرحمن|"
    r"بن\s+[\u0600-\u06FF]+|ابن\s+[\u0600-\u06FF]+)"
)

# Questions that ask for the SOURCE of a statement (footnote, reference,
# transmitter). They are usually short, factual metadata queries whose lexical
# overlap with the chunk text is thin ("source reference for Ibn Hajar's
# comment on the Qiblah" vs. a chunk quoting the comment but not the phrase
# "source reference"), so the rerank score lands far below the general gate
# even when the vector search pinned the right chunk. These questions need a
# relaxed quality gate, not a relax of retrieval.
_CITATION_QUESTION_RE = re.compile(
    r"(?:source\s+(?:reference|of\s+this|of\s+that|of\s+the)|footnote|side\s+note|"
    r"marginal\s+note|citation|cited\s+in|reference\s+(?:for|of|to)|reported\s+by|"
    r"narrated\s+by|transmitted\s+by|(?:who|من|کون)\s+(?:reported|narrated)|"
    r"who\s+(?:reports|narrates)\b|رواه|أخرجه|المصدر|المرجع|الحاشية|الهامش|مصدر|حوالہ)",
    re.IGNORECASE,
)


def _answer_language(answer: str) -> str:
    latin = len(re.findall(r"[A-Za-z]", answer))
    arabic = len(re.findall(r"[\u0600-\u06FF]", answer))
    if arabic == 0:
        return "en"
    urdu_specific = sum(1 for ch in answer if ch in _URDU_SPECIFIC)
    if urdu_specific >= max(3, arabic // 10):
        return "ur"
    if latin > arabic:
        return "en"
    return "ar"



def _resolve_citations(answer: str, passages: list[dict]) -> str:
    """Replace [Pn, Page X] tag groups with canonical [Book, Page X] strings.

    Handles both single tags ([P1, Page 2]) and combined groups the LLM may
    produce ([P1, Page 2; P2, Page 1]). This ensures user-visible citations
    reference actual book names and page numbers from the retrieved chunk
    metadata — never from the LLM's memory. Each tag's page is resolved
    sentence-aware: the claim sentence preceding the tag is located inside
    the ⟨Page N⟩-marked passage text and the citation is corrected to the
    marker page that actually precedes the claim.
    """
    def _replace(m: re.Match) -> str:
        claim_tokens = _claim_tokens(_claim_sentence(answer[: m.start()]))
        resolved: list[str] = []
        for tag in _TAG_RE.finditer(m.group(0)):
            idx = int(tag.group(1)) - 1  # convert to 0-based
            if 0 <= idx < len(passages):
                p = passages[idx]
                book = p.get("book_name") or ""
                page = _claim_page(passages, idx, tag.group(2), claim_tokens)
                if page is not None:
                    resolved.append(f"[{book}, Page {page}]" if book else f"[Page {page}]")
                else:
                    resolved.append(f"[{book}]" if book else "")
        return "; ".join(resolved)

    return _CITATION_BRACKET_RE.sub(_replace, answer)


def _validate_citations(answer: str, passages: list[dict]) -> list[int]:
    """Return a list of out-of-range passage indices found in the answer."""
    n = len(passages)
    return [idx for idx in _iter_tag_indices(answer) if idx < 1 or idx > n]



def _is_citation_question(question: str) -> bool:
    """True for questions asking about a statement's source/reference.

    Such questions are short metadata queries whose lexical overlap with the
    chunk text is thin (e.g. "source reference for Ibn Hajar's comment on the
    Qiblah" vs. a chunk quoting the comment but not those words), so they
    routinely fall below the rerank gate even when the right chunk was found.
    """
    return bool(_CITATION_QUESTION_RE.search(question or ""))

