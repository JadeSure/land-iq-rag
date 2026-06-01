# land-iq-rag

Address-scoped Retrieval-Augmented Generation layer for the LandIQ platform. It
ingests user-uploaded, address-specific documents (Feasibility Reports, DCP
uploads, planning files), turns them into retrievable chunks with provenance,
and answers address-scoped queries for the report-generation Agent.

Specs live in [`docs/`](docs/): [PRD-RAG](docs/PRD-RAG.md),
[PRD-Agent](docs/PRD-Agent.md), [MVP-Scope](docs/MVP-Scope.md), and the
[execution + landing plan](docs/PLAN-RAG.md).

## Quickstart (local)

```bash
# 1. Postgres + pgvector
docker compose up -d

# 2. Python deps (uv)
uv sync --extra dev

# 3. Config
cp .env.example .env          # defaults to a zero-dependency embedding model

# 4. Run the API + ingestion worker
uv run uvicorn landiq_rag.main:app --port 8099

# 5. (optional) seed a demo address and run a sample query
RAG_BASE_URL=http://localhost:8099 uv run python scripts/seed_demo.py
```

The default embedding model is `hash:feature-256` — a deterministic, no-API-key
feature-hashing embedder so the whole pipeline runs offline. For real retrieval
quality set `RAG_OPENAI_API_KEY` and `RAG_ACTIVE_EMBEDDING_MODEL=openai:text-embedding-3-small`
in `.env` (the model can also be switched at runtime via the admin endpoint,
which triggers a rebuild).

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/addresses/{address_id}/documents` | Upload a document (returns `202` + `task_id`); chunking/embedding run async |
| GET | `/ingestion/{task_id}` | Per-handle ingestion status |
| GET | `/addresses/{address_id}/ingestion-status` | Address rollup incl. `fully_indexed` (for Agent fallback) |
| POST | `/addresses/{address_id}/query` | Ranked, address-scoped chunks with provenance |
| DELETE | `/documents/{document_id}` | Remove a document and all derived chunks/embeddings |
| GET/PUT | `/config/embedding-model` | Read / switch the active embedding model (switch is destructive, queues a rebuild) |
| POST | `/config/rebuild` | Re-embed under the active model (optionally one address) |
| GET | `/health` | Liveness + active model |

Interactive docs at `http://localhost:8099/docs` when running.

## Tests

```bash
uv run pytest -q
```

The suite runs the full app (API + worker + pgvector) against a fresh schema and
covers the PRD acceptance criteria: async ingest, address isolation, idempotency
(unchanged-skip and changed-replace), provenance, model switch + rebuild, failure
isolation, and graceful degradation.

## Design

- **Vector store:** Postgres + pgvector. One embedding table *per model*
  (dimension-correct, own ANN index) so a query can never mix models (F14) and
  models of different dimensions coexist.
- **Async ingestion:** a Postgres job table (`ingest_job`) polled by a worker
  with `FOR UPDATE SKIP LOCKED`. The job table *is* the status interface.
- **Ports** (`ports.py`) are the only local↔AWS seam: `StorageBackend`
  (filesystem ↔ S3), `JobQueue` (Postgres on either side), `EmbeddingProvider`
  (OpenAI / self-hosted). See `docs/PLAN-RAG.md` for the AWS landing.

## AWS landing (Track B)

The cloud target is serverless (API Gateway + Lambda for the API, SQS + Lambda for
ingestion/rebuild) with a single small EC2 running Postgres + pgvector + pgbouncer,
and a `t4g.nano` NAT instance for OpenAI egress. The `ingest_job` table stays the
durable status/retry/resume record; SQS only delivers. The three ports
(`StorageBackend`, `JobQueue`, `EmbeddingProvider`) are the only application seam.
See [`docs/PLAN-RAG.md`](docs/PLAN-RAG.md) section 10.

The CDK stack lives in `infra/` and **synthesises** today (`cdk synth`, exit 0); it
is not deployed. To deploy into an account:

```bash
uv sync --extra aws-cdk
infra/build_lambda.sh                       # bundle landiq_rag + deps into infra/build/
cd infra
CDK_DEPLOY_ACCOUNT=<acct> CDK_DEPLOY_REGION=ap-southeast-2 npx aws-cdk synth
# create the OpenAI key as an SSM SecureString: /landiq-rag/openai_api_key
# set a real DB password in /landiq-rag/db_password and /landiq-rag/database_url
CDK_DEPLOY_ACCOUNT=<acct> npx aws-cdk deploy
```

AWS-specific code: `src/landiq_rag/aws/` (`lambda_api`, `lambda_worker`,
`lambda_reconciler`, `ssm_config`), `store/queue_sqs.py`, `store/files.py::S3Storage`.

## Project layout

```
src/landiq_rag/
  api/         FastAPI routes + DTOs (the Agent/Portal contract)
  ingest/      extract (pdfplumber) · chunk (token-aware) · pipeline · worker
  embedding/   providers (hash, OpenAI) + registry
  store/       db (pool, migrations, per-model tables) · documents · embeddings · tasks · files
  retrieval/   address-scoped ANN search
migrations/    numbered SQL (core schema)
tests/         acceptance + unit tests
scripts/       seed_demo.py
data/demo/     synthetic demo documents
```
