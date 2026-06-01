"""The ingest and rebuild job bodies (shared by local worker and AWS worker).

Ingest: extract -> chunk -> write chunks at a new doc_version -> embed -> flip
live_version atomically (no half-replaced read) -> done.
Rebuild: idempotently backfill missing embeddings under the target model, then
set it active (the old model keeps serving until backfill completes; F9, 8.5).
"""

from __future__ import annotations

from psycopg.rows import dict_row

from ..context import ACTIVE_MODEL_KEY, AppContext
from ..ports import ClaimedJob
from ..store import documents, embeddings, tasks
from ..store.db import ensure_embedding_table, set_config
from .chunk import chunk_segments
from .extract import extract_segments_with_fallback

_EMBED_BATCH = 128


class PipelineError(Exception):
    def __init__(self, step: str, cause: BaseException) -> None:
        super().__init__(f"{step}: {cause}")
        self.step = step
        self.cause = cause


def _batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def run_ingest_job(ctx: AppContext, job: ClaimedJob) -> None:
    pool = ctx.pool

    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT storage_ref, content_type FROM document WHERE document_id = %s",
                (job.document_id,),
            )
        ).fetchone()
    if row is None:
        # Document was removed before this job ran; nothing to index.
        await tasks.mark_done(pool, job.task_id, state="skipped")
        return
    storage_ref, content_type = row

    # Extract
    await tasks.set_state(pool, job.task_id, "extracting")
    try:
        data = await ctx.storage.get(storage_ref)
        segments = await extract_segments_with_fallback(
            data, content_type,
            anthropic_api_key=ctx.settings.anthropic_api_key,
            threshold=ctx.settings.pdf_fallback_threshold,
        )
    except Exception as exc:  # noqa: BLE001
        raise PipelineError("extract", exc) from exc

    # Chunk + persist chunks at the new version
    await tasks.set_state(pool, job.task_id, "chunking")
    try:
        chunks = chunk_segments(
            segments, chunk_tokens=ctx.settings.chunk_tokens, overlap=ctx.settings.chunk_overlap
        )
        async with pool.connection() as conn:
            await documents.insert_chunks(
                conn,
                document_id=job.document_id,
                address_id=job.address_id,
                doc_version=job.doc_version,
                chunks=chunks,
            )
    except Exception as exc:  # noqa: BLE001
        raise PipelineError("chunking", exc) from exc
    await tasks.set_progress(pool, job.task_id, chunk_count=len(chunks))

    # Embed
    await tasks.set_state(pool, job.task_id, "embedding")
    try:
        provider = ctx.provider_for(job.model_id)
        async with pool.connection() as conn:
            table = await ensure_embedding_table(conn, provider.model_id, provider.dimension)
            items = await documents.live_chunks(conn, job.document_id, job.doc_version)

        embedded = tokens = 0
        cost = 0.0
        for batch in _batched(items, _EMBED_BATCH):
            result = await provider.embed([text for _, text in batch])
            rows = list(zip([cid for cid, _ in batch], result.vectors))
            async with pool.connection() as conn:
                await embeddings.insert_embeddings(
                    conn,
                    table=table,
                    model_id=provider.model_id,
                    dim=provider.dimension,
                    address_id=job.address_id,
                    rows=rows,
                    on_conflict="update",
                )
            embedded += len(rows)
            tokens += result.tokens
            cost += result.cost_usd
            await tasks.set_progress(
                pool, job.task_id, embedding_count=embedded, tokens=tokens, cost_usd=cost
            )
    except PipelineError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PipelineError("embedding", exc) from exc

    # Atomic flip: make the new version live and drop superseded versions.
    try:
        async with pool.connection() as conn, conn.transaction():
            await documents.set_live_version(conn, job.document_id, job.doc_version)
            await documents.delete_other_versions(conn, job.document_id, job.doc_version)
    except Exception as exc:  # noqa: BLE001
        raise PipelineError("commit", exc) from exc

    await tasks.mark_done(pool, job.task_id, state="done")


async def run_rebuild_job(ctx: AppContext, job: ClaimedJob) -> None:
    """Backfill missing embeddings under job.model_id, then set it active.

    Idempotent (ON CONFLICT DO NOTHING) so an interrupted rebuild resumes by
    re-selecting only the still-missing chunks. The currently-active model keeps
    serving until this completes and flips the active pointer.
    """
    pool = ctx.pool
    provider = ctx.provider_for(job.model_id)
    scope_all = job.address_id == "*"

    async with pool.connection() as conn:
        table = await ensure_embedding_table(conn, provider.model_id, provider.dimension)

    where = "c.doc_version = d.live_version" + ("" if scope_all else " AND c.address_id = %(addr)s")
    params = {} if scope_all else {"addr": job.address_id}

    await tasks.set_state(pool, job.task_id, "embedding")
    try:
        async with pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"""
                SELECT c.chunk_id, c.address_id, c.text
                  FROM chunk c
                  JOIN document d ON d.document_id = c.document_id
             LEFT JOIN {table} e ON e.chunk_id = c.chunk_id
                 WHERE {where} AND e.chunk_id IS NULL
                """,
                params,
            )
            missing = await cur.fetchall()

        embedded = tokens = 0
        cost = 0.0
        await tasks.set_progress(pool, job.task_id, chunk_count=len(missing))
        for batch in _batched(missing, _EMBED_BATCH):
            result = await provider.embed([r["text"] for r in batch])
            # group rows by address_id for the per-row address column
            async with pool.connection() as conn:
                for (r, vec) in zip(batch, result.vectors):
                    await embeddings.insert_embeddings(
                        conn,
                        table=table,
                        model_id=provider.model_id,
                        dim=provider.dimension,
                        address_id=r["address_id"],
                        rows=[(str(r["chunk_id"]), vec)],
                        on_conflict="nothing",
                    )
            embedded += len(batch)
            tokens += result.tokens
            cost += result.cost_usd
            await tasks.set_progress(
                pool, job.task_id, embedding_count=embedded, tokens=tokens, cost_usd=cost
            )
    except Exception as exc:  # noqa: BLE001
        raise PipelineError("rebuild", exc) from exc

    # Flip the active model pointer once the backfill is complete.
    async with pool.connection() as conn:
        await set_config(conn, ACTIVE_MODEL_KEY, provider.model_id)
    await tasks.mark_done(pool, job.task_id, state="done")
