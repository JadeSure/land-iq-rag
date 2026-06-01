"""Admin endpoints: active embedding model configuration and rebuild (F8, F9)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..context import ACTIVE_MODEL_KEY, AppContext
from ..embedding.registry import make_provider
from ..ports import IngestJobSpec
from ..store.db import get_config
from .schemas import (
    ActiveModel,
    RebuildRequest,
    RebuildResponse,
    SetModelRequest,
    SetModelResponse,
)

router = APIRouter(prefix="/config", tags=["admin"])


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _validate_model(ctx: AppContext, model_id: str) -> None:
    try:
        make_provider(model_id, ctx.settings)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid model_id: {exc}") from exc


async def _enqueue_rebuild(ctx: AppContext, *, model_id: str, address_scope: str) -> str:
    spec = IngestJobSpec(
        document_id=None,
        address_id=address_scope,
        upload_id="-",
        content_hash="",
        model_id=model_id,
        doc_version=0,
        job_type="rebuild",
    )
    return await ctx.queue.enqueue(spec)


@router.get("/embedding-model", response_model=ActiveModel)
async def get_active_model(request: Request) -> ActiveModel:
    ctx = _ctx(request)
    return ActiveModel(active_model=await ctx.active_model_id())


@router.put("/embedding-model", response_model=SetModelResponse)
async def set_active_model(body: SetModelRequest, request: Request) -> SetModelResponse:
    """Switching the active embedding model is destructive to existing vectors.

    The active pointer is NOT changed immediately; a rebuild generates the new
    model's embeddings while the current model keeps serving, and the pointer
    flips only when the rebuild completes (F9, 6.3, 8.5).
    """
    ctx = _ctx(request)
    _validate_model(ctx, body.model_id)
    task_id = await _enqueue_rebuild(ctx, model_id=body.model_id, address_scope="*")
    return SetModelResponse(
        target_model=body.model_id,
        rebuild_task_id=task_id,
        warning=(
            "Destructive operation. Existing vectors were produced by a different model. "
            "A rebuild has been queued; the active model switches to the target only once "
            "the rebuild completes. Queries keep using the current model until then."
        ),
    )


@router.post("/rebuild", response_model=RebuildResponse)
async def rebuild(body: RebuildRequest, request: Request) -> RebuildResponse:
    ctx = _ctx(request)
    async with ctx.pool.connection() as conn:
        active = await get_config(conn, ACTIVE_MODEL_KEY)
    model_id = body.model_id or active or ctx.settings.active_embedding_model
    _validate_model(ctx, model_id)
    scope = body.address_id or "*"
    task_id = await _enqueue_rebuild(ctx, model_id=model_id, address_scope=scope)
    return RebuildResponse(rebuild_task_id=task_id, model_id=model_id, address_scope=scope)
