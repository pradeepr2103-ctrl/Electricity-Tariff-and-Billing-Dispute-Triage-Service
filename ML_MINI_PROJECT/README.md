# ⚡ Electricity Tariff & Billing Dispute Triage Service

**Course:** UE24AM342AA5 — ML System Design and AgentOps  
**Mini Project 11 | Domain:** Power Utilities & Regulation (KERC)

> A production-grade multi-agent service that accepts a billing dispute, validates it, retrieves the relevant KERC tariff provisions via RAG, verifies citations, and routes the case with clause-level justification — all powered by LangGraph + Gemini + FAISS.

---

## Architecture Overview

```
Consumer/Client
      │
      ▼
┌─────────────┐     Redis Queue      ┌──────────────────────────────────────┐
│  FastAPI API │ ──────────────────► │         LangGraph Agent Pipeline     │
│  (Port 8000) │                     │                                       │
│             │ ◄── run_id + status ─│  Classifier → Retriever → Verifier   │
└─────────────┘                     │                    ↓                   │
      │                             │               Router Agent             │
      ▼                             └──────────────────────────────────────┘
  Redis                                         │              │
(Queue+Cache)                              FAISS Index    Gemini LLM
                                         (KERC Corpus)   (or Stub)
      │
      ▼
┌─────────┐  ┌──────────┐  ┌────────┐
│Prometheus│  │ Grafana  │  │ Jaeger │
│  :9090  │  │  :3000   │  │ :16686 │
└─────────┘  └──────────┘  └────────┘
```

### Agent Topology (LangGraph StateGraph)
```
START → [Classifier] → [Retriever] → [Verifier] ──► [Router] → END
                              ▲              │
                              └─ retry ◄────┘ (partial verification)
                                             │
                                          escalate (failed verification)
```

---

## Quick Start

### Prerequisites
- Docker + Docker Compose (v2.x)
- Google Gemini API key (free): https://aistudio.google.com/

### 1. Clone & Configure
```bash
# Copy env template
cp .env.example .env
# Edit .env and set your GEMINI_API_KEY
```

### 2. Start Everything
```bash
docker compose up --build
# Or to scale agent workers:
docker compose up --build --scale agent-worker=3
```

### 3. Run Corpus Ingestion
The indexer service runs automatically and indexes the KERC corpus.
To re-run manually:
```bash
docker compose run --rm indexer --force
```

### 4. Access Services

| Service | URL | Description |
|---------|-----|-------------|
| API | http://localhost:8000 | FastAPI REST |
| API Docs | http://localhost:8000/docs | Swagger UI |
| Web Client | Open `web-client/index.html` | Browser UI |
| Grafana | http://localhost:3000 | Dashboard (admin/admin) |
| Prometheus | http://localhost:9090 | Metrics |
| Jaeger | http://localhost:16686 | Distributed tracing |

---

## API Reference

### Submit a Dispute
```bash
POST /disputes/submit
Content-Type: application/json

{
  "consumer_id": "BES-2024-001",
  "consumer_name": "Meena Devi",
  "premises_type": "domestic",
  "connected_load_kw": 4.0,
  "billing_month": "2024-03",
  "disputed_amount": 2200.0,
  "tariff_category_applied": "LT-2",
  "dispute_reason": "tariff_category",
  "description": "I run a home tailoring shop with 2 sewing machines...",
  "additional_premises_use": "home tailoring shop"
}

# Response:
{
  "run_id": "uuid",
  "dispute_id": "uuid",
  "status": "queued"
}
```

### Poll Status
```bash
GET /disputes/{run_id}/status

# Response when complete:
{
  "run_id": "...",
  "status": "completed",
  "result": {
    "routing_decision": "site-inspection-required",
    "tariff_position": "Consumer may qualify for LT-1 under Clause 8.3...",
    "citations": [...],
    "verification_status": "verified",
    "reasoning": "...",
    "token_usage": {...},
    "cost_usd": 0.000125,
    "latency_ms": 4320
  }
}
```

### Submit Feedback
```bash
POST /feedback/{run_id}
{"run_id": "...", "satisfactory": true, "comment": "Accurate determination"}
```

---

## The Difficult Case: Home Tailoring Shop

This is **Scenario S003** in the evaluation set. A consumer with a domestic connection
runs a home tailoring shop with 2 sewing machines and 1 part-time helper.

**Agent reasoning chain:**
1. **Classifier** → detects `is_mixed_use=true`, `home_based_occupation=true`
2. **Retriever** → searches for "home-based occupation tailoring residential tariff"  
   → retrieves **Clause 8.3** (home-based occupation provision)  
   → retrieves **Clause 8.2** (50% area threshold)
3. **Verifier** → confirms citations resolve to actual corpus text
4. **Router** → produces: `site-inspection-required`  
   → *"Consumer's tailoring activity with 1 employee may qualify under Clause 8.3  
   if non-domestic area < 50% of built-up area. Physical inspection required."*

**Disqualifying factors that would change routing to `bill-stands-with-reason`:**
- More than 2 outside employees → Clause 8.4(a)
- Industrial machinery → Clause 8.4(b)
- Non-domestic area > 50% → Clause 8.4(d)

---

## Load Testing (with LLM Stub)

```bash
# Start with stub LLM for load testing
USE_LLM_STUB=true docker compose up --scale agent-worker=3

# Run Locust
pip install locust
cd load_tests
locust -f locustfile.py --host=http://localhost:8000 --users=50 --spawn-rate=5

# P95 SLO target: < 30 seconds (total submission + completion)
# Throughput SLO: >= 20 requests/minute
```

---

## Evaluation

```bash
# Run evaluation against all 22 scenarios
cd evaluation
pip install httpx
python run_eval.py v2

# Results saved to evaluation/results/
# Key metrics: routing_accuracy, groundedness_score, hallucination_rate
```

---

## Prompt Versioning

Two prompt versions are maintained:

| Version | Strategy | Notes |
|---------|----------|-------|
| v1 | Minimal role description + JSON schema | Faster, less accurate |
| v2 | Detailed context + step-by-step protocol | More accurate, costlier |

Set via env var: `PROMPT_VERSION=v1` or `PROMPT_VERSION=v2`

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Agent Framework | LangGraph |
| LLM | Google Gemini 1.5 Flash (free tier) |
| Embeddings | sentence-transformers/all-MiniLM-L6-v2 (local) |
| Vector Store | FAISS |
| API | FastAPI + async |
| Task Queue | Redis lists (custom) |
| Observability | OpenTelemetry + Prometheus + Grafana + Jaeger |
| Load Testing | Locust |
| Containerization | Docker Compose |

---

## Project Structure

```
mini project/
├── docker-compose.yml          # 8 services
├── .env.example                # Configuration template
├── services/
│   ├── api/                    # FastAPI REST service
│   ├── agent-worker/           # LangGraph multi-agent worker
│   ├── indexer/                # Corpus ingestion pipeline
│   └── llm-stub/               # LLM stub for load testing
├── corpus/                     # KERC tariff documents (2 files)
├── evaluation/                 # 22 eval scenarios + runner
├── load_tests/                 # Locust load tests
├── monitoring/                 # Prometheus + Grafana config
├── web-client/                 # HTML/JS web interface
├── RUNBOOK.md                  # Operations guide
└── INCIDENT_LOG.md             # Incident tracking
```

---

## Security

- **Prompt injection**: Pattern-matched and blocked before LLM calls
- **Toxicity filtering**: Heuristic keyword filter (extensible)
- **Input sanitization**: Control character removal, length limiting
- **No hardcoded credentials**: All secrets via `.env`
- **Output filtering**: LLM output sanitized before storage

---

## SLOs

| Metric | SLO |
|--------|-----|
| API p95 latency | < 500ms |
| Agent p95 total latency | < 30s |
| Error rate | < 1% |
| Throughput | ≥ 20 req/min |
| Citation verification rate | ≥ 80% |
