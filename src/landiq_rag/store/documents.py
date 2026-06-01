"""Document + chunk repository.

Idempotency (F10): one logical document per (address_id, upload_id); content_hash
gates the unchanged-skip path; a changed document writes a new doc_version and the
live_version pointer is flipped atomically so a reader never sees a half-replaced
document. Removal (F11) cascades to chunks and, through them, to every model's
embeddings.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg


@dataclass
class ChunkRow:
    text: str
    ordinal: int
    page_number: int | None
    paragraph_index: int | None
    char_start: int | None
    char_end: int | None
    token_count: int | None


@dataclass
class DocumentRef:
    document_id: str
    upload_id: str
    address_id: str
    content_hash: str
    live_version: int
    is_new: bool
    next_version: int


async def ensure_address(conn: psycopg.AsyncConnection, address_id: str, display_name: str) -> None:
    await conn.execute(
        "INSERT INTO address (address_id, display_name) VALUES (%s, %s) "
        "ON CONFLICT (address_id) DO NOTHING",
        (address_id, display_name),
    )


async def get_document_by_upload(
    conn: psycopg.AsyncConnection, address_id: str, upload_id: str
) -> DocumentRef | None:
    row = await (
        await conn.execute(
            "SELECT document_id, content_hash, live_version FROM document "
            "WHERE address_id = %s AND upload_id = %s",
            (address_id, upload_id),
        )
    ).fetchone()
    if row is None:
        return None
    return DocumentRef(
        document_id=str(row[0]),
        upload_id=upload_id,
        address_id=address_id,
        content_hash=row[1],
        live_version=row[2],
        is_new=False,
        next_version=0,
    )


async def upsert_document(
    conn: psycopg.AsyncConnection,
    *,
    address_id: str,
    upload_id: str,
    content_type: str,
    content_hash: str,
    storage_ref: str,
    original_name: str | None,
) -> DocumentRef:
    """Create or update the document row; compute the next doc_version to write into."""
    existing = await get_document_by_upload(conn, address_id, upload_id)
    if existing is None:
        document_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO document
                (document_id, address_id, upload_id, content_type, content_hash,
                 storage_ref, original_name, live_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 0)
            """,
            (document_id, address_id, upload_id, content_type, content_hash,
             storage_ref, original_name),
        )
        return DocumentRef(document_id, upload_id, address_id, content_hash, 0, True, 1)

    next_version = existing.live_version + 1
    await conn.execute(
        "UPDATE document SET content_type = %s, content_hash = %s, storage_ref = %s, "
        "original_name = %s, ingest_ts = now() WHERE document_id = %s",
        (content_type, content_hash, storage_ref, original_name, existing.document_id),
    )
    existing.content_hash = content_hash
    existing.next_version = next_version
    return existing


async def insert_chunks(
    conn: psycopg.AsyncConnection,
    *,
    document_id: str,
    address_id: str,
    doc_version: int,
    chunks: list[ChunkRow],
) -> list[str]:
    """Insert chunks with client-generated ids (so embeddings can reference them)."""
    chunk_ids = [str(uuid.uuid4()) for _ in chunks]
    async with conn.cursor() as cur:
        await cur.executemany(
            """
            INSERT INTO chunk
                (chunk_id, document_id, address_id, doc_version, ordinal,
                 page_number, paragraph_index, char_start, char_end, text, token_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (cid, document_id, address_id, doc_version, c.ordinal, c.page_number,
                 c.paragraph_index, c.char_start, c.char_end, c.text, c.token_count)
                for cid, c in zip(chunk_ids, chunks)
            ],
        )
    return chunk_ids


async def set_live_version(conn: psycopg.AsyncConnection, document_id: str, version: int) -> None:
    await conn.execute(
        "UPDATE document SET live_version = %s WHERE document_id = %s", (version, document_id)
    )


async def delete_other_versions(
    conn: psycopg.AsyncConnection, document_id: str, keep_version: int
) -> None:
    """Drop superseded chunks (embeddings cascade) so only the live version remains."""
    await conn.execute(
        "DELETE FROM chunk WHERE document_id = %s AND doc_version <> %s",
        (document_id, keep_version),
    )


async def delete_document(conn: psycopg.AsyncConnection, document_id: str) -> bool:
    """Remove a document and all derived chunks/embeddings (F11)."""
    cur = await conn.execute("DELETE FROM document WHERE document_id = %s", (document_id,))
    return cur.rowcount > 0


async def live_chunks(
    conn: psycopg.AsyncConnection, document_id: str, doc_version: int
) -> list[tuple[str, str]]:
    """Return (chunk_id, text) for a document version, in order (for embedding/rebuild)."""
    rows = await (
        await conn.execute(
            "SELECT chunk_id, text FROM chunk WHERE document_id = %s AND doc_version = %s "
            "ORDER BY ordinal",
            (document_id, doc_version),
        )
    ).fetchall()
    return [(str(r[0]), r[1]) for r in rows]
