"""Request/response DTOs — the stable contract the Portal and Agent depend on."""

from __future__ import annotations

from pydantic import BaseModel, Field


class IngestAccepted(BaseModel):
    task_id: str
    document_id: str | None
    address_id: str
    upload_id: str
    doc_version: int
    state: str  # 'pending' | 'skipped'
    model_id: str


class JobStatus(BaseModel):
    task_id: str
    address_id: str
    upload_id: str
    document_id: str | None
    doc_version: int
    job_type: str
    state: str
    model_id: str
    chunk_count: int
    embedding_count: int
    attempts: int
    failed_step: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    cost_usd: float = 0.0


class AddressIngestionStatus(BaseModel):
    address_id: str
    active_model: str
    document_count: int
    has_documents: bool
    in_flight: int
    failed: int
    missing_embeddings: int
    fully_indexed: bool
    jobs: list[JobStatus]


class QueryRequest(BaseModel):
    query: str = Field(min_length=1)
    k: int | None = Field(default=None, ge=1, le=100)


class Citation(BaseModel):
    chunk_id: str
    document_id: str
    original_name: str | None
    storage_ref: str
    page_number: int | None
    paragraph_index: int | None
    ordinal: int


class QueryResultItem(BaseModel):
    text: str
    score: float  # RRF fusion score (max ≈ 0.033 at default rrf_k=60); not cosine similarity
    citation: Citation


class QueryResponse(BaseModel):
    address_id: str
    model_id: str
    latency_ms: int
    candidates_examined: int
    chunks_returned: int
    results: list[QueryResultItem]


class ActiveModel(BaseModel):
    active_model: str


class SetModelRequest(BaseModel):
    model_id: str


class SetModelResponse(BaseModel):
    target_model: str
    rebuild_task_id: str
    warning: str


class RebuildRequest(BaseModel):
    address_id: str | None = None
    model_id: str | None = None


class RebuildResponse(BaseModel):
    rebuild_task_id: str
    model_id: str
    address_scope: str


class DeleteResponse(BaseModel):
    document_id: str
    deleted: bool
