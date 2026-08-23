"""LLM provider chain: OpenAI-compatible + Gemini, tried in order."""
import asyncio
import logging

import httpx
from openai import AsyncOpenAI

from app.core.config import settings

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
