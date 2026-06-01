"""FastAPI entrypoint.

Lifespan: run migrations, open the pgvector-registered pool, seed runtime config,
ensure the active model's embedding table exists, then start the ingestion worker.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from .config import get_settings
from .context import ACTIVE_MODEL_KEY, AppContext
from .store.db import ensure_embedding_table, get_config, make_pool, run_migrations, set_config
from .store.files import make_storage

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    if settings.run_migrations_on_startup:
        applied = await run_migrations(settings.database_url)
        if applied:
            log.info("migrations.applied", files=applied)

    pool = await make_pool(settings.database_url)

    async with pool.connection() as conn:
        # Seed runtime config from settings only if absent (DB stays authoritative).
        if await get_config(conn, ACTIVE_MODEL_KEY) is None:
            await set_config(conn, ACTIVE_MODEL_KEY, settings.active_embedding_model)
        if await get_config(conn, "chunk_tokens") is None:
            await set_config(conn, "chunk_tokens", str(settings.chunk_tokens))
        if await get_config(conn, "chunk_overlap") is None:
            await set_config(conn, "chunk_overlap", str(settings.chunk_overlap))

        active = await get_config(conn, ACTIVE_MODEL_KEY)
        from .embedding.registry import make_provider

        provider = make_provider(active, settings)
        await ensure_embedding_table(conn, provider.model_id, provider.dimension)

    from .store.queue import make_queue

    ctx = AppContext(
        settings=settings,
        pool=pool,
        storage=make_storage(settings),
        queue=make_queue(settings, pool),
    )
    app.state.ctx = ctx

    worker_task: asyncio.Task | None = None
    if settings.worker_enabled:
        from .ingest.worker import run_worker

        worker_task = asyncio.create_task(run_worker(ctx), name="ingest-worker")
        log.info("worker.started")

    try:
        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        await pool.close()


def create_app() -> FastAPI:
    app = FastAPI(title="LandIQ RAG", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        ctx: AppContext = app.state.ctx
        async with ctx.pool.connection() as conn:
            await conn.execute("SELECT 1")
            active = await get_config(conn, ACTIVE_MODEL_KEY)
        return {"status": "ok", "active_embedding_model": active}

    from .api.routes_admin import router as admin_router
    from .api.routes_ingest import router as ingest_router
    from .api.routes_query import router as query_router

    app.include_router(ingest_router)
    app.include_router(query_router)
    app.include_router(admin_router)
    return app


app = create_app()
