# Runbook: Electricity Tariff Dispute Triage Service

**Service:** Tariff Dispute Triage Service  
**Version:** 1.0.0  
**Last Updated:** 2024-01-01  
**On-Call Contact:** ops-team@utility.karnataka.gov.in

---

## Service Overview

This runbook covers operational procedures for the Tariff Dispute Triage Service,
a multi-agent system that triages electricity billing disputes using KERC tariff corpus.

**Key SLOs:**
- API p95 latency: < 500ms
- Agent processing p95: < 30 seconds
- Error rate: < 1%
- Availability: 99.5% uptime

---

## Starting the Service

```bash
# Full start
docker compose up -d

# With scaled workers
docker compose up -d --scale agent-worker=3

# Check all services are up
docker compose ps

# View logs
docker compose logs -f api
docker compose logs -f agent-worker
```

## Stopping the Service

```bash
# Graceful stop (drains queue)
docker compose stop

# Full teardown (removes containers, keeps volumes)
docker compose down

# Teardown + delete data volumes (DESTRUCTIVE)
docker compose down -v
```

---

## Health Checks

### API Health
```bash
curl http://localhost:8000/health
# Expected: {"status": "healthy", "services": {"redis": "ok", "faiss_index": "ok"}}
```

### Redis Health
```bash
docker compose exec redis redis-cli ping
# Expected: PONG
```

### Queue Depth
```bash
docker compose exec redis redis-cli llen tariff:tasks
# Alert if > 100 (workers may be stuck)
```

### Escalation Queue
```bash
docker compose exec redis redis-cli llen tariff:escalations
# Review if > 10 per hour
```

---

## Common Incidents

### INC-001: API is down (no response on port 8000)

**Symptoms:** `curl http://localhost:8000/health` returns connection refused  
**Steps:**
1. `docker compose ps api` — check container status
2. `docker compose logs api --tail=50` — check for startup errors
3. If Redis is down → `docker compose restart redis` then `docker compose restart api`
4. If FAISS index missing → run `docker compose run --rm indexer --force`
5. If persistent: `docker compose restart api`

---

### INC-002: Tasks stuck in queue (not being processed)

**Symptoms:** `redis-cli llen tariff:tasks` returns > 20 for > 5 minutes  
**Steps:**
1. Check worker logs: `docker compose logs agent-worker --tail=100`
2. Check circuit breaker: look for "Circuit breaker OPENED" in logs
3. If circuit is open: wait 60 seconds for auto-reset, or `docker compose restart agent-worker`
4. Check Gemini API key: `docker compose exec agent-worker env | grep GEMINI`
5. Scale up workers: `docker compose up -d --scale agent-worker=5`

---

### INC-003: High latency (agent processing > 30s)

**Symptoms:** Locust p95 > 30s, user complaints  
**Steps:**
1. Check Gemini API status: https://status.google.com
2. Enable LLM stub for fallback: set `USE_LLM_STUB=true` and restart workers
3. Check FAISS index health: `docker compose logs indexer`
4. Scale workers: `docker compose up --scale agent-worker=5`
5. Check Redis: `docker compose exec redis redis-cli info latency`

---

### INC-004: High error rate / circuit breaker open

**Symptoms:** `circuit_breaker_trips_total` counter increasing  
**Steps:**
1. Check Gemini API: verify API key is valid and quota not exceeded
2. `docker compose logs agent-worker | grep "Circuit breaker"`
3. Set `USE_LLM_STUB=true` as emergency fallback
4. Restart workers after fixing: `docker compose restart agent-worker`

---

### INC-005: FAISS index corrupted or missing

**Symptoms:** Workers log "FAISS index not available"  
**Steps:**
1. Re-run ingestion: `docker compose run --rm indexer --force`
2. Check corpus files: `ls corpus/*.txt`
3. Check volume: `docker volume inspect mini-project_faiss_index`
4. If volume corrupted: `docker compose down -v` and restart (re-ingests automatically)

---

### INC-006: Redis out of memory

**Symptoms:** Redis logs "OOM command not allowed"  
**Steps:**
1. Check memory: `docker compose exec redis redis-cli info memory`
2. Flush old keys (non-destructive): `docker compose exec redis redis-cli --scan --pattern "run:*" | head -100`
3. Increase Redis memory limit in docker-compose.yml: add `command: redis-server --maxmemory 512mb --maxmemory-policy allkeys-lru`
4. Restart Redis: `docker compose restart redis`

---

## Corpus Re-ingestion

When corpus files are updated:
```bash
# Update corpus/*.txt files, then:
docker compose run --rm indexer --corpus-dir /app/corpus --index-path /app/faiss_index --force

# Restart workers to pick up new index
docker compose restart agent-worker
```

---

## Escalated Cases

Review escalated cases (unresolved by agents):
```bash
docker compose exec redis redis-cli lrange tariff:escalations 0 20
```

Forward to human review team with the `escalation_id` and `dispute_id`.
Target SLA for human review: **2 business days**.

---

## Monitoring

- **Grafana Dashboard:** http://localhost:3000 (admin/admin_change_me)
  - Panel: Agent Task Rate (should be > 0 when load is applied)
  - Panel: Agent Task Latency — alert if p95 > 30s
  - Panel: Retries & Circuit Breaker — alert if circuit trips > 3/hour

- **Prometheus:** http://localhost:9090
  - Key query: `histogram_quantile(0.95, rate(agent_task_latency_seconds_bucket[5m]))`

- **Jaeger:** http://localhost:16686
  - Search for traces by `service.name=tariff-triage-service`

---

## Emergency: Disable LLM (Use Stub)

For emergencies when Gemini API is unavailable:
```bash
# In .env:
USE_LLM_STUB=true
docker compose up -d agent-worker  # Restarts with stub
```

**Note:** Stub provides deterministic fallback responses without LLM costs.
All routing decisions will be `bill-stands-with-reason` by default.

---

## Scaling

```bash
# Scale to 5 agent workers
docker compose up -d --scale agent-worker=5

# Scale down
docker compose up -d --scale agent-worker=1
```

Each worker processes one task at a time from the Redis queue.

---

## Backup

```bash
# Backup FAISS index
docker run --rm -v mini-project_faiss_index:/data -v $(pwd):/backup alpine tar czf /backup/faiss_backup.tar.gz /data

# Backup Redis (RDB snapshot)
docker compose exec redis redis-cli bgsave
docker cp tariff-redis:/data/dump.rdb ./redis_backup.rdb
```
