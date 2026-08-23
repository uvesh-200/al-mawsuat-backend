import asyncio
import logging
import re
import unicodedata
from typing import Annotated, TypedDict

import httpx
from langgraph.graph import END, StateGraph
from openai import AsyncOpenAI

from app.config import settings
from app.core.embedder import embed_query
from app.core.translation import detect_language, translate_for_retrieval, translate_to_english
from app.rag.reranker import _query_terms, normalise_transliteration, rerank
from app.rag.retriever import keyword_search, vector_search

logger = logging.getLogger(__name__)


def _status_of(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status


class _OpenAIProvider:
    """Any OpenAI-compatible chat endpoint (Groq, DeepSeek, …)."""

    def __init__(self, name: str, base_url: str, api_key: str, model: str) -> None:
        self.name = name
        self._model = model
        # max_retries=0: the SDK's built-in 429 backoff (up to 12s+ per attempt)
        # stacks on top of the provider-chain retries below and makes failures
        # take minutes. Let the chain own all retry/backoff logic.
        self._client = AsyncOpenAI(
            base_url=base_url, api_key=api_key, timeout=240.0, max_retries=0
        )

    async def complete(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""


class _GeminiProvider:
    """Native Gemini generateContent REST fallback (not OpenAI-compatible)."""

    def __init__(self) -> None:
        self.name = "gemini"
        self._model = settings.GEMINI_LLM_MODEL

    async def complete(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        system_parts: list[str] = []
        user_parts: list[str] = []
        for m in messages:
            if m.get("role") == "system":
                system_parts.append(m.get("content", ""))
            else:
                user_parts.append(m.get("content", ""))
        body: dict = {
            "contents": [{"parts": [{"text": "\n\n".join(user_parts)}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
        url = f"{settings.GEMINI_API_BASE}/v1beta/models/{self._model}:generateContent"
        async with httpx.AsyncClient(timeout=240.0) as client:
            resp = await client.post(url, params={"key": settings.GEMINI_API_KEY}, json=body)
        if resp.status_code != 200:
            raise httpx.HTTPStatusError(
                f"Gemini returned {resp.status_code}", request=resp.request, response=resp
            )
        candidates = resp.json().get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini returned no candidates")
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)


def _build_providers() -> list:
    """Build the active provider chain in LLM_PROVIDERS order.

    Only providers listed in settings.LLM_PROVIDERS are activated; a provider
    whose key is set but is not listed (e.g. DeepSeek with no credits) is
    skipped entirely so requests never waste a retry cycle on it.
    """
    registry: list = []
    if settings.GROQ_API_KEY:
        registry.append(
            _OpenAIProvider("groq", settings.GROQ_BASE_URL, settings.GROQ_API_KEY, settings.GROQ_LLM_MODEL)
        )
    if settings.DEEPSEEK_API_KEY:
        registry.append(
            _OpenAIProvider(
                "deepseek", settings.DEEPSEEK_BASE_URL, settings.DEEPSEEK_API_KEY, settings.DEEPSEEK_LLM_MODEL
            )
        )
    if settings.GEMINI_API_KEY:
        registry.append(_GeminiProvider())
    wanted = [name.strip().lower() for name in settings.LLM_PROVIDERS.split(",") if name.strip()]
    by_name = {p.name: p for p in registry}
    providers = [by_name[name] for name in wanted if name in by_name] if wanted else list(registry)
    logger.info(
        "LLM provider chain built: %s (LLM_PROVIDERS=%r, keys present: %s)",
        [p.name for p in providers], settings.LLM_PROVIDERS, [p.name for p in registry],
    )
    return providers


async def _chat_with_retry(
    messages: list[dict], temperature: float = 0.3, max_tokens: int = 1024
) -> str:
    """Try each configured provider in order; retry 429s, then move on.

    Quota (429), insufficient balance (402), auth (401/403), server (5xx)
    and network errors on one provider do not fail the request while another
    provider still has capacity.
    """
    providers = _build_providers()
    if not providers:
        raise RuntimeError("No LLM provider is configured")
    last_exc: Exception | None = None
    for provider in providers:
        for attempt in range(3):
            try:
                text = await provider.complete(messages, temperature, max_tokens)
                if text:
                    logger.info(
                        "LLM provider '%s' produced the response (model=%s)",
                        provider.name, getattr(provider, "_model", "n/a"),
                    )
                    return text
                last_exc = RuntimeError(f"{provider.name} returned an empty completion")
            except Exception as exc:
                last_exc = exc
                status = _status_of(exc)
                logger.warning(
                    "LLM provider '%s' attempt %d failed (HTTP %s): %s",
                    provider.name, attempt + 1, status if status is not None else "network", exc,
                )
                if status == 429 and attempt < 2:
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
            break
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("All LLM providers returned empty completions")


class AgentState(TypedDict):
    question: str
    tenant_id: str
    book_id: str | None
    language: str | None
    passages: list[dict]
    best_score: float
    top_vector_score: float
    retry_count: int
    answer: str
    sources: list[dict]
    no_result: bool
    streaming: bool
    embed_failed: bool


MAX_PASSAGE_TOKENS = 800
LANGUAGE_NAMES = {"ar": "Arabic", "ur": "Urdu", "en": "English"}

GROUNDING_PROMPT_SYSTEM = """\
You are an Islamic knowledge assistant specialising in the Deobandi tradition.
Answer ONLY using the passages provided below. Do not use your own knowledge,
and do not fill gaps with inference beyond what the passages state.

LANGUAGE:
Write your entire answer in the exact same language and script as the
question. Do not switch languages or transliterate unless the question does.

CITATION FORMAT — STRICT:
Cite every factual claim with [Pn, Page X]. Never invent a page number. Cite
in the sentence where the claim appears, not just at the end.

BEFORE YOU FLAG A CONTRADICTION — REQUIRED CHECK:
Passages about people, especially classical Arabic/Urdu biographical or
genealogical texts, often describe ONE person using a title (laqab) and a
given name (ism) in the same sentence — e.g. "Burhān al-Sharīʿa: Maḥmūd ibn
X" names ONE person, not two candidates. Before writing anything like
"scholars disagree" or listing multiple names as alternatives, ask yourself:

  (a) Are these actually different claims about the SAME question — i.e. two
      passages genuinely proposing different, incompatible answers?
  (b) Or is this ONE entity described two ways (title + name) in a single
      sentence, or two DIFFERENT questions being answered by different
      passages (e.g. "who wrote book X" vs "who is person X's ancestor")?

Only use contradiction language for (a). For (b), state the single fact
plainly, using whichever passage states it most directly and citing that
passage's actual page — and if a genuinely separate question is also
addressed in the passages, answer it separately and label it as a different
question, not as a competing answer to the first.

Example of the mistake to avoid: given a passage stating "my grandfather,
Burhān al-Sharīʿa: Maḥmūd ibn Ṣadr al-Sharīʿa, wrote al-Wiqāya" and a
separate passage debating whether Burhān al-Sharīʿa and Tāj al-Sharīʿa are
the same person — the correct answer is "the author is Burhān al-Sharīʿa,
whose name is Maḥmūd" (one fact, one citation), plus a separately-labeled
note that scholars debate his identity relative to Tāj al-Sharīʿa. The
incorrect answer treats "Burhān al-Sharīʿa," "Maḥmūd ibn Ṣadr al-Sharīʿa,"
and "Tāj al-Sharīʿa" as three competing candidates for the author's name.

IF THE PASSAGES DO NOT ANSWER THE QUESTION:
Respond with exactly: No relevant information found in the provided sources.

SPECIFICITY:
Reproduce the specific reasoning present in the source rather than
summarizing it away — if the source gives a chain of names, dates, or
reasoning steps, preserve that structure in your answer.

SOURCES AND TRANSMISSION CHAINS:
Questions about who reported/narrated a statement, its transmission chain,
or its source reference must be answered from the passage's own wording:
- If the passage names MULTIPLE reporters or transmitters (e.g. "Reported
  by Al-Bukhari, Muslim, Ahmad, and At-Tirmidhi"), list ALL of them — never
  narrow the answer to one transmitter just because one narration "version"
  is described ("In Muslim's version there is the addition" is a VARIANT of
  one transmission, not the set of reporters).
- If a footnote or marginal note carries a source reference ("Fath al-Bari,
  vol. 1, p. 122"), quote the complete reference — volume and page — as it
  appears; never invent page numbers that are not in the passage.
- An "addition in Muslim's version" describes a longer wording within a
  hadith reported by Muslim; when the question asks who reported the hadith,
  it is not a candidate answer by itself.

PASSAGES:
{passages}"""

LLM_ERROR_FALLBACK = "I encountered an error while generating the answer. Please try again."

NO_RESULT_REFUSALS = {
    "ar": "لا توجد معلومات ذات صلة في المصادر المقدمة.",
    "ur": "فراہم کردہ ذرائع میں کوئی متعلقہ معلومات نہیں ملی۔",
}

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


async def _enforce_language(answer: str, lang_name: str) -> str:
    try:
        rewritten = await _chat_with_retry(
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Rewrite the following answer entirely in {lang_name}. "
                        "Keep all citation brackets [Pn, Page X] exactly as they "
                        "appear in the original answer. "
                        "Do not add, modify, or remove any citations."
                        f"Answer:\n{answer}"
                    ),
                }
            ],
            temperature=0.2,
            max_tokens=1024,
        )
        return rewritten.strip() or answer
    except Exception:
        logger.exception("Language rewrite failed, keeping original answer")
        return answer


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
_CLAIM_STOPWORDS = {
    "في", "من", "إلى", "عن", "على", "أن", "إن", "كان", "كانت", "يكون",
    "تكون", "هو", "هي", "له", "لها", "ما", "ال", "ثم", "أو", "إذا", "كما",
    "هذا", "هذه", "ذلك", "التي", "الذي", "الذين", "وقد", "لم", "لا", "به",
    "منه", "عنه", "قد", "بل", "بعد", "قبل", "عند", "بين", "غير", "كل",
    "أنه", "إنه", "أي", "ولا", "ولم", "وأن", "حتى", "لأن",
    # normalized forms after hamza stripping (إن/أن -> ان, إلى -> الى, ...)
    "ان", "الى", "اذ", "انه", "لن", "ليس",
}

_MARKER_RE = re.compile(r"\u27e8Page (\d+)\u27e9")
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!؟\u06d4\n]")
_CLAIM_CITATION_GROUP_RE = re.compile(r"\[[^\]]*P\d[^\]]*\]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u0600-\u06FF]+")


def _annotation_for(p: dict) -> str:
    """The exact ⟨Page N⟩-marked passage text the LLM was shown."""
    return _annotate_pages(_truncate_text(p.get("text", "")), p.get("page_offsets") or [])


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


def _locate_claim(claim_tokens: list[str], annotated_text: str) -> tuple[int, int, list[dict]] | None:
    """LCS (longest common subsequence in passage order) match of the claim's
    tokens inside the annotated passage text; returns
    ``(start_offset, end_offset, matched)`` of the matched span, or None.

    ``matched`` lists every matched passage token as
    ``{"claim_idx": int, "exact": bool, "start": int}`` so the caller can
    decide WHICH page of the span carries the claim (the DENSE page, not the
    last matched token) — a plain end-anchor drifts when the claim's trailing
    tokens are high-frequency words that also occur on a later page.

    A plain greedy subsequence fails when the LLM glosses the claim — e.g.
    "مؤلف «الوقاية»" inserted between phrases that DO occur in the source
    ("...ديباجة «النقاية» ... برهان الشريعة محمود بن صدر الشريعة"). LCS
    tolerates such insertions while keeping claim order. Accepts the match
    only when ≥ 2 tokens match and the matched ratio is not collapsed below
    half (except when the whole claim matched).
    """
    if not claim_tokens:
        return None
    n = len(claim_tokens)
    spans = [m.span() for m in _TOKEN_RE.finditer(annotated_text)]
    m = len(spans)
    if m == 0:
        return None
    p_tokens = [_normalise_token(annotated_text[s:e]) for s, e in spans]

    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        ti = p_tokens[i - 1]
        row, prev = dp[i], dp[i - 1]
        if not ti:
            row[1:] = prev[1:]
            continue
        for j in range(1, n + 1):
            # classic LCS three-way max: dp[i][j] = max(dp[i-1][j],
            # dp[i][j-1], dp[i-1][j-1] + 1 if tokens match)
            row[j] = row[j - 1] if row[j - 1] > prev[j] else prev[j]
            if _token_match(ti, claim_tokens[j - 1]):
                cand = prev[j - 1] + 1
                if cand > row[j]:
                    row[j] = cand

    best = dp[m][n]
    if best < 2 or (best != n and best * 2 < n):
        return None

    i, j = m, n
    matched: list[dict] = []  # reversed: last passage token first
    while i > 0 and j > 0:
        if p_tokens[i - 1] and _token_match(p_tokens[i - 1], claim_tokens[j - 1]) and dp[i][j] == dp[i - 1][j - 1] + 1:
            matched.append(
                {
                    "claim": j - 1,
                    "exact": p_tokens[i - 1] == claim_tokens[j - 1],
                    "start": spans[i - 1][0],
                }
            )
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1
    if not matched:
        return None
    matched.reverse()  # passage order
    return matched[0]["start"], matched[-1]["start"], matched


def _claim_page(passages: list[dict], idx: int, llm_page: str | None, claim_tokens: list[str]) -> int | None:
    """Resolve a citation tag to the page the cited claim actually appears on.

    The claim sentence's content tokens are located in the ⟨Page N⟩-annotated
    text of every same-book passage, and the passage with the strongest match
    wins (the model often tags the wrong chunk for a claim that lives in a
    neighbouring one). Strength is scored by how many claim tokens the LCS
    matched in that passage first (a spurious anchor token in a weak passage
    must not outrank a long exact match), then by the count of exactly-matched
    tokens, then by the number of matched tokens exclusive to that passage —
    a plain LCS can otherwise "succeed" inside a page that merely repeats the
    claim's generic vocabulary (e.g. the author's name), and pure in-order
    coincidence elsewhere is the failure mode. The cited passage breaks ties.

    Within the winning passage, the claim's page is the page that CONTAINS
    MOST of the matched tokens (the dense page), not the page of the last
    matched token: a claim whose tail is a high-frequency word ("the people",
    "the book") frequently drifts onto a later page. Only when the claim is
    nowhere locatable does the fallback use the model's page if that is a
    real page of the chunk, then the passage's start page.
    """
    if claim_tokens:
        cited = passages[idx]
        candidates = [(idx, cited)] + [
            (i, p) for i, p in enumerate(passages)
            if i != idx and p.get("book_name") == cited.get("book_name")
        ]
        token_freq: dict[str, int] = {}
        for _, q in candidates:
            seen: set[str] = set()
            for tok in _TOKEN_RE.finditer(_annotation_for(q)):
                t = _normalise_token(tok.group(0))
                if t and t not in seen:
                    seen.add(t)
                    token_freq[t] = token_freq.get(t, 0) + 1
        claim_weights: dict[int, int] = {}
        for j, tok in enumerate(claim_tokens):
            freq = token_freq.get(tok, 1)
            claim_weights[j] = 3 if freq == 1 else 2 if freq == 2 else 1
        best_score = None
        best_page = None
        best_i = None
        for i, q in candidates:
            annotated = _annotation_for(q)
            located = _locate_claim(claim_tokens, annotated)
            if located is None:
                continue
            _start, _end, matched = located
            page = _dense_page(annotated, matched, claim_weights, claim_tokens)
            if page is None:
                continue
            total = len(matched)
            exact = sum(1 for mt in matched if mt["exact"])
            anchors = sum(
                1 for mt in matched if token_freq.get(claim_tokens[mt["claim"]], 0) == 1
            )
            score = (total, exact, anchors)
            if best_score is None or score > best_score or (score == best_score and i == idx):
                best_score = score
                best_page = page
                best_i = i
        # A claim that only matches fragments of generic vocabulary (a
        # summary sentence like "there are multiple narrations...") must not
        # drag the citation onto an unrelated page just because two generic
        # words coincide. Accept the found page when it falls inside the
        # cited chunk's own page range at the normal LCS bar, or — for an
        # alternate passage of the same book — only on a strong match (>=3/4
        # of the claim's tokens) so a neighbouring chunk must genuinely win.
        n = len(claim_tokens)
        cited_pages = [
            po.get("page")
            for po in (cited.get("page_offsets") or [])
            if isinstance(po, dict) and po.get("page") is not None
        ]
        accept = False
        if best_score is not None:
            outside = best_page not in cited_pages
            if outside:
                # Outside the cited chunk the bar is a genuine full-claim
                # match: a short generic lead-in ("according to", "as the
                # scholars said") must not drag the citation onto an
                # unrelated chunk's page merely because the same two words
                # occur there. Floor the accepted matched count at 3 tokens
                # so tiny generic claims can never override the chunk the
                # model actually cited.
                accept = best_score[0] >= max(3, n * 3 // 4 + 1)
            else:
                accept = best_score[0] * 2 >= n
        if best_page is not None and best_i is not None and accept:
            if best_i != idx:
                logger.info(
                    "[TRACE] citation page from alternate passage: cited [P%d] but claim found in passage %d of same book; page=%s claim=%r",
                    idx + 1, best_i + 1, best_page, " ".join(claim_tokens[:12]),
                )
            elif str(best_page) != (llm_page or ""):
                logger.info(
                    "[TRACE] citation page corrected: [P%d] llm_page=%r -> page=%s claim=%r",
                    idx + 1, llm_page, best_page, " ".join(claim_tokens[:12]),
                )
            return best_page
    # Fallbacks operate purely in PHYSICAL page space (the space the viewer
    # and /highlight endpoint use): preferring printed footer numbers here
    # returned pages that pointed at the wrong rendered page.
    offsets = passages[idx].get("page_offsets") or []
    pages = [
        po.get("page")
        for po in offsets
        if isinstance(po, dict) and po.get("page") is not None
    ]
    if llm_page and pages:
        try:
            cited = int(llm_page)
        except (ValueError, TypeError):
            cited = None
        if cited is not None and cited in pages:
            return cited
    return passages[idx].get("page_start")


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


def _dense_page(
    annotated_text: str,
    matched: list[dict],
    token_weights: dict[int, int] | None = None,
    claim_tokens: list[str] | None = None,
) -> int | None:
    """The page containing most of the matched claim tokens.

    Each matched token's offset is mapped through the ⟨Page N⟩ markers; the
    page claiming the largest share of matched tokens is where the claim
    actually sits. Tokens weigh more when they are rare across the candidate
    passages (``token_weights`` maps a claim-token index to a rarity weight,
    e.g. 3 for a token unique to one passage, 2 for two passages, else 1): a
    distinctive claim token like a footnote number ("122") or a proper noun
    must dominate generic vocabulary ("as", "mentioned") that happens to
    repeat on a nearby page.

    When the whole claim token list is known (``claim_tokens``) and the
    global LCS span has CROSSED a page boundary, each touched page is
    re-scored by its OWN page-local LCS against the full claim: the global
    LCS may legally collect the claim's tail on a later page merely because
    the same generic words occur there again (e.g. "…am the brick and…"
    matched up to page 12, with "am the seal of the prophets" then picked
    from page 13 which repeats the sentence's tail) even though the earlier
    page's own text holds the whole claim. The page whose own text carries
    the longest in-page claim subsequence wins; ties fall back to the
    weighted-token rule, then to the page of the first matched token (the
    claim start anchors better than its high-frequency tail).
    """
    markers = list(_MARKER_RE.finditer(annotated_text))
    if not markers:
        return None

    def _page_of(offset: int) -> int | None:
        page = None
        for m in markers:
            if m.start() < offset:
                page = int(m.group(1))
            else:
                break
        return page

    by_page: dict[int, list[tuple[int, int]]] = {}
    first_token_pages: dict[int, int] = {}
    for pos, mt in enumerate(matched):
        page = _page_of(mt["start"])
        if page is None:
            continue
        by_page.setdefault(page, []).append((pos, (token_weights or {}).get(mt["claim"], 1)))
        first_token_pages.setdefault(page, pos)
    if not by_page:
        return None

    if claim_tokens and len(by_page) > 1:
        tokens_by_page: dict[int, list[str]] = {}
        for m in _TOKEN_RE.finditer(annotated_text):
            page = _page_of(m.start())
            if page in by_page:
                t = _normalise_token(m.group(0))
                if t:
                    tokens_by_page.setdefault(page, []).append(t)
        if len(tokens_by_page) > 1:
            lcs_scores = {p: _lcs_len(claim_tokens, toks) for p, toks in tokens_by_page.items()}
            best_len = max(lcs_scores.values())
            leaders = [p for p, v in lcs_scores.items() if v == best_len]
            if len(leaders) == 1:
                return leaders[0]
            # an LCS tie: the page with the larger weighted matched share
            weights = {p: sum(w for _, w in by_page[p]) for p in leaders}
            top = max(weights.values())
            leaders = [p for p in leaders if weights[p] == top]
            if len(leaders) == 1:
                return leaders[0]
            leaders.sort(key=lambda p: first_token_pages.get(p, 10**9))
            return leaders[0]

    weights = {p: sum(w for _, w in items) for p, items in by_page.items()}
    best_weight = max(weights.values())
    pages = sorted(p for p, w in weights.items() if w == best_weight)
    if len(pages) == 1:
        return pages[0]
    # Same total weight across pages: the claim's first matched token is the
    # most reliable anchor (its tail is often a high-frequency word that
    # spills onto the next page).
    pages.sort(key=lambda p: first_token_pages.get(p, 10**9))
    return pages[0]


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


CONSISTENCY_CHECK_PROMPT = """\
You are a consistency checker for a RAG system that answers ONLY from the
provided source passages. Given the question, the generated answer, and the
passages it cites, decide whether the passages CONTRADICT each other on the
answer to the question. Start your reply with exactly 'AGREE' or 'CONTRADICT',
then one sentence of reasoning.

REQUIRED CHECK BEFORE ANSWERING 'CONTRADICT':
Passages about people, especially classical Arabic/Urdu biographical or
genealogical texts, often describe ONE person using a title (laqab) and a
given name (ism) in the same sentence, e.g. "Burhān al-Sharīʿa: Maḥmūd ibn X"
is ONE person, not two candidates. Before you answer 'CONTRADICT', decide
which of these holds:
  (a) two passages genuinely propose different, incompatible answers to the
      SAME question — only this is a CONTRADICT; or
  (b) one entity described two ways (title + name) in a single sentence, or
      two DIFFERENT questions being answered by different passages, or
      passages that complement each other (each supporting a different part
      of the answer) — this is AGREE.

Only 'CONTRADICT' for (a). Title+name variants of the same person and
separate questions are NOT contradictions and must be answered 'AGREE'."""


async def _consistency_check(
    question: str, answer: str, passages_text: str
) -> str:
    """For multi-source answers: ask the LLM whether the cited passages agree.

    Returns the answer unchanged, or with a contradiction note prepended.
    Only called when ≥ 2 distinct passages are cited.
    """
    cited_indices = sorted(set(_iter_tag_indices(answer)))
    if len(cited_indices) < 2:
        return answer
    try:
        verdict = await _chat_with_retry(
            messages=[
                {
                    "role": "system",
                    "content": CONSISTENCY_CHECK_PROMPT,
                },
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\n\n"
                        f"Answer: {answer}\n\n"
                        f"Passages:\n{passages_text}"
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=160,
        )
        verdict = verdict.strip()
        logger.info("[TRACE] consistency_check verdict=%r", verdict)
        if verdict.upper().startswith("CONTRADICT"):
            note = (
                "⚠️ Note: the source passages contain a disagreement on this point. "
                "Each passage's claim is stated separately below.\n\n"
            )
            return note + answer
    except Exception:
        logger.exception("Consistency check failed, returning answer as-is")
    return answer


def _build_keyword_query(normalised: str, english_query: str, embed_query_text: str) -> str:
    """Combined keyword query (original-language + English terms, deduped)."""
    terms = _query_terms(normalised) + _query_terms(english_query)
    terms = list(dict.fromkeys(terms))
    return " ".join(terms) or english_query or embed_query_text


def _build_keyword_queries(
    normalised: str, english_query: str, embed_query_text: str
) -> tuple[str, str | None]:
    """Split keyword queries for the two keyword-search legs.

    Returns (primary, secondary): one leg per script so the Meilisearch
    `frequency` AND-over-anywhere-matches behaviour cannot zero the whole
    keyword leg with a word that only matches another book's chunks.
    """
    primary_terms = _query_terms(normalised)
    secondary_terms = _query_terms(english_query)
    primary = " ".join(primary_terms) or " ".join(secondary_terms) or english_query or embed_query_text
    secondary = " ".join(secondary_terms) or None
    if secondary and secondary == primary:
        secondary = None
    return primary, secondary


async def retrieve_node(state: AgentState) -> dict:
    question = state["question"]
    tenant_id = state["tenant_id"]
    book_id = state.get("book_id")
    lang = await detect_language(question)
    arabic_query = await translate_for_retrieval(question, lang)
    # Arabic/Urdu questions need an English leg too: the indexed chunk text is
    # English, so a purely Arabic overlap against it is 0 and the rerank score
    # collapses below the confidence threshold even when the vector search
    # found the right passage.
    english_query = await translate_to_english(question, lang)

    # Strip Arabic-transliteration marks (ā, ṣ, ʿ, …) so "Kinānah" matches the
    # indexed "Kinanah" in both the embedding query and the rerank overlap.
    normalised = normalise_transliteration(question)

    embed_parts = [arabic_query]
    for extra in (normalised, english_query):
        if extra.strip() and extra.strip() not in embed_parts:
            embed_parts.append(extra)
    embed_query_text = " ".join(embed_parts)
    keyword_query, keyword_alt_query = _build_keyword_queries(normalised, english_query, embed_query_text)

    logger.info(
        "[TRACE] retrieve_queries lang=%s arabic=%r english=%r keyword=%r keyword_alt=%r embed_query=%r",
        lang, arabic_query[:300], english_query[:300], keyword_query[:300],
        keyword_alt_query[:300] if keyword_alt_query else None, embed_query_text[:300],
    )

    embed_failed = False
    try:
        vector = await embed_query(embed_query_text, tenant_id)
    except Exception:
        logger.exception("embed_query failed")
        embed_failed = True
        vector = None

    vs = []
    ks = []
    if vector is not None:
        vs_result, ks_result = await asyncio.gather(
            vector_search(vector, tenant_id, top_k=20, book_id=book_id),
            keyword_search(keyword_query, tenant_id, top_k=20, book_id=book_id, alt_query=keyword_alt_query),
            return_exceptions=True,
        )
        if not isinstance(vs_result, Exception):
            vs = vs_result
        else:
            logger.error("vector_search failed: %s", vs_result)
        if not isinstance(ks_result, Exception):
            ks = ks_result
        else:
            logger.error("keyword_search failed: %s", ks_result)
    else:
        try:
            ks = await keyword_search(keyword_query, tenant_id, top_k=20, book_id=book_id, alt_query=keyword_alt_query)
        except Exception as exc:
            logger.error("keyword_search fallback failed: %s", exc)
    combined = vs + ks

    # Capture the raw Qdrant cosine BEFORE rerank: rerank() overwrites each
    # result dict's "score" in place with the reranked value, so reading it
    # after would lose the raw similarity signal the quality gate needs.
    top_vector_score = max((r.get("score", 0.0) for r in vs), default=0.0)

    # Prefer the secondary query in the other script when available so the
    # overlap component can match either the Arabic or English chunk text.
    secondary_query = english_query if lang == "ar" else arabic_query
    reranked = await rerank(normalised, combined, top_k=8, arabic_query=secondary_query)
    best_score = reranked[0]["score"] if reranked else 0.0
    logger.info(
        "[TRACE] rerank_results count=%d", len(reranked),
    )
    for i, r in enumerate(reranked):
        logger.info(
            "[TRACE] rerank rank=%d score=%.4f book=%s page=%s chapter=%s preview=%r",
            i + 1, r["score"], r.get("book_name"), r.get("page_start"),
            r.get("chapter"), r.get("text", "")[:200],
        )
    logger.info(
        "retrieve: tenant=%s book_id=%s lang=%s vs_raw=%d ks_raw=%d combined=%d reranked=%d "
        "best_score=%.4f top_vector_score=%.4f",
        tenant_id, book_id, lang, len(vs), len(ks), len(combined), len(reranked),
        best_score, top_vector_score,
    )
    return {
        "passages": reranked,
        "best_score": best_score,
        "top_vector_score": top_vector_score,
        "embed_failed": embed_failed,
        "language": lang,
    }


def _is_citation_question(question: str) -> bool:
    """True for questions asking about a statement's source/reference.

    Such questions are short metadata queries whose lexical overlap with the
    chunk text is thin (e.g. "source reference for Ibn Hajar's comment on the
    Qiblah" vs. a chunk quoting the comment but not those words), so they
    routinely fall below the rerank gate even when the right chunk was found.
    """
    return bool(_CITATION_QUESTION_RE.search(question or ""))


def quality_check_node(state: AgentState) -> str:
    """Route to generation only when retrieval is confident; otherwise retry
    once, then refuse.

    Two independent confidence signals, combined with OR:

    * ``best_score >= RAG_MIN_CONFIDENCE_SCORE`` — the reranked hybrid score
      (RRF + lexical overlap + entity boost). This is the historical gate; it
      carries Arabic-content matches whose evidence is lexical (vector
      similarity for Arabic chunks is weak in this corpus).
    * ``top_vector_score >= RAG_VECTOR_MIN_CONFIDENCE`` — the raw Qdrant
      cosine of the top vector hit. English-content matches score 0.65-0.78
      even when a long question dilutes the lexical overlap below the rerank
      threshold. This rescued genuine hits like the "which two Qur'anic
      verses..." question that previously returned an empty source list.

    Citation/source-reference questions ("what is the source reference for
    Ibn Hajar's comment on the Qiblah") use a lower vector bar: the correct
    chunk is nearly always found (vector ~0.62) but the question's words
    barely overlap the answer text, so a normal gate falsely refuses them.
    They are gate-relaxed but never retrieval-relaxed.

    Unrelated-but-lexically-overlapping noise (e.g. a riba question matching
    an unrelated Arabic chunk) sits at ~0.27 rerank and ~0.55 vector — below
    both relaxed signals — and is refused.
    """
    threshold = settings.RAG_MIN_CONFIDENCE_SCORE
    vector_min = settings.RAG_VECTOR_MIN_CONFIDENCE
    if _is_citation_question(state.get("question", "")):
        vector_min = settings.RAG_CITATION_VECTOR_MIN_CONFIDENCE
    best_score = state.get("best_score", 0.0)
    top_vector_score = state.get("top_vector_score", 0.0)
    confident = best_score >= threshold or top_vector_score >= vector_min
    if state["passages"] and confident:
        decision = "generate"
    elif state["retry_count"] < 1:
        decision = "retry"
    else:
        decision = "no_result"
    logger.info(
        "quality_gate: best_score=%.4f threshold=%.2f top_vector_score=%.4f vector_min=%.2f "
        "passages=%d citation_question=%s -> %s",
        best_score, threshold, top_vector_score, vector_min, len(state["passages"]),
        _is_citation_question(state.get("question", "")), decision,
    )
    return decision


async def retry_node(state: AgentState) -> dict:
    """One reformulation attempt before giving up. The rephrase is produced in
    the question's own language — the old unconditional Arabic rephrase
    mangled English questions on their second pass."""
    try:
        lang = state.get("language") or await detect_language(state["question"])
        lang_name = LANGUAGE_NAMES.get(lang, "English")
        rephrased = await _chat_with_retry(
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Rephrase this question in {lang_name} as a clear, "
                        f"concise search query: {state['question']}"
                    ),
                }
            ],
            temperature=0.7,
            max_tokens=256,
        )
    except Exception:
        logger.exception("LLM rephrase failed, keeping original question")
        rephrased = state["question"]
    return {"question": rephrased, "retry_count": state["retry_count"] + 1}


async def generate_node(state: AgentState) -> dict:
    lang = state.get("language") or await detect_language(state["question"])
    lang_name = LANGUAGE_NAMES.get(lang, "English")
    passages = state["passages"]
    passages_text = _format_passages(passages)

    prompt = GROUNDING_PROMPT_SYSTEM.format(
        passages=passages_text,
        language=lang_name,
        n_passages=len(passages),
    )
    logger.info("[TRACE] generate prompt for question=%r language=%s", state["question"], lang_name)
    logger.info("[TRACE] generate full prompt:\n%s", prompt)

    try:
        answer = await _chat_with_retry(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"{state['question']}\n\nWrite your entire answer in {lang_name}."},
            ],
            temperature=0.3,
            max_tokens=1024,
        )

        # Post-generation citation validation
        bad_indices = _validate_citations(answer, passages)
        if bad_indices:
            logger.warning(
                "Generated answer contained out-of-range citation tags %s "
                "(passage count: %d). Re-generating with stricter prompt.",
                bad_indices, len(passages),
            )
            stricter_prompt = prompt + (
                f"\n\nIMPORTANT: Your previous answer used invalid citation tags "
                f"{bad_indices}. You only have {len(passages)} passages ([P1]–"
                f"[P{len(passages)}]). Only use tags in that range."
            )
            answer = await _chat_with_retry(
                messages=[
                    {"role": "system", "content": stricter_prompt},
                    {"role": "user", "content": f"{state['question']}\n\nWrite your entire answer in {lang_name}."},
                ],
                temperature=0.2,
                max_tokens=1024,
            )

        # Multi-source consistency check
        answer = await _consistency_check(state["question"], answer, passages_text)

        # Resolve [Pn] tags → canonical [Book, Page N] strings for the user
        answer = _resolve_citations(answer, passages)

        actual = _answer_language(answer)
        if actual != lang:
            answer = await _enforce_language(answer, lang_name)

    except Exception:
        logger.exception("LLM generate failed, returning fallback")
        answer = LLM_ERROR_FALLBACK

    logger.info("[TRACE] generate answer=%r", answer[:1000])

    sources = [
        {
            "text": p.get("text", ""),
            "book_name": p.get("book_name", ""),
            "author": p.get("author", ""),
            "book_type": p.get("book_type"),
            "page_start": p.get("page_start"),
            "page_end": p.get("page_end"),
            "page_bboxes": p.get("page_bboxes"),
            "page_offsets": p.get("page_offsets"),
            "minio_path": p.get("minio_path"),
            "chapter": p.get("chapter"),
            "bbox": p.get("bbox"),
            "score": p.get("score", 0),
            "book_id": p.get("book_id"),
            "relevance_score": p.get("score", 0),
        }
        for p in passages
    ]
    return {"answer": answer, "sources": sources}


def no_result_node(state: AgentState) -> dict:
    if state.get("embed_failed"):
        return {"answer": "The search service is temporarily unavailable. Please try again in a few minutes.", "no_result": True}
    lang = state.get("language") or "en"
    refusal = NO_RESULT_REFUSALS.get(lang, "No relevant information found in the provided sources.")
    if state.get("retry_count", 0) > 0:
        return {"answer": f"{refusal} Try rephrasing your question.", "no_result": True}
    return {"answer": refusal, "no_result": True}


graph = StateGraph(AgentState)
graph.add_node("retrieve", retrieve_node)
graph.add_node("retry", retry_node)
graph.add_node("generate", generate_node)
graph.add_node("no_result_handler", no_result_node)
graph.set_entry_point("retrieve")
graph.add_conditional_edges(
    "retrieve", quality_check_node, {"generate": "generate", "retry": "retry", "no_result": "no_result_handler"}
)
graph.add_conditional_edges(
    "retry", quality_check_node, {"generate": "generate", "no_result": "no_result_handler"}
)
graph.add_edge("generate", END)
graph.add_edge("no_result_handler", END)

rag_graph = graph.compile()
