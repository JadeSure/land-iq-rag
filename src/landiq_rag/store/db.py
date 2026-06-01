"""Database access: async pool, migration runner, and per-model embedding tables.

The per-model embedding table is the structural guarantee for F14 (a query
against one model's table physically cannot return another model's vectors) and
for differing dimensions (1536 vs 3072 vs a self-hosted model's dim).
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector_async
from psycopg_pool import AsyncConnectionPool

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

_TABLE_SAFE = re.compile(r"[^a-z0-9]+")


def sanitise_model_table(model_id: str) -> str:
    """'openai:text-embedding-3-small' -> 'embedding_openai_text_embedding_3_small'."""
    slug = _TABLE_SAFE.sub("_", model_id.lower()).strip("_")
    return f"embedding_{slug}"


async def run_migrations(dsn: str) -> list[str]:
    """Apply every migrations/*.sql not yet recorded, in filename order.

    Runs on a standalone connection before the pgvector-registered pool opens,
    so the ``vector`` extension exists before any vector type is registered.
    """
    applied: list[str] = []
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {
            row[0]
            async for row in (await conn.execute("SELECT filename FROM schema_migrations"))
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            sql = path.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)  # type: ignore[arg-type]
                await conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                )
            applied.append(path.name)
    return applied


async def make_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    async def _configure(conn: psycopg.AsyncConnection) -> None:
        await register_vector_async(conn)

    pool = AsyncConnectionPool(
        conninfo=dsn, min_size=min_size, max_size=max_size, open=False, configure=_configure
    )
    await pool.open(wait=True)
    return pool


async def ensure_embedding_table(conn: psycopg.AsyncConnection, model_id: str, dim: int) -> str:
    """Create the per-model embedding table + indexes if absent; register it.

    HNSW indexing supports up to 2000 dimensions; for larger models we create the
    table without the ANN index (exact scan, fine at this data volume).
    """
    table = sanitise_model_table(model_id)
    await conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            chunk_id   UUID NOT NULL REFERENCES chunk(chunk_id) ON DELETE CASCADE,
            address_id TEXT NOT NULL,
            model_id   TEXT NOT NULL,
            dim        INT  NOT NULL,
            embedding  vector({dim}) NOT NULL,
            PRIMARY KEY (chunk_id)
        )
        """
    )
    await conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_addr ON {table}(address_id)")
    if dim <= 2000:
        await conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_hnsw ON {table} "
            f"USING hnsw (embedding vector_cosine_ops)"
        )
    await conn.execute(
        "INSERT INTO embedding_model (model_id, dim, table_name) VALUES (%s, %s, %s) "
        "ON CONFLICT (model_id) DO NOTHING",
        (model_id, dim, table),
    )
    return table


async def get_model_table(conn: psycopg.AsyncConnection, model_id: str) -> tuple[str, int] | None:
    row = await (
        await conn.execute(
            "SELECT table_name, dim FROM embedding_model WHERE model_id = %s", (model_id,)
        )
    ).fetchone()
    return (row[0], row[1]) if row else None


async def get_config(conn: psycopg.AsyncConnection, key: str) -> str | None:
    row = await (
        await conn.execute("SELECT value FROM rag_config WHERE key = %s", (key,))
    ).fetchone()
    return row[0] if row else None


async def set_config(conn: psycopg.AsyncConnection, key: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO rag_config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, value),
    )
