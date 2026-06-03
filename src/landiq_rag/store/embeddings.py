"""Per-model embedding storage and address-scoped hybrid search.

Every retrieval goes through hybrid_search, which combines:
  - ANN vector search (pgvector cosine distance)
  - Full-text BM25 search (Postgres tsvector / GIN index)
  - Reciprocal Rank Fusion (RRF) to merge the two rankings

The mandatory address filter (isolation, PRD 8.1) and single-model table
selection (F14) are enforced inside this function — no caller can bypass them.
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


async def hybrid_search(
    conn: psycopg.AsyncConnection,
    *,
    table: str,
    address_id: str,
    query_vector: list[float],
    query_text: str,
    k: int,
    rrf_k: int = 60,
) -> tuple[list[dict], int]:
    """Hybrid search: vector ANN + BM25 full-text, fused with RRF.

    Returns (ranked rows, candidates examined).

    The result field `score` is an RRF fusion score — not cosine similarity.
    Max value ≈ 1/(1+rrf_k) * 2 ≈ 0.033 at default rrf_k=60. Do not threshold
    as if it were cosine in [0,1].

    Falls back gracefully to pure vector search when the query produces no
    FTS matches (e.g. very short or stop-word-only queries).

    Q2 fix: chunks that match BM25 but have no embedding in the active model's
    table (e.g. during a rebuild) are included via LEFT JOIN rather than dropped.
    """
    cur = conn.cursor(row_factory=dict_row)
    qv = _vec_literal(query_vector)
    rrf_limit = k * 4

    await cur.execute(
        f"""
        WITH
        -- ── Vector search ───────────────────────────────────────────────────
        vec AS (
            SELECT e.chunk_id,
                   ROW_NUMBER() OVER (ORDER BY e.embedding <=> %(qv)s::vector) AS rank
            FROM {table} e
            JOIN chunk    c ON c.chunk_id   = e.chunk_id
            JOIN document d ON d.document_id = c.document_id
            WHERE e.address_id = %(addr)s
              AND c.doc_version = d.live_version
            ORDER BY e.embedding <=> %(qv)s::vector
            LIMIT %(rrf_limit)s
        ),
        -- ── Full-text (BM25) search ─────────────────────────────────────────
        fts AS (
            SELECT c.chunk_id,
                   ROW_NUMBER() OVER (
                       ORDER BY ts_rank_cd(c.fts, plainto_tsquery('english', %(qt)s)) DESC
                   ) AS rank
            FROM chunk    c
            JOIN document d ON d.document_id = c.document_id
            WHERE c.address_id = %(addr)s
              AND c.doc_version = d.live_version
              AND c.fts @@ plainto_tsquery('english', %(qt)s)
            ORDER BY ts_rank_cd(c.fts, plainto_tsquery('english', %(qt)s)) DESC
            LIMIT %(rrf_limit)s
        ),
        -- ── RRF fusion ──────────────────────────────────────────────────────
        rrf AS (
            SELECT
                COALESCE(vec.chunk_id, fts.chunk_id) AS chunk_id,
                COALESCE(1.0 / (vec.rank + %(rrf_k)s), 0)
                + COALESCE(1.0 / (fts.rank + %(rrf_k)s), 0) AS score
            FROM vec
            FULL OUTER JOIN fts ON vec.chunk_id = fts.chunk_id
        )
        -- ── Fetch full rows for top-k winners ───────────────────────────────
        -- LEFT JOIN on the embedding table so FTS-only chunks (no embedding yet
        -- in the active model, e.g. mid-rebuild) are not silently dropped (Q2).
        SELECT
            c.chunk_id, c.text, c.document_id, d.original_name, d.storage_ref,
            c.page_number, c.paragraph_index, c.ordinal,
            COALESCE(e.model_id, '') AS model_id,
            rrf.score
        FROM rrf
        JOIN chunk    c ON c.chunk_id    = rrf.chunk_id
        JOIN document d ON d.document_id = c.document_id
        LEFT JOIN {table}  e ON e.chunk_id    = rrf.chunk_id
        ORDER BY rrf.score DESC
        LIMIT %(k)s
        """,
        {"qv": qv, "addr": address_id, "qt": query_text,
         "k": k, "rrf_limit": rrf_limit, "rrf_k": rrf_k},
    )
    results = await cur.fetchall()

    # Count only live embeddings for this address (candidates actually indexed).
    examined = await (
        await conn.execute(
            f"""
            SELECT count(*) FROM {table} e
            JOIN chunk    c ON c.chunk_id   = e.chunk_id
            JOIN document d ON d.document_id = c.document_id
            WHERE e.address_id = %s AND c.doc_version = d.live_version
            """,
            (address_id,),
        )
    ).fetchone()

    return results, examined[0]


async def ann_search(
    conn: psycopg.AsyncConnection,
    *,
    table: str,
    address_id: str,
    query_vector: list[float],
    k: int,
) -> tuple[list[dict], int]:
    """Pure vector search (no FTS). Delegates to hybrid_search with empty text."""
    return await hybrid_search(
        conn,
        table=table,
        address_id=address_id,
        query_vector=query_vector,
        query_text="",
        k=k,
    )
