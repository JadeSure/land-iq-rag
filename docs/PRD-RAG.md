# PRD — Retrieval-Augmented Generation System (v2)

## 1. Overview

### 1.1 Purpose
RAG 系统是 LandIQ 平台的地址级知识检索层。它摄取用户上传的与特定地址关联的源文档，将其转化为语义可检索单元，持久化向量表示，并向下游消费者（主要是报告生成 Agent）暴露查询接口，使其输出能植根于源材料。

### 1.2 Scope
本文档仅规范 RAG 系统本身，不规定实现方式或部署拓扑。

### 1.3 Definitions
- **Address（地址）**: 属性标识符，知识隔离的最小单元。所有文档、Chunk、向量均绑定到唯一地址。
- **Document（文档）**: 用户上传的源文件（通常为 PDF），包括可行性报告（Feasibility Report）、DCP、规划文件等。
- **LTM（Long-Term Memory / `ltm/` 目录）**: 由 Jing 维护的静态知识库，包含报告结构、计算口径、法规、Benchmark。**不属于 RAG**，以静态文件直读进 Prompt 的方式使用，走主路径，零成本，是报告专业度的来源。
- **地址文档 RAG**: 用户上传的该地址专属文档（Feasibility Report、DCP 等），走 chunking + embedding + 向量检索流程。这是本文档规范的系统。
- **Chunk**: 从文档中提取的文本片段，适合 embedding 与检索。
- **Embedding**: 由配置的 embedding 模型生成的稠密向量表示。
- **Consumer（消费者）**: 向 RAG 系统发出检索查询的任何组件（Agent、报告生成器等）。

---

## 2. Context and Goals

### 2.1 Problem Statement
平台为单个地址生成可行性分析报告。每个地址会积累异构支撑材料（规划文件、法规、市场报告、法律文件、财务预测）。生成 Agent 需要在查询时从该材料中检索有依据、可引用的证据。

**两个知识来源必须严格分开：**

| 知识来源 | 类型 | 使用方式 | 说明 |
|---|---|---|---|
| `ltm/` 目录 | 静态知识 | 按章节直读入 Prompt | 报告结构、法规、计算口径；**必走主路径，不经过 RAG** |
| 地址文档 RAG | 动态知识 | Chunking + Embedding + 向量检索 | 用户上传的 Feasibility Report、DCP 等；**本文档规范此部分** |

**地址文档 RAG 进主路径，但可降级**：有上传文档时检索引用，无文档时只靠 `ltm/` + detail 数据也能出报告。RAG 不是阻塞点，demo 路径上"有文档时报告能引用到"是唯一强制保证。

### 2.2 Goals
- 提供严格限定于单一地址的检索。
- 将文档摄取与任何用户可见同步工作流解耦。
- 允许更换 embedding 模型而不修改代码。
- 每个检索 Chunk 均可溯源至其源文档及在文档中的具体位置。
- **本地可运行**：本阶段目标是本地 pipeline 能跑通，不上云、不追精度。

### 2.3 Non-Goals
- 跨地址检索或地址间知识共享。
- 图片页 / 扫描件 PDF 的一等公民支持。
- 实时或亚秒级摄取延迟。
- 作为原始文档的系统记录。
- Bedrock Knowledge Base 等托管 RAG 服务（最低成本不兼容低流量运营）。
- 法规数据（DCP、SEPP、LEP 等全量文件）的向量化存储——法规内容走 `ltm/` 静态读取路径，**不进 RAG**。

---

## 3. Users and Use Cases

### 3.1 Primary Consumer — Report Generation Agents
Agent 以特定地址为范围，向 RAG 系统发出自然语言查询，接收带引用的有序文本 Chunk，作为生成报告的依据。

典型查询形式：
- "该地块的 setback 要求是多少？"
- "该地址的容积率限制？"
- "Feasibility Report 中的开发成本估算"
- "该地块适用哪些规划约束？"

### 3.2 Secondary Consumer — Human Contributor (Jing)
人工贡献者上传、替换或移除特定地址的源文档。摄取在后台进行，贡献者无需等待 embedding 完成。

### 3.3 Administrative Consumer
管理员选择当前活跃的 embedding 模型，并在模型变更时触发重建。在 Console/Settings 界面通过下拉选择，不同 agent 或功能模块可绑定不同 embedding 模型配置。

---

## 4. Functional Requirements

| ID | Requirement |
|---|---|
| F1 | 系统必须接受绑定到唯一地址的文档上传。 |
| F2 | 系统必须从支持的文档格式（首要：PDF）中提取文本内容。 |
| F3 | 系统必须将提取的文本分割为适合 embedding 与检索的 Chunk。 |
| F4 | 系统必须使用当前活跃 embedding 模型为每个 Chunk 生成向量 embedding。 |
| F5 | 系统必须持久化文档、Chunk 和 embedding，三者均可按地址检索。 |
| F6 | 系统必须暴露检索接口，接受查询和地址，返回限定于该地址的有序 Chunk。 |
| F7 | 每个返回的 Chunk 必须附带足以识别来源文档及其在文档中位置的溯源信息。 |
| F8 | 系统必须支持通过配置更换活跃 embedding 模型（OpenAI `text-embedding-*` 系列为默认，支持切换）。 |
| F9 | 系统必须支持重建操作，在新模型下重新生成所有 embedding。 |
| F10 | 重新摄取同一文档时，新 Chunk 和 embedding 必须替代旧版本，不得重复。 |
| F11 | 移除文档后，其 Chunk 和 embedding 不再可被检索。 |
| F12 | 系统必须报告在途摄取工作的状态，消费者可据此判断某地址是否已完全索引。 |
| F13 | 系统必须以异步方式执行摄取，上传请求必须在文档持久化后立即返回，无需等待 chunking 和 embedding 完成。 |
| F14 | 系统必须区分不同 embedding 模型产生的向量，同一查询不得混合来自不同模型的向量进行排名。 |

---

## 5. Data Requirements

### 5.1 Identity and Scoping
- 每个文档、Chunk 和 embedding 必须携带其摄取时对应的地址标识符。
- 地址标识符是访问控制的主要单元，也是检索的主要分区依据。
- **一地址一套 pgvector**：每个地址对应独立的向量集合，物理或逻辑隔离均可，但查询时不得跨地址返回数据。

### 5.2 Document Records
文档记录至少包含：地址标识符、上传标识符、内容类型、摄取时间戳、足以恢复原始文件的引用。

### 5.3 Chunk Records
Chunk 记录至少包含：父文档标识符、文本内容、文档内位置引用（如页码、段落编号），足以人工验证引用。

### 5.4 Embedding Records
Embedding 记录至少包含：Chunk 标识符、向量值、产生该向量的 embedding 模型标识符、向量维度。

不同 embedding 模型产生的向量必须可区分，查询时不得混合。

### 5.5 Address Isolation
任何查询、检索响应或管理列表，均不得暴露非请求地址的数据。跨地址泄漏是正确性缺陷，而非性能问题。

---

## 6. Behavioral Requirements

### 6.1 Asynchrony
摄取必须相对于上传请求异步执行。上传请求在文档被持久化接受后立即返回，chunking 和 embedding 在后台完成（Lambda 触发或定时任务均可）。

### 6.2 Idempotency
- 重新摄取未变更的文档不得产生重复 Chunk 或 embedding。
- 在相同上传标识下重新摄取已变更文档，必须替代该文档的早期 Chunk 和 embedding。

### 6.3 Model Change Semantics
- 更换活跃 embedding 模型不得损坏或静默失效已有数据。
- 模型变更后，系统必须能区分过期 embedding（由旧模型产生）与当前 embedding。
- 重建操作必须产生一致的索引，其中每个 Chunk 在当前活跃模型下都有 embedding。
- 单次查询中不得混合来自不同模型的 embedding 进行排名。

### 6.4 Citation Fidelity
每个从查询返回的 Chunk 必须可溯源至与查询地址关联的特定文档中的特定位置。

### 6.5 Failure Semantics
- 一个文档的 chunking 或 embedding 失败，不得阻塞同一地址或其他地址的其他文档的摄取。
- 失败必须通过摄取状态接口可报告；静默失败不可接受。

### 6.6 Graceful Degradation
RAG 不是阻塞点。若某地址无已上传文档或 RAG 索引未就绪，报告生成 Agent 必须能退回至仅依赖 `ltm/` 静态知识 + 结构化地块数据出报告，而不崩溃。**"有文档时报告能引用到"是唯一强制保证**。

---

## 7. Interface Requirements

系统至少必须通过稳定接口暴露以下能力。精确的传输方式和载荷形态不在此约束。

| Capability | Description |
|---|---|
| Ingest | 接受绑定地址的文档；返回可用于跟踪摄取进度的 handle。 |
| Ingestion Status | 给定 handle 或地址，返回在途及已完成摄取工作的状态（state、chunk 数、embedding 数、模型标识、错误信息）。 |
| Query | 接受自然语言查询和地址；返回限定于该地址的有序 Chunk，附带溯源。 |
| Document Removal | 移除已摄取文档及其所有派生 Chunk 和 embedding。 |
| Embedding Model Configuration | 读取和更新当前活跃 embedding 模型的标识符（Console/Settings 界面下拉选择）。 |
| Rebuild | 触发在当前活跃 embedding 模型下重新生成所有 embedding，可选择限定于某地址。 |

---

## 8. Quality Attributes

### 8.1 Isolation
跨地址泄漏是正确性缺陷，不是性能问题。任何可能返回非查询地址数据的代码路径均视为缺陷。

### 8.2 Cost Discipline
- 系统必须可在无最低消费不成比例的托管服务的情况下运营（排除 Bedrock Knowledge Base 等）。
- 使用 pgvector on EC2（或本地 Postgres）即可满足向量存储需求，数据量不大，定期备份即可。
- 每文档摄取成本和每查询成本必须可观测。
- Embedding 模型默认使用 OpenAI `text-embedding-*` 系列；后续可在 Console 中切换，但切换时需告知调用方这是破坏性操作。

### 8.3 Observability
每个摄取任务必须暴露：状态、生成 chunk 数、生成 embedding 数、使用的模型标识符、任何错误条件。每次查询必须暴露：延迟、考察 chunk 数、返回 chunk 数。

### 8.4 Determinism Under Fixed Inputs
给定相同文档内容、相同 chunking 配置和相同 embedding 模型，重复摄取必须产生等价的检索行为。

### 8.5 Operational Safety
重建操作必须可安全中断和恢复，不损坏索引。

### 8.6 Local-First（本阶段）
本阶段目标是本地 pipeline 能跑通，Shawn 本地搭通即可。云端部署（Lambda 触发 + EC2 pgvector）为后续阶段，不是当前阻塞点。

---

## 9. Constraints

- 地址级隔离是硬性产品约束，不受性能权衡影响。
- Embedding 模型是可更换组件；系统不得耦合于任何单一提供商。
- 切换 embedding 模型是针对现有向量的破坏性操作，调用方必须对此知情。
- 最低持有成本与低流量运营不兼容的托管检索服务被排除（如 Bedrock Knowledge Base）。
- **法规类静态文档（DCP、LEP、SEPP、联邦/州/Council 规则等）不进 RAG**，走 `ltm/` 直读路径；RAG 仅处理用户上传的地址专属文档。

---

## 10. Dependencies

- 用于持久化原始上传文档的耐久存储（本地文件系统或 S3，本阶段本地即可）。
- 支持元数据过滤检索的向量存储，地址为主要过滤器（pgvector on Postgres，本阶段本地运行）。
- 一个或多个 embedding 模型提供商的访问（默认 OpenAI）。
- 可选取活跃 embedding 模型的配置界面（Console/Settings 下拉）。

---

## 11. Acceptance Criteria

- 给定一个地址及一或多个已上传源文档，摄取异步完成，地址范围查询接口返回带引用的有序 Chunk，引用可溯源至源文档。
- 给定两个不同地址，针对其中一个地址的查询绝不返回属于另一个地址的 Chunk。
- 给定活跃 embedding 模型变更后执行重建，所得索引在新模型下内部一致，检索不再引用旧模型产生的向量。
- 给定重新上传的文档，索引反映新内容，不含旧版本的孤立 Chunk 或 embedding。
- 给定已移除的文档，后续查询不返回任何源自该文档的 Chunk。
- 给定一个文档摄取失败，同一地址及其他地址的其他文档继续摄取，失败通过摄取状态接口可报告。
- 给定某地址无上传文档或 RAG 未就绪，报告生成 Agent 能降级至 `ltm/` 路径出报告，不崩溃。

---

## 12. Open Questions

- 主要为图片或扫描页的文档如何处理（文本提取产出极少）？后续优化，当前 pipeline 搭通即可。
- 超大单文档如何分区，在保留结构上下文的同时不超出 embedding/检索实际限制？
- 应在哪些条件下自动触发重建（检测到文档变更、定时、仅限显式管理员操作）？
- 仅稠密检索是否足以满足报告生成 Agent 的依据质量要求，还是需要重排序阶段？
- 文档及派生数据的留存策略是什么（地址归档后）？
