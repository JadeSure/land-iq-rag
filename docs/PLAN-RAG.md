# LandIQ RAG — Execution and Landing Plan

## Context

`land-iq-rag` is the address-scoped retrieval layer of the LandIQ platform. Given a property
address, the platform produces a Feasibility Report through a multi-agent pipeline; this RAG
system is the component that ingests user-uploaded address-specific documents (Feasibility
Reports, DCP uploads, planning files), turns them into retrievable chunks with provenance, and
answers address-scoped queries for the downstream Agent system.

The repo is greenfield: three PRDs (`docs/PRD-RAG.md`, `docs/PRD-Agent.md`, `docs/MVP-Scope.md`)
and zero code. The hard deadline is the **end-of-June 2026 local-only demo** (today is
2026-06-01, so roughly four weeks). The demo must show: address to Portal, upload, background
ingestion, report generation, Markdown report with citations. RAG is one of three subsystems
(RAG + Agent + Portal/Console); this plan covers RAG only, but respects the consumer contract
the Agent depends on.

Two knowledge sources are kept strictly separate and only one is RAG:
- `ltm/` static knowledge (report structure, regulations, calculation conventions, benchmarks):
  read directly into prompts, zero-cost, **not RAG**. Out of scope here.
- Address-specific uploaded documents: chunk + embed + vector retrieval. **This is the system.**

The intended outcome: a local pipeline that satisfies the PRD acceptance criteria, built against
clean port seams so the AWS landing is a deployment swap rather than a rewrite.

### Decisions locked with Shawn (2026-06-01)

1. **Sequencing: local now + AWS infra in parallel.** The local demo path is the priority and
   must never be blocked by AWS work. AWS rides the same ports.
2. **AWS vector store: self-managed pgvector on a small EC2** (the PRD's literal line). We own
   backups, patching and hardening; this plan makes that explicit.
3. **No Bedrock. Own embedding pipeline.** `EmbeddingProvider` port stays; default OpenAI
   `text-embedding-3-small` for the demo. A self-hosted open embedding model remains the
   first-class "own pipeline" path for later, switchable by config plus a rebuild.
4. **AWS backend is serverless; EC2 runs pgvector only (revised 2026-06-01).** The earlier
   "everything co-located on one EC2, no SQS/Lambda" shape is dropped. On AWS the EC2 does one
   job, host Postgres + pgvector in a private subnet, to keep the always-on cost to a single small
   box. The application is serverless: API Gateway + Lambda for the API, SQS + Lambda for ingestion
   and rebuild. The `ingest_job` table stays as the durable status/retry/rebuild-resume record
   (F12, 6.5, 8.5); SQS only handles delivery. Two trade-offs accepted: (a) Lambda in the VPC
   reaching OpenAI needs outbound internet, served by a small `t4g.nano` NAT instance (about
   $3/month, not the roughly $32/month managed NAT gateway); (b) Lambda-to-Postgres connection
   storms are bounded by capped Lambda concurrency plus pgbouncer on the EC2. SQS fan-out (one
   message per chunk-batch) also dissolves Lambda's 15-minute ceiling for large rebuilds. Honest
   note: at demo traffic the single-EC2 shape was marginally cheaper; serverless is chosen for
   elastic, no-always-on-process operation, and the delta is roughly the NAT instance.
5. **No Step Functions; EventBridge Scheduler only for backup and an optional reconciler
   (2026-06-01).** Ingestion is a short linear sequence inside one Lambda, so there is nothing to
   orchestrate; the state machine already lives on the `ingest_job` row, and SQS fan-out handles the
   only long task (rebuild). Step Functions is revisited only if scanned-PDF OCR lands (PRD 12.1).
   EventBridge runs the nightly backup and (optionally) a reconciler sweep; it is **not** used to
   periodically re-embed — determinism (8.4) makes timed re-embedding pure waste. See §10.

---

## 1. Requirement analysis (what the PRD actually demands, and the tensions)

The PRD is well specified. The load-bearing constraints and how this plan honours each:

- **Address isolation is a correctness property, not performance** (8.1, 5.5, Constraints).
  Every query is forced through a mandatory `WHERE address_id = :addr` chokepoint in one
  repository function; a cross-address result is a defect with a dedicated CI test
  (acceptance criterion 11).
- **No mixing vectors from different embedding models in one ranking** (F14, 6.3). This collides
  with a physical reality: a pgvector `vector(N)` column is fixed-dimension and HNSW/IVFFlat
  indexes are per-column/per-dimension, yet OpenAI small is 1536-dim, large is 3072-dim, and a
  self-hosted model could be 384/768/1024-dim. Resolution below: **one embedding table per
  model** (dimension-correct, own ANN index). F14 becomes structural, not a runtime filter you
  can forget.
- **Async ingestion** (F13, 6.1): upload persists the document and returns a handle immediately;
  chunking and embedding run in the background.
- **Idempotency** (F10, 6.2): re-ingesting unchanged content does nothing; changed content
  replaces the prior version with no duplicates and no window where a query sees a half-replaced
  document.
- **Model-change + rebuild** (F8, F9, 6.3, 8.5): swapping the active model is destructive to
  existing vectors and the caller must know; rebuild re-embeds under the new model, is safely
  interruptible/resumable, and never destroys the serving vectors until the new set is complete.
- **Failure isolation + no silent failures** (6.5, 8.3): one document failing must not block
  siblings; every failure is reportable through the status interface.
- **Graceful degradation** (6.6): RAG is never a blocker; "documents present then the report can
  cite them" is the only hard guarantee. The status interface exposes an address-level
  `fully_indexed` boolean the Agent uses to decide whether to rely on RAG or fall back to `ltm/`.
- **Cost discipline** (8.2, Non-Goals): no managed service with a disproportionate minimum spend.
  Bedrock Knowledge Base is excluded (its OpenSearch Serverless floor is roughly $350/month idle).
  pgvector on Postgres has no such floor.
- **Determinism under fixed inputs** (8.4): fixed content + fixed chunking config + fixed model
  produces equivalent retrieval; guaranteed by deterministic chunking and the content-hash gate.

---

## 2. Architecture overview

Single FastAPI service (the stable interface the Portal and Agent call), a Postgres + pgvector
store, and a background ingestion worker that polls a job table. Three ports are the only things
that differ between local and AWS.

```
 Portal (upload) ─┐                         ┌─ Agent (query, consumer)
                  ▼                         ▼
            ┌───────────────────────────────────────┐
            │           FastAPI service              │
            │  ingest · status · query · remove ·    │
            │  config(model) · rebuild               │
            └───────────────┬───────────────────────┘
                            │ writes doc row + enqueues job row
              ┌─────────────┼──────────────────────────────┐
              ▼             ▼                                ▼
       StorageBackend   Postgres + pgvector            (job table)
       (raw bytes)      documents · chunks ·            ingest_job
        local FS / S3    embedding_<model> · config       │ FOR UPDATE
                                                           │ SKIP LOCKED
                                                           ▼
                                                  Ingestion worker
                                                  extract → chunk →
                                                  embed → versioned write
                                                  → atomic live-version flip
                                                           │
                                                           ▼
                                                  EmbeddingProvider
                                                  OpenAI / self-hosted
```

The core pipeline (extract, chunk, embed, versioned write) is identical local and AWS; what differs
is how a job is delivered to it. Local: StorageBackend = filesystem, delivery = a worker process (or
asyncio task) polling the Postgres job table on the dev box, EmbeddingProvider = OpenAI. AWS:
StorageBackend = S3, delivery = SQS triggering a Lambda (the same `process_job` body),
EmbeddingProvider = OpenAI (outbound via the NAT instance) or a self-hosted server. In both, the
`ingest_job` table is the durable status/retry record; only the delivery transport changes.

---

## 3. Tech stack

| Concern | Choice | Note |
|---|---|---|
| Language/runtime | Python 3.12 | Matches the Agent system; best embedding/pgvector ecosystem |
| Web framework | FastAPI + uvicorn | Async-native (F13), auto OpenAPI = the Agent contract, Pydantic DTOs |
| DB driver | psycopg 3 + `pgvector` adapter | Native async, pooling, `COPY` bulk insert, registers the `vector` type |
| Vector store | Postgres 16 + pgvector (`pgvector/pgvector:pg16` locally) | pgvector 0.8.x; HNSW cosine index |
| PDF extraction | **pdfplumber** (MIT) default; PyMuPDF optional | pdfplumber gives page + word boxes for page/paragraph provenance. PyMuPDF is faster but AGPL — flagged for a commercial Versent product |
| Chunking | small in-house token-aware splitter + `tiktoken` | ~500–800 tokens, ~80–120 overlap, configurable; deterministic. Avoids a heavy LangChain dependency |
| Embedding | `EmbeddingProvider` port; OpenAI `text-embedding-3-small` default | Self-hosted open model (TEI / sentence-transformers) as the "own pipeline" path. No Bedrock |
| Config | `pydantic-settings` + `.env` locally; SSM Parameter Store on AWS | Active model id, chunk params, DSN in one place |
| Migrations | numbered `.sql` files + tiny runner; `ensure_embedding_table(model_id, dim)` | Per-model tables auto-created so a model swap is config-only |
| Observability | `structlog` + `ingest_job` / `query_log` tables; CloudWatch on AWS | Per-ingest and per-query cost/latency |

---

## 4. Repo / package structure

```
land-iq-rag/
├── docker-compose.yml              # local Postgres + pgvector
├── pyproject.toml
├── .env.example
├── migrations/
│   ├── 0001_init.sql               # core tables, enum, indexes, config
│   └── 0002_embedding_template.sql # per-model embedding table template (applied by ensure_*)
├── src/landiq_rag/
│   ├── main.py                     # FastAPI app; startup runs migrations + starts worker
│   ├── config.py                   # pydantic-settings; reads SSM on AWS
│   ├── ports.py                    # StorageBackend, JobQueue, EmbeddingProvider Protocols + value types
│   ├── api/
│   │   ├── routes_ingest.py        # POST documents, DELETE doc, GET status
│   │   ├── routes_query.py         # POST query
│   │   ├── routes_admin.py         # GET/PUT active model, POST rebuild
│   │   └── schemas.py              # request/response DTOs (the consumer contract)
│   ├── ingest/
│   │   ├── pipeline.py             # ingest(address_id, upload_id): extract→chunk→embed→persist
│   │   ├── extract.py              # pdfplumber → [(page, para_idx, text, char span)]
│   │   ├── chunk.py                # token-aware splitter carrying provenance through
│   │   └── worker.py               # claim loop (SKIP LOCKED), state machine, failure capture
│   ├── embedding/
│   │   ├── openai_provider.py      # text-embedding-3-small/large
│   │   ├── selfhosted_provider.py  # TEI / sentence-transformers HTTP or in-process
│   │   └── registry.py             # model_id -> provider; resolves active model from config
│   ├── store/
│   │   ├── db.py                   # async pool, migration runner, ensure_embedding_table
│   │   ├── documents.py            # document + chunk repo (idempotency, cascade)
│   │   ├── embeddings.py           # per-model insert + ANN query (mandatory address filter)
│   │   ├── tasks.py                # ingest_job status repo (F12)
│   │   └── files.py                # StorageBackend impls: LocalFsStorage, S3Storage
│   └── retrieval/search.py         # (query, address) -> ranked chunks + provenance + metrics
├── infra/                          # AWS landing (parallel track): CDK or Terraform
└── tests/
    ├── test_isolation.py           # cross-address never leaks (acceptance 11)
    ├── test_idempotency.py         # re-ingest replaces, no dupes (F10)
    ├── test_provenance.py          # page/paragraph round-trip (F7)
    ├── test_model_filter.py        # no cross-model mixing (F14)
    ├── test_rebuild.py             # interruptible/resumable, consistent index (F9, 8.5)
    └── test_failure_isolation.py   # one doc fails, siblings proceed (6.5)
```

---

## 5. Data model (Postgres + pgvector)

### 5.1 Core, model-agnostic tables

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE address (
  address_id   TEXT PRIMARY KEY,            -- normalised key (Google place id / canonical string)
  display_name TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE document (
  document_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  address_id   TEXT NOT NULL REFERENCES address(address_id) ON DELETE CASCADE,
  upload_id    TEXT NOT NULL,               -- stable logical-document identity (F10 key)
  content_type TEXT NOT NULL,
  content_hash TEXT NOT NULL,               -- sha256(raw bytes): unchanged-skip gate
  storage_ref  TEXT NOT NULL,               -- file://... or s3://...  (recover original, 5.2)
  original_name TEXT,
  live_version INT  NOT NULL DEFAULT 1,     -- atomic-switch pointer (idempotency, 3.2)
  ingest_ts    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (address_id, upload_id)
);
CREATE INDEX idx_document_address ON document(address_id);

CREATE TABLE chunk (
  chunk_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id  UUID NOT NULL REFERENCES document(document_id) ON DELETE CASCADE,
  address_id   TEXT NOT NULL REFERENCES address(address_id) ON DELETE CASCADE, -- denormalised for isolation/index
  doc_version  INT  NOT NULL,
  ordinal      INT  NOT NULL,
  page_number  INT,                          -- provenance (F7)
  paragraph_index INT,                        -- provenance (F7)
  char_start   INT, char_end INT,
  text         TEXT NOT NULL,
  token_count  INT,
  UNIQUE (document_id, doc_version, ordinal)  -- duplicate-proof under at-least-once delivery
);
CREATE INDEX idx_chunk_address  ON chunk(address_id);
CREATE INDEX idx_chunk_document ON chunk(document_id);

CREATE TYPE ingest_state AS ENUM
  ('pending','extracting','chunking','embedding','done','skipped','failed');

CREATE TABLE ingest_job (              -- the F12 status record AND the local queue
  task_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),   -- the handle returned to caller
  document_id    UUID REFERENCES document(document_id) ON DELETE CASCADE,
  address_id     TEXT NOT NULL,
  upload_id      TEXT NOT NULL,
  content_hash   TEXT NOT NULL,
  model_id       TEXT NOT NULL,        -- model this run embeds under
  doc_version    INT  NOT NULL,
  state          ingest_state NOT NULL DEFAULT 'pending',
  chunk_count    INT DEFAULT 0,
  embedding_count INT DEFAULT 0,
  attempts       INT DEFAULT 0,
  max_attempts   INT DEFAULT 5,
  failed_step    TEXT, error_type TEXT, error_message TEXT,   -- no silent failure (6.5)
  tokens_embedded BIGINT DEFAULT 0, cost_usd NUMERIC(12,6) DEFAULT 0,  -- per-ingest cost (8.2)
  worker_id      TEXT, claimed_at TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ
);
CREATE INDEX idx_job_claimable ON ingest_job(state) WHERE state = 'pending';
CREATE INDEX idx_job_address   ON ingest_job(address_id);

CREATE TABLE rag_config (             -- active model + chunk params (F8); admin-editable
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);  -- seed: ('active_embedding_model', 'openai:text-embedding-3-small')

CREATE TABLE embedding_model (        -- registry of which model tables exist
  model_id TEXT PRIMARY KEY, dim INT NOT NULL, table_name TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE query_log (              -- per-query observability (8.3)
  query_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  address_id TEXT NOT NULL, model_id TEXT NOT NULL,
  latency_ms INT, candidates_examined INT, chunks_returned INT,
  cost_usd NUMERIC(12,6), created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 5.2 Per-model embedding tables (resolves F14)

`ensure_embedding_table(model_id, dim)` issues `CREATE TABLE IF NOT EXISTS` with the correct
dimension on first use of a model, and inserts into `embedding_model`. So switching the active
model (F8) needs no migration edit. Example for `text-embedding-3-small`:

```sql
CREATE TABLE IF NOT EXISTS embedding_openai_text_embedding_3_small (
  chunk_id   UUID NOT NULL REFERENCES chunk(chunk_id) ON DELETE CASCADE,
  address_id TEXT NOT NULL,
  model_id   TEXT NOT NULL,
  dim        INT  NOT NULL,                 -- = 1536 (recorded per 5.4)
  embedding  vector(1536) NOT NULL,
  PRIMARY KEY (chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_emb_te3s_addr ON embedding_openai_text_embedding_3_small(address_id);
CREATE INDEX IF NOT EXISTS idx_emb_te3s_hnsw ON embedding_openai_text_embedding_3_small
  USING hnsw (embedding vector_cosine_ops);
```

Why table-per-model and not one table with a `model_id` filter: a single `vector` column cannot
hold 1536-dim and 3072-dim (or 1024-dim self-hosted) vectors, and you cannot index a mixed-
dimension column. Table-per-model makes "never mix models in one ranking" structural, makes
rebuild and stale-model retirement a `DROP TABLE`, and keeps each ANN index tight.

Note on large vectors: pgvector HNSW/IVFFlat indexes support up to 2000 dimensions. The default
`text-embedding-3-small` (1536) indexes fine. If `text-embedding-3-large` (3072) is ever chosen,
either pass OpenAI's `dimensions` parameter to cap at <=2000, use `halfvec` (pgvector 0.7+ indexes
to 4000 dims), or accept an unindexed exact scan (fine at demo data volume). A self-hosted model
at <=1024 dims sidesteps this entirely.

---

## 6. Port interfaces (the only local/AWS seam)

`Protocol` types in `ports.py`. A single `PROFILE=local|aws` switch selects the concrete trio;
nothing in the pipeline knows which profile it runs in.

```python
class StorageBackend(Protocol):
    async def put(self, *, address_id, upload_id, data: bytes, content_type) -> str: ...  # returns storage_ref
    async def get(self, ref: str) -> bytes: ...
    async def delete(self, ref: str) -> None: ...

class JobQueue(Protocol):
    async def enqueue(self, job: IngestJobSpec) -> str: ...            # returns task_id (handle)
    async def claim(self, *, max_jobs: int) -> list[ClaimedJob]: ...   # SELECT ... FOR UPDATE SKIP LOCKED
    async def complete(self, task_id) -> None: ...
    async def fail(self, task_id, err, *, retryable: bool) -> None: ...
    async def heartbeat(self, task_id) -> None: ...                    # extend lease

class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str: ...     # "openai:text-embedding-3-small"
    @property
    def dimension(self) -> int: ...    # 1536
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...
```

| Port | Local | AWS (this plan) |
|---|---|---|
| StorageBackend | `LocalFsStorage(root)` | `S3Storage(bucket)`, key `address=<id>/doc=<upload_id>/original/<name>` |
| JobQueue | `PostgresJobQueue` (same DB, `SKIP LOCKED` poll) | `SqsJobQueue`: `enqueue` sends an SQS message and writes the job row; delivery is the SQS-to-Lambda event source, not a poll loop |
| EmbeddingProvider | `OpenAIEmbeddingProvider` | `OpenAIEmbeddingProvider` or `SelfHostedEmbeddingProvider` (own pipeline) |

The JobQueue port covers enqueue and status; the difference is delivery. Locally a worker loop calls
`claim()` (`SELECT ... FOR UPDATE SKIP LOCKED`); on AWS the SQS event source mapping invokes a Lambda
directly, so there is no `claim()` poll. Both share one transport-agnostic `process_job` body and
both write state to `ingest_job`, so the pipeline never knows which delivery path ran it.

---

## 7. Pipeline behaviours

**Async ingestion (F13).** Upload handler: persist bytes via `StorageBackend`, compute
`content_hash`, insert `document` + a `pending` `ingest_job`, return `task_id` immediately. The
worker does the rest. Delivery to the worker is the `JobQueue` port: a Postgres poll loop locally,
an SQS-to-Lambda event source on AWS, both running the same `process_job` body.

**Ingestion state machine.** `pending → extracting → chunking → embedding → done`, with `skipped`
(unchanged content) and `failed` (after retries). `chunk_count`, `embedding_count`, `model_id`,
errors and cost all live on the `ingest_job` row, which is exactly what the status interface
returns.

**Idempotency (F10), no half-state window.**
- Compute `content_hash` at upload. If an existing `done` job for `(address_id, upload_id)` has
  the same hash, the new job goes straight to `skipped`. No duplicate work or vectors. Observable,
  not silent.
- If the hash differs, write the new version's chunks/embeddings under a new `doc_version` while
  the old version keeps serving, then flip `document.live_version` in one short transaction and
  sweep the old rows. Retrieval filters `live_version = current`, so a reader sees entirely-old or
  entirely-new, never a blend. Embedding (slow) happens outside the transaction; only the pointer
  flip is transactional, so no long-held locks.

**Rebuild (F9, 8.5), interruptible and non-destructive.** Rebuild is another job type that fans
out per chunk under the active model. Each unit: embed, then
`INSERT ... ON CONFLICT (chunk_id) DO NOTHING` into the active model's table. Progress is the
embedding rows themselves, so the resume query is "chunks with no current-model embedding." The
old model's table keeps serving untouched. Only when
`count(active-model embeddings) == count(chunks in scope)` does the active-model config flip in one
transaction. Old-model tables are swept after the flip (kept briefly for cheap rollback). Active
model pointer is **global** (one model serves the system; F14 is per-query and per-address
anyway); an address-scoped rebuild is a backfill that brings one lagging address up to the global
model.

**Failure isolation (6.5).** The unit of work is one document = one job row. A failure updates
that row to `failed` with `failed_step`/`error_type`/`error_message` and releases its lease;
siblings and other addresses keep flowing. Transient provider errors (429/5xx/timeout) retry to
`max_attempts` then surface; deterministic failures (corrupt PDF, no extractable text) go straight
to `failed`. Nothing crashes the worker loop.

**Graceful degradation (6.6).** The status interface exposes an address rollup:
`fully_indexed = every upload_id has a done/skipped job with embedding_count == chunk_count under
the active model`. The Agent uses this to decide RAG-vs-`ltm/` fallback.

**Observability (8.2, 8.3).** Per ingest: chunk/embedding counts, tokens, cost (tokens x model
rate), latency, state, errors on `ingest_job`. Per query: latency, candidates examined, returned
count, cost on `query_log`. A small rate table keyed by `model_id` computes cost for any provider.

---

## 8. Query / retrieval path (F6, F7, isolation, F14)

`POST /addresses/{address_id}/query {query, k}`:
1. Resolve active model from `rag_config`; resolve its table/dimension from `embedding_model`.
2. Embed the query with that same model (one call, cost logged).
3. ANN search against that model's table only (structural F14), with the mandatory
   `address_id` filter, joined to `chunk`/`document` for text + provenance.
4. Write `query_log`; return ordered chunks with provenance.

```sql
SELECT e.chunk_id, c.text, d.document_id, d.original_name, d.storage_ref,
       c.page_number, c.paragraph_index, c.ordinal, e.model_id,
       1 - (e.embedding <=> :query_vec::vector) AS cosine_similarity
FROM embedding_openai_text_embedding_3_small e        -- table chosen by active model_id
JOIN chunk    c ON c.chunk_id   = e.chunk_id
JOIN document d ON d.document_id = c.document_id
WHERE e.address_id = :address_id                       -- MANDATORY isolation chokepoint
  AND c.doc_version = d.live_version
ORDER BY e.embedding <=> :query_vec::vector
LIMIT :k;
```

Dense retrieval only for the demo; reranking/hybrid is a deferred open question (PRD 12). The
address filter lives in exactly one repository function so no call path can omit it.

---

## 9. API surface (FastAPI, the consumer contract)

| Method | Path | Purpose | PRD |
|---|---|---|---|
| POST | `/addresses/{address_id}/documents` (multipart) | Ingest; returns `202 {task_id}` | F1, F13, Ingest |
| GET | `/ingestion/{task_id}` | Per-handle status | F12 |
| GET | `/addresses/{address_id}/ingestion-status` | Address rollup + `fully_indexed` | F12, 6.6 |
| POST | `/addresses/{address_id}/query` | Ranked chunks + provenance + metrics | F6, F7 |
| DELETE | `/documents/{document_id}` | Cascade removal | F11 |
| GET/PUT | `/config/embedding-model` | Read/set active model (Console dropdown); PUT warns it is destructive | F8 |
| POST | `/rebuild` (optional `address_id`) | Rebuild under active model | F9 |

---

## 10. AWS landing (parallel track, serverless backend + pgvector-only EC2)

Target: shrink the always-on footprint to a single small EC2 that does nothing but host Postgres +
pgvector. Everything else is serverless and scales with traffic. No managed RAG. (Rendered local and
AWS diagrams: `docs/architecture.dio`, openable in draw.io / the VS Code Draw.io extension.)

```
                      ┌──────── API Gateway (HTTPS) ────────┐
 Agent / Portal ─────▶│  ingest · status · query · remove · │
                      │  config · rebuild                   │
                      └─────────────────┬───────────────────┘
                                        ▼
                                 API Lambda (FastAPI + Mangum)
                                 upload: S3 put → insert document +
                                 ingest_job(pending) → send SQS → 202
                                        │
                                        ▼
                                 SQS (ingest / rebuild) + DLQ
                                        │ event source
            ┌──────────── VPC ──────────┼─────────────────┐
            │ private subnet            ▼                  │
            │   EC2 t4g.small      Ingest Lambda           │
            │    Postgres 16  ◀─5432─ (process_job:        │
            │    + pgvector           extract→chunk→       │
            │    + pgbouncer          embed→write→flip)    │
            │ public subnet             │ OpenAI (outbound)│
            │   NAT instance ◀──────────┘                  │
            │   (t4g.nano) ──▶ internet                    │
            └───────┬──────────────────────────────────────┘
                    │ S3 Gateway endpoint (free)
                    ▼
              S3 (raw docs)   SSM Parameter Store (config, OpenAI key SecureString)
                              CloudWatch (logs / metrics)
                              EventBridge Scheduler ─▶ backup Lambda (nightly pg_dump → S3)
                                                   └▶ [optional] reconciler Lambda (sweep ingest_job)
```

- **Always-on cost = one small EC2.** A single `t4g.small` (Graviton, gp3 ~20–30 GB) in a private
  subnet runs Postgres 16 + pgvector and pgbouncer, nothing else. It is the only box running 24/7
  and the only thing to patch and back up beyond the DB.
- **API: API Gateway + Lambda.** The FastAPI app runs on Lambda via Mangum, same routes, same
  Pydantic DTOs, same consumer contract as local. The upload handler persists bytes to S3, computes
  the content hash, inserts the `document` + `pending` `ingest_job` rows, sends one SQS message, and
  returns `202 {task_id}`. Query, status and admin are plain request/response Lambdas.
- **Ingestion + rebuild: SQS + Lambda.** A standard SQS queue feeds an Ingest Lambda whose handler
  is the transport-agnostic `process_job` body (the same code the local poll loop calls).
  At-least-once delivery is safe because the content-hash gate makes re-processing idempotent. Set
  `maxReceiveCount` to mirror `ingest_job.max_attempts`, route exhausted messages to a DLQ, and set
  the queue visibility timeout above the Lambda timeout. **Rebuild fans out one message per
  chunk-batch**, so each invocation stays well under the 15-minute Lambda ceiling and the job is
  naturally resumable (resume = re-enqueue chunks with no current-model embedding).
- **`ingest_job` table stays.** SQS only delivers. The row remains the durable F12 status, the
  retry counter (6.5), the per-ingest cost record (8.2), and the rebuild-resume cursor (8.5).
- **Raw storage: S3.** Keys `address=<id>/doc=<upload_id>/original/<name>`, versioning on, SSE
  (SSE-S3 minimum; SSE-KMS if the client requires key control), Block Public Access on, TLS-only
  bucket policy, lifecycle to IA/Glacier after 30/90 days. Reached through a Gateway VPC endpoint
  (free), so S3 traffic never touches the NAT.
- **Outbound to OpenAI: NAT instance, not NAT gateway.** The Ingest Lambda sits in the VPC to reach
  private Postgres, so its OpenAI calls need outbound internet. A `t4g.nano` NAT instance in a
  public subnet provides it for about $3/month versus roughly $32/month for a managed NAT gateway.
  It is a single point of failure with no HA, acceptable for demo and low traffic; swap to a managed
  NAT gateway when uptime needs justify it. If embeddings move to a self-hosted server on the EC2,
  the Lambda needs no internet and the NAT instance disappears.
- **Connection management.** Lambda concurrency against a self-managed Postgres can exhaust
  `max_connections`. Bound it two ways: cap the Ingest Lambda's reserved/SQS `maxConcurrency` (for
  example 10), and run pgbouncer (transaction pooling) on the EC2 so DB connections stay flat no
  matter how many Lambdas are warm. This is the operational cost of Lambda + self-managed PG over
  the local pool-held worker.
- **Config/secrets:** SSM Parameter Store (Standard, free) for active model, chunk params, DSN host;
  OpenAI key as an SSM SecureString. Nothing in code or the image.
- **Access/security:** Postgres in a private subnet, security group allows 5432 only from the Lambda
  security group, no public Postgres; admin via SSM Session Manager (no open SSH); the API is public
  only through API Gateway; instance role least-privilege (S3 on the docs prefix, SSM read on the
  config path) with no wildcards.
- **Scheduled jobs (EventBridge Scheduler).** Two thin periodic jobs: (1) **nightly backup** — a
  backup Lambda runs `pg_dump` to S3, with EBS snapshots via Data Lifecycle Manager alongside and a
  documented restore runbook (this is our responsibility since Postgres is self-managed); (2)
  **optional reconciler** — a Lambda that sweeps `ingest_job` for rows stuck in a non-terminal state
  past a threshold, or inserted-but-never-enqueued (the upload-Lambda partial-failure window), and
  re-enqueues them, plus sweeps orphaned old `doc_version` rows. The reconciler is production
  hardening, not a demo blocker: SQS visibility-timeout (Lambda died mid-flight → message returns)
  and the DLQ (poison messages) already cover most failure paths. **There is no scheduled
  re-embedding.** Determinism (8.4) means re-embedding unchanged content under the same model yields
  identical vectors, so a timer would only burn tokens; vectors change on document change
  (event-driven re-ingest) or model change (explicit admin rebuild), never on a clock.
- **IaC:** AWS CDK (Python, one language with the app) defining the VPC (public + private subnets),
  EC2 + instance role + user-data bootstrap (Postgres + pgvector + pgbouncer), NAT instance, S3 +
  Gateway endpoint, SQS + DLQ, the Lambdas + API Gateway, EventBridge Scheduler (backup, optional
  reconciler), SSM parameters, CloudWatch. Terraform is an equally valid alternative if the wider
  LandIQ estate standardises on it.
- **Observability:** CloudWatch for Lambda logs and metrics plus the `structlog` JSON; per-ingest
  and per-query cost surface as EMF custom metrics dimensioned by `address_id` / `model_id`.

**Cost shape:** one `t4g.small` (~$12–15/mo) plus a `t4g.nano` NAT (~$3/mo) plus S3 (a few dollars)
plus SQS and Lambda (effectively free at this volume) plus OpenAI tokens. No ALB baseline, no
Bedrock KB floor (~$350/month idle). Honest caveat: at demo traffic the original single-EC2 shape
was marginally cheaper (no NAT, no second hop); serverless is chosen for elastic,
no-always-on-process operation, and the delta is roughly the NAT instance.

**Local→AWS diff:** swap `LocalFsStorage`→`S3Storage`; swap the local poll-loop worker for the SQS
event source + Ingest Lambda (both call the same `process_job`); point the config loader at SSM. No
change to the `process_job` body, the query handler, or the schema. If any of these forces a change
inside the pipeline body, a seam has leaked and must be fixed.

**Why no Step Functions.** Ingestion is a linear, short sequence (extract → chunk → embed → write →
flip) that fits one Lambda invocation; there is no branching or external wait to orchestrate. The
state machine, retries and resume cursor already live on the `ingest_job` row, so a Step Functions
layer would duplicate state in two places. The only task that would exceed Lambda's 15-minute
ceiling is a large rebuild, and SQS fan-out (one message per chunk-batch) handles that more simply
and cheaply than a Step Functions Map. Revisit Step Functions only if scanned-PDF OCR lands (PRD
12.1): "start Textract job → wait for callback → chunk → embed" is an async-wait, multi-step flow
where the wait/callback pattern earns its place.

---

## 11. Build sequence

Two tracks. **Track A (local) is demo-critical and takes priority; Track B (AWS) proceeds in
parallel and must never block A.** Track B is mostly port implementations + IaC once A's core
exists.

### Track A — local pipeline (priority, for the June demo)
1. **Scaffold + schema + ports.** `docker-compose` Postgres+pgvector, migrations + `0001`/`0002`,
   `ports.py`, local implementations (`LocalFsStorage`, `PostgresJobQueue`, `OpenAIEmbeddingProvider`),
   `PROFILE` switch, config.
2. **Upload + async.** Upload handler (persist → hash → insert document + pending job → return
   `task_id`); content-hash skip gate; worker claim loop with `SKIP LOCKED` and the state machine.
3. **Extract + chunk + embed + persist.** pdfplumber extraction with page/paragraph provenance;
   token-aware chunker; per-model embedding insert; versioned write + atomic `live_version` flip.
4. **Status interface.** Per-handle + address rollup (`fully_indexed`) for Agent degradation.
5. **Query path + query_log.** Mandatory address filter, single-model table, provenance, metrics.
6. **Removal + rebuild + model config.** Cascade delete (F11); rebuild job (resumable, atomic
   flip); GET/PUT active model with destructive-change warning.
7. **Tests** (the acceptance criteria): isolation, idempotency, provenance, model-filter, rebuild,
   failure-isolation, degradation. Seed a NSW demo address with a real Feasibility Report so the
   demo is substantive (MVP success criterion 9).

### Track B — AWS infra (parallel, must not block A)
1. **`infra/` CDK skeleton**: VPC (public + private subnets), S3 bucket (versioning, SSE, BPA,
   lifecycle) + S3 Gateway endpoint.
2. **pgvector EC2 + NAT instance**: `t4g.small` Postgres + pgvector + pgbouncer in the private
   subnet (instance role, user-data bootstrap); `t4g.nano` NAT instance in the public subnet;
   security groups (5432 only from the Lambda SG); SSM Session Manager access.
3. **`S3Storage` + `SqsJobQueue` port implementations** + SSM config loader; verify against the
   same tests under `PROFILE=aws`.
4. **SQS + DLQ + Lambdas**: API Lambda (FastAPI + Mangum) behind API Gateway; ingest/rebuild Lambda
   on the SQS event source running `process_job`; capped concurrency; visibility timeout above the
   Lambda timeout; `maxReceiveCount` = `max_attempts`.
5. **EventBridge Scheduler + backup Lambda**: nightly `pg_dump`→S3 + DLM EBS snapshots + restore
   runbook; optional reconciler Lambda (sweep stuck/never-enqueued `ingest_job` rows, orphaned
   `doc_version`s). No scheduled re-embedding.
6. **Optional self-hosted embedding server** (`SelfHostedEmbeddingProvider` + TEI on the EC2) once
   OpenAI is proven; this also removes the NAT instance, since the Lambda then needs no internet.
7. **Run the acceptance criteria on AWS** to prove parity (including the rebuild fan-out across SQS).

---

## 12. Verification (end-to-end)

Local first, then repeat on AWS for parity. Each maps to a PRD acceptance criterion.

- **Run it:** `docker compose up` (Postgres), `uvicorn landiq_rag.main:app` (API + worker).
- **Ingest + async:** `POST /addresses/A/documents` with a real PDF returns `202 {task_id}`
  immediately; `GET /ingestion/{task_id}` shows `pending → ... → done` with rising
  `embedding_count`. (F13, F12)
- **Query + provenance:** `POST /addresses/A/query` returns ordered chunks each carrying
  document, page and paragraph; open the source PDF at that page to confirm. (F6, F7)
- **Isolation:** seed addresses A and B; a query on A never returns B's chunks. Automated in
  `test_isolation.py`. (acceptance 11)
- **Idempotency:** re-`POST` the identical file → job `skipped`, chunk count unchanged; re-`POST`
  a modified file → new content reflected, no orphan chunks. (F10)
- **Removal:** `DELETE /documents/{id}` → subsequent queries return nothing from it. (F11)
- **Model swap + rebuild:** `PUT /config/embedding-model` to a second model, `POST /rebuild`; kill
  the worker mid-rebuild and restart to prove resumability; after completion the index is
  internally consistent and queries never reference old-model vectors. (F8, F9, F14, 8.5)
- **Failure isolation:** ingest a corrupt PDF alongside a good one → the corrupt job is `failed`
  with a readable error; the good one completes. (6.5)
- **Degradation:** with no documents for an address, the status rollup reports
  `fully_indexed=false` so the Agent falls back to `ltm/`. (6.6)
- **Observability:** `ingest_job` and `query_log` rows carry counts, latency and cost. (8.2, 8.3)

---

## 13. Open decisions flagged (non-blocking, revisit during build)

- **PDF library licence:** pdfplumber (MIT) is the default to keep the eventual Versent product
  clean. Switch to PyMuPDF only if its AGPL/commercial terms are cleared and the speed matters.
- **Chunk size/overlap:** start ~500 tokens / ~80 overlap; one tuning pass against a real
  Feasibility Report before the demo.
- **Self-hosted embedding model choice:** if going own-pipeline, pick a model at <=1024 dims (BGE
  / e5 family) so HNSW indexing is unconstrained, and budget a rebuild when switching off OpenAI.
- **EC2 sizing + co-location:** start single-box `t4g.small/medium`; split Postgres onto its own
  instance only if the demo or load warrants it.
- **Reranking:** deferred (PRD open question 12.4); dense-only is sufficient for the demo.
