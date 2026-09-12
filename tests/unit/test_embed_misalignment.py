"""Fix 7: a Gemini batch embedding that returns fewer vectors than requested
must raise loudly instead of silently dropping chunks via zip(chunks, vectors).
"""
import httpx
import pytest

from app.core import embedder


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.request = httpx.Request("POST", "http://x")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=self.request, response=self)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.post_calls = 0

    async def post(self, *args, **kwargs):
        self.post_calls += 1
        return _FakeResp(200, self._payload)


async def test_embed_batch_raises_on_vector_count_mismatch(monkeypatch):
    # 3 texts requested, API returns only 2 embeddings -> must raise.
    client = _FakeClient(
        {
            "embeddings": [
                {"values": [0.1] * 10},
                {"values": [0.2] * 10},
            ]
        }
    )
    texts = ["chunk one", "chunk two", "chunk three"]

    with pytest.raises(ValueError, match="returned 2 vectors for 3 inputs"):
        await embedder._embed_batch(client, texts)

    # It must fail fast on the first attempt (no retries for a deterministic
    # count mismatch) and never return a silently truncated list.
    assert client.post_calls == 1


async def test_embed_batch_returns_all_when_counts_match(monkeypatch):
    client = _FakeClient(
        {
            "embeddings": [
                {"values": [0.1] * 10},
                {"values": [0.2] * 10},
                {"values": [0.3] * 10},
            ]
        }
    )
    texts = ["chunk one", "chunk two", "chunk three"]
    out = await embedder._embed_batch(client, texts)
    assert len(out) == 3
