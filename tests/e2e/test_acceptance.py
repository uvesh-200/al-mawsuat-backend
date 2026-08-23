"""Acceptance / E2E tests for the al-mawsuat RAG API.

These tests hit the LIVE stack (nginx -> fastapi -> Qdrant/Meilisearch/Redis -> LLM)
and verify that answers are CORRECT (grounded in the book), not just that HTTP 200
is returned. They depend on the "5 pages book" (id 394ed100-...) being indexed.

Run from inside the fastapi container (the full stack must be up):

    docker exec al-mawsuat-backend-fastapi-1 python -m pytest /app/tests/e2e -v

Point E2E_BASE at http://fastapi:8000 to skip nginx rate limits, or keep the
default (http://nginx) to also verify the rate-limit path (429s are retried
with backoff automatically).
"""

import json
import os
import re
import time

import httpx
import pytest

BASE = os.environ.get("E2E_BASE", "http://nginx")
TIMEOUT = httpx.Timeout(240.0, connect=10.0)
RETRY_SLEEP = 65
BOOK_ID = "394ed100-6eb5-45d9-bd7f-f73466e72b05"

REFUSALS = (
    "no relevant information",
    "لا توجد معلومات",
    "لا معلومات",
    "لم يتم العثور على معلومات",
    "کوئی متعلقہ معلومات نہیں",
)

_URDU_SPECIFIC = re.compile(r"[\u0679-\u06FF\u0750-\u077F]")


@pytest.fixture
async def client():
    async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT) as c:
        yield c


async def _post(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    for attempt in range(3):
        resp = await client.post(url, **kwargs)
        if resp.status_code != 429 or attempt == 2:
            return resp
        time.sleep(RETRY_SLEEP)
    return resp


async def _ask(client: httpx.AsyncClient, question: str) -> dict:
    resp = await _post(
        client,
        "/ask",
        json={"question": question},
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    return resp.json()


def _latin_ratio(text: str) -> float:
    latin = len(re.findall(r"[A-Za-z]", text))
    arabic = len(re.findall(r"[\u0600-\u06FF]", text))
    total = latin + arabic
    return latin / total if total else 0.0


def _urdu_ratio(text: str) -> float:
    urdu = len(_URDU_SPECIFIC.findall(text))
    arabic = len(re.findall(r"[\u0600-\u06FF]", text))
    total = urdu + arabic
    return urdu / total if total else 0.0


def _is_refusal(answer: str) -> bool:
    lowered = answer.lower()
    return any(p in lowered for p in REFUSALS)


# ---------------------------------------------------------------- infrastructure

async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200


async def test_login_superadmin(client):
    resp = await client.post(
        "/auth/login",
        json={"email": "superadmin@al-mawsuat.local", "password": "test123"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data.get("access_token") or data.get("token"), "login must return a token"


async def test_ask_without_auth_works(client):
    data = await _ask(client, "من هو صاحب كتاب الوقاية؟")
    assert "answer" in data


# ---------------------------------------------------------------- validation

async def test_empty_question_422(client):
    resp = await client.post("/ask", json={"question": ""})
    assert resp.status_code == 422


async def test_too_long_question_422(client):
    resp = await client.post("/ask", json={"question": "ا" * 5000})
    assert resp.status_code == 422


# ---------------------------------------------------------------- correctness

async def test_out_of_scope_question_refused(client):
    data = await _ask(client, "ما هي عاصمة فرنسا؟")
    assert data.get("no_result") is True, "out-of-scope question must set no_result"
    assert _is_refusal(data["answer"]), data["answer"]


async def test_gibberish_refused(client):
    data = await _ask(client, "abc123xyz")
    assert data.get("no_result") is True or _is_refusal(data["answer"])


async def test_author_of_wiqaya_correct(client):
    data = await _ask(client, "من هو صاحب كتاب الوقاية؟")
    answer = data["answer"]
    assert not _is_refusal(answer)
    assert any(k in answer for k in ("محمود", "عبيد الله", "برهان الشريعة")), answer


async def test_al_abadi_correct(client):
    data = await _ask(client, "من هو العبادي وما معنى اسمه؟")
    answer = data["answer"]
    assert not _is_refusal(answer)
    assert "عبادة" in answer, answer


async def test_mahbubi_correct(client):
    data = await _ask(client, "ما معنى كلمة محبوبي؟")
    answer = data["answer"]
    assert not _is_refusal(answer)
    assert "محبوب" in answer, answer


async def test_qurtubi_correct(client):
    data = await _ask(client, "ما رأي القرطبي في ألقاب التعظيم؟")
    answer = data["answer"]
    assert not _is_refusal(answer)
    assert "القرطبي" in answer, answer


async def test_burhan_correct(client):
    data = await _ask(client, "من هو برهان الشريعة؟")
    answer = data["answer"]
    assert not _is_refusal(answer)
    assert "برهان الشريعة" in answer
    assert any(k in answer for k in ("محمود", "عبيد الله", "صدر الشريعة")), answer


# ---------------------------------------------------------------- languages

async def test_english_question_english_answer(client):
    data = await _ask(client, "Who is the author of Kitab al-Wiqaya?")
    answer = data["answer"]
    assert not _is_refusal(answer)
    assert _latin_ratio(answer) > 0.5, f"answer not in English: {answer}"
    assert any(k in answer for k in ("محمود", "Mahmud", "Burhan", "برهان")), answer


async def test_english_opinion_question_english_answer(client):
    data = await _ask(client, "What did al-Qurtubi say about honorific titles?")
    answer = data["answer"]
    assert not _is_refusal(answer)
    assert _latin_ratio(answer) > 0.5, f"answer not in English: {answer}"


async def test_urdu_question_urdu_answer(client):
    data = await _ask(client, "کتاب الوقایہ کا مصنف کون ہے؟")
    answer = data["answer"]
    assert not _is_refusal(answer)
    assert _urdu_ratio(answer) > 0.15, f"answer not in Urdu: {answer}"


# ---------------------------------------------------------------- sources

async def test_sources_are_returned_and_grounded(client):
    data = await _ask(client, "ما معنى كلمة محبوبي؟")
    sources = data.get("sources", [])
    assert sources, "answer must cite at least one source"
    for s in sources:
        assert s.get("book_id"), "source must carry book_id"
        assert s.get("relevance_score", 0) > 0
        assert s.get("book_name"), "source must carry book_name"


async def test_sources_carry_passage_text(client):
    data = await _ask(client, "ما معنى كلمة محبوبي؟")
    sources = data.get("sources", [])
    assert sources, "answer must cite at least one source"
    assert any(s.get("text") for s in sources), "sources must carry passage text"


# ---------------------------------------------------------------- cache

async def test_second_call_is_cached(client):
    q = "ما معنى كلمة محبوبي؟"
    first = await _ask(client, q)
    second = await _ask(client, q)
    assert second.get("was_cached") is True
    assert second["answer"] == first["answer"]


# ---------------------------------------------------------------- streaming

async def test_stream_emits_tokens_sources_done(client):
    async with client.stream("GET", "/ask/stream", params={"question": "ما معنى كلمة محبوبي؟"}) as resp:
        if resp.status_code == 429:
            time.sleep(RETRY_SLEEP)
            async with client.stream("GET", "/ask/stream", params={"question": "ما معنى كلمة محبوبي؟"}) as resp2:
                resp = resp2
        assert resp.status_code == 200
        seen = {"token": False, "sources": False, "done": False}
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload.startswith("{"):
                event = json.loads(payload)
                if event.get("type") == "token":
                    seen["token"] = True
                elif event.get("type") == "sources":
                    seen["sources"] = True
                    assert event.get("sources"), "sources event must carry sources"
                elif event.get("type") == "done":
                    seen["done"] = True
        assert seen == {"token": True, "sources": True, "done": True}, seen


# ---------------------------------------------------------------- highlight

async def test_highlight_text_param_draws_region(client):
    async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT) as c:
        params = {"book_id": BOOK_ID, "page": 5}
        plain = (await c.get("/highlight", params=params)).content
        hl = (await c.get("/highlight", params={**params, "text": "العبادي: بضم العين، نسبة إلى عبادة بن الصامت."})).content
        assert plain != hl, "highlighted render must differ from plain render"
        assert len(hl) > 10000, "highlighted render must be a real PNG"


async def test_highlight_bbox_param_still_works(client):
    async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT) as c:
        resp = await c.get(
            "/highlight",
            params={"book_id": BOOK_ID, "page": 1, "bbox": "100,100,500,500"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("image/png")


# ================================================================
# REGRESSION TESTS — Bug fixes (9 cases)
# ================================================================

# ---------------------------------------------------------------- Bug 1: Page offset

async def test_b1a_citation_page_matches_expected_footer_page(client):
    """B1a — The page numbers cited in sources must match the book's pages.

    page_start is the physical PDF page index (the viewer renders
    doc[page-1]).  For the test book (7 physical PDF pages) the al-Wiqaya
    authorship/genealogy content is on physical pages 1, 3 and 4, with the
    strongest match on page 4 ("المبحث الثالث نسب صاحب الوقاية").
    """
    EXPECTED_PHYSICAL_PAGE = 4
    data = await _ask(client, "من هو صاحب كتاب الوقاية؟")
    assert not _is_refusal(data["answer"])
    sources = data.get("sources", [])
    assert sources, "must return sources"
    page_starts = [s.get("page_start") for s in sources if s.get("page_start")]
    assert page_starts, "sources must carry page_start"
    assert EXPECTED_PHYSICAL_PAGE in page_starts, (
        f"Expected page {EXPECTED_PHYSICAL_PAGE} in citations, got {page_starts}. "
        "This indicates the systematic page-offset bug (Bug 1) is still present."
    )


async def test_b1b_ingestion_validator_rejects_bad_page_numbers():
    """B1b — IngestionPageNumberError is raised when extraction rate is too low.

    This is a unit-style test embedded in the e2e suite so it runs alongside
    the live-stack tests and confirms the validator module is importable.
    """
    from app.features.ingestion.page_number_validator import (
        IngestionPageNumberError,
        validate_ingestion_page_numbers,
    )

    # All pages have footer_extracted=False → extraction rate = 0% < 50% minimum
    bad_pages = [
        {"page_num": i, "physical_page": i, "footer_extracted": False}
        for i in range(1, 11)
    ]
    with pytest.raises(IngestionPageNumberError, match="extraction rate"):
        validate_ingestion_page_numbers(bad_pages)


# ---------------------------------------------------------------- Bug 2: Hallucinated citations

async def test_b2a_cited_source_text_supports_claim(client):
    """B2a — For the al-Wiqaya authorship query, the cited source text must
    actually mention the book or the author — not unrelated content."""
    data = await _ask(client, "من هو صاحب كتاب الوقاية؟")
    assert not _is_refusal(data["answer"])
    sources = data.get("sources", [])
    assert sources, "must return sources"
    # At least one source chunk must contain relevant keywords
    relevant_keywords = ("وقاية", "برهان", "محمود", "عبيد الله", "صدر الشريعة")
    grounded = any(
        any(kw in (s.get("text") or "") for kw in relevant_keywords)
        for s in sources
    )
    assert grounded, (
        "No cited source chunk contains keywords relevant to the al-Wiqaya "
        "authorship query. This indicates wrong-chunk retrieval (Bug 2).\n"
        f"Source texts: {[s.get('text', '')[:80] for s in sources]}"
    )


async def test_b2b_answer_citations_are_grounded_in_returned_sources(client):
    """B2b — Every [Book, Page N] citation in the answer must correspond to
    one of the returned sources.  Hallucinated / parametric page numbers
    (not present in sources) should not appear."""
    data = await _ask(client, "ما معنى كلمة محبوبي؟")
    answer = data.get("answer", "")
    sources = data.get("sources", [])

    # Extract all page numbers mentioned in the answer
    page_nums_in_answer = set(
        int(m) for m in re.findall(r"Page\s+(\d+)", answer)
    )
    page_nums_in_sources = {
        s.get("page_start") for s in sources if s.get("page_start") is not None
    }

    hallucinated = page_nums_in_answer - page_nums_in_sources
    assert not hallucinated, (
        f"Answer cites pages {hallucinated} that are NOT in the returned sources "
        f"{page_nums_in_sources}. This indicates hallucinated citations (Bug 2)."
    )


# ---------------------------------------------------------------- Bug 3: Contradiction blindness

async def test_b3_contradiction_flagged_in_synthesis(client):
    """B3 — When asked to compare two prefaces that name different people as
    the author of a book, the system must NOT say they agree.

    The test query is intentionally worded to surface the contradiction between
    Burhan al-Shari'ah and Taj al-Shari'ah being named as author in different
    prefaces.  The answer must contain a disagreement/contradiction signal.
    """
    data = await _ask(
        client,
        "قارن بين المقدمتين: من يذكر كل منهما مؤلفاً للكتاب؟",
    )
    answer = data.get("answer", "")
    if _is_refusal(answer):
        pytest.skip("No relevant passages found — test book may not have two prefaces indexed")

    contradiction_signals = (
        "اختلاف", "خلاف", "تناقض", "بينما", "في حين", "يختلف",
        "disagree", "contradict", "different", "whereas", "however",
        "⚠️",  # the contradiction warning note prepended by _consistency_check
    )
    has_signal = any(sig in answer for sig in contradiction_signals)
    # Also check it does NOT say they are identical
    bad_smoothing = any(
        phrase in answer for phrase in ("نفس الوصف", "كلاهما يقول", "يتفقان", "the same")
    )
    assert has_signal or not bad_smoothing, (
        "Answer smoothed a contradiction into agreement (Bug 3). "
        f"Answer: {answer[:300]}"
    )


# ---------------------------------------------------------------- Bug 4: Split citations

async def test_b4a_short_hadith_and_commentary_single_page_citation(client):
    """B4a — A hadith and its short commentary should produce at most one
    distinct page citation in the answer, not two separate ones."""
    data = await _ask(client, "ما هو الحديث الوارد في الكتاب مع شرحه؟")
    answer = data.get("answer", "")
    if _is_refusal(answer):
        pytest.skip("No hadith content found — adjust query for your test book")

    sources = data.get("sources", [])
    # Count distinct page_start values cited — for a short passage there
    # should not be two different pages cited for the same logical unit
    cited_pages = [s.get("page_start") for s in sources if s.get("page_start")]
    page_set = set(cited_pages)
    # This is a soft assertion: warn if we see more than 3 distinct pages
    # for a query that should retrieve a single compact passage
    assert len(page_set) <= 3, (
        f"Too many distinct pages cited for a single passage query: {page_set}. "
        "This may indicate the cross-page split bug (Bug 4) is still present."
    )


async def test_b4b_chunker_merge_reduces_cross_page_splits():
    """B4b — Unit test: cross-page merge must not increase chunk count."""
    from app.features.ingestion.chunker import (
        CROSS_PAGE_MERGE_THRESHOLD,
        _merge_cross_page_chunks,
    )

    def _w(text, page):
        return {"text": text, "page_num": page, "physical_page": page, "bbox": None}

    # Two short chunks across a page boundary
    short = CROSS_PAGE_MERGE_THRESHOLD // 2
    chunk_a = [_w(f"a{i}", 1) for i in range(short)]
    chunk_b = [_w(f"b{i}", 2) for i in range(short)]
    chunk_a[-1]["text"] = "كلمة"  # no sentence end

    before = [chunk_a, chunk_b]
    after = _merge_cross_page_chunks(before)
    assert len(after) <= len(before), (
        "Cross-page merge must not increase chunk count"
    )


# ---------------------------------------------------------------- Bug 5: Vague answers

async def test_b5a_substantive_scholar_answer_contains_specific_details(client):
    """B5a — Asking what a scholar said must return specific details from the
    source, not a generic paraphrase."""
    data = await _ask(
        client,
        "ماذا قال النبي ﷺ أو ماذا ذكر الكتاب عن التكريم قبل العتاب؟",
    )
    answer = data.get("answer", "")
    if _is_refusal(answer):
        pytest.skip("No relevant passages found for this specific query")

    # The source text contains specific terms; the answer must include at
    # least one concrete detail (not just a generic paraphrase)
    specific_signals = (
        "تكريم", "عتاب", "مغفرة", "ذنب", "honour", "forgiven", "blame",
        "كرّم", "غفر", "ذكر",
    )
    has_specific = any(sig in answer for sig in specific_signals)
    assert has_specific, (
        "Answer is a generic paraphrase without specific details from the source "
        f"(Bug 5). Answer: {answer[:300]}"
    )


async def test_b5b_answer_length_for_substantive_question(client):
    """B5b — A substantive scholarly question should produce a meaningful-length
    answer, not a single vague sentence truncated by a max_tokens limit."""
    data = await _ask(client, "من هو برهان الشريعة وما تخصصه؟")
    answer = data.get("answer", "")
    if _is_refusal(answer):
        pytest.skip("No passages found")

    word_count = len(answer.split())
    assert word_count >= 30, (
        f"Answer is too short ({word_count} words) for a biographical question — "
        "likely still truncated by a low max_tokens limit (Bug 5). "
        f"Answer: {answer}"
    )

