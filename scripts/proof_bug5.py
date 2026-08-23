"""BUG 5 proof: show the built provider chain (DeepSeek skipped) and the
success log line naming the provider that produced a real answer."""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

from app.core.config import settings
from app.features.qa.agent import _build_providers, _chat_with_retry


async def main() -> None:
    print("=== settings ===")
    print(f"LLM_PROVIDERS={settings.LLM_PROVIDERS!r}")
    print(f"GROQ_API_KEY set: {bool(settings.GROQ_API_KEY)}")
    print(f"DEEPSEEK_API_KEY set: {bool(settings.DEEPSEEK_API_KEY)}")
    print(f"GEMINI_API_KEY set: {bool(settings.GEMINI_API_KEY)}")
    print("=== active chain ===")
    chain = _build_providers()
    print("chain:", [p.name for p in chain])
    print("=== real generation call ===")
    answer = await _chat_with_retry(
        messages=[{"role": "user", "content": "Reply with exactly: proof-ok"}],
        temperature=0.1,
        max_tokens=20,
    )
    print("answer:", answer)


asyncio.run(main())
