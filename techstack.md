# Tech Stack & Architectural Decisions

This document outlines the core technologies powering **RegSpine**, detailing why each tool was chosen, key architectural tradeoffs, and expected operating costs.

---

## 1. AI & Orchestration Layer

| Component | Technology | Role & Practical Rationale | Cost / Quota |
| --- | --- | --- | --- |
| **Ingestion & Extraction** | OpenAI GPT-5.1 (via Azure AI Foundry) | Parses high-volume regulatory documents and bank control manuals into structured JSON schemas. Routed via Foundry for enterprise-tier throughput and to avoid transient `503` capacity drops during heavy ingestion batches. | Pay-per-use |
| **Reasoning & Auditor (Primary)** | OpenAI GPT-5.1 (via Azure AI Foundry) | High-precision audit engine comparing extracted regulatory obligations against internal policies and evidence to pinpoint compliance gaps. | Pay-per-use |
| **Reasoning & Auditor (Fallback)** | OpenAI GPT-5.6-Luna (via Azure AI Foundry) | Lightweight, cost-effective fallback triggered automatically if the primary endpoint rate-limits. Fallback audit findings are routed to human-in-the-loop (HITL) triage by policy before committing. | Pay-per-use (low-frequency) |
| **Remediation & Summaries** | OpenAI GPT-5.1 (via Azure AI Foundry) | Drafts actionable remediation roadmaps, maps control owners, and generates executive compliance summaries. Uses the shared Foundry pipeline for consistency. | Pay-per-use |
| **Independent Judge** | Google Gemini 3.8 Flash | Serves as an independent reviewer for generated audit findings. Using an external model family prevents self-evaluation bias (a model grading its own output). Kept on default temperature (`1.0`) per provider recommendations for complex reasoning. | Free tier (Google AI Studio) |
| **Agent Orchestration** | LangGraph (Python) | Manages cyclical agent workflows, conditional decision trees, structured state persistence, and automatic self-correction loops. | Open Source |

---

## 2. Retrieval & Data Layer

### Embedding & Retrieval Strategy

We deliberately stick with `sentence-transformers/all-MiniLM-L6-v2` alongside local ChromaDB. While its 256-token context window requires clean semantic chunking, running it locally eliminates API latency, network flakiness, and embedding costs. To counter edge cases on dense regulatory text, raw vector hits pass through a secondary cross-encoder (`bge-reranker-base`) before reaching the reasoning agents.

| Component | Technology | Implementation Details | Cost |
| --- | --- | --- | --- |
| **Embeddings** | `sentence-transformers/all-MiniLM-L6-v2` | Runs locally on CPU. Provides zero-latency, rate-limit-free embeddings for internal policy chunks. | Local / Free |
| **Vector Database** | ChromaDB (Embedded Persistent Mode) | Embedded vector store. **Note:** Do not check raw `./chroma_db` binaries into git; track the source corpus and rely on our deterministic ingestion script to prevent merge conflicts across developer machines. | Local / Free |
| **Primary Relational DB** | Supabase (Hosted PostgreSQL) | Central team source of truth. Stores structured obligations, control mappings, audit trails, and user permissions leveraging native Postgres features (`jsonb`, `int4range`, RLS). | Free tier (up to 500 MB) |
| **Local Response Cache** | SQLite (`cache.db`) | Local key-value cache keyed by prompt/input hash. Speeds up regression tests, LangGraph state checkpointing, and offline demo runs without incurring API costs. | Local / Free |

---

## 3. Application & Frontend Layer

```
[ Browser / React SPA ]
        │  (HTTP / JSON)
        ▼
[ FastAPI Backend ] ──► [ LangGraph Agents ] ──► [ LLM APIs / Local Chroma ]
        │
        ▼
[ Supabase PostgreSQL ]

```

* **Backend API (`FastAPI`):** Lightweight asynchronous service exposing document upload endpoints, pipeline status polling, and report exports. Uses Pydantic v2 models for end-to-end request validation and handles CORS for the frontend origin.
* **Frontend SPA (`React + TypeScript + Vite`):**
* **Lineage & Traceability Graph:** Built with React Flow (`@xyflow/react`) to visually trace relationships: *Regulation → Obligation → Existing Control → Gap Identified*.
* **Audit Provenance Inspector:** Slide-out drawer linking every agent recommendation directly back to specific document pages, snippet text, and source hashes.
* **Styling:** Tailwind CSS for a clean, dense enterprise UI.



---

## 4. Operational Cost Model

| Tier | Services | Estimated Cost & Guardrails |
| --- | --- | --- |
| **Paid APIs** | OpenAI GPT-5.1 & GPT-5.6-Luna | ~$10 – $30 per complete pipeline test suite. Controlled via aggressive SQLite response caching and configurable run-level budget limits. |
| **Hosted Free Tier** | Google Gemini (Judge role), Supabase | $0 within standard development quotas. |
| **Self-Hosted / Open Source** | FastAPI, LangGraph, ChromaDB, SQLite, React / Vite, Sentence-Transformers | $0 (runs on local developer machines or shared staging boxes). |