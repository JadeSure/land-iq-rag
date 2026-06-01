"""Port interfaces: the only seam that changes between local and AWS.

A single ``RAG_PROFILE`` switch selects the concrete trio. Nothing in the
ingestion pipeline or query path knows which profile it runs in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


# ── Value types ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class IngestJobSpec:
    """A unit of work enqueued after a document is persisted (or a rebuild)."""

    document_id: str | None
    address_id: str
    upload_id: str
    content_hash: str
    model_id: str
    doc_version: int
    job_type: str = "ingest"  # 'ingest' | 'rebuild'


@dataclass
class ClaimedJob:
    """A job leased to a worker. Mirrors the ingest_job row the worker needs."""

    task_id: str
    document_id: str | None
    address_id: str
    upload_id: str
    content_hash: str
    model_id: str
    doc_version: int
    job_type: str
    attempts: int
    max_attempts: int
    state: str


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    tokens: int = 0
    cost_usd: float = 0.0
    extra: dict = field(default_factory=dict)


# ── Ports ─────────────────────────────────────────────────────────────────────
@runtime_checkable
class StorageBackend(Protocol):
    """Durable storage for raw uploaded document bytes."""

    async def put(
        self, *, address_id: str, upload_id: str, data: bytes, content_type: str
    ) -> str:
        """Persist bytes; return an opaque, serialisable storage_ref (URI)."""
        ...

    async def get(self, ref: str) -> bytes: ...

    async def delete(self, ref: str) -> None: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Produces vectors and self-identifies (model_id + dimension)."""

    @property
    def model_id(self) -> str:
        """Canonical identifier stamped onto every embedding (5.4, F14)."""
        ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed documents/passages."""
        ...

    async def embed_query(self, text: str) -> EmbeddingResult:
        """Embed a single query (some providers distinguish query vs passage)."""
        ...


@runtime_checkable
class JobQueue(Protocol):
    """Async ingestion dispatch. Local = Postgres job table; AWS (this plan) =
    the same Postgres job table on EC2 (no SQS/Lambda required)."""

    async def enqueue(self, spec: IngestJobSpec) -> str:
        """Insert a pending job; return the task_id (handle)."""
        ...

    async def claim(self, *, worker_id: str, max_jobs: int) -> list[ClaimedJob]:
        """Lease up to max_jobs pending jobs (FOR UPDATE SKIP LOCKED)."""
        ...

    async def heartbeat(self, task_id: str) -> None:
        """Extend the lease on an in-flight job."""
        ...
