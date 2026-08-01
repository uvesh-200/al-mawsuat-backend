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
    assert "صدر الشريعة" in answer, answer


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
