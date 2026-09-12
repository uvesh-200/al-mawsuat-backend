"""Fix 2: guest (no-auth) ask requests must fall back to settings.DEFAULT_TENANT_ID
instead of the hardcoded "default".

The bug: ask_json()/ask_stream() used ``tenant_id = user.tenant_id if user else "default"``
while every other part of the codebase (books router, retriever, seed, upload) uses
settings.DEFAULT_TENANT_ID. If that setting were anything but the literal "default",
an unauthenticated /ask would query a tenant with no indexed books and return a
no-result refusal instead of the real sources.

This test sets DEFAULT_TENANT_ID to a custom value, calls ask_json() with no user,
and asserts the RAG graph is invoked with that tenant — proving the guest fallback
now matches the configured default tenant.
"""
import pytest

from app.core.config import settings
from app.features.qa import router as qa_router
from app.features.qa.router import ask_json


@pytest.fixture
def non_default_tenant():
    original = settings.DEFAULT_TENANT_ID
    settings.DEFAULT_TENANT_ID = "tenant-alpha"
    try:
        yield "tenant-alpha"
    finally:
        settings.DEFAULT_TENANT_ID = original


@pytest.fixture
def mock_graph_and_cache(monkeypatch):
    captured = {}

    async def fake_ainvoke(state: dict):
        captured["tenant"] = state["tenant_id"]
        captured["question"] = state["question"]
        return {
            "answer": "A grounded answer about the book.",
            "no_result": False,
            "sources": [
                {
                    "book_id": "b1", "book_name": "Kitab", "author": None,
                    "book_type": None, "chapter": None, "page_start": 1,
                    "page_end": 1, "score": 0.9, "text": "Source passage.",
                }
            ],
        }

    monkeypatch.setattr(qa_router, "rag_graph", _FakeGraph(fake_ainvoke))
    monkeypatch.setattr(qa_router, "_get_cached", _async_returning(None))
    monkeypatch.setattr(qa_router, "_set_cached", _async_void())
    monkeypatch.setattr(qa_router, "_record_stats", _async_void())
    return captured


def _async_returning(value):
    async def _f(*args, **kwargs):
        return value
    return _f


class _FakeGraph:
    def __init__(self, ainvoke):
        self.ainvoke = ainvoke


def _async_void():
    async def _f(*args, **kwargs):
        return None
    return _f


async def test_guest_ask_uses_default_tenant_id(non_default_tenant, mock_graph_and_cache):
    body = type("Body", (), {"question": "ماهو الكتاب؟", "book_id": None, "language": None})()
    resp = await ask_json(body, user=None)
    assert resp is not None
    # The RAG graph must be invoked with the CONFIGURED default tenant, not the
    # hardcoded literal "default".
    assert mock_graph_and_cache["tenant"] == "tenant-alpha"
    assert mock_graph_and_cache["tenant"] != "default"
    # Querying the right tenant returns real sources instead of a no-result refusal.
    assert resp.no_result is False
    assert resp.sources and resp.sources[0].book_id == "b1"
