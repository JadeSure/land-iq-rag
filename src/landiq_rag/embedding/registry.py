"""Resolve a model_id string to an EmbeddingProvider instance.

The active model_id lives in rag_config (admin-editable, F8). The registry maps
its provider prefix to a concrete implementation so adding a provider needs no
change at any call site.
"""

from __future__ import annotations

from ..config import Settings
from ..ports import EmbeddingProvider
from .providers import (
    FeatureHashEmbeddingProvider,
    GeminiEmbeddingProvider,
    HuggingFaceEmbeddingProvider,
    OpenAIEmbeddingProvider,
    SelfHostedEmbeddingProvider,
)


def make_provider(model_id: str, settings: Settings) -> EmbeddingProvider:
    provider, _, rest = model_id.partition(":")
    if provider == "hash":
        dim = int(rest.rsplit("-", 1)[-1]) if "-" in rest else 256
        return FeatureHashEmbeddingProvider(dim=dim)
    if provider == "openai":
        return OpenAIEmbeddingProvider(rest, api_key=settings.openai_api_key)
    if provider == "selfhosted":
        return SelfHostedEmbeddingProvider(
            rest, base_url=settings.selfhosted_url, dim=settings.selfhosted_dim
        )
    if provider == "hf":
        return HuggingFaceEmbeddingProvider(rest)
    if provider == "gemini":
        return GeminiEmbeddingProvider(rest, api_key=settings.gemini_api_key)
    raise ValueError(f"Unknown embedding provider in model_id: {model_id!r}")
