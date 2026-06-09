# LandIQ RAG — 数据库设计文档

> 范围:本文档完整描述 LandIQ RAG 的数据库设计——表结构、字段、约束、索引、关系、数据流与演进方式。计算分层(为什么 embedding/抽取放在数据库之外)见 `DESIGN-DB-and-Compute.md`;图形化 ER 见 `architecture.dio` 第三页。
>
> 对照实现:`migrations/0001_init.sql`、`migrations/0002_fts.sql`、运行时建表的 `src/landiq_rag/store/db.py`。SQL 与标识符保留英文,末尾附中英术语表。

---

## 1. 概述

- **引擎**:PostgreSQL 16,启用扩展 `vector`(pgvector,向量类型与 ANN 索引)与 `pgcrypto`(`gen_random_uuid()`)。
- **本地**:Docker,宿主端口 5433。**AWS(Track B)**:EC2 上的 Postgres + pgvector + pgbouncer。
- **核心设计原则**:**用结构与约束表达正确性**。隔离、版本一致性、模型不混用、状态合法性等关键不变量,尽量由 schema(外键、唯一约束、ENUM、定维向量列、物理分表)在引擎层强制,而非依赖应用代码自觉。
- **运行时权威**:活动 embedding 模型、分块参数等存于 `rag_config` 表,数据库为运行时权威来源,环境变量仅首启播种。

### 表清单

| 表 | 角色 | 来源 |
|---|---|---|
| `address` | 地址,隔离与分区单元 | migration |
| `document` | 逻辑文档(每地址每 upload_id 一条) | migration |
| `chunk` | 带出处的文本块(含生成列 `fts`) | migration(`fts` 在 0002) |
| `embedding_<model>` | 每模型一张的向量表 | **运行时建** |
| `embedding_model` | 每模型向量表的注册表 | migration |
| `ingest_job` | 摄取队列 + 状态记录(F12) | migration |
| `rag_config` | 运行时配置 KV | migration |
| `query_log` | 每次查询的可观测性 | migration |
| `schema_migrations` | 已应用迁移的跟踪表 | 迁移器自建 |

### 关系总览

```
address ──1:N──> document ──1:N──> chunk ──1:1──> embedding_<model>
   │                                  │
   │  address_id 去规范化下沉到 chunk 与 embedding
   │                                  └── fts (generated tsvector, GIN)
   └──1:N──> chunk (直接外键, 便于隔离过滤与级联)

document ──1:N──> ingest_job        (FK 可空; rebuild 任务无 document)
embedding_model  ··逻辑注册··> embedding_<model>
rag_config       ··逻辑引用 active model··> embedding_model
query_log        ··逻辑引用(无 FK)··> address / model
```

---

## 2. 逐表详解

### 2.1 `address` — 地址 / 隔离单元

地址是访问控制与数据分区的最小单元。跨地址数据泄漏被定义为正确性缺陷(PRD 8.1)。

| 列 | 类型 | 说明 |
|---|---|---|
| `address_id` | TEXT, **PK** | 业务侧地址标识(非自增,调用方给定) |
| `display_name` | TEXT, NOT NULL | 展示名 |
| `created_at` | TIMESTAMPTZ, NOT NULL, DEFAULT now() | 创建时间 |

### 2.2 `document` — 逻辑文档

一个 `(address_id, upload_id)` 唯一对应一份逻辑文档;其内容可随重新上传产生多个版本。

| 列 | 类型 | 说明 |
|---|---|---|
| `document_id` | UUID, **PK**, DEFAULT gen_random_uuid() | 内部主键,跨版本不变 |
| `address_id` | TEXT, NOT NULL, **FK → address** ON DELETE CASCADE | 所属地址 |
| `upload_id` | TEXT, NOT NULL | 调用方给定的文档身份(缺省回退文件名,见 §4.4) |
| `content_type` | TEXT, NOT NULL | MIME 类型 |
| `content_hash` | TEXT, NOT NULL | 内容 SHA-256;幂等闸门(§4.4) |
| `storage_ref` | TEXT, NOT NULL | 原始字节的存储地址(`s3://…` / `file://…`),原件不入库(§4.7) |
| `original_name` | TEXT | 原始文件名 |
| `live_version` | INT, NOT NULL, DEFAULT 0 | 活动版本指针;0 表示尚未 live(§4.3) |
| `ingest_ts` | TIMESTAMPTZ, NOT NULL, DEFAULT now() | 最近摄取时间 |

**约束**:`UNIQUE (address_id, upload_id)`。**索引**:`idx_document_address (address_id)`。

### 2.3 `chunk` — 文本块(带出处)

文档抽取后切分出的可检索单元,携带足以人工核对的出处信息。

| 列 | 类型 | 说明 |
|---|---|---|
| `chunk_id` | UUID, **PK**, DEFAULT gen_random_uuid() | 主键;被向量表外键引用 |
| `document_id` | UUID, NOT NULL, **FK → document** ON DELETE CASCADE | 所属文档 |
| `address_id` | TEXT, NOT NULL, **FK → address** ON DELETE CASCADE | 下沉的隔离键(§4.1) |
| `doc_version` | INT, NOT NULL | 版本号;查询恒过滤 `= document.live_version` |
| `ordinal` | INT, NOT NULL | 文档内顺序 |
| `page_number` | INT | 出处:页码 |
| `paragraph_index` | INT | 出处:段索引 |
| `char_start` / `char_end` | INT | 出处:原文字符区间 |
| `text` | TEXT, NOT NULL | 块文本 |
| `token_count` | INT | token 数 |
| `fts` | tsvector, **GENERATED ALWAYS … STORED** | `to_tsvector('english', text)`,自动维护,供 BM25(§4.6) |

**约束**:`UNIQUE (document_id, doc_version, ordinal)`(新旧版本并存不冲突)。**索引**:`idx_chunk_address`、`idx_chunk_document`、`idx_chunk_doc_ver (document_id, doc_version)`、`idx_chunk_fts` GIN(fts)。

### 2.4 `embedding_<model>` — 每模型一张的向量表(运行时建)

每个 embedding 模型一张物理表,表名由 `model_id` 规整而来(如 `embedding_openai_text_embedding_3_small`)。首次使用某模型时由 `db.py::ensure_embedding_table` 创建,migration 中不存在。

| 列 | 类型 | 说明 |
|---|---|---|
| `chunk_id` | UUID, **PK**, **FK → chunk** ON DELETE CASCADE | 与 chunk 一一对应;删 chunk 级联删向量 |
| `address_id` | TEXT, NOT NULL | 下沉的隔离键,供向量检索直接过滤 |
| `model_id` | TEXT, NOT NULL | 冗余(表已模型专属),仅供校验/排错 |
| `dim` | INT, NOT NULL | 维度 |
| `embedding` | vector(`<dim>`), NOT NULL | 定维向量列 |

**索引**:`idx_<table>_addr (address_id)`;`idx_<table>_hnsw` USING hnsw(embedding vector_cosine_ops) **仅当 dim ≤ 2000**(HNSW 维度上限),超限则不建 ANN 索引、退化为精确扫描。建表后向 `embedding_model` 登记。设计意义见 §4.2。

### 2.5 `embedding_model` — 向量表注册表

记录每个模型对应的表与维度,与 `rag_config` 共同回答"该查哪张向量表"。

| 列 | 类型 | 说明 |
|---|---|---|
| `model_id` | TEXT, **PK** | 模型标识,如 `hash:feature-256` |
| `dim` | INT, NOT NULL | 维度 |
| `table_name` | TEXT, NOT NULL | 对应物理表名 |
| `created_at` | TIMESTAMPTZ, NOT NULL, DEFAULT now() | 登记时间 |

### 2.6 `ingest_job` — 摄取队列 + 状态记录

一张表两用:既是 F12 的可观测状态记录,又是本地任务队列(§4.5)。

| 列 | 类型 | 说明 |
|---|---|---|
| `task_id` | UUID, **PK**, DEFAULT gen_random_uuid() | 任务句柄(返回给调用方) |
| `document_id` | UUID, **FK → document** ON DELETE CASCADE, **可空** | rebuild 任务无关联文档,故可空 |
| `address_id` | TEXT, NOT NULL | 地址(rebuild 全局时为 `*`) |
| `upload_id` | TEXT, NOT NULL | 文档身份 |
| `content_hash` | TEXT, NOT NULL | 内容指纹 |
| `model_id` | TEXT, NOT NULL | 目标模型 |
| `doc_version` | INT, NOT NULL | 目标版本 |
| `job_type` | TEXT, NOT NULL, DEFAULT 'ingest' | `'ingest'` \| `'rebuild'` |
| `state` | **ingest_state** ENUM, NOT NULL, DEFAULT 'pending' | 状态机(见下) |
| `chunk_count` / `embedding_count` | INT, DEFAULT 0 | 进度计数 |
| `attempts` / `max_attempts` | INT, DEFAULT 0 / 5 | 重试计数与上限 |
| `failed_step` / `error_type` / `error_message` | TEXT | 失败定位(不静默) |
| `tokens_embedded` | BIGINT, DEFAULT 0 | 累计 token |
| `cost_usd` | NUMERIC(12,6), DEFAULT 0 | 累计成本 |
| `worker_id` | TEXT | 领取者 |
| `claimed_at` / `lease_until` | TIMESTAMPTZ | 领取时间 / 租约到期(崩溃恢复) |
| `created_at` | TIMESTAMPTZ, NOT NULL, DEFAULT now() | 入队时间 |
| `finished_at` | TIMESTAMPTZ | 终态时间 |

**ENUM `ingest_state`**:`pending → extracting → chunking → embedding → done`,旁支 `skipped`(内容未变)、`failed`(永久失败)。

**索引**:
- `idx_job_claimable (created_at)` **WHERE state IN ('pending','extracting','chunking','embedding')** — 部分索引,只索引未终态任务(§4.5、§5);
- `idx_job_address (address_id)`、`idx_job_document (document_id)`。

### 2.7 `rag_config` — 运行时配置

数据库为运行时权威。键例:`active_embedding_model`、`chunk_tokens`、`chunk_overlap`。

| 列 | 类型 | 说明 |
|---|---|---|
| `key` | TEXT, **PK** | 配置键 |
| `value` | TEXT, NOT NULL | 值(字符串) |
| `updated_at` | TIMESTAMPTZ, NOT NULL, DEFAULT now() | 更新时间 |

### 2.8 `query_log` — 查询可观测性

每次查询落一条(8.3)。

| 列 | 类型 | 说明 |
|---|---|---|
| `query_id` | UUID, **PK**, DEFAULT gen_random_uuid() | 主键 |
| `address_id` | TEXT, NOT NULL | 查询地址(无 FK,纯记录) |
| `model_id` | TEXT, NOT NULL | 所用模型 |
| `latency_ms` | INT | 延迟 |
| `candidates_examined` | INT | 检视候选数 |
| `chunks_returned` | INT | 返回块数 |
| `cost_usd` | NUMERIC(12,6) | 成本 |
| `created_at` | TIMESTAMPTZ, NOT NULL, DEFAULT now() | 时间 |

### 2.9 `schema_migrations` — 迁移跟踪

迁移器自建。`filename` TEXT PK,`applied_at` TIMESTAMPTZ DEFAULT now()。记录已应用的迁移文件,确保每个恰好执行一次(§6)。

---

## 3. 关系与外键

| 子表.列 | → 父表.列 | 基数 | 删除行为 | 备注 |
|---|---|---|---|---|
| `document.address_id` | `address.address_id` | N:1 | CASCADE | |
| `chunk.document_id` | `document.document_id` | N:1 | CASCADE | |
| `chunk.address_id` | `address.address_id` | N:1 | CASCADE | 下沉键,双路径到 chunk |
| `embedding_<model>.chunk_id` | `chunk.chunk_id` | 1:1 | CASCADE | 跨所有模型表 |
| `ingest_job.document_id` | `document.document_id` | N:1 | CASCADE | 可空(rebuild) |

逻辑引用(无 FK 约束,不由引擎强制):`embedding_model` 注册 `embedding_<model>`;`rag_config.active_embedding_model` 对应 `embedding_model.model_id`;`query_log` 引用 `address`/`model`。

**级联删除链**:删 `address` → `document` → `chunk` → 各模型 `embedding`(同时 `ingest_job` 随 `document` 级联删)。原始字节不在库内,需应用层在删除时另行清理对象存储(§4.7、§7)。

---

## 4. 关键设计决策

### 4.1 地址隔离:`address_id` 下沉 + 强制过滤
`address_id` 冗余存于 `chunk` 与每张 `embedding_<model>`,使检索热路径能直接 `WHERE address_id = :addr`,无需 join 回 `document`。该过滤焊死在唯一检索函数 `store/embeddings.py::hybrid_search` 内,形成单一"咽喉"(chokepoint),调用方无法绕过。代价:写入时各层均须正确填充 `address_id`。

### 4.2 一模型一表(F14)
`vector(N)` 列定维,且不同模型向量不可比。故每模型一张物理表:查询对单表发起,"不混用模型"成为物理事实而非运行时过滤;维度差异天然兼容;HNSW 2000 维上限以"超限不建索引"优雅降级。

### 4.3 版本化与原子切换
变更内容写入 `doc_version = live_version + 1` 的新一批 chunk(与旧版并存,靠 `UNIQUE(document_id, doc_version, ordinal)`),嵌入完成后在单事务内 `set_live_version` + 删旧版本。读路径恒带 `doc_version = live_version`,故读者只见全旧或全新,无"半替换"中间态。

### 4.4 幂等(content_hash)
上传按 `(address_id, upload_id)` 定位文档:不存在→新建(version 1);存在且 `content_hash` 相同→记 `skipped` 不做功;不同→出新版本。`upload_id` 由调用方给定(缺省回退文件名,再回退 hash 前缀)。

### 4.5 队列即表(SKIP LOCKED + 租约)
`ingest_job` 兼作队列。worker 用 `SELECT … FOR UPDATE SKIP LOCKED` 领取,由数据库行锁保证多 worker 不重复处理(此并发安全由 DB 而非应用逻辑保证)。`lease_until` 实现崩溃恢复:领取条件含 `state IN (in-flight) AND lease_until < now()`,租约过期的在途任务可被重新领取。失败写 `failed_step/error_type/error_message`,临时错误退回 `pending` 重试,永久错误置 `failed`,绝不静默。

### 4.6 混合检索的 schema 支撑
`chunk.fts` 生成列 + GIN 索引支撑 BM25;每模型 HNSW 索引支撑向量 ANN。检索 `hybrid_search` 以 RRF 融合两路排名(`1/(rank+60)` 相加),两路均受 §4.1 隔离与 §4.3 版本过滤约束。

### 4.7 原始文件外置(storage_ref)
`document.storage_ref` 仅存指针(`s3://…`/`file://…`),原始字节存对象存储,不入库。保持库精简、备份小、缓存干净;由 `StorageBackend` 端口抽象(本地 FS ↔ S3/MinIO)。代价:删除须两端清理,否则对象孤儿。

---

## 5. 索引清单

| 索引 | 表 | 类型 | 服务的访问模式 |
|---|---|---|---|
| `idx_document_address` | document | btree | 按地址列文档 |
| `idx_chunk_address` | chunk | btree | 隔离过滤 / 级联 |
| `idx_chunk_document` | chunk | btree | 取文档全部 chunk |
| `idx_chunk_doc_ver` | chunk | btree(复合) | 取某文档某版本 chunk |
| `idx_chunk_fts` | chunk | **GIN** | BM25 全文(倒排索引) |
| `idx_job_claimable` | ingest_job | btree **部分索引** | 仅索引未终态任务;队列 claim 热查询 |
| `idx_job_address` | ingest_job | btree | 按地址查任务 |
| `idx_job_document` | ingest_job | btree | 按文档查任务 / 级联 |
| `idx_<model>_addr` | embedding_<model> | btree | 向量检索的地址过滤 |
| `idx_<model>_hnsw` | embedding_<model> | **HNSW** | 余弦 ANN(仅 dim ≤ 2000) |

索引设计原则:每个索引对应一个真实查询;最热的两条路径(地址隔离过滤、队列 claim)各有专门索引(后者用部分索引,使历史任务再多也不拖累)。

---

## 6. 约束清单(每条挡住的错误)

| 约束 | 表 | 保证 |
|---|---|---|
| PK | 全部 | 行唯一可寻址 |
| FK + ON DELETE CASCADE | document/chunk/embedding/ingest_job | 无孤儿子行;删父自动清整支 |
| UNIQUE (address_id, upload_id) | document | 一个文档槽一条逻辑文档 |
| UNIQUE (document_id, doc_version, ordinal) | chunk | 新旧版本并存不冲突 |
| NOT NULL | 各关键列 | 必填不缺 |
| ENUM ingest_state | ingest_job | 状态只能取合法值 |
| vector(dim) 定维 | embedding_<model> | 不同维度/模型不可混入同列 |
| GENERATED … STORED | chunk.fts | tsvector 始终与 text 同步,无需触发器 |

---

## 7. 数据流 → 表

**写路径(上传 → 摄取)**
1. `POST /addresses/{id}/documents`:upsert `address` → 写/更 `document` → 原始字节落 `StorageBackend`(不入库)→ 写 `ingest_job` (pending) → 202 返回 task_id。
2. worker:`claim` `ingest_job`(SKIP LOCKED)→ 从 storage 取字节 → 写新版本 `chunk` → 写 `embedding_<model>` → 单事务翻 `live_version` + 删旧版本 → `ingest_job` 置 done。

**读路径(查询)**
1. `POST /addresses/{id}/query`:读 `rag_config`/`embedding_model` 定位向量表 → 在 `embedding_<model>` + `chunk` 上跑 `hybrid_search`(隔离 + 版本过滤 + RRF)→ 写一条 `query_log`。

**删除**:`DELETE /documents/{id}` 删 `document` 行(级联清 `chunk`/`embedding`/`ingest_job`),并由应用层清理 `storage_ref` 指向的对象。

---

## 8. Schema 演进(migrations)

- `migrations/*.sql` 按文件名顺序应用;`schema_migrations` 跟踪已应用文件,确保每个恰好执行一次。启动时仅应用未跑过的迁移。
- 现有迁移:`0001_init.sql`(核心表与索引、ENUM、扩展)、`0002_fts.sql`(`chunk.fts` 生成列 + GIN)。
- **例外**:`embedding_<model>` 表不进 migration,首次用到某模型时运行时建(`ensure_embedding_table`)。原因:事先不知会用哪些模型,新增模型不应改 migration。
- 故表有两种来路:① 固定结构经 migration;② 每模型向量表经运行时 DDL。

---

## 9. 已知取舍与边角

- **`ingest_job` 随 document 级联删**:删文档会丢失其摄取历史(取舍:数据清洁 vs 审计留存)。
- **重新版本覆盖 S3 同 key 原始字节**:`upsert_document` 在摄取完成前即覆盖写 `storage_ref` 对应对象;新版本永久失败时,live 仍服务旧 chunk,但原始字节已被新内容覆盖。
- **`RAG_INGEST_MAX_ATTEMPTS` 为死配置**:`enqueue` 不写 `max_attempts`,实际取 DB 默认 5。
- **`embedding_<model>.model_id/dim` 冗余**:表已模型专属,仅作校验/排错。
- **`query_log`/`embedding_model` 等无强 FK**:为逻辑引用,引擎不强制。

---

## 术语表(中 / English)

| 中文 | English |
|---|---|
| 地址 | address |
| 文档 | document |
| 分块 | chunk |
| 嵌入 / 向量 | embedding / vector |
| 维度 | dimension (dim) |
| 活动版本指针 | live_version pointer |
| 内容哈希 | content_hash (SHA-256) |
| 原子切换 | atomic switch / flip |
| 级联删除 | ON DELETE CASCADE |
| 去规范化 | denormalisation |
| 外键 | foreign key (FK) |
| 唯一约束 | unique constraint |
| 生成列 | generated column (STORED) |
| 全文检索 | full-text search (FTS) |
| 倒排索引 | inverted index (GIN) |
| 近似最近邻 | approximate nearest neighbour (ANN, HNSW) |
| 部分索引 | partial index |
| 租约 | lease |
| 队列 | queue |
| 迁移 | migration |
| 咽喉点 / 单一关卡 | chokepoint |
| 倒数排名融合 | Reciprocal Rank Fusion (RRF) |

---

## 对照代码位置
- 固定 schema:`migrations/0001_init.sql`、`migrations/0002_fts.sql`
- 运行时建每模型表:`src/landiq_rag/store/db.py::ensure_embedding_table`
- 检索 SQL(隔离 + RRF):`src/landiq_rag/store/embeddings.py::hybrid_search`
- 版本化与级联:`src/landiq_rag/store/documents.py`、`ingest/pipeline.py`
- 队列 / 状态机 / 重试:`src/landiq_rag/store/tasks.py`、`ingest/runner.py`
- 迁移器:`src/landiq_rag/store/db.py::run_migrations`
