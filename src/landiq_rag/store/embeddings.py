"""Per-model embedding storage and the address-scoped ANN query.

Every retrieval goes through ann_search, which forces the mandatory address
filter (isolation is a correctness property, PRD 8.1) and targets exactly one
model's table (so a single ranking can never mix models, F14).
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row


def _vec_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(float(x)) for x in vector) + "]"


async def insert_embeddings(
    conn: psycopg.AsyncConnection,
    *,
    table: str,
    model_id: str,
    dim: int,
    address_id: str,
    rows: list[tuple[str, list[float]]],
    on_conflict: str = "update",
) -> int:
    """Insert (chunk_id, vector) rows into a per-model table.

    on_conflict='update' overwrites (re-ingest); 'nothing' is idempotent (rebuild
    resume). Vectors are sent as literals with an explicit ::vector cast so this
    works without a numpy dependency.
    """
    if not rows:
        return 0
    conflict = (
        "DO UPDATE SET embedding = EXCLUDED.embedding, model_id = EXCLUDED.model_id, dim = EXCLUDED.dim"
        if on_conflict == "update"
        else "DO NOTHING"
    )
    async with conn.cursor() as cur:
        await cur.executemany(
            f"""
            INSERT INTO {table} (chunk_id, address_id, model_id, dim, embedding)
            VALUES (%s, %s, %s, %s, %s::vector)
            ON CONFLICT (chunk_id) {conflict}
            """,
            [(cid, address_id, model_id, dim, _vec_literal(vec)) for cid, vec in rows],
        )
    return len(rows)


async def count_missing_for_address(
    conn: psycopg.AsyncConnection, *, table: str, address_id: str
) -> int:
    """Live chunks for an address that lack a vector in this model's table."""
    row = await (
        await conn.execute(
            f"""
            SELECT count(*) FROM chunk c
            JOIN document d ON d.document_id = c.document_id
            LEFT JOIN {table} e ON e.chunk_id = c.chunk_id
            WHERE c.address_id = %s AND c.doc_version = d.live_version AND e.chunk_id IS NULL
            """,
            (address_id,),
        )
    ).fetchone()
    return row[0]


async def ann_search(
    conn: psycopg.AsyncConnection,
    *,
    table: str,
    address_id: str,
    query_vector: list[float],
    k: int,
) -> tuple[list[dict], int]:
    """Return (ranked rows with provenance, candidates_examined) for one address."""
    cur = conn.cursor(row_factory=dict_row)
    qv = _vec_literal(query_vector)
    await cur.execute(
        f"""
        SELECT e.chunk_id, c.text, c.document_id, d.original_name, d.storage_ref,
               c.page_number, c.paragraph_index, c.ordinal, e.model_id,
               1 - (e.embedding <=> %(qv)s::vector) AS cosine_similarity
          FROM {table} e
          JOIN chunk    c ON c.chunk_id   = e.chunk_id
          JOIN document d ON d.document_id = c.document_id
         WHERE e.address_id = %(addr)s
           AND c.doc_version = d.live_version
         ORDER BY e.embedding <=> %(qv)s::vector
         LIMIT %(k)s
        """,
        {"qv": qv, "addr": address_id, "k": k},
    )
    results = await cur.fetchall()

    examined = await (
        await conn.execute(
            f"SELECT count(*) FROM {table} WHERE address_id = %s", (address_id,)
        )
    ).fetchone()
    return results, examined[0]
