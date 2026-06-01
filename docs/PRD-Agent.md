# PRD — Agent System

## 1. Overview

### 1.1 Purpose
The Agent system is the autonomous reasoning and orchestration layer of the LandIQ platform. Given a property address, it asynchronously assembles relevant data, dispatches specialised sub-agents to analyse distinct concerns (such as flood, bushfire, heritage, zoning, and other diligence dimensions), evaluates and reconciles their outputs, and composes a Feasibility Report. The system is designed around the assumption that the underlying language models are capable of substantive reasoning and that the orchestration layer's responsibility is to plan, route, ground, verify, and persist.

### 1.2 Scope
This document specifies the behavioral, structural, and quality requirements of the Agent system. It defines what the system must do given an address as input, how it must coordinate internal components, how it must handle missing data and failures, how it must remember what it has learned, and how it must expose its work to consumers and reviewers. It does not prescribe a specific orchestration framework, model provider, or deployment topology.

### 1.3 Definitions
- **Address**: A property identifier supplied as the system's primary input. The unit of work and of project identity.
- **Project**: The persistent record of all work performed for a single address.
- **Orchestrator agent**: The agent responsible for planning and dispatching sub-agents based on rules and available data.
- **Sub-agent**: An agent specialised to a single section or concern of the Feasibility Report.
- **Skill**: A reusable, declaratively described capability that an agent can invoke. The orchestrator itself is expressed as a skill.
- **Evaluation agent**: An agent that verifies the outputs of other agents for factual grounding and citation integrity.
- **Memory**: Persistent state, separated by lifetime and scope, used by agents to retain and recall information across runs.
- **Model router**: The component through which any agent's model selection is resolved at call time.
- **Feasibility Report**: The composed, citable Markdown deliverable that the system produces for an address.

---

## 2. Context and Goals

### 2.1 Problem statement
Producing a feasibility analysis for a property today requires assembling information from disparate jurisdictions, planning instruments, geospatial data sources, and proprietary datasets, and synthesising those inputs into a coherent narrative. The work is repetitive in shape but variable in content, and authoritative data is frequently incomplete. The Agent system exists to perform this synthesis end-to-end from a single address input, to be resilient to partial data, and to remain auditable and grounded.

### 2.2 Goals
- Produce a Feasibility Report end-to-end from an address input, without requiring synchronous user interaction during generation.
- Continue to produce a useful report when some data sources are unavailable.
- Ground every factual claim in either retrieved evidence or an explicit model-inferred marker.
- Allow the model used at each decision point to be selected by configuration rather than by code change.
- Accumulate reusable knowledge across runs through a memory subsystem that is reviewable by a human.

### 2.3 Non-goals
- Synchronous, request-response report generation.
- Producing reports in formats other than Markdown.
- Sharing memory across addresses such that one project's specifics influence another's analysis.
- Fully autonomous self-improvement without any human review surface.
- Replacing the RAG system; the Agent system is a consumer of RAG.

---

## 3. Users and Use Cases

### 3.1 Analyst — initiator
An analyst submits an address. The system creates or resolves a project for that address and begins asynchronous report generation. The analyst may check progress, inspect intermediate outputs, and retrieve the final report when it is available.

### 3.2 Reviewer — memory custodian
A reviewer periodically inspects updates the Agent system proposes to its long-term memory and confirms, edits, or rejects them. The reviewer's primary concern is correctness and durability of the knowledge the system retains.

### 3.3 Administrator — configuration
An administrator configures which models are bound to which agent roles, the retry ceilings, the context-management thresholds, and the human-review cadence for long-term memory.

### 3.4 Downstream consumer — report reader
A consumer of the generated Feasibility Report reads it expecting that every factual claim either carries a citation to a verifiable source or is unambiguously marked as model-inferred.

---

## 4. Functional Requirements

| ID | Requirement |
|---|---|
| F1 | The system must accept an address as the trigger input for a project. |
| F2 | The system must treat identical addresses as a single project; submitting the same address again must not create a duplicate project. |
| F3 | Project creation must not block on data acquisition; data acquisition must be initiated asynchronously after the project exists. |
| F4 | The system must include an orchestrator agent that plans which sub-agents to run, in what order, and with what inputs, based on the applicable rules and the data available for the address. |
| F5 | The orchestrator must be itself expressible as a skill so that its planning logic can be modified without changing the surrounding runtime. |
| F6 | The system must support multiple sub-agents, each specialised to a distinct section or concern of the Feasibility Report. |
| F7 | The system must include an evaluation step that inspects sub-agent outputs for factual grounding and citation presence before those outputs are admitted to the final report. |
| F8 | The system must support automatic retry of failed sub-agent invocations up to a configurable ceiling, after which the failure must be surfaced for human attention rather than silently dropped. |
| F9 | The system must aggregate accepted sub-agent outputs into a single Feasibility Report in Markdown form. |
| F10 | The system must terminate further sub-agent work early when applicable rules indicate that continued analysis is not warranted, and must record the reason for early termination. |
| F11 | The system must allow per-agent-role selection of which model the agent uses, through a routing layer external to the agent's own logic. |
| F12 | The system must expose a human review surface for proposed updates to long-term memory. |
| F13 | The system must permit a human to inspect, at any time during or after a run, which agents executed, which models were used, which memory entries were read or written, and which sources were cited. |

---

## 5. Data Requirements

### 5.1 Project identity
- A project is identified by its address.
- Submitting the same address must resolve to the existing project rather than create a new one.
- A project owns all artefacts produced for that address, including intermediate agent outputs, the final report, citations, and execution traces.

### 5.2 Report artefact
- The Feasibility Report must be produced as Markdown.
- The report must be structured into the configured set of sections, with sections absent when their corresponding sub-agents could not produce admissible output.
- Every factual claim in the report must be associated with either a citation to a source or an explicit marker indicating that the claim is model-inferred.

### 5.3 Execution trace
- Every run must produce a trace that records, at minimum: which agents were invoked, in what order, with what inputs, which models served their calls, which memory entries were accessed or modified, and which external data sources were consulted.

### 5.4 Source grounding
- Whenever an agent emits a factual claim derived from retrieved evidence, the claim must reference the source from which it was derived in a form that supports human verification.

---

## 6. Memory Requirements

### 6.1 Memory layers
The system must distinguish at least the following memory layers:

| Layer | Purpose | Lifetime |
|---|---|---|
| Shared long-term | Rarely-changing jurisdictional knowledge applicable across projects (federal, state, council-level rules and conventions). | Persistent across all projects and runs. |
| Agent long-term | Knowledge the agents have curated through prior runs, organised in a form the agents themselves can read. | Persistent across runs. |
| Session short-term | Ephemeral working state confined to a single run. | Discarded when the run ends. |

### 6.2 Memory access discipline
- Each memory layer must be stored at a deterministic location agreed by all agents in the system, so that an agent does not need to discover where to read or write.
- An agent must not read from or write to a memory layer outside the disciplined locations.

### 6.3 Conflict resolution in long-term memory
- When two long-term memory entries make incompatible claims, the entry with the later timestamp is authoritative.
- The system must consolidate long-term memory periodically, removing superseded entries and preserving the authoritative ones.

### 6.4 Human review of long-term memory
- Proposed updates to long-term memory must be reviewable by a human through a defined surface.
- If a proposed update is not reviewed within a configurable quiet period, the system may accept it automatically; this auto-acceptance must itself be recorded in the trace.

### 6.5 Cross-project isolation
- Project-specific knowledge must not contaminate shared long-term memory.
- Memory accumulated under one address must not bias analysis of another address.

---

## 7. Behavioral Requirements

### 7.1 Asynchrony
- Triggering a run must return immediately with a handle that identifies the run.
- All substantive work must execute asynchronously and be observable through the status interface.

### 7.2 Resilience to partial data
- The system must not abort a run because some data sources are unavailable. It must produce the report from what is available and explicitly mark or omit sections that could not be supported by evidence.

### 7.3 Grounding discipline
- The system must never present model-inferred content as if it were sourced. The distinction between sourced and inferred content must be visible in the report itself, not only in the trace.

### 7.4 Failure handling
- Transient failures from external services (rate limits, server errors, timeouts) must be retried within the configured ceiling before being surfaced as run-affecting failures.
- A failure in one sub-agent must not abort the whole run; the report must reflect which sections succeeded, which were degraded, and which were skipped.

### 7.5 Context management
- The system must enforce an upper bound on the working context size used by any agent call.
- When the bound is approached, the system must compress prior context into the session short-term memory rather than truncate silently.

### 7.6 Determinism under fixed inputs
- Given the same address, the same memory snapshot, and the same model routing, repeated runs must produce comparable reports. Strict bit-equality is not required; substantive equivalence is.

### 7.7 Early termination
- When applicable rules indicate that further analysis is not warranted (for example, when the property is disqualified by an early check), the orchestrator must terminate further sub-agent dispatch and record the reason in the report and trace.

---

## 8. Interface Requirements

The system must expose, at minimum, the following capabilities through stable interfaces. The transport and payload shapes are not prescribed here.

| Capability | Description |
|---|---|
| Trigger | Accept an address; return a run handle. |
| Run status | Given a run handle, return overall state and per-sub-agent state. |
| Report retrieval | Given a project or a completed run, return the Feasibility Report. |
| Trace retrieval | Given a run handle, return the execution trace. |
| Human feedback on memory | Allow a reviewer to inspect proposed long-term memory updates and accept, edit, or reject them. |
| Model routing configuration | Allow an administrator to read and update which model serves which agent role. |
| Orchestration configuration | Allow the orchestration plan to be expressed as a skill and updated without modifying surrounding runtime. |

---

## 9. Quality Attributes

### 9.1 Grounding integrity
Every factual claim in the report must be either cited to a source or marked as model-inferred. Claims that are neither must be treated as defects.

### 9.2 Cost governance
The system must expose per-run token consumption and per-run cost broken down by agent role and model. The system must support per-run budget ceilings beyond which further work is halted and the run is closed out with whatever has been produced.

### 9.3 Resilience
Transient provider errors and rate limits must not surface as run failures while remaining within the configured retry ceiling.

### 9.4 Auditability
Every run must be reconstructible from its trace: which agents ran, which models served them, which memory entries were read and written, which external sources were consulted, and which outputs were accepted or rejected by evaluation.

### 9.5 Configurability without code change
Changing which model serves which agent role, adjusting retry ceilings, adjusting context thresholds, and modifying the orchestration plan must all be possible through configuration and skill definitions, not through changes to the surrounding runtime.

### 9.6 Reviewability of memory
A human reviewer must be able to understand the current contents of long-term memory, the provenance of each entry, and the history of changes to each entry.

---

## 10. Constraints

- The final deliverable format is Markdown. The system must not produce PDF reports.
- The system must not be coupled to a single model provider. Switching providers for any agent role must be a configuration change.
- Model-inferred content must never be presented as sourced. This is a correctness constraint, not a stylistic preference.
- Project-specific memory must not leak into shared long-term memory.
- Memory paths and conventions must be uniform across agents; ad-hoc per-agent locations are not permitted.
- The orchestrator is expressed as a skill and must remain so; hard-coding orchestration logic outside that skill is not permitted.

---

## 11. Dependencies

- The RAG system, used by agents to retrieve grounded evidence from address-scoped documents.
- External data sources covering federal, state, and council-level planning and geospatial information.
- Optional third-party data sources reachable through authenticated user sessions.
- Multiple language model providers reachable through the model routing layer.
- A persistent store for memory layers and for execution traces.

---

## 12. Acceptance Criteria

- Given an address, the system asynchronously produces a Markdown Feasibility Report composed of the configured sections, with every factual claim either cited or explicitly marked as model-inferred.
- Given the same address submitted twice, the system resolves the second submission to the existing project without creating a duplicate.
- Given a run in which one or more sub-agents fail beyond the retry ceiling, the report is still produced and clearly indicates which sections were degraded or omitted, and the failures are visible in the trace.
- Given a change to the model bound to an agent role, subsequent runs use the new model without code changes; the trace reflects the change.
- Given an early-termination condition triggered by the orchestrator, the report and the trace record the reason and no further sub-agent work is dispatched for that run.
- Given a proposed update to long-term memory, a reviewer can inspect and confirm, edit, or reject it; the outcome is recorded in the trace.
- Given two distinct projects, neither project's specifics influence the other's analysis, and shared long-term memory is unaffected by project-specific learnings.
- Given any completed run, the execution trace is sufficient to reconstruct which agents ran, which models served them, which memory entries were read and written, and which sources were cited.

---

## 13. Open Questions

- What is the persistence strategy for memory layers in non-local deployment environments, given that agent reasoning quality is sensitive to how memory is laid out and accessed?
- What is the concrete rubric by which the evaluation agent decides whether a sub-agent's output is admissible to the report?
- Under which conditions is model-inferred content acceptable in a delivered report, and under which conditions must the absence of sourced evidence cause a section to be omitted rather than inferred?
- How should context compression preserve citations and source references when summarising long working contexts?
- What are the hard per-run token-budget ceilings, and how should the system behave when a ceiling is reached partway through a run?
- How should the system respond when human review of long-term memory updates falls persistently behind, beyond the auto-acceptance quiet period?
