# LandIQ — MVP Scope for End-of-June Demo

## 1. Purpose of This Document

This document defines what the LandIQ platform will demonstrate at the end of June. It captures what must be shown end-to-end, what is intentionally deferred, what shortcuts are acceptable for demo purposes, and what the success criteria are. It is derived from the decisions taken in the team standup of 2026-05-29 with the boss present.

The MVP is a **locally runnable demonstration** of the platform's core proposition: from a property address, produce a Feasibility Report through coordinated agents, grounded against an address-scoped knowledge base. Polish, cloud deployment, accuracy, and ancillary console features are explicitly out of scope.

---

## 2. Demo Objective

By the end of June, the team must be able to demonstrate, on a local machine, the following end-to-end story:

1. A user opens the Portal and creates a project by entering a property address.
2. The user uploads supporting documents to that project.
3. The system ingests those documents into an address-scoped RAG index in the background.
4. The user triggers Feasibility Report generation.
5. A multi-agent pipeline assembles data, runs per-section sub-agents, evaluates outputs, and emits a Markdown Feasibility Report.
6. The user views the report inside the Portal.

The demo does not need to be deployed to the cloud. The demo does not need to be accurate. The demo does need to look and feel like a complete product walkthrough.

---

## 3. In Scope

### 3.1 Portal

| Feature | Required behavior |
|---|---|
| Project creation by address | Address is supplied by the user. The system resolves the address through Google API. Identical addresses resolve to the same project; no duplicates are created. The address itself becomes the project name. |
| Project list and detail view | The user can see their projects and open one. |
| Document upload per project | The user can upload PDF documents to a project. Documents are scoped to the project's address. |
| Document list per project | The user can see which documents belong to a project. |
| Report generation trigger | The user can initiate Feasibility Report generation for a project. |
| Report progress view | The user can see that the system is working, including high-level progress over the configured sub-agents. |
| Report viewer | The user can view the generated Markdown Feasibility Report inside the Portal. |
| Analysis tier selector | At report generation time the user can select among three tiers — Quick Scan, Standard, Deep Dive — each of which routes to a different model behind the scenes. |

### 3.2 RAG

| Capability | Required behavior |
|---|---|
| Per-address isolation | All ingested documents, chunks, and vectors are scoped to a single address. No cross-address retrieval. |
| Asynchronous ingestion | Upload returns immediately; chunking and embedding run in the background. |
| Chunking + embedding pipeline | PDFs are extracted, chunked, embedded, and persisted. |
| Vector store | A working vector index that supports address-filtered retrieval. |
| Query interface | A retrieval interface scoped per address, returning ranked chunks with provenance, callable by the Agent system. |
| Embedding model configuration | The active embedding model is configurable in one place. |

### 3.3 Agent

| Capability | Required behavior |
|---|---|
| Orchestrator agent | Plans which sub-agents to run for a given address and dispatches them. Expressed as a skill, not hard-coded. |
| Sub-agents per report section | At least the sections required for a recognisable Feasibility Report (for example flood, bushfire, heritage, zoning) are each handled by a dedicated sub-agent or skill. |
| Evaluation step | Sub-agent outputs are passed through an evaluation step before being admitted to the final report. |
| Retry and fallback | Transient failures (rate limits, provider errors) are retried up to a configurable ceiling. |
| Final aggregator | Accepted sub-agent outputs are composed into a single Markdown Feasibility Report. |
| Citation or inferred marker | Every factual claim in the report carries either a citation or an explicit model-inferred marker. |
| Early termination | The orchestrator may terminate further work when applicable rules indicate analysis is not warranted, and records the reason. |
| Short-term session memory | A deterministic per-run working area is used and discarded at the end of the run. |
| Long-term memory (rules) | Federal, state, and council-level rules are accessible to agents as long-term memory. |
| Long-term memory (agent-curated) | Agents may persist learnings across runs into a reviewable long-term memory store. |

### 3.4 Model Routing

| Capability | Required behavior |
|---|---|
| Per-tier model binding | Quick Scan, Standard, and Deep Dive each map to a model selection. |
| Configuration-driven | Changing which model serves which tier is a configuration change, not a code change. |
| No single-provider lock-in | The routing layer must be able to address more than one provider, even if only one is exercised on demo day. |

### 3.5 Super-Admin Console

The console exists in the demo as a minimal surface, sufficient to show that administrative control points exist. The following are in scope:

- User management.
- Organisation management.
- Audit log view.
- Integration page surface where the active embedding model and per-tier model bindings are visible and editable.

### 3.6 Data Coverage

For demo addresses, sufficient data must be pre-prepared so that the live flow produces a substantive report rather than an empty one:

- NSW data must be fully prepared.
- VIC data must be substantially prepared.
- QLD data must be at least partially prepared.
- Council-level DCP material for the demo addresses must be available to the agents in structured form.

For addresses outside prepared coverage, the agents may produce model-inferred content provided it is clearly marked as such.

---

## 4. Out of Scope

The following are explicitly **not** part of the June demo. They may be discussed but will not be shown working.

### 4.1 Deployment and infrastructure
- Cloud deployment of any kind. The demo is local.
- AWS Bedrock Knowledge Base or any other managed retrieval service with high minimum spend.
- Production-grade persistence strategy for agent memory in cloud environments.
- Production observability, alerting, multi-tenant isolation at the infrastructure level.

### 4.2 Portal features
- Job and job-alert features.
- Building-management views.
- Polish and refinement of pages outside the core demo flow. Non-core pages may display a "coming soon" state.

### 4.3 Console features
- Anything beyond user management, organisation management, audit log, and the integration surface for model configuration.

### 4.4 Accuracy and quality
- Production-grade accuracy of the Feasibility Report. The report must be coherent and demo-credible; it does not need to be factually defensible.
- Full anti-hallucination guarantees. The grounding rule is that inferred content must be marked, not that inferred content must be eliminated.
- Image-only or scanned PDFs as a first-class RAG input.
- Re-ranking, hybrid retrieval, or other retrieval quality improvements beyond a single embedding-based dense retrieval.

### 4.5 Agent capabilities
- Continuous self-improvement loops running outside of user-triggered runs.
- Full multi-provider model routing in production form. A minimal router that satisfies the per-tier requirement is sufficient.
- Fully autonomous long-term-memory curation without any human review path. A review surface must exist; sophisticated review workflow does not.

### 4.6 Proprietary data integrations
- CoreLogic, Landchecker, or other paid third-party data integrations via browser plugin or otherwise. These are not required for the demo. If invoked at all, they are stubbed.

### 4.7 Output formats
- PDF report output. The Feasibility Report is delivered as Markdown only.

---

## 5. Acceptable Shortcuts for Demo Purposes

The following compromises are explicitly permitted to make the demo achievable. They are recorded here so that no one is surprised on demo day, and so that the team and reviewers share the same understanding of what is real and what is staged.

| Shortcut | Rationale |
|---|---|
| The demo runs entirely on a local machine. | Cloud deployment is out of scope and would consume disproportionate effort relative to demo value. |
| Demo addresses are pre-seeded with documents and supporting data. | The demo must look substantive in a few minutes, not require live data ingestion across the public web. |
| All agents may be served by a single inexpensive model behind different prompts. | Model cost is the dominant operating risk. The router and tier selector must still be visible in the UI; the underlying call may be uniform. |
| Where authoritative data is unavailable, agents may produce model-inferred content. | Anything else would block the demo. The content must be visibly marked as inferred. |
| Per-section sub-agents may be a small representative set rather than the full eventual catalog. | A few well-behaved sub-agents are more convincing than many failing ones. |
| The evaluation step may be a lightweight pass rather than a full rubric. | Sufficient to demonstrate that the pattern exists. |
| The report's structure may be templated rather than freely composed. | Predictability matters for demo. |
| Console pages outside the in-scope subset may display "coming soon". | Honest signal that the surface exists without committing build effort. |

---

## 6. Success Criteria for the Demo

The demo is considered successful if all of the following can be shown live, on a local machine, from a cold start within a small number of minutes:

1. A new project can be created from an address typed into the Portal, and the same address typed twice resolves to the same project.
2. Documents can be uploaded to the project; the user sees an indication that ingestion is happening in the background.
3. The user can trigger Feasibility Report generation and select one of Quick Scan, Standard, or Deep Dive.
4. The Portal shows per-sub-agent progress while the run is executing.
5. A Markdown Feasibility Report is produced and displayed inside the Portal, with sections corresponding to the configured sub-agents.
6. Every factual claim in the report is visibly either cited or marked as model-inferred.
7. The super-admin console can be opened to show users, organisations, the audit log, and the integration surface where models are bound to tiers.
8. Changing the model bound to a tier in configuration takes effect on the next run without code changes.
9. At least one demo address that exercises NSW data produces a substantive report driven by real source material rather than purely by inference.

---

## 7. Demo Risks and Mitigations

| Risk | Mitigation accepted for demo |
|---|---|
| External data sources unavailable on demo day. | All demo addresses are pre-seeded; live external fetches are not on the critical path. |
| Model provider rate limits during demo. | A single demo address is rehearsed against a single inexpensive model. The router config can be switched in advance. |
| RAG ingestion too slow during a live upload. | Pre-ingested fixtures are loaded for the rehearsed demo addresses. Live upload is shown as a capability, not as a timing-critical step. |
| Agent run failures mid-demo. | Retry ceilings are tuned to be visible but not blocking. A rehearsed demo address has been pre-validated. |
| Memory layer paths drift between agents. | A single agreed location is fixed before demo prep begins. |
| Report contains obviously wrong content. | Inferred content is visibly marked. The narrative emphasises pattern and grounding, not factual correctness. |
| Console looks empty. | Coming-soon placeholders are in place for non-scoped surfaces. |

---

## 8. What This Demo Is Not Claiming

To prevent miscommunication, the demo will not claim, and the team will not represent, any of the following:

- That the platform is deployed to the cloud.
- That the Feasibility Reports are accurate enough for commercial delivery.
- That the agents are running fully autonomously, continuously, or improving themselves over time.
- That the system has integrated paid third-party datasets.
- That every Australian state and council is covered to the same depth.
- That the model routing is exercising multiple providers under live load.

The demo is claiming, and the team will represent, the following:

- That a coherent end-to-end pipeline exists, from address input to Markdown report output.
- That the architecture cleanly separates retrieval (RAG), reasoning (Agents), memory, and model routing.
- That the platform is positioned to expand along each of those axes without rework of the core flow.
