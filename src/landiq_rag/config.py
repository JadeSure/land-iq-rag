"""Application configuration (pydantic-settings, RAG_ prefix).

The DB ``rag_config`` table is authoritative for the active embedding model and
chunk params at runtime (admin-editable, F8); these settings seed it on first
start and provide everything else (DSN, storage, worker tuning).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Deployment profile selects port implementations.
    profile: Literal["local", "aws"] = "local"

    # Postgres + pgvector.
    database_url: str = "postgresql://landiq:landiq@localhost:5433/landiq_rag"

    # Raw-document storage backend.
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_root: str = "./storage"
    s3_bucket: str = ""
    # S3-compatible endpoint for local dev against a fake S3 (MinIO/LocalStack).
    # Leave empty for real AWS — boto3 then uses the default endpoint + IAM role.
    s3_endpoint_url: str = ""
    aws_region: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""

    # Embedding (seeds rag_config; DB is authoritative at runtime).
    active_embedding_model: str = "hash:feature-256"
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # PDF extraction: Claude API fallback for low-quality pdfplumber results.
    # Set anthropic_api_key to enable; leave empty to disable the fallback.
    anthropic_api_key: str = ""
    pdf_fallback_threshold: float = 0.4  # quality score 0–1 below which Claude is used

    # Self-hosted embedding server (the "own pipeline" path, e.g. TEI).
    selfhosted_url: str = ""
    selfhosted_dim: int = 1024

    # AWS landing (Track B) — unused under PROFILE=local.
    sqs_queue_url: str = ""
    ssm_prefix: str = ""  # e.g. /landiq-rag/ ; when set under aws, config is hydrated from SSM

    # Chunking (deterministic under fixed values; PRD 8.4).
    chunk_tokens: int = 600
    chunk_overlap: int = 100

    # Startup behaviour (Lambda sets these off; migrations run via EC2 bootstrap).
    run_migrations_on_startup: bool = True

    # Ingestion worker.
    worker_enabled: bool = True
    worker_poll_seconds: float = 1.0
    worker_batch: int = 2
    ingest_max_attempts: int = 5
    lease_seconds: int = 300

    # Retrieval.
    query_default_k: int = 8


@lru_cache
def get_settings() -> Settings:
    return Settings()
