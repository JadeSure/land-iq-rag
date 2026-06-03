# LandIQ RAG — Review Findings & Remediation Plan

> **Status:** review complete, **no code changed yet**. This document is the execution
> backlog. It is self-contained: each item has the location, the mechanism, the fix, and an
> acceptance check, so an implementing session can act without re-deriving anything.

## How to use this document (for the executing session)

1. Read **"Decision gate (E2)"** first — it determines whether several items are *fix* or *delete*.
2. Work in priority order: **P0 → P1 → P2 → P3 → P4 → P5**. P0 affects the local June demo.
3. Each item is `ID. Title  SEVERITY (confidence)` with **Where / Problem / Fix / Acceptance**.
4. Do **not** chase the items under "Considered and dismissed" — they were checked and are not defects.
5. After each fix, add the regression test named in its Acceptance line. The current suite
   (`tests/test_acceptance.py`, `tests/test_chunking.py`) has **no** coverage for retry-idempotency,
   scoped-rebuild isolation, rebuild completeness, or hybrid/FTS behaviour.

## Method (how these were found)

Full read of the service (~3.5k LOC) + `docs/PLAN-RAG.md`, `docs/PRD-RAG.md`, `docs/MVP-Scope.md`,
`infra/`, and tests; then 17 adversarial verifiers (each tried to *refute* a candidate defect against
the real code) and 5 dimension sweeps (security/authz, AWS IaC, SQL/concurrency, PRD-coverage,
misc-correctness). Confidence values come from that verification pass.

Priority rationale: the hard target is a **local** end-of-June demo (Track A). AWS (Track B) is built
but **not deployed**; `MVP-Scope.md` defers infra-level multi-tenant isolation. So local correctness >
AWS > product-hardening — but all listed items are real.

---

## Decision gate (E2) — resolve before P1/P2

**Hybrid retrieval, the Claude vision fallback, and OCR were all built, but `MVP-Scope.md §4.4`
lists "hybrid retrieval / re-ranking" and "image-only/scanned PDFs as first-class input" as
explicitly OUT OF SCOPE for the demo.**

- Source files: `src/landiq_rag/store/embeddings.py:68-152` (hybrid BM25+RRF), `migrations/0002_fts.sql`,
  `src/landiq_rag/ingest/extract.py:45-82,124-169,283-308` (Claude + OCR).
- Consequences: hybrid is the root cause of **Q1** and **Q2**; the Claude fallback breaks PRD 8.4
  determinism whenever `RAG_ANTHROPIC_API_KEY` is set (LLM extraction is non-deterministic).
- **Two paths:**
  - **Keep** → fix Q1/Q2/E1, document the determinism caveat, update PLAN/CLAUDE to describe hybrid.
  - **Revert** → drop hybrid (use pure vector `ann_search`), remove Claude/OCR, restore dense-only +
    pdfplumber to match the signed scope; Q1/Q2/E1 become deletions.
- **This decision must be made by a human (Shawn).** Default recommendation if unsure: for the demo,
  **revert to dense-only + pdfplumber** (matches scope, removes Q1/Q2/E1 risk and a non-determinism
  source); revisit hybrid/Claude post-demo as a deliberate feature.

---

## P0 — Local correctness, demo-affecting

### I1. Ingest retry is non-idempotent → one transient embedding error permanently fails a document  HIGH (0.95)
- **Where:** `src/landiq_rag/ingest/pipeline.py:63-79` (chunk write) + `src/landiq_rag/store/documents.py:117-131`
  (`insert_chunks`, fresh `uuid4`, **no `ON CONFLICT`**) + `src/landiq_rag/ingest/runner.py:28-34`
  (`is_retryable`) + `src/landiq_rag/store/tasks.py:219-220` (`mark_failed`) + `migrations/0001_init.sql:46`
  (`UNIQUE(document_id, doc_version, ordinal)`).
- **Problem:** chunks are written at `job.doc_version` *before* embedding. If embedding fails with a
  retryable error (OpenAI `RateLimitError`/timeout), the job resets to `pending` and re-runs from extract,
  re-inserting the same `(document_id, doc_version, ordinal)` → `UniqueViolation`, which is **not** in
  `is_retryable` → the job goes straight to `failed`. A single 429 permanently fails a brand-new document,
  defeating PRD 6.5.
- **Fix:** make the chunk write idempotent across attempts. Preferred: delete any existing rows for
  `(document_id, doc_version)` at the start of the chunking step before inserting; or add
  `ON CONFLICT (document_id, doc_version, ordinal) DO NOTHING` and stable chunk ids; or classify
  `UniqueViolation` so the pipeline resumes cleanly.
- **Acceptance:** new test `test_retry_after_transient_embed_error` — inject a provider that raises
  `RateLimitError` on the first `embed` call then succeeds; assert the job reaches `done` and
  `embedding_count == chunk_count`.

### I2. Synchronous PDF extraction blocks the shared event loop  HIGH (0.98)
- **Where:** `src/landiq_rag/ingest/extract.py:45-82` (`extract_segments_with_fallback`), `:116-119`
  (`_pdf_page_count`), `:249-308` (`_extract_pdf`, `_ocr_pages`). Worker runs in-process with the API:
  `src/landiq_rag/main.py:60-65`.
- **Problem:** the async extraction function calls synchronous pdfplumber + pytesseract directly (no
  `asyncio.to_thread`). Parsing a large feasibility PDF stalls every concurrent API request
  (`/health`, `/query`, status). Embedding providers already offload correctly (`providers.py:136`).
- **Fix:** run the blocking extraction via `asyncio.to_thread` (or a thread/process pool) from
  `pipeline.run_ingest_job`.
- **Acceptance:** ingest a large multi-page PDF while polling `GET /health`; health latency must not
  spike for the parse duration.

### D1. Documented quickstart crashes on startup  HIGH (0.99)
- **Where:** `.env.example:24` (`RAG_ACTIVE_EMBEDDING_MODEL=hf:BAAI/bge-base-en-v1.5`, also trailing space)
  vs `CLAUDE.md:14-15`, `README.md:22,31-35`; provider import `src/landiq_rag/embedding/providers.py:117`;
  `[hf]` extra `pyproject.toml:36-38`; lifespan `src/landiq_rag/main.py:44-48`.
- **Problem:** docs say the default is zero-dependency `hash:feature-256` installed via `uv sync --extra dev`,
  but `.env.example` ships an `hf:` model whose provider imports `sentence_transformers` (only in `[hf]`).
  Following the docs verbatim ⇒ `ImportError` before the server serves.
- **Fix:** set `.env.example` to `RAG_ACTIVE_EMBEDDING_MODEL=hash:feature-256` (list `hf:`/`openai:` as
  commented alternatives) and strip the trailing space; OR change the three docs to instruct `--extra hf`.
  Make `.env.example`, `README.md`, `CLAUDE.md` consistent.
- **Acceptance:** `uv sync --extra dev && cp .env.example .env && uv run uvicorn landiq_rag.main:app --port 8099`
  starts cleanly.

### Q1. Query `similarity` is an RRF score mislabelled as cosine; `candidates_examined` is wrong  HIGH (0.98)
- **Where:** `src/landiq_rag/store/embeddings.py:124-133` (`… AS cosine_similarity`), `:146-151`
  (`candidates_examined`); surfaced at `src/landiq_rag/api/routes_query.py:23` → `schemas.py:62-65`.
- **Problem:** `similarity` is the RRF fusion score (max ≈ 0.033), not cosine in [0,1]; an Agent
  thresholding on it gets nothing. `candidates_examined` returns `count(*)` of *all* address embeddings,
  not the `k*4` actually examined.
- **Fix (if E2 = keep):** rename the field to `score` and document it as a fusion rank, or also return true
  cosine for the vector component; set `candidates_examined` to the real CTE candidate count. **(if E2 = revert:**
  pure vector search returns genuine cosine — this resolves naturally.)
- **Acceptance:** a `/query` response exposes a score whose meaning matches its name; a documented threshold works.

---

## P1 — Rebuild / model-switch correctness (drives the Console model-switch demo)

### R1. Address-scoped rebuild flips the GLOBAL active-model pointer  HIGH (0.95)
- **Where:** `src/landiq_rag/ingest/pipeline.py:185-188` (unconditional `set_config(ACTIVE_MODEL_KEY, …)`),
  triggered by `src/landiq_rag/api/routes_admin.py:74-83` (`POST /config/rebuild` accepts `address_id`).
  Intended behaviour: `docs/PLAN-RAG.md:377-386`.
- **Problem:** `POST /config/rebuild {address_id:"A", model_id:"N"}` embeds only A under N, then flips the
  *global* model to N → every other address queries N's (empty) table and returns nothing.
- **Fix:** only `set_config` when `scope_all` (`job.address_id == "*"`); a scoped rebuild is a pure backfill.
- **Acceptance:** test `test_scoped_rebuild_does_not_flip_global` — ingest A and B under M; scoped-rebuild A→N;
  assert active model is still M and B still returns results.

### R2. Rebuild flips the model with no completeness guard + concurrent-ingest race  HIGH (0.92)
- **Where:** `src/landiq_rag/ingest/pipeline.py:157` (snapshot of "missing") → `:185-188` (flip);
  ingest stamps `job.model_id` at enqueue (`pipeline.py:84`). Spec: `docs/PLAN-RAG.md:377-386`.
- **Problem:** PLAN requires flipping only when `count(new-model embeddings) == count(chunks)`. A document
  ingested during the rebuild is embedded under the *old* model and missing from the new table at flip time,
  so it's invisible under the new active model — violates F9/F14 silently.
- **Fix:** re-check the missing count inside the flip transaction and re-enqueue stragglers; or block/queue
  ingests during a global rebuild; or auto-sweep after the flip.
- **Acceptance:** test that ingests a doc mid-rebuild and asserts it is retrievable under the new model after flip.

### R3. No mutual exclusion between concurrent rebuilds (last-write-wins)  MEDIUM (0.85)
- **Where:** `src/landiq_rag/ingest/pipeline.py:186-188`.
- **Problem:** two `/rebuild` calls for different models both reach `set_config` with no lock.
- **Fix:** track `rebuild_in_progress` in `rag_config`; reject overlapping rebuilds with HTTP 409 in
  `routes_admin.py`.
- **Acceptance:** second concurrent rebuild returns 409.

### R4. Old-model tables never swept; `"*"` scope sentinel is in-band  MEDIUM/LOW (0.95)
- **Where:** `src/landiq_rag/ingest/pipeline.py:135,185-188`; spec `docs/PLAN-RAG.md:377-386`;
  `address_id` is arbitrary `TEXT` (`migrations/0001_init.sql:10-14`).
- **Problem:** superseded per-model tables accumulate (the promised "cheap rollback" is never wired up).
  `address_id == "*"` collides with a real address literally named `*`.
- **Fix:** drop the superseded table after a grace period; replace the `"*"` sentinel with an explicit
  `job_scope` field (or nullable `address_id`) on the rebuild job.
- **Acceptance:** after a model switch, the old table is gone (post-grace); a `job_scope` field exists.

### Q2. Hybrid search drops FTS-only chunks lacking a new-model embedding  MEDIUM (0.85) — only if E2 = keep
- **Where:** `src/landiq_rag/store/embeddings.py:130-139` (final INNER `JOIN {table} e`).
- **Problem:** a chunk that matched BM25 but has no embedding in the active model's table is silently
  dropped — worst during a model switch, where keyword matches vanish until rebuild completes.
- **Fix:** LEFT JOIN and rank embedding-less FTS hits separately, or guarantee embeddings exist before a
  chunk is "live". (If E2 = revert, this disappears with hybrid.)
- **Acceptance:** during a rebuild, an FTS keyword query still returns the keyword match.

---

## P2 — Observability / ingest quality

### I4. Cost / tokens / embedding_count overwritten (not accumulated) across retries  MEDIUM (0.8)
- **Where:** `src/landiq_rag/store/tasks.py:174-194` (`set_progress` uses `COALESCE(%s, col)` = replace);
  callers pass running totals (`pipeline.py:107-109,179-181`).
- **Problem:** a retried/partial embed loses earlier attempts' cost → under-reports API spend (PRD 8.2/8.3);
  `embedding_count` semantics are ambiguous across retries.
- **Fix:** accumulate (`col = col + COALESCE(%s,0)`), or reset accumulators to 0 when a job re-enters `pending`.
- **Acceptance:** a job that retries reports cumulative cost/tokens.

### E1. Claude PDF fallback truncates large docs and its cost is untracked  MEDIUM (0.75) — only if E2 = keep
- **Where:** `src/landiq_rag/ingest/extract.py:136` (`max_tokens=8192`), `:162` (`resp.content[0].text`),
  cost path `pipeline.py:89-109` (embedding cost only).
- **Problem:** large PDFs are silently truncated despite the "do not omit content" prompt; Claude extraction
  cost is never recorded (8.2 gap); `content[0]` is unsafe (masked by the broad `except`, low practical risk).
- **Fix:** page/section the PDF for Claude (or loop the call), capture and record extraction cost into
  `ingest_job.cost_usd`, guard the content access. (If E2 = revert, delete this path.)
- **Acceptance:** a multi-page PDF is fully extracted; `cost_usd` includes extraction cost.

### D4. Dead test assertion + `JobStatus` missing `doc_version`  MEDIUM (0.99)
- **Where:** `tests/test_acceptance.py:85`; `src/landiq_rag/api/schemas.py:18-32` (no `doc_version`);
  status SELECT `src/landiq_rag/api/routes_ingest.py:25-27`.
- **Problem:** `assert final["doc_version"] == 2 if "doc_version" in final else True` always evaluates `True`
  (JobStatus has no `doc_version`), so the version-bump check never runs.
- **Fix:** add `doc_version` to `JobStatus` + the status SELECT/`_job_status` mapping, and rewrite the assertion
  to check it (`assert final["doc_version"] == 2`).
- **Acceptance:** the idempotency-changed test actually verifies version 2.

---

## P3 — AWS / Track B (not deployed; fix before any deploy)

- **A1. No automated schema creation; API Lambda cold-start crash**  HIGH (0.95).
  `infra/bootstrap_ec2.sh:59-63` leaves migrations commented; Lambdas set
  `RAG_RUN_MIGRATIONS_ON_STARTUP=false`, but `main.py` lifespan still calls
  `get_config`/`ensure_embedding_table` → `ProgrammingError` if tables don't exist. **Fix:** run migrations
  in the EC2 bootstrap (or a one-shot deploy step) and verify before first Lambda invocation.
- **A3. pgbouncer `auth_type=scram-sha-256` with plaintext `userlist.txt`**  HIGH.
  `infra/bootstrap_ec2.sh:44-56` — SCRAM needs a SCRAM verifier or `auth_query`, not plaintext; auth will
  likely fail. **Fix:** set `auth_query` against `pg_shadow`, or store the SCRAM secret, or drop to `plain`
  inside the private VPC.
- **A5. DB password handling**  HIGH. `CHANGEME` hardcoded into `database_url`
  (`infra/landiq_rag_stack.py:178-180`) and synthesised into the CloudFormation template; `userlist.txt`
  written world-readable (`bootstrap_ec2.sh:56`). **Fix:** source from SSM SecureString/Secrets Manager;
  `chmod 600` the userlist; unset the bash var after use.
- **A4. No SQS/SSM VPC interface endpoints → single `t4g.nano` NAT is a SPOF**  MEDIUM-HIGH.
  `infra/landiq_rag_stack.py:70-91` (only an S3 gateway endpoint). **Fix:** add interface endpoints for SQS & SSM.
- **A2. SSM hydration omits `anthropic_api_key` and `gemini_api_key`**  MEDIUM (0.95).
  `src/landiq_rag/aws/ssm_config.py:13-24` `_FIELDS` lacks both → features unconfigurable on AWS. **Fix:** add them.
- **A6. API Lambda unbounded concurrency vs pgbouncer pool 20; worker batch vs Lambda pool max=2**  MEDIUM.
  `infra/landiq_rag_stack.py:230-233` (no reserved concurrency on `api_fn`); `bootstrap_ec2.sh:54`
  (`default_pool_size=20`); `aws/lambda_worker.py:41` (`max_size=2`). **Fix:** cap API concurrency and size
  pools coherently; keep `worker_batch=1` on Lambda or raise the Lambda pool.
- **A7. `claim_by_id` ignores the lease**  MEDIUM (0.85). `src/landiq_rag/store/tasks.py:100-142` re-claims any
  non-terminal job without `lease_until < now()` (unlike `claim`). Safe **only** because `visibility_timeout
  (960s) > lease (300s)` — implicit and undocumented. **Fix:** add the lease check or document the coupling.
- **A8. `build_lambda.sh` silent fallback installs host-arch wheels**  MEDIUM.
  `infra/build_lambda.sh:18-19` — the no-platform fallback would grab the dev Mac's wheels → an unbootable
  Lambda bundle. **Fix:** fail hard instead of falling back; build on/for Linux x86_64 (matching Lambda's default).

---

## P4 — Security / product hardening (MVP defers infra isolation; required before commercial use)

- **S1. No API authentication/authorization; delete by bare `document_id`**  HIGH for product.
  `src/landiq_rag/main.py:77-95` (no auth middleware); `src/landiq_rag/api/routes_ingest.py:132-137`
  (`DELETE /documents/{document_id}` with no address-ownership check). PRD 8.1 makes address the
  access-control unit but nothing enforces it. **Fix:** add authn + per-address authz; scope delete to
  `/addresses/{id}/documents/{doc}` and verify the document's `address_id`.
- **S2. No rate limiting / upload-size cap / `address_id` length-format validation**  MEDIUM.
  `main.py:77-95`, `routes_ingest.py:57-63`. **Fix:** add rate-limit middleware, explicit upload size limit,
  and `Field(max_length=…, pattern=…)` on path/form params.
- **S3. `LocalFsStorage.get/delete` path traversal via `storage_ref`**  LOW (contingent).
  `src/landiq_rag/store/files.py:40-46`. **Fix:** `path.resolve().relative_to(self.root)` guard.
- **S4. Dynamic f-string table names unvalidated at call sites**  LOW (latent).
  `src/landiq_rag/store/embeddings.py:41-42,91-152`, `pipeline.py:148-154`. **Fix:** assert the
  `embedding_[a-z0-9_]+` shape (defence-in-depth) or look the table up from the registry inside the function.

---

## P5 — Concurrency edge cases & minor correctness

- **C1. Delete vs in-flight re-ingest race; embeddings inserted after cascade-delete → orphans**  MEDIUM.
  `src/landiq_rag/ingest/pipeline.py:115-121`, `routes_ingest.py:132-137`. `SKIP LOCKED` stops two workers on
  one job, but a delete or a newer-version job can race an in-flight embed. **Fix:** pre-flip `live_version`
  check / row lock, or soft-delete.
- **L1. `SelfHostedEmbeddingProvider` httpx `AsyncClient` never closed**  LOW-MEDIUM.
  `src/landiq_rag/embedding/providers.py:85`. **Fix:** tie client lifecycle to app lifespan / add `aclose`.
- **L4. OpenAI `dimensions` param not passed**  LOW/latent. `providers.py:221` — harmless today (registry never
  overrides `dim`) but `provider.dimension` would lie if it did. **Fix:** pass `dimensions=self._dim`.
- **L5. `/health` reads `app.state.ctx` before lifespan sets it**  LOW. `main.py:80-86`. **Fix:** guard with
  `getattr(app.state, "ctx", None)` and return a warming-up status.
- **L3. Gemini dead `isinstance(texts, str)` branch**  LOW. `providers.py:183`. **Fix:** remove dead branch.

---

## Documentation drift (low effort; do alongside)

- **D2.** `README.md:81-82` tells users `uv sync --extra ocr`; no `ocr` extra exists (`pyproject.toml:27-41`);
  OCR deps are already main deps. **Fix:** remove the instruction or add the extra.
- **D3.** `src/landiq_rag/ports.py:90-92` JobQueue docstring still says AWS needs "no SQS/Lambda" (contradicts
  revised PLAN §10 and the implemented `SqsJobQueue`). `PLAN-RAG.md §8` says "dense retrieval only"; `PLAN §4`
  lists non-existent files (`0002_embedding_template.sql` → `0002_fts.sql`; `openai_provider.py`/
  `selfhosted_provider.py` → consolidated `providers.py`). `CLAUDE.md` "Query path" describes ANN-only.
  `SqsJobQueue.claim` raises `NotImplementedError` while the port declares `claim` (AWS uses the free function
  `claim_by_id` instead — design wart, not a runtime bug). **Fix:** reconcile docs with the implemented design
  (depends on E2).

---

## Considered and dismissed (do NOT action these)

- **Provenance char-offset drift** — refuted (0.95). `chunk.py` encodes the full text **once** then slices
  tokens, so `len(decode(a:b)) + len(decode(b:c)) == len(decode(a:c))` exactly; offsets/page mapping are correct.
- **"Postgres rejects Lambda" via pg_hba** — dismissed. pgbouncer proxies; Postgres sees a localhost
  connection, which `127.0.0.1/32` allows. (The real DB risks are A3/A5.)
- **"Mangum re-runs lifespan every request"** — dismissed. `lifespan="auto"` runs once per cold start and the
  config seeds are idempotent (`if … is None`).
- **boto3 clients "leak"** — dismissed as ~benign; boto3 clients need no explicit close and are reused per warm env.
- **HNSW index build blocking cold start** — overstated; `CREATE INDEX IF NOT EXISTS` is a no-op once built.
- **EC2-arm64 vs Lambda-x86_64 wheel "mismatch"** — overstated; the Python app runs only on Lambda (x86_64,
  matching the build); EC2 runs only Postgres. The genuine residue is the build-script fallback (captured as A8).

---

## Suggested execution order

1. **P0** (I1, I2, D1, Q1) — demo happy path + consumer contract.
2. **Resolve E2** (keep vs revert hybrid/Claude/OCR) — determines whether Q1/Q2/E1 are fixes or deletions.
3. **P1** rebuild correctness (R1, R2; then R3/R4/Q2).
4. **P2** observability (I4, E1, D4) + doc drift (D2/D3).
5. **Track B (P3)** before any AWS deploy; **security (P4)** before commercial use; **P5** hardening as capacity allows.
