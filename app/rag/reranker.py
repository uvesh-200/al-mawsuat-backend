import re

ARABIC_STOPWORDS = {
    "من", "ما", "هو", "هي", "هم", "هن", "في", "على", "عن", "إلى", "الي",
    "الذي", "التي", "الذين", "و", "ف", "ثم", "أو", "او", "لم", "لن", "لا",
    "مع", "هذا", "هذه", "ذلك", "تلك", "كان", "كانت", "لماذا", "ما هو", "ما هي",
    "اذكر", "هو", "ماهو", "ماهي", "اي", "أي", "بان", "أن", "ان", "قد", "لقد",
}

_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670\u0640]")
_ALEFS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})


def _normalise_token(token: str) -> str:
    t = token.strip(".,;:!?()[]\"'«»،؛؟*#-").lower()
    t = _DIACRITICS.sub("", t)
    t = t.translate(_ALEFS)
    return t


def _query_terms(question: str) -> list[str]:
    terms = []
    for token in question.split():
        norm = _normalise_token(token)
        if len(norm) >= 2 and norm not in ARABIC_STOPWORDS and not norm.isdigit():
            terms.append(norm)
    return terms


def _term_overlap(question: str, text: str) -> float:
    terms = _query_terms(question)
    if not terms:
        return 0.0
    haystack = " ".join(_normalise_token(t) for t in text.split())
    matches = sum(1 for t in terms if t in haystack)
    return matches / len(terms)


async def rerank(question: str, results: list[dict], top_k: int = 5) -> list[dict]:
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

    for r in deduped:
        key = r.get("text", "")
        rrf = rrf_scores.get(key, 0)
        overlap = _term_overlap(question, r.get("text", ""))
        r["score"] = round(rrf + 0.5 * overlap, 4)

    deduped.sort(key=lambda r: r.get("score", 0), reverse=True)

    return deduped[:top_k]
