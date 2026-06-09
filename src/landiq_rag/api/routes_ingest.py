"""Ingest, document removal, and ingestion-status endpoints."""

from __future__ import annotations

import hashlib

import structlog
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from psycopg.rows import dict_row

from ..context import ACTIVE_MODEL_KEY, AppContext
from ..ports import IngestJobSpec
from ..store import tasks
from ..store.db import get_config, get_model_table
from ..store.documents import (
    delete_document,
    ensure_address,
    get_document_by_upload,
    upsert_document,
)
from ..store.embeddings import count_missing_for_address
from .schemas import AddressIngestionStatus, DeleteResponse, IngestAccepted, JobStatus

router = APIRouter(tags=["ingest"])
log = structlog.get_logger()

_JOB_COLUMNS = (
    "task_id, address_id, upload_id, document_id, doc_version, job_type, state, model_id, "
    "chunk_count, embedding_count, attempts, failed_step, error_type, error_message, cost_usd"
)


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _job_status(row: dict) -> JobStatus:
    return JobStatus(
        task_id=str(row["task_id"]),
        address_id=row["address_id"],
        upload_id=row["upload_id"],
        document_id=str(row["document_id"]) if row["document_id"] else None,
        doc_version=row["doc_version"],
        job_type=row["job_type"],
        state=str(row["state"]),
        model_id=row["model_id"],
        chunk_count=row["chunk_count"],
        embedding_count=row["embedding_count"],
        attempts=row["attempts"],
        failed_step=row["failed_step"],
        error_type=row["error_type"],
        error_message=row["error_message"],
        cost_usd=float(row["cost_usd"] or 0),
    )


@router.post(
    "/addresses/{address_id}/documents", status_code=202, response_model=IngestAccepted
)
async def ingest_document(
    address_id: str,
    request: Request,
    file: UploadFile = File(...),
    upload_id: str | None = Form(default=None),
    display_name: str | None = Form(default=None),
) -> IngestAccepted:
    ctx = _ctx(request)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    content_hash = hashlib.sha256(data).hexdigest()
    content_type = file.content_type or "application/pdf"
    upload_id = upload_id or file.filename or content_hash[:16]

    async with ctx.pool.connection() as conn:
        await ensure_address(conn, address_id, display_name or address_id)
        existing = await get_document_by_upload(conn, address_id, upload_id)
        active_model = await get_config(conn, ACTIVE_MODEL_KEY) or ctx.settings.active_embedding_model

    # Unchanged content: record a terminal skipped job, no work, no duplicates (F10).
    if existing and existing.content_hash == content_hash:
        spec = IngestJobSpec(
            document_id=existing.document_id,
            address_id=address_id,
            upload_id=upload_id,
            content_hash=content_hash,
            model_id=active_model,
            doc_version=existing.live_version,
        )
        task_id = await tasks.record_skipped(ctx.pool, spec)
        return IngestAccepted(
            task_id=task_id,
            document_id=existing.document_id,
            address_id=address_id,
            upload_id=upload_id,
            doc_version=existing.live_version,
            state="skipped",
            model_id=active_model,
        )

    # New or changed: persist bytes, upsert document, enqueue async ingestion (F13).
    # Storage is network I/O under the S3 backend; surface an unavailable backend
    # as a clean 503 rather than a bare 500, and never create the document row
    # when its bytes did not land.
    try:
        storage_ref = await ctx.storage.put(
            address_id=address_id, upload_id=upload_id, data=data, content_type=content_type
        )
    except Exception as exc:  # noqa: BLE001
        log.error("storage.put_failed", address_id=address_id, upload_id=upload_id, error=str(exc))
        raise HTTPException(
            status_code=503, detail="document storage backend unavailable"
        ) from exc
    async with ctx.pool.connection() as conn:
        docref = await upsert_document(
            conn,
            address_id=address_id,
            upload_id=upload_id,
            content_type=content_type,
            content_hash=content_hash,
            storage_ref=storage_ref,
            original_name=file.filename,
        )
    spec = IngestJobSpec(
        document_id=docref.document_id,
        address_id=address_id,
        upload_id=upload_id,
        content_hash=content_hash,
        model_id=active_model,
        doc_version=docref.next_version,
    )
    task_id = await ctx.queue.enqueue(spec)
    return IngestAccepted(
        task_id=task_id,
        document_id=docref.document_id,
        address_id=address_id,
        upload_id=upload_id,
        doc_version=docref.next_version,
        state="pending",
        model_id=active_model,
    )


@router.delete("/documents/{document_id}", response_model=DeleteResponse)
async def remove_document(document_id: str, request: Request) -> DeleteResponse:
    ctx = _ctx(request)
    async with ctx.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT storage_ref FROM document WHERE document_id = %s", (document_id,)
            )
        ).fetchone()
        deleted = await delete_document(conn, document_id)

    # Drop the raw bytes too so removal does not leak storage (F11). Best-effort
    # and after the DB delete: a storage failure must not resurrect the document
    # (an orphaned object is recoverable; a dangling document row is not).
    if deleted and row is not None:
        try:
            await ctx.storage.delete(row[0])
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "storage.delete_failed", document_id=document_id,
                storage_ref=row[0], error=str(exc),
            )

    return DeleteResponse(document_id=document_id, deleted=deleted)


@router.get("/ingestion/{task_id}", response_model=JobStatus)
async def ingestion_status(task_id: str, request: Request) -> JobStatus:
    ctx = _ctx(request)
    async with ctx.pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            f"SELECT {_JOB_COLUMNS} FROM ingest_job WHERE task_id = %s", (task_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown task_id")
    return _job_status(row)


@router.get(
    "/addresses/{address_id}/ingestion-status", response_model=AddressIngestionStatus
)
async def address_status(address_id: str, request: Request) -> AddressIngestionStatus:
    ctx = _ctx(request)
    active_model = await ctx.active_model_id()

    async with ctx.pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            f"SELECT {_JOB_COLUMNS} FROM ingest_job WHERE address_id = %s ORDER BY created_at DESC",
            (address_id,),
        )
        jobs = [_job_status(r) for r in await cur.fetchall()]

        doc_count = (
            await (
                await conn.execute(
                    "SELECT count(*) FROM document WHERE address_id = %s", (address_id,)
                )
            ).fetchone()
        )[0]

        table_dim = await get_model_table(conn, active_model)
        if table_dim is not None:
            missing = await count_missing_for_address(
                conn, table=table_dim[0], address_id=address_id
            )
        else:
            missing = (
                await (
                    await conn.execute(
                        "SELECT count(*) FROM chunk c JOIN document d "
                        "ON d.document_id = c.document_id "
                        "WHERE c.address_id = %s AND c.doc_version = d.live_version",
                        (address_id,),
                    )
                ).fetchone()
            )[0]

    in_flight = sum(
        1 for j in jobs if j.state in ("pending", "extracting", "chunking", "embedding")
    )
    failed = sum(1 for j in jobs if j.state == "failed")
    return AddressIngestionStatus(
        address_id=address_id,
        active_model=active_model,
        document_count=doc_count,
        has_documents=doc_count > 0,
        in_flight=in_flight,
        failed=failed,
        missing_embeddings=missing,
        fully_indexed=in_flight == 0 and failed == 0 and missing == 0,
        jobs=jobs,
    )
