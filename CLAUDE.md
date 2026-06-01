# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Start Postgres + pgvector (port 5433 on host)
docker compose up -d

# Install deps (Python 3.12, uv)
uv sync --extra dev

# Copy config (default uses the zero-dependency hash embedder — no API key needed)
cp .env.example .env

# Run the API + inline ingestion worker
uv run uvicorn landiq_rag.main:app --port 8099

# Run all tests
uv run pytest -q

# Run a single test file
uv run pytest tests/test_acceptance.py -q

# Run a single test by name
uv run pytest tests/test_acceptance.py::test_ingest_and_query -q

# Seed demo address and run a sample query
RAG_BASE_URL=http://localhost:8099 uv run python scripts/seed_demo.py

# CDK (AWS, Track B only)
uv sync --extra aws-cdk
infra/build_lambda.sh
cd infra && CDK_DEPLOY_ACCOUNT=<acct> CDK_DEPLOY_REGION=ap-southeast-2 npx aws-cdk synth
```

Interactive API docs: `http://localhost:8099/docs`

## Architecture

### Core design

Single FastAPI service backed by Postgres + pgvector. An async ingestion worker runs as an `asyncio.Task` inside the same process locally (started in the lifespan in `main.py`). Three `Protocol` types in `ports.py` are the **only seam between local and AWS** — nothing in the pipeline or query path knows which profile it runs under:

- `StorageBackend` — `LocalFsStorage` (local) / `S3Storage` (AWS)
- `JobQueue` — `PostgresJobQueue` using `SELECT … FOR UPDATE SKIP LOCKED` locally; `SqsJobQueue` on AWS
- `EmbeddingProvider` — `OpenAIEmbeddingProvider` or `HashEmbeddingProvider`; resolved by `embedding/registry.py`

`RAG_PROFILE=local|aws` selects the concrete trio; `context.py::AppContext` holds the live instances and is stored on `app.state.ctx`.

### One embedding table per model (F14)

`pgvector` columns are fixed-dimension — you cannot mix 1536-dim and 1024-dim vectors in one column. The design uses **one table per model** (`embedding_<model_slug>`), created on demand by `store/db.py::ensure_embedding_table(model_id, dim)`. This makes "never mix models in a ranking" structural rather than a runtime filter. The registry in `store/db.py::embedding_model` tracks which tables exist. Switching the active model means updating `rag_config` and running a rebuild — the old table keeps serving untouched until the new one is complete.

### Ingestion pipeline

`POST /addresses/{address_id}/documents` → persist bytes (`StorageBackend.put`), compute `sha256` hash, insert `document` + `pending` `ingest_job` row, return `202 {task_id}` immediately.

Worker (`ingest/worker.py`) claims rows via `SKIP LOCKED` and drives the state machine:

```
pending → extracting → chunking → embedding → done
                                            → skipped (same content_hash, no work)
                                            → failed  (after max_attempts)
```

The pipeline body is in `ingest/pipeline.py`. Extract (`pdfplumber`), chunk (token-aware, `tiktoken`), embed, then write chunks/embeddings under a new `doc_version`, then atomically flip `document.live_version`. Queries filter `doc_version = live_version`, so a reader always sees entirely-old or entirely-new content.

### Query path

`POST /addresses/{address_id}/query` → embed the query, run ANN search against the active model's table with a mandatory `WHERE address_id = :address_id` chokepoint in `store/embeddings.py`. Cross-address leakage is a correctness defect tested in the acceptance suite.

### Configuration

All settings use the `RAG_` prefix (`config.py`, `pydantic-settings`). The DB `rag_config` table is authoritative for the active model and chunk params at runtime (seeded from env on first start). `get_settings()` is `lru_cache`-wrapped — call `get_settings.cache_clear()` in tests if needed.

Default embedding model is `hash:feature-256` (deterministic, no API key, runs fully offline). For real quality: set `RAG_ACTIVE_EMBEDDING_MODEL=openai:text-embedding-3-small` and `RAG_OPENAI_API_KEY=…`.

### AWS landing (Track B)

CDK stack in `infra/`. AWS-specific code lives in `src/landiq_rag/aws/` (Lambda handlers, SSM config loader) and `store/queue_sqs.py` / `store/files.py::S3Storage`. The target shape: API Gateway + Lambda (FastAPI via Mangum) for the API, SQS + Lambda for ingestion, single `t4g.small` EC2 running Postgres + pgvector + pgbouncer in a private subnet, `t4g.nano` NAT instance for OpenAI egress. The CDK stack synthesises today (`cdk synth`) but is not deployed.

**If any AWS work forces a change inside `ingest/pipeline.py` or `retrieval/search.py`, a port seam has leaked and must be fixed.**

### PDF extraction

`pdfplumber` (MIT) is the default. PyMuPDF is faster but AGPL — do not switch without confirming licence terms are acceptable for a commercial Versent product.
