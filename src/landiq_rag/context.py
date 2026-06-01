"""Shared application context, held on app.state.ctx and passed to the worker."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from .config import Settings
from .embedding.registry import make_provider
from .ports import EmbeddingProvider, JobQueue, StorageBackend
from .store.db import get_config

ACTIVE_MODEL_KEY = "active_embedding_model"


@dataclass
class AppContext:
    settings: Settings
    pool: AsyncConnectionPool
    storage: StorageBackend
    queue: JobQueue

    async def active_model_id(self) -> str:
        async with self.pool.connection() as conn:
            value = await get_config(conn, ACTIVE_MODEL_KEY)
        return value or self.settings.active_embedding_model

    def provider_for(self, model_id: str) -> EmbeddingProvider:
        return make_provider(model_id, self.settings)

    async def active_provider(self) -> EmbeddingProvider:
        return self.provider_for(await self.active_model_id())
