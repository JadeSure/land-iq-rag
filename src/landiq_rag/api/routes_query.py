"""Query endpoint: address-scoped retrieval with provenance."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..context import AppContext
from ..retrieval.search import search
from .schemas import Citation, QueryRequest, QueryResponse, QueryResultItem

router = APIRouter(tags=["query"])


@router.post("/addresses/{address_id}/query", response_model=QueryResponse)
async def query_address(address_id: str, body: QueryRequest, request: Request) -> QueryResponse:
    ctx: AppContext = request.app.state.ctx
    k = body.k or ctx.settings.query_default_k
    outcome = await search(ctx, address_id=address_id, query=body.query, k=k)

    items = [
        QueryResultItem(
            text=r["text"],
            score=round(float(r["score"]), 6),
            citation=Citation(
                chunk_id=str(r["chunk_id"]),
                document_id=str(r["document_id"]),
                original_name=r["original_name"],
                storage_ref=r["storage_ref"],
                page_number=r["page_number"],
                paragraph_index=r["paragraph_index"],
                ordinal=r["ordinal"],
            ),
        )
        for r in outcome["results"]
    ]
    return QueryResponse(
        address_id=address_id,
        model_id=outcome["model_id"],
        latency_ms=outcome["latency_ms"],
        candidates_examined=outcome["candidates_examined"],
        chunks_returned=len(items),
        results=items,
    )
