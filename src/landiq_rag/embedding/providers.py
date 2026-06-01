"""EmbeddingProvider implementations.

FeatureHashEmbeddingProvider is a deterministic, zero-dependency local provider
(no API key, no network, no cost) that uses feature hashing so cosine similarity
still reflects lexical overlap. It makes the whole pipeline runnable and the
acceptance tests deterministic (PRD 8.4). OpenAIEmbeddingProvider is the PRD
default for substantive retrieval.
"""

from __future__ import annotations

import hashlib
import math
import re

from ..ports import EmbeddingResult

_TOKEN = re.compile(r"[a-z0-9]+")

# USD per 1M tokens (for per-ingest / per-query cost observability, PRD 8.2/8.3).
_OPENAI_RATES = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
}
_OPENAI_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


def _stable_hash(token: str, salt: str) -> int:
    return int.from_bytes(hashlib.blake2b((salt + token).encode(), digest_size=8).digest(), "big")


class FeatureHashEmbeddingProvider:
    """Deterministic feature-hashing embedder. model_id 'hash:feature-<dim>'."""

    def __init__(self, dim: int = 256) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return f"hash:feature-{self._dim}"

    @property
    def dimension(self) -> int:
        return self._dim

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for tok in _TOKEN.findall(text.lower()):
            idx = _stable_hash(tok, "idx") % self._dim
            sign = 1.0 if _stable_hash(tok, "sgn") & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult(vectors=[self._vector(t) for t in texts])

    async def embed_query(self, text: str) -> EmbeddingResult:
        return EmbeddingResult(vectors=[self._vector(text)])


class SelfHostedEmbeddingProvider:
    """Own-pipeline path: a self-hosted embedding server (e.g. HuggingFace TEI).

    Talks to TEI's /embed endpoint (POST {"inputs": [...]} -> [[...], ...]). No
    third-party egress; runs on the EC2 (or a GPU box) so client data stays in-VPC.
    model_id 'selfhosted:<name>'.
    """

    def __init__(self, name: str, base_url: str, dim: int) -> None:
        if not base_url:
            raise ValueError(f"RAG_SELFHOSTED_URL required for selfhosted:{name}")
        import httpx

        self._name = name
        self._dim = dim
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=60.0)

    @property
    def model_id(self) -> str:
        return f"selfhosted:{self._name}"

    @property
    def dimension(self) -> int:
        return self._dim

    async def _call(self, texts: list[str]) -> EmbeddingResult:
        resp = await self._client.post("/embed", json={"inputs": texts})
        resp.raise_for_status()
        return EmbeddingResult(vectors=resp.json())

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        return await self._call(texts)

    async def embed_query(self, text: str) -> EmbeddingResult:
        return await self._call([text])


class OpenAIEmbeddingProvider:
    """OpenAI text-embedding-* provider. model_id 'openai:<model_name>'."""

    def __init__(self, model_name: str, api_key: str, dim: int | None = None) -> None:
        if not api_key:
            raise ValueError(f"RAG_OPENAI_API_KEY required for openai:{model_name}")
        from openai import AsyncOpenAI

        self._name = model_name
        self._dim = dim or _OPENAI_DIMS.get(model_name, 1536)
        self._rate = _OPENAI_RATES.get(model_name, 0.0)
        self._client = AsyncOpenAI(api_key=api_key)

    @property
    def model_id(self) -> str:
        return f"openai:{self._name}"

    @property
    def dimension(self) -> int:
        return self._dim

    async def _call(self, texts: list[str]) -> EmbeddingResult:
        resp = await self._client.embeddings.create(model=self._name, input=texts)
        vectors = [item.embedding for item in resp.data]
        tokens = resp.usage.total_tokens if resp.usage else 0
        cost = tokens / 1_000_000 * self._rate
        return EmbeddingResult(vectors=vectors, tokens=tokens, cost_usd=cost)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        return await self._call(texts)

    async def embed_query(self, text: str) -> EmbeddingResult:
        return await self._call([text])
