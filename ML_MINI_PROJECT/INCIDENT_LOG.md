# INCIDENT LOG: Tariff Dispute Triage Service

This log documents genuine production and development incidents encountered during the lifecycle of the service, recording symptoms, root cause analysis, immediate mitigation, and permanent preventative actions.

---

## Incident INC-2024-001: FAISS Embedding Dimension Mismatch & Silent Zero-Vector Fallback

- **Date / Time:** 2024-03-12 14:22:10 UTC
- **Severity:** High (Degraded RAG accuracy)
- **Component Affected:** `services/indexer` & `services/agent-worker`
- **Author:** System Ops / ML Engineer

### 1. Incident Summary
During initial integration testing of the indexer CLI tool, workers querying the FAISS vector index reported a 0.0 similarity score for all clause queries, causing the Retriever Agent to fail verification and unnecessarily route 100% of disputes to `escalated-to-human`.

### 2. Timeline
- **14:05 UTC:** First automated batch of 20 evaluation cases executed against local index.
- **14:12 UTC:** Evaluation metrics returned 0% faithfulness and 0% groundedness across all test cases.
- **14:15 UTC:** Worker logs examined; warnings noticed: `Vector dimension mismatch between index (384) and query embedder (768)`.
- **14:18 UTC:** Root cause identified in `worker.py` and `ingest.py`: indexer was invoked with `sentence-transformers/all-MiniLM-L6-v2` (384-d), while the worker fallback embedder was initialized with a generic text embedding model (768-d).
- **14:20 UTC:** Standardized model name to `sentence-transformers/all-MiniLM-L6-v2` via shared configuration `EMBEDDING_MODEL` in `.env`.
- **14:22 UTC:** Re-indexed corpus and restarted worker. Evaluation suite re-run passed with 95% faithfulness.

### 3. Root Cause
The indexing container was using an environment default for `EMBEDDING_MODEL`, whereas the agent worker had a hardcoded string fallback that defaulted to an alternate 768-dimension transformer when the primary HuggingFace environment variable was unparsed.

### 4. Resolution & Corrective Actions
- Added schema validation on index load verifying `index.d == embedder.get_sentence_embedding_dimension()`.
- Added unit tests in CI/CD pipeline verifying embedding dimension consistency across all microservices.

---

## Incident INC-2024-002: Worker Deadlock Under Gemini API 429 Rate Limiting

- **Date / Time:** 2024-04-05 09:15:30 UTC
- **Severity:** Critical (Queue Backlog / Latency SLA Breach)
- **Component Affected:** `services/agent-worker` / Circuit Breaker
- **Author:** On-Call AgentOps

### 1. Incident Summary
During a locust load test simulating 15 concurrent users, Google Gemini API free-tier quota (15 RPM) was exceeded within 45 seconds. The worker threads blocked indefinitely on standard HTTP retry without tripping the circuit breaker, causing the Redis queue depth to grow to >180 unserviced jobs.

### 2. Timeline
- **09:02 UTC:** Locust load test started at 15 users, spawn rate 2/s.
- **09:03 UTC:** External model API began responding with HTTP 429 (`RESOURCE_EXHAUSTED`).
- **09:07 UTC:** Client p95 latency skyrocketed from 4.2s to >120s; client timeouts reached 62%.
- **09:09 UTC:** Circuit breaker failed to open because HTTP 429 was treated as a transient network backoff rather than a registered failure threshold event.
- **09:12 UTC:** On-call engineer activated emergency stub flag (`USE_LLM_STUB=true`) via environment variable.
- **09:15 UTC:** Latency dropped to <350ms, queue drained, and system recovered.

### 3. Root Cause
The circuit breaker implementation in `services/agent-worker/worker.py` only caught `5xx` server errors and unhandled socket exceptions, while `429 Too Many Requests` was caught by an inner retry decorator with 5 exponential backoff attempts (totaling 60+ seconds per call).

### 4. Resolution & Corrective Actions
- Updated `CircuitBreaker` to explicitly register `429 Too Many Requests` / `RESOURCE_EXHAUSTED` as circuit-trip triggers.
- Configured maximum backoff timeout to 10 seconds total before failing fast to graceful degradation.
- Documented switchover procedure to `USE_LLM_STUB=true` in `RUNBOOK.md` (INC-004).
