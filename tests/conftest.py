"""Test harness: a fresh schema per test, the app run with its real lifespan
(migrations + pgvector pool + ingestion worker), and an httpx client over ASGI.
"""

from __future__ import annotations

import asyncio
import os

# Speed up the worker poll for tests; must be set before settings are read.
os.environ.setdefault("RAG_WORKER_POLL_SECONDS", "0.1")
os.environ.setdefault("RAG_WORKER_BATCH", "4")
os.environ.setdefault("RAG_ACTIVE_EMBEDDING_MODEL", "hash:feature-256")
# Pin storage to the local filesystem so the suite is independent of the dev's
# .env (which may select the S3/MinIO backend). os.environ wins over .env.
os.environ.setdefault("RAG_STORAGE_BACKEND", "local")

import httpx  # noqa: E402
import psycopg  # noqa: E402
import pytest_asyncio  # noqa: E402

from landiq_rag.config import get_settings  # noqa: E402
from landiq_rag.main import create_app  # noqa: E402


@pytest_asyncio.fixture
async def client():
    get_settings.cache_clear()
    settings = get_settings()
    async with await psycopg.AsyncConnection.connect(
        settings.database_url, autocommit=True
    ) as conn:
        await conn.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def upload(client, address_id, *, text=None, data=None, content_type="text/plain",
                 filename="doc.txt", upload_id=None):
    body = data if data is not None else text.encode("utf-8")
    form = {}
    if upload_id is not None:
        form["upload_id"] = upload_id
    resp = await client.post(
        f"/addresses/{address_id}/documents",
        files={"file": (filename, body, content_type)},
        data=form,
    )
    return resp


async def wait_terminal(client, task_id, timeout=20.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/ingestion/{task_id}")
        body = r.json()
        if body["state"] in ("done", "skipped", "failed"):
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"task {task_id} did not finish within {timeout}s")
