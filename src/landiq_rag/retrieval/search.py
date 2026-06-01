"""Retrieval: (query, address) -> ranked chunks with provenance.

This is the single chokepoint for the mandatory address filter (isolation, 8.1)
and the single-model table selection (F14). Every query logs latency, candidates
examined and returned count (8.3).
"""

from __future__ import annotations

import time

from ..context import AppContext
from ..store.db import get_model_table
from ..store.embeddings import ann_search


async def search(ctx: AppContext, *, address_id: str, query: str, k: int) -> dict:
    started = time.perf_counter()
    model_id = await ctx.active_model_id()
    provider = ctx.provider_for(model_id)

    results: list[dict] = []
    examined = 0
    cost = 0.0

    async with ctx.pool.connection() as conn:
        table_dim = await get_model_table(conn, provider.model_id)

    if table_dim is not None:
        table, _ = table_dim
        embedded = await provider.embed_query(query)
        cost = embedded.cost_usd
        async with ctx.pool.connection() as conn:
            results, examined = await ann_search(
                conn,
                table=table,
                address_id=address_id,
                query_vector=embedded.vectors[0],
                k=k,
            )

    latency_ms = int((time.perf_counter() - started) * 1000)

    async with ctx.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO query_log (address_id, model_id, latency_ms, candidates_examined, "
            "chunks_returned, cost_usd) VALUES (%s, %s, %s, %s, %s, %s)",
            (address_id, provider.model_id, latency_ms, examined, len(results), cost),
        )

    return {
        "model_id": provider.model_id,
        "latency_ms": latency_ms,
        "candidates_examined": examined,
        "results": results,
    }
