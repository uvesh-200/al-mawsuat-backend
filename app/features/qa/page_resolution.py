"""Resolve a cited claim to the physical page its text falls on."""
import logging

from app.features.qa.claim_matching import (
    _CLAIM_CITATION_GROUP_RE,
    _SENTENCE_BOUNDARY_RE,
    _TOKEN_RE,
    _lcs_len,
    _normalise_token,
    _token_match,
)
from app.features.qa.passages import _MARKER_RE, _annotation_for

logger = logging.getLogger(__name__)

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


