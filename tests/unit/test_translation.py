"""Unit tests for provider-swappable translation (loose coupling).

With no GOOGLE_TRANSLATE_API_KEY the pipeline falls back to Gemini using
the existing GEMINI_API_KEY; adding the Google key later must require no
code change (Google simply wins provider selection).
"""
import httpx
import pytest

from app.core import translation
from app.core.translation import (
    _translate_gemini,
    translate_for_retrieval,
)


class _FakeGeminiClient:
    """Stand-in for httpx.AsyncClient that returns a canned Gemini response."""

    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self.payload = payload
        self.posted_url: str | None = None
        self.posted_headers: dict | None = None
        self.posted_json: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self.posted_url = url
        self.posted_headers = kwargs.get("headers")
        self.posted_json = kwargs.get("json")
        return httpx.Response(self.status_code, json=self.payload, request=httpx.Request("POST", url))


def _gemini_payload(text: str = "ماذا قال مؤلف الشفاء؟") -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


class TestTranslateGemini:
    def test_returns_translation_text(self):
        client = _FakeGeminiClient(payload=_gemini_payload("ما قاله مؤلف الشفاء"))
        import asyncio
        result = asyncio.run(_translate_gemini(client, "hello", "en"))
        assert result == "ما قاله مؤلف الشفاء"
        assert client.posted_url.endswith("/v1/models/gemini-3.1-flash-lite:generateContent")
        assert client.posted_headers["x-goog-api-key"]
        assert "hello" in client.posted_json["contents"][0]["parts"][0]["text"]

    def test_empty_candidates_raises(self):
        client = _FakeGeminiClient(payload={"candidates": []})
        import asyncio
        with pytest.raises(ValueError):
            asyncio.run(_translate_gemini(client, "hello", "en"))

    def test_empty_translation_raises(self):
        client = _FakeGeminiClient(payload=_gemini_payload("   "))
        import asyncio
        with pytest.raises(ValueError):
            asyncio.run(_translate_gemini(client, "hello", "en"))

    def test_http_error_propagates(self):
        client = _FakeGeminiClient(status_code=500, payload={})
        import asyncio
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(_translate_gemini(client, "hello", "en"))


class TestProviderSelection:
    def test_arabic_text_unchanged(self, monkeypatch):
        monkeypatch.setattr(translation.settings, "GOOGLE_TRANSLATE_API_KEY", "")
        monkeypatch.setattr(translation.settings, "GEMINI_API_KEY", "")
        import asyncio
        assert asyncio.run(translate_for_retrieval("ما قاله المؤلف؟", "ar")) == "ما قاله المؤلف؟"

    def test_no_provider_returns_original(self, monkeypatch):
        monkeypatch.setattr(translation.settings, "GOOGLE_TRANSLATE_API_KEY", "")
        monkeypatch.setattr(translation.settings, "GEMINI_API_KEY", "")
        import asyncio
        assert asyncio.run(translate_for_retrieval("hello", "en")) == "hello"

    def test_google_key_wins(self, monkeypatch):
        """With a Google key set, the Google endpoint is used, not Gemini."""
        called = {"google": False, "gemini": False}
        import asyncio

        async def fake_translate_google(client, text, lang):
            called["google"] = True
            return "ترجمة"

        async def fake_translate_gemini(client, text, lang):
            called["gemini"] = True
            return "ترجمة"

        monkeypatch.setattr(translation.settings, "GOOGLE_TRANSLATE_API_KEY", "AIza-fake")
        monkeypatch.setattr(translation.settings, "GEMINI_API_KEY", "gemini-fake")
        monkeypatch.setattr(translation, "_translate_google", fake_translate_google)
        monkeypatch.setattr(translation, "_translate_gemini", fake_translate_gemini)
        result = asyncio.run(translate_for_retrieval("hello", "en"))
        assert result == "ترجمة"
        assert called == {"google": True, "gemini": False}

    def test_gemini_fallback_without_google_key(self, monkeypatch):
        import asyncio
        monkeypatch.setattr(translation.settings, "GOOGLE_TRANSLATE_API_KEY", "")
        monkeypatch.setattr(translation.settings, "GEMINI_API_KEY", "gemini-fake")
        client = _FakeGeminiClient(payload=_gemini_payload("ترجمة بالعربية"))
        monkeypatch.setattr(translation.httpx, "AsyncClient", lambda *a, **k: client)
        result = asyncio.run(translate_for_retrieval("hello", "en"))
        assert result == "ترجمة بالعربية"
        assert client.posted_url.endswith(":generateContent")

    def test_urdu_gets_translated(self, monkeypatch):
        import asyncio
        monkeypatch.setattr(translation.settings, "GOOGLE_TRANSLATE_API_KEY", "")
        monkeypatch.setattr(translation.settings, "GEMINI_API_KEY", "gemini-fake")
        client = _FakeGeminiClient(payload=_gemini_payload("ترجمة"))
        monkeypatch.setattr(translation.httpx, "AsyncClient", lambda *a, **k: client)
        result = asyncio.run(translate_for_retrieval("یہ کیا ہے؟", "ur"))
        assert result == "ترجمة"
