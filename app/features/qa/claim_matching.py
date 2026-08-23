"""Claim tokenisation, normalisation and tolerant token matching."""
import re
import unicodedata

_CLAIM_STOPWORDS = {
    "في", "من", "إلى", "عن", "على", "أن", "إن", "كان", "كانت", "يكون",
    "تكون", "هو", "هي", "له", "لها", "ما", "ال", "ثم", "أو", "إذا", "كما",
    "هذا", "هذه", "ذلك", "التي", "الذي", "الذين", "وقد", "لم", "لا", "به",
    "منه", "عنه", "قد", "بل", "بعد", "قبل", "عند", "بين", "غير", "كل",
    "أنه", "إنه", "أي", "ولا", "ولم", "وأن", "حتى", "لأن",
    # normalized forms after hamza stripping (إن/أن -> ان, إلى -> الى, ...)
    "ان", "الى", "اذ", "انه", "لن", "ليس",
}


_SENTENCE_BOUNDARY_RE = re.compile(r"[.!؟\u06d4\n]")
_CLAIM_CITATION_GROUP_RE = re.compile(r"\[[^\]]*P\d[^\]]*\]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u0600-\u06FF]+")

def _claim_sentence(prefix: str) -> str:
    """The sentence in the answer that ends at a citation tag."""
    claim = next((part.strip() for part in reversed(_SENTENCE_BOUNDARY_RE.split(prefix)) if part.strip()), None)
    if claim is None:
        return prefix.strip()
    return _CLAIM_CITATION_GROUP_RE.sub(" ", claim).strip()


def _normalise_token(t: str) -> str:
    t = unicodedata.normalize("NFKC", t).lower()
    if re.fullmatch(r"[\u0600-\u06FF]+", t):
        t = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", t)
        t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ء", "")
        if len(t) > 3 and t[0] in "وف":
            t = t[1:]
        # accusative tanwin-alef: "محمودا" -> "محمود"
        if len(t) > 3 and t.endswith("ا") and len(t.rstrip("ا")) >= 3:
            t = t.rstrip("ا")
    return t


def _claim_tokens(text: str) -> list[str]:
    """Content tokens of a claim sentence (stopwords removed, normalised)."""
    tokens: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9]+|[\u0600-\u06FF]+", text):
        t = _normalise_token(raw)
        if len(t) < 2:
            continue
        if re.fullmatch(r"[\u0600-\u06FF]+", t) and t in _CLAIM_STOPWORDS:
            continue
        tokens.append(t)
    return tokens


def _token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    # prefix tolerance for Arabic: "محمودا"~"محمود" handled by normalisation,
    # "مؤلف"~"مؤلفاته" / "اسم"~"أسماء" here (longer side wins, min len 3)
    if re.fullmatch(r"[\u0600-\u06FF]+", a) and re.fullmatch(r"[\u0600-\u06FF]+", b):
        if len(a) >= 3 and len(b) >= 3 and (a.startswith(b) or b.startswith(a)):
            return True
    return False
def _lcs_len(a: list[str], b: list[str]) -> int:
    """Length of the longest common subsequence of two token sequences.

    Rolling two-row DP, token comparison via :func:`_token_match`.
    """
    if not a or not b:
        return 0
    n = len(a)
    prev = [0] * (n + 1)
    for tb in b:
        row = [0] * (n + 1)
        for j in range(1, n + 1):
            row[j] = prev[j] if prev[j] > row[j - 1] else row[j - 1]
            if _token_match(tb, a[j - 1]) and prev[j - 1] + 1 > row[j]:
                row[j] = prev[j - 1] + 1
        prev = row
    return prev[n]
