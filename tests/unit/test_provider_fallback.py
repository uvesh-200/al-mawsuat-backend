"""Unit tests for the multi-provider fallback chain (no live calls)."""
import pytest

from app.features.qa.agent import _build_providers, _chat_with_retry


class _FakeError(Exception):
    def __init__(self, status_code):
        super().__init__(f"fake error {status_code}")
        self.status_code = status_code


class _FakeProvider:
    def __init__(self, name, failures=(), text="answer"):
        self.name = name
        self.failures = list(failures)
        self.text = text
        self.calls = 0

    async def complete(self, messages, temperature, max_tokens):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.text


async def _run(messages=None, **kwargs):
    return await _chat_with_retry(messages or [{"role": "user", "content": "q"}], **kwargs)


@pytest.fixture
def monkeypatch_build(monkeypatch):
    def _patch(providers):
        monkeypatch.setattr("app.features.qa.llm._build_providers", lambda: providers)
        return providers
    return _patch


async def test_first_provider_success_no_fallback(monkeypatch_build):
    first = _FakeProvider("groq", text="ok")
    second = _FakeProvider("gemini", text="fallback")
    monkeypatch_build([first, second])
    result = await _run()
    assert result == "ok"
    assert first.calls == 1
    assert second.calls == 0


async def test_falls_through_after_provider_failure(monkeypatch_build):
    first = _FakeProvider("groq", failures=[_FakeError(402)], text="ok")
    second = _FakeProvider("gemini", text="fallback")
    monkeypatch_build([first, second])
    result = await _run()
    assert result == "fallback"
    assert first.calls == 1
    assert second.calls == 1


async def test_all_providers_fail_raises_last_error(monkeypatch_build):
    first = _FakeProvider("groq", failures=[_FakeError(402)])
    second = _FakeProvider("gemini", failures=[_FakeError(500)])
    monkeypatch_build([first, second])
    with pytest.raises(_FakeError) as exc_info:
        await _run()
    assert exc_info.value.status_code == 500


async def test_empty_completion_moves_to_next_provider(monkeypatch_build):
    first = _FakeProvider("groq", text="")
    second = _FakeProvider("gemini", text="fallback")
    monkeypatch_build([first, second])
    result = await _run()
    assert result == "fallback"


async def test_retries_429_within_provider_then_succeeds(monkeypatch_build, monkeypatch):
    async def _noop_sleep(seconds):
        pass
    monkeypatch.setattr("asyncio.sleep", _noop_sleep)
    retry = _FakeProvider("groq", failures=[_FakeError(429), _FakeError(429)], text="late")
    monkeypatch_build([retry])
    result = await _run()
    assert result == "late"
    assert retry.calls == 3


async def test_no_providers_configured_raises(monkeypatch_build):
    monkeypatch_build([])
    with pytest.raises(RuntimeError, match="No LLM provider"):
        await _run()


class TestProviderChainFiltering:
    """Bug 5: LLM_PROVIDERS must exclude providers even when keys are set."""

    def test_deepseek_skipped_when_not_listed(self, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.GROQ_API_KEY", "gsk_test")
        monkeypatch.setattr("app.core.config.settings.DEEPSEEK_API_KEY", "sk_dead")
        monkeypatch.setattr("app.core.config.settings.GEMINI_API_KEY", "gem-test")
        monkeypatch.setattr("app.core.config.settings.LLM_PROVIDERS", "groq,gemini")
        names = [p.name for p in _build_providers()]
        assert names == ["groq", "gemini"]
        assert "deepseek" not in names

    def test_order_follows_llm_providers(self, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.GROQ_API_KEY", "gsk_test")
        monkeypatch.setattr("app.core.config.settings.DEEPSEEK_API_KEY", "sk_test")
        monkeypatch.setattr("app.core.config.settings.GEMINI_API_KEY", "gem-test")
        monkeypatch.setattr("app.core.config.settings.LLM_PROVIDERS", "gemini,groq")
        names = [p.name for p in _build_providers()]
        assert names == ["gemini", "groq"]

    def test_deepseek_runs_only_when_explicitly_listed(self, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.GROQ_API_KEY", "gsk_test")
        monkeypatch.setattr("app.core.config.settings.DEEPSEEK_API_KEY", "sk_test")
        monkeypatch.setattr("app.core.config.settings.GEMINI_API_KEY", "gem-test")
        monkeypatch.setattr("app.core.config.settings.LLM_PROVIDERS", "groq,deepseek,gemini")
        names = [p.name for p in _build_providers()]
        assert names == ["groq", "deepseek", "gemini"]

    def test_provider_without_key_not_built_even_if_listed(self, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.GROQ_API_KEY", "")
        monkeypatch.setattr("app.core.config.settings.GEMINI_API_KEY", "gem-test")
        monkeypatch.setattr("app.core.config.settings.LLM_PROVIDERS", "groq,gemini")
        names = [p.name for p in _build_providers()]
        assert names == ["gemini"]
