"""ingest_job repository: the F12 status record and the local queue in one table.

Claiming uses FOR UPDATE SKIP LOCKED so multiple workers never double-process a
job, and reclaims in-flight jobs whose lease expired (crashed worker recovery).
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ..ports import ClaimedJob, IngestJobSpec

_IN_FLIGHT = ("extracting", "chunking", "embedding")


class PostgresJobQueue:
    def __init__(self, pool: AsyncConnectionPool, *, lease_seconds: int = 300) -> None:
        self.pool = pool
        self.lease_seconds = lease_seconds

    async def enqueue(self, spec: IngestJobSpec) -> str:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    """
                    INSERT INTO ingest_job
                        (document_id, address_id, upload_id, content_hash,
                         model_id, doc_version, job_type, state)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                    RETURNING task_id
                    """,
                    (
                        spec.document_id,
                        spec.address_id,
                        spec.upload_id,
                        spec.content_hash,
                        spec.model_id,
                        spec.doc_version,
                        spec.job_type,
                    ),
                )
            ).fetchone()
            return str(row[0])

    async def claim(self, *, worker_id: str, max_jobs: int) -> list[ClaimedJob]:
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"""
                UPDATE ingest_job j
                   SET state = CASE WHEN job_type = 'rebuild' THEN 'embedding'::ingest_state
                                    ELSE 'extracting'::ingest_state END,
                       worker_id = %(wid)s,
                       claimed_at = now(),
                       lease_until = now() + (%(lease)s || ' seconds')::interval,
                       attempts = attempts + 1
                  FROM (
                       SELECT task_id FROM ingest_job
                        WHERE state = 'pending'
                           OR (state IN {_IN_FLIGHT} AND lease_until < now())
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT %(lim)s
                  ) AS c
                 WHERE j.task_id = c.task_id
             RETURNING j.task_id, j.document_id, j.address_id, j.upload_id,
                       j.content_hash, j.model_id, j.doc_version, j.job_type,
                       j.attempts, j.max_attempts, j.state
                """,
                {"wid": worker_id, "lease": self.lease_seconds, "lim": max_jobs},
            )
            rows = await cur.fetchall()
        return [
            ClaimedJob(
                task_id=str(r["task_id"]),
                document_id=str(r["document_id"]) if r["document_id"] else None,
                address_id=r["address_id"],
                upload_id=r["upload_id"],
                content_hash=r["content_hash"],
                model_id=r["model_id"],
                doc_version=r["doc_version"],
                job_type=r["job_type"],
                attempts=r["attempts"],
                max_attempts=r["max_attempts"],
                state=str(r["state"]),
            )
            for r in rows
        ]

    async def heartbeat(self, task_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE ingest_job SET lease_until = now() + (%s || ' seconds')::interval "
                "WHERE task_id = %s",
                (self.lease_seconds, task_id),
            )


async def claim_by_id(
    pool: AsyncConnectionPool, task_id: str, *, worker_id: str, lease_seconds: int
) -> ClaimedJob | None:
    """Lease a specific job (SQS Lambda path). Returns None if already terminal.

    Safe under SQS at-least-once: a job already 'done'/'skipped'/'failed' is not
    re-claimed, so a redelivered message no-ops.
    """
    async with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            """
            UPDATE ingest_job j
               SET state = CASE WHEN job_type = 'rebuild' THEN 'embedding'::ingest_state
                                ELSE 'extracting'::ingest_state END,
                   worker_id = %(wid)s,
                   claimed_at = now(),
                   lease_until = now() + (%(lease)s || ' seconds')::interval,
                   attempts = attempts + 1
             WHERE j.task_id = %(tid)s
               AND j.state NOT IN ('done','skipped','failed')
         RETURNING j.task_id, j.document_id, j.address_id, j.upload_id,
                   j.content_hash, j.model_id, j.doc_version, j.job_type,
                   j.attempts, j.max_attempts, j.state
            """,
            {"wid": worker_id, "lease": lease_seconds, "tid": task_id},
        )
        r = await cur.fetchone()
    if r is None:
        return None
    return ClaimedJob(
        task_id=str(r["task_id"]),
        document_id=str(r["document_id"]) if r["document_id"] else None,
        address_id=r["address_id"],
        upload_id=r["upload_id"],
        content_hash=r["content_hash"],
        model_id=r["model_id"],
        doc_version=r["doc_version"],
        job_type=r["job_type"],
        attempts=r["attempts"],
        max_attempts=r["max_attempts"],
        state=str(r["state"]),
    )


async def record_skipped(
    pool: AsyncConnectionPool, spec: IngestJobSpec
) -> str:
    """Insert a terminal 'skipped' job for an unchanged re-ingest (F10, observable)."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                INSERT INTO ingest_job
                    (document_id, address_id, upload_id, content_hash, model_id,
                     doc_version, job_type, state, finished_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'skipped', now())
                RETURNING task_id
                """,
                (spec.document_id, spec.address_id, spec.upload_id, spec.content_hash,
                 spec.model_id, spec.doc_version, spec.job_type),
            )
        ).fetchone()
        return str(row[0])


# ── Worker-side state transitions ────────────────────────────────────────────
async def set_state(pool: AsyncConnectionPool, task_id: str, state: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE ingest_job SET state = %s::ingest_state WHERE task_id = %s", (state, task_id)
        )


async def set_progress(
    pool: AsyncConnectionPool,
    task_id: str,
    *,
    chunk_count: int | None = None,
    embedding_count: int | None = None,
    tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            """
            UPDATE ingest_job SET
                chunk_count     = COALESCE(%s, chunk_count),
                embedding_count = embedding_count + COALESCE(%s, 0),
                tokens_embedded = tokens_embedded + COALESCE(%s, 0),
                cost_usd        = cost_usd + COALESCE(%s, 0)
            WHERE task_id = %s
            """,
            (chunk_count, embedding_count, tokens, cost_usd, task_id),
        )


async def mark_done(pool: AsyncConnectionPool, task_id: str, state: str = "done") -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE ingest_job SET state = %s::ingest_state, finished_at = now(), "
            "error_type = NULL, error_message = NULL, failed_step = NULL WHERE task_id = %s",
            (state, task_id),
        )


async def mark_failed(
    pool: AsyncConnectionPool,
    job: ClaimedJob,
    *,
    step: str,
    error_type: str,
    message: str,
    retryable: bool,
) -> str:
    """Re-queue for retry if attempts remain and the error is transient, else fail.

    Returns the resulting state ('pending' or 'failed'). No silent failures (6.5).
    """
    will_retry = retryable and job.attempts < job.max_attempts
    new_state = "pending" if will_retry else "failed"
    async with pool.connection() as conn:
        await conn.execute(
            """
            UPDATE ingest_job SET
                state = %s::ingest_state,
                failed_step = %s, error_type = %s, error_message = %s,
                lease_until = NULL,
                finished_at = CASE WHEN %s = 'failed' THEN now() ELSE finished_at END
            WHERE task_id = %s
            """,
            (new_state, step, error_type, message[:2000], new_state, job.task_id),
        )
    return new_state
