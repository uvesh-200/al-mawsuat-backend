import re
import unicodedata

ARABIC_STOPWORDS = {
    "من", "ما", "هو", "هي", "هم", "هن", "في", "على", "عن", "إلى", "الي",
    "الذي", "التي", "الذين", "و", "ف", "ثم", "أو", "او", "لم", "لن", "لا",
    "مع", "هذا", "هذه", "ذلك", "تلك", "كان", "كانت", "لماذا", "ما هو", "ما هي",
    "اذكر", "هو", "ماهو", "ماهي", "اي", "أي", "بان", "أن", "ان", "قد", "لقد",
}

# English questions are common (the corpus is translated classical works);
# without English stopwords the overlap score is dominated by function words
# ("the", "is", "in") and any English question scores ~0.3, defeating the
# confidence threshold. These are dropped alongside short tokens.
ENGLISH_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at",
    "by", "for", "with", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "do", "does", "did", "have", "has", "had", "this",
    "that", "these", "those", "it", "its", "his", "her", "their", "there",
    "here", "what", "which", "who", "whom", "whose", "when", "where", "why",
    "how", "not", "no", "yes", "if", "then", "than", "so", "too", "very",
    "can", "could", "would", "should", "may", "might", "will", "shall",
    "must", "about", "over", "under", "between", "into", "during", "before",
    "after", "above", "below", "also", "only", "just", "such", "both",
    "each", "either", "neither", "some", "any", "all", "more", "most",
    "other", "another", "one", "two", "first", "last", "per", "etc",
    "against", "through", "upon", "among", "within", "without", "up",
    "down", "out", "off", "again", "further", "once", "very",
    "say", "said", "says", "saying", "told", "asked", "tell", "state",
    "states", "stated", "wrote", "writes", "write", "according", "regarding",
    "regards", "mention", "mentions", "mentioned", "refer", "refers",
    "referred", "referring", "regarding", "mean", "means", "meant",
    "please", "note", "noted", "etc", "similar", "like", "such",
}

_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670\u0640]")
_ALEFS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})

# Latin transliteration artifacts from Arabic (macrons ā ī ū, subscript dots
# ṣ ḍ ṭ ẓ, hamza/alif ʿ ʾ ʼ). NFD decomposition splits macrons/subscript-dots
# into a base letter + combining mark so they can be dropped; the modifier
# letters (hamza etc.) are removed directly. This keeps "Kinānah" ≡ "Kinanah"
# and "al-Asqaʿ" ≡ "al-Asqa" for overlap matching against the indexed text.
_COMBINING_MARKS = re.compile(r"[\u0300-\u036F]")
_MODIFIER_LETTERS = str.maketrans({"ʿ": "", "ʾ": "", "ʼ": "", "ʻ": "", "’": "'", "‘": "'"})


def normalise_transliteration(text: str) -> str:
    """Strip Arabic-transliteration marks from Latin text (NFD + removal)."""
    decomposed = unicodedata.normalize("NFD", text)
    decomposed = _COMBINING_MARKS.sub("", decomposed)
    return decomposed.translate(_MODIFIER_LETTERS)

# Named-entity detection: Arabic compound titles with common suffixes
# e.g. "برهان الشريعة", "صدر الشريعة", "تاج الدين", "ابن حجر"
_ENTITY_COMPOUND_RE = re.compile(
    r"[\u0600-\u06FF]+\s+(?:الشريعة|الدين|الإسلام|الملة|الله|الرحمن|"
    r"الحق|العلماء|الأئمة|الحديث|الفقه)",
    re.UNICODE,
)
_IBN_RE = re.compile(r"(?:ابن|بن)\s+[\u0600-\u06FF]+", re.UNICODE)
_NISBA_RE = re.compile(r"[\u0600-\u06FF]{4,}(?:ي|وي)\b", re.UNICODE)

_IS_ARABIC_CHAR = re.compile(r"[\u0600-\u06FF]", re.UNICODE)

ENTITY_BOOST = 0.15


def _normalise_token(token: str) -> str:
    t = token.strip(".,;:!?()[]\"'«»،؛؟*#-").lower()
    t = normalise_transliteration(t)
    t = _DIACRITICS.sub("", t)
    t = t.translate(_ALEFS)
    return t


def _query_terms(question: str) -> list[str]:
    terms = []
    for token in question.split():
        norm = _normalise_token(token)
        if not norm:
            continue
        if norm in ARABIC_STOPWORDS or norm in ENGLISH_STOPWORDS:
            continue
        if norm.isdigit():
            continue
        is_arabic = bool(_IS_ARABIC_CHAR.search(norm))
        # English content words need at least 3 letters to be meaningful
        # ("pbuh", "ash" are 2-4; short noise like "vs"/"pp" is dropped);
        # Arabic words are kept even at 2-3 chars since Arabic relies on
        # short stems and stopwords are already filtered above.
        if not is_arabic and len(norm) < 3:
            continue
        terms.append(norm)
    return terms


def _extract_entities(question: str) -> list[str]:
    """Extract named-entity tokens from the question for boosting."""
    strip_chars = ".,;:!?()[]\"'«»،؛؟*#-"
    entities: list[str] = []
    for m in _ENTITY_COMPOUND_RE.finditer(question):
        entities.append(_normalise_diacritics(m.group(0)))
    for m in _IBN_RE.finditer(question):
        entities.append(_normalise_diacritics(m.group(0)))
    # Individual nisba tokens (e.g. القرطبي, العبادي)
    for token in question.split():
        if _NISBA_RE.match(token.strip(strip_chars)):
            entities.append(_normalise_diacritics(token.strip(strip_chars)))
    return entities


def _normalise_diacritics(text: str) -> str:
    return _DIACRITICS.sub("", text).translate(_ALEFS).lower()


def _term_overlap(question: str, text: str) -> float:
    terms = _query_terms(question)
    if not terms:
        return 0.0
    haystack = " ".join(_normalise_token(t) for t in text.split())
    matches = sum(1 for t in terms if t in haystack)
    return matches / len(terms)


def _entity_boost(question: str, result: dict) -> float:
    """Return a boost score if the result text contains named entities from the question."""
    entities = _extract_entities(question)
    if not entities:
        return 0.0
    text_norm = _normalise_diacritics(result.get("text", ""))
    matched = sum(1 for e in entities if e in text_norm)
    if matched == 0:
        return 0.0
    # Scale: full boost if all entities match, partial otherwise
    return ENTITY_BOOST * (matched / len(entities))


async def rerank(
    question: str,
    results: list[dict],
    top_k: int = 8,
    arabic_query: str | None = None,
) -> list[dict]:
    if not results:
        return []

    vector_results = [r for r in results if r.get("score", 0) != 0.5]
    keyword_results = [r for r in results if r.get("score", 0) == 0.5]

    rrf_scores: dict[str, float] = {}
    for i, r in enumerate(vector_results):
        key = r.get("text", "")
        rrf_scores[key] = rrf_scores.get(key, 0) + 1.0 / (i + 60)
    for i, r in enumerate(keyword_results):
        key = r.get("text", "")
        rrf_scores[key] = rrf_scores.get(key, 0) + 1.0 / (i + 60)

    seen: set[str] = set()
    deduped = []
    for r in results:
        key = r.get("text", "")
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    # For cross-lingual queries (e.g. an English question over Arabic books),
    # match the Arabic retrieval translation against Arabic chunk text so the
    # overlap component (and the confidence threshold downstream) is fair.
    secondary_query = (
        arabic_query
        if arabic_query and arabic_query.strip() != question.strip()
        else None
    )

    for r in deduped:
        key = r.get("text", "")
        rrf = rrf_scores.get(key, 0)
        overlap = _term_overlap(question, r.get("text", ""))
        if secondary_query:
            overlap = max(overlap, _term_overlap(secondary_query, r.get("text", "")))
        entity = _entity_boost(question, r)
        r["score"] = round(rrf + 0.5 * overlap + entity, 4)

    deduped.sort(key=lambda r: r.get("score", 0), reverse=True)

    return _suppress_overlap_chunks(deduped)[:top_k]


def _token_list(text: str) -> list[str]:
    return [t for t in (_normalise_token(w) for w in text.split()) if t]


def _pages_overlap(a: dict, b: dict) -> bool:
    a_s, a_e = a.get("page_start"), a.get("page_end")
    b_s, b_e = b.get("page_start"), b.get("page_end")
    if a_s is None or a_e is None or b_s is None or b_e is None:
        return False
    return a_s <= b_e and b_s <= a_e


def _contiguous_coverage(shorter: list[str], longer: list[str], ngram: int = 6) -> float:
    """Fraction of ``shorter`` covered by contiguous token runs of ``longer``.

    Measured as positions whose n-gram is present in the longer sequence; a
    verbatim span shared between overlapping chunks therefore reports ~1.0
    inside the run, and the final score is the covered fraction of the shorter
    chunk.
    """
    if len(shorter) < ngram:
        return 0.0
    ngrams = {tuple(longer[i:i + ngram]) for i in range(len(longer) - ngram + 1)}
    covered = sum(
        1 for i in range(len(shorter) - ngram + 1)
        if tuple(shorter[i:i + ngram]) in ngrams
    )
    return covered / (len(shorter) - ngram + 1)


def _suppress_overlap_chunks(ranked: list[dict]) -> list[dict]:
    """Drop same-book chunks that duplicate text already covered by a
    higher-ranked chunk.

    The chunker produces overlapping page-window chunks (window shift < window
    size), so consecutive chunks share a verbatim segment (~15%+ of the shorter
    chunk). When a query matches inside that shared segment, both chunks are
    returned and the answer ends up citing the same passage under two different
    page numbers ("[Page 2], [Page 1]"). Keep only the highest-ranked chunk per
    overlapping page range; its text already contains the shared passage.
    """
    kept: list[tuple[dict, list[str]]] = []
    for r in ranked:
        if not r.get("book_id"):
            kept.append((r, _token_list(r.get("text", ""))))
            continue
        r_tokens = _token_list(r.get("text", ""))
        if any(
            r.get("book_id") == acc.get("book_id")
            and _pages_overlap(r, acc)
            and _contiguous_coverage(r_tokens, acc_tokens) >= 0.12
            for acc, acc_tokens in kept
        ):
            continue
        kept.append((r, r_tokens))
    return [r for r, _ in kept]
