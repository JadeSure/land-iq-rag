# LandIQ RAG — 数据库设计原理 与 计算分层取舍

> 目的:把这套 RAG 的**数据库 schema 为什么这么设计**,以及**为什么"算 embedding"这类计算放在数据库之外**,完整捋一遍,供仔细研究与 demo 评审时回答问题。
>
> 本文对照的代码状态:`migrations/0001_init.sql`、`migrations/0002_fts.sql`、运行时建表的 `src/landiq_rag/store/db.py`,以及读写路径 `store/embeddings.py` / `store/documents.py` / `store/tasks.py` / `ingest/pipeline.py`。SQL 与代码标识符保留英文原样;末尾附中英术语对照表。

---

## Part A — 数据库设计原理

### A.0 全局结构

```
address ──1:N──> document ──1:N──> chunk ──1:1──> embedding_<model>   (每个模型一张表)
   │                                   │
   │   address_id 一路下沉到 chunk 和 embedding(刻意去规范化)
   │                                   └── fts tsvector(生成列) + GIN 索引
   │
ingest_job        摄取状态记录 + 本地任务队列(双重身份)
rag_config        运行时配置 KV(active model / chunk 参数,DB 权威)
embedding_model   每模型向量表的注册表
query_log         每次查询的可观测性记录
```

固定建出来的表在 `0001_init.sql`;`embedding_<model>` 系列是运行时由 `db.py::ensure_embedding_table` 按需创建的,migration 里没有。

### A.1 一条贯穿全文的设计哲学

**用结构(schema、约束、物理分表)来保证正确性,而不是把正确性寄托在应用层的运行时检查上。** 能让数据库直接挡住的错误,就不留到 Python 里去防。后面每条设计都是这句话的具体落地:

| 不变量 | 靠什么结构保证 |
|---|---|
| 跨地址不泄漏 | `address_id` 下沉到 chunk/embedding + 检索 SQL 强制过滤 |
| 不同模型向量永不混排 | 一个模型一张物理表(列定维) |
| 读不到半替换的文档 | `live_version` 指针 + 事务内原子翻转 |
| 重传不产生重复 | `UNIQUE (address_id, upload_id)` + `content_hash` 闸门 |
| 不静默失败 | `ingest_state` ENUM 状态机 + 失败列齐全 |

### A.2 address:隔离与分区的最小单元

`address`(`0001:10`)是访问控制和数据分区的边界。PRD 8.1 把**跨地址泄漏定义为正确性缺陷**,不是性能问题。

关键手法:`address_id` 不只挂在 `document`,还**去规范化下沉**到 `chunk`(`0001:37`)和每张 `embedding_<model>` 表(`db.py` 建表语句)。

- **为什么宁可冗余也要下沉**:检索热路径 `hybrid_search`(`store/embeddings.py`)的强制过滤 `WHERE e.address_id = :addr` 要**直接打在向量表和 chunk 表上**,不能依赖逐层 join 回 `document` 才知道归属。这样 `idx_chunk_address` 和每模型的 `idx_<table>_addr` 能直接服务这个 chokepoint,而且"加过滤"在最低层就成立,任何调用方都绕不过去。
- **代价**:写入时必须把 `address_id` 正确填进每一层(`insert_chunks` / `insert_embeddings` 都带着它)。这是用一点写入冗余,换检索路径的简单与安全。

### A.3 document → chunk:版本化与原子切换

`document` 一行 = 一个逻辑文档,`UNIQUE (address_id, upload_id)`(`0001:28`)保证同地址下一个 `upload_id` 只有一条逻辑记录。两个关键列:

- **`content_hash`** —— 幂等闸门(F10)。重传同字节直接记一条 `skipped` job,不做任何工作(`routes_ingest.py` 的 skip 分支 + `tasks.record_skipped`)。
- **`live_version`** —— 原子切换指针(`0001:26`),默认 0(尚未 live)。

**重新摄取变更内容的流程**(`ingest/pipeline.py::run_ingest_job`):

1. 新内容写到 `doc_version = live_version + 1` 的一批**新** chunk 上。`chunk` 的 `UNIQUE (document_id, doc_version, ordinal)`(`0001:46`)让新旧版本物理共存、互不覆盖。
2. 嵌入算在新版本的 chunk 上。
3. **在一个事务里**(`pipeline.py` 的 commit 段)把 `live_version` 翻到新值,并删掉其余版本(`set_live_version` + `delete_other_versions`)。

所有读路径都带 `c.doc_version = d.live_version`,所以读者**要么看到全旧、要么看到全新**,绝不会撞见半替换状态。

**级联删除链**(F11):删 `document` → `chunk` 因 `ON DELETE CASCADE`(`0001:36`)删除 → 每个模型表的 embedding 又因 `chunk_id` 上的 `ON DELETE CASCADE`(`db.py` 建表)删除。一条 DELETE 把派生数据全清。原始字节由应用层在删除时 best-effort 清掉(见 `routes_ingest.py::remove_document`,这是本次修复项)。

### A.4 一模型一表:F14 的结构性保证

最有特色的一处。`pgvector` 的 `vector(N)` 列是**定维**的,1536 维和 1024 维塞不进同一列。所以设计选择是**每个模型一张表** `embedding_<model_slug>`,由 `db.py::ensure_embedding_table` 在运行时按需创建:

```sql
CREATE TABLE embedding_<slug> (
    chunk_id   UUID PRIMARY KEY REFERENCES chunk(chunk_id) ON DELETE CASCADE,
    address_id TEXT NOT NULL,          -- 下沉的隔离键
    model_id   TEXT NOT NULL,          -- 表已是模型专属,这里冗余仅供校验/排错
    dim        INT  NOT NULL,
    embedding  vector(<dim>) NOT NULL
);
CREATE INDEX ... ON embedding_<slug>(address_id);
CREATE INDEX ... USING hnsw (embedding vector_cosine_ops);  -- 仅当 dim ≤ 2000
```

带来的好处:

- **"永不把两个模型的向量混进同一次排序"成了物理事实**,而非运行时 `WHERE model_id = ...` 过滤。查询本身就是对某一张表发起的,结构上不可能串。
- **维度差异天然兼容**:1536 / 3072 / 自托管模型各自维度,各表各管。
- **HNSW 的 2000 维上限**被优雅处理:超过 2000 维的模型建表时不建 ANN 索引(`db.py` 的 `if dim <= 2000`),退化为精确扫描,当前数据量下可接受。
- **切模型 = 重建到新表,零停机**:`embedding_model` 注册表(`0001:96`)记录哪些表已存在;切换时往新表 backfill(`ingest/pipeline.py::run_rebuild_job`),旧表原封不动继续服务,backfill 完成后才翻 `rag_config` 的 active 指针。旧表可作回滚兜底。

### A.5 ingest_job:一张表干两件事

`ingest_job`(`0001:58`)同时是 **F12 状态记录**和**本地任务队列**:

- **状态机**用 `ingest_state` ENUM(`0001:54`)固化:`pending → extracting → chunking → embedding → done`,旁支 `skipped` / `failed`。非法状态写不进去。
- **队列语义**靠 `claim` 的 `SELECT ... FOR UPDATE SKIP LOCKED`(`tasks.py::claim`):多 worker 并发抢任务不重复处理。本地是这张 Postgres 表;AWS 上同一份 `process_job` 改由 SQS + Lambda 驱动,业务体不变。
- **崩溃恢复**:`lease_until` 是租约。claim 时推后租约;worker 崩了租约过期,claim 的 `WHERE state = 'pending' OR (state IN in_flight AND lease_until < now())` 会重新捞起任务。
- **不静默失败(6.5)**:`attempts` / `max_attempts` / `failed_step` / `error_type` / `error_message` 把每次失败钉在行上(`tasks.mark_failed`)。重试与否由 `runner.py::is_retryable` 分类。
- **用量与成本**:`tokens_embedded` / `cost_usd` 累加,接真实模型时能算账。
- **关键索引 `idx_job_claimable`** 是**部分索引**(`0001:83`):只索引未终态的行、按 `created_at` 排,精确匹配 claim 查询,终态行不进索引,队列再长也扫得便宜。

### A.6 两种检索模态都在库内建好索引

`chunk.fts` 是**生成列**(`0002:6`):

```sql
ALTER TABLE chunk ADD COLUMN fts tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(text, ''))) STORED;
CREATE INDEX idx_chunk_fts ON chunk USING GIN(fts);
```

Postgres 在 INSERT/UPDATE 时自动维护它,**不需要触发器**;GIN 索引让 BM25 全文检索在大数据量下也快。再加上每模型表的 HNSW 向量索引,**向量 ANN 与全文 BM25 两条通道都在库内有索引支撑**,`hybrid_search` 用 RRF(Reciprocal Rank Fusion)在一条 SQL 里融合两者(并对 FTS-only、尚无 embedding 的 chunk 用 LEFT JOIN 兜底,避免 rebuild 期间漏召回)。

这一点和 Part B 直接呼应:**检索计算是被刻意下推进数据库的**,这里的 schema 就是为它铺的索引。

### A.7 rag_config 与 embedding_model:运行时的权威

- `rag_config`(`0001:89`)是 KV 表,持有 active embedding model、`chunk_tokens`、`chunk_overlap`。环境变量只在**首次启动播种**(`main.py` lifespan),之后管理员改模型或分块参数(F8)直接改这张表,不必重新部署。
- `embedding_model`(`0001:96`)记录每个 model_id 对应的表名与维度,配合 `rag_config` 共同回答"现在该查哪张向量表"。

### A.8 query_log:可观测性

`query_log`(`0001:104`)记录每次查询的 `latency_ms` / `candidates_examined` / `chunks_returned` / `cost_usd`(8.3)。便于 demo 时展示"检索确实在工作"以及后续做检索质量分析。

### A.9 索引策略一览

| 索引 | 服务的访问模式 |
|---|---|
| `idx_document_address` | 按地址列文档 |
| `idx_chunk_address` | 隔离过滤(chokepoint) |
| `idx_chunk_document` | 删除/取文档全部 chunk |
| `idx_chunk_doc_ver` | 取某文档某版本的 chunk |
| `idx_chunk_fts` (GIN) | BM25 全文检索 |
| `idx_job_claimable` (部分索引) | 队列 claim 热查询 |
| `idx_<model>_addr` | 向量检索的地址过滤 |
| `idx_<model>_hnsw` | 余弦 ANN(仅 ≤ 2000 维) |

### A.10 数据流 → 表 的映射

- **上传**(`POST /addresses/{id}/documents`):写 `address`(upsert)→ 写 `document`(新建或新版本)→ 写 `ingest_job`(pending)。原始字节落 StorageBackend(本地 FS 或 S3),不进 DB。
- **摄取 worker**:claim `ingest_job` → 从存储取回字节 → 写 `chunk`(新版本)→ 写 `embedding_<model>` → 事务内翻 `live_version`、删旧版本 → `ingest_job` 置 done。
- **查询**(`POST /addresses/{id}/query`):读 `rag_config`/`embedding_model` 定位向量表 → 在 `embedding_<model>` + `chunk` 上跑 `hybrid_search` → 写一条 `query_log`。

### A.11 已知取舍 / 边角(研究时留意)

- `ingest_job.document_id` 是 `ON DELETE CASCADE`(`0001:60`):删文档会**连带删掉它的摄取历史**。可观测性 vs 清洁的取舍,目前选了清洁。
- **重新版本会覆盖同 key 的 S3 原始字节**:`upsert_document` 在摄取完成前就把 `storage_ref` 指向新 key 并覆盖写。若新版本摄取永久失败,live 仍服务旧 chunk,但 S3 里的原始字节已是新的(未索引)内容,provenance 会对不上。本地 FS 时代同样存在,低优先级。
- **`RAG_INGEST_MAX_ATTEMPTS` 当前是死配置**:`enqueue` 不写 `max_attempts`,靠 DB 默认 `INT DEFAULT 5`(`0001:71`)。改环境变量无效。
- `model_id` / `dim` 列在已是模型专属的向量表里属冗余,仅用于排错与一致性校验。

---

## Part B — 为什么"计算"放在数据库之外

### B.1 先把"RAG 的计算"拆成两半

并不是所有计算都在 DB 外。**检索那半本来就在库里跑,只有嵌入/摄取那半在外面**,这个划分是刻意的:

| 计算 | 跑在哪 | 为什么 |
|---|---|---|
| 向量 ANN、BM25 全文、RRF 融合排序 | **DB 内**(`hybrid_search` 的 SQL) | 数据本地、集合式、吃索引;搬出来要把大量候选行拉到应用层,慢且蠢 |
| PDF 抽取、分块、生成 embedding | **DB 外**(本地 asyncio worker / AWS 上 SQS+Lambda) | 模型与 IO 重活,详见下 |

### B.2 一个前提事实:pgvector 只存不算

`pgvector` 只负责**存向量、建索引、做相似度搜索**,它本身**不生成 embedding**。所以"算 embedding"必须有个带模型的计算层;问题只是这层放在 Postgres 进程**里面**还是**外面**。下面是选"外面"的理由。

### B.3 放在 DB 外的理由

1. **资源隔离。** 嵌入是 CPU/GPU/IO 密集且突发的(pdfplumber、OCR、几百 MB 的 transformer、或调外部 API)。DB 是有状态、难水平扩展的稀缺资源。不想让一次 OCR 把 CPU 打满、顺带拖垮查询延迟,也不想让 torch 进程和 Postgres 抢内存。
2. **独立伸缩 + Track B 落地形态。** AWS 目标是单台 `t4g.small` 跑 Postgres,摄取走 SQS + Lambda。`ports.py` 的三个 Protocol(StorageBackend / JobQueue / EmbeddingProvider)就是为让摄取能独立搬去 Lambda、而 DB 不动。计算一旦进 DB 进程,这个解耦就没了,也别想在那台小机器的 Postgres 里塞模型推理。
3. **外部 API 不该在 DB 事务里调。** 调 OpenAI/Gemini/Claude 是带重试、限流、超时的网络 I/O;在 DB 函数里干这个会占着连接和锁、极脆弱。现在的 worker 在 DB 外做租约、重试、退避。
4. **崩溃隔离(blast radius)。** 损坏 PDF 让 pdfminer 抛异常,在应用里只是把一个 job 标 failed;若跑在 DB 进程内(plpython),原生库段错误可能直接搞挂 backend。
5. **依赖与镜像。** 应用要 pdfplumber、tiktoken、boto3、anthropic、可选 sentence-transformers;塞进 Postgres 镜像会让 DB 镜像臃肿、难打补丁、难升级。现在 DB 就是标准的 `pgvector/pgvector:pg16`。
6. **可换 provider。** `EmbeddingProvider` 端口让你在 hash / openai / gemini / hf / selfhosted 间切换;在 DB 内做会被那个扩展支持的东西绑死。
7. **语言与生态。** 分块(token 感知)、provenance、抽取降级级联(pdfplumber → OCR → Claude)、版本状态机,这些逻辑用 Python 应用代码 + 单测表达最自然,而非 SQL/plpython。

### B.4 "在 DB 里算"的替代方案及其代价

如果真要把嵌入也塞进数据库,确实有现成工具:

- **PostgresML(`pgml`)**:在 Postgres 进程内嵌一个 Python 运行时,真的在库里跑 transformer 推理。
- **pgai / vectorizer(Timescale)**:提供 SQL 函数去调嵌入 API,还能在 insert 时自动向量化。

代价正是 B.3 的反面:计算与 DB 强耦合、镜像变胖、伸缩与崩溃隔离变差、Track B 的 Lambda 摄取路线基本作废。

**什么时候反而该选 in-DB**:如果项目永远是单机/单库、数据量小、团队想要"insert 即自动嵌入、零 worker"的极简运维,且没有把摄取独立伸缩或上 serverless 的诉求——那 `pgml`/`pgai` 更省事。本项目这三条都不满足(有明确的 AWS Lambda 摄取落地形态),所以选了外置。

### B.5 结论

现在的划分是"**该下推的下推、该隔离的隔离**":

- 把**数据本地、集合式、吃索引**的检索运算(向量 ANN + 全文 + RRF)下推进 DB——这是数据库最擅长的。
- 把**模型推理与文档处理**这种重、突发、易崩、依赖多的活留在可独立伸缩、可换 Lambda 的计算层。

这不是疏忽,正是 `ports.py` 那个 seam 想保住的东西:摄取路径换实现(本地 worker ↔ SQS+Lambda)、存储换实现(本地 FS ↔ S3),而 `ingest/pipeline.py` 和检索路径一行不用动。

---

## 术语对照表(中 / English)

| 中文 | English | 说明 |
|---|---|---|
| 地址 | address | 隔离与分区的最小单元 |
| 文档 | document | 一个逻辑源文件(一般是 PDF) |
| 分块 | chunk | 带 provenance 的可检索单元 |
| 嵌入 / 向量 | embedding / vector | 文本的向量表示 |
| 维度 | dimension (dim) | 向量长度,模型固定 |
| 活动版本指针 | live_version | 原子切换用的指针列 |
| 内容哈希 | content_hash | 幂等闸门(SHA-256) |
| 原子切换 | atomic switch / flip | 事务内翻转 live_version |
| 级联删除 | ON DELETE CASCADE | 删父行自动删派生行 |
| 去规范化 | denormalisation | 把 address_id 下沉冗余存储 |
| 摄取 | ingestion | extract → chunk → embed 全流程 |
| 任务队列 | job queue | 这里即 ingest_job 表 |
| 租约 | lease (lease_until) | 防止崩溃 worker 卡住任务 |
| 部分索引 | partial index | 带 WHERE 的索引 |
| 生成列 | generated column | STORED,自动维护 |
| 全文检索 | full-text search (FTS) | tsvector + GIN |
| 近似最近邻 | approximate nearest neighbour (ANN) | HNSW 索引 |
| 倒数排名融合 | Reciprocal Rank Fusion (RRF) | 融合向量与全文两路排名 |
| 重建 | rebuild | 换模型时 backfill 新表 |
| 端口 / 接缝 | port / seam | 本地与 AWS 的唯一切换点(ports.py) |
| 检索 | retrieval | 查询时的搜索计算(在 DB 内) |
| 爆炸半径 | blast radius | 故障波及范围 |

---

## 对照代码位置

- 固定 schema:`migrations/0001_init.sql`、`migrations/0002_fts.sql`
- 运行时建每模型表:`src/landiq_rag/store/db.py::ensure_embedding_table`
- 检索 SQL(隔离 chokepoint + RRF):`src/landiq_rag/store/embeddings.py::hybrid_search`
- 版本化与级联:`src/landiq_rag/store/documents.py`、`ingest/pipeline.py::run_ingest_job`
- 队列 / 状态机 / 重试:`src/landiq_rag/store/tasks.py`、`ingest/runner.py`
- 本地↔AWS 接缝:`src/landiq_rag/ports.py`、`context.py`、`main.py`
