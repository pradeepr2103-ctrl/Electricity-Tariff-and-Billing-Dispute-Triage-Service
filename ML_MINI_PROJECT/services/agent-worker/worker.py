"""
Celery worker entry-point for the agent-worker service.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import redis as redis_lib
from prometheus_client import Counter, Histogram, Gauge, start_http_server

from agents.orchestrator import run_orchestrator
from guardrails import validate_agent_input

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","message":"%(message)s"}',
)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
TASK_TOTAL = Counter("agent_tasks_total", "Total agent tasks", ["status"])
TASK_LATENCY = Histogram(
    "agent_task_latency_seconds",
    "Agent task latency",
    buckets=[1, 5, 10, 30, 60, 120],
)
TASK_STEPS = Histogram("agent_task_steps", "Steps per run", buckets=[1, 2, 3, 5, 8, 13])
RETRY_RATE = Counter("agent_retry_total", "Total task retries")
CIRCUIT_BREAKER_TRIPS = Counter("circuit_breaker_trips_total", "Circuit breaker trips")

# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------
class CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 5, timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.state = self.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0.0

    def call(self, func, *args, **kwargs):
        if self.state == self.OPEN:
            if time.time() - self.last_failure_time > self.timeout:
                self.state = self.HALF_OPEN
            else:
                raise Exception("Circuit breaker is OPEN — LLM calls blocked")

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _on_success(self):
        self.failure_count = 0
        self.state = self.CLOSED

    def _on_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            if self.state != self.OPEN:
                CIRCUIT_BREAKER_TRIPS.inc()
                logger.warning("Circuit breaker OPENED after %d failures", self.failure_count)
            self.state = self.OPEN


# Global circuit breaker
_circuit_breaker = CircuitBreaker(
    failure_threshold=int(os.getenv("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5")),
    timeout=int(os.getenv("CIRCUIT_BREAKER_TIMEOUT_SECONDS", "60")),
)

# ---------------------------------------------------------------------------
# Redis connection
# ---------------------------------------------------------------------------
def get_redis() -> redis_lib.Redis:
    return redis_lib.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"),
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


# ---------------------------------------------------------------------------
# Task processing with retry + backoff
# ---------------------------------------------------------------------------
def process_task_with_retry(task_data: dict, max_retries: int = 3) -> dict:
    run_id = task_data["run_id"]
    correlation_id = task_data.get("correlation_id", str(uuid.uuid4()))
    dispute = task_data["dispute"]

    backoff_base = float(os.getenv("RETRY_BACKOFF_BASE", "2.0"))
    max_wait = float(os.getenv("RETRY_MAX_WAIT_SECONDS", "30"))

    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                wait = min(backoff_base ** attempt, max_wait)
                logger.info("Retry %d for run %s, waiting %.1fs", attempt, run_id, wait)
                RETRY_RATE.inc()
                time.sleep(wait)

            result = _circuit_breaker.call(
                run_orchestrator,
                run_id=run_id,
                dispute_id=dispute.get("dispute_id", ""),
                correlation_id=correlation_id,
                dispute=dispute,
            )
            return result

        except Exception as e:
            logger.error("Attempt %d failed for run %s: %s", attempt + 1, run_id, e)
            if attempt == max_retries:
                raise

    raise RuntimeError("Max retries exceeded")


# ---------------------------------------------------------------------------
# Main worker loop
# ---------------------------------------------------------------------------
def worker_loop():
    """Blocking loop: pop tasks from Redis queue and process them."""
    r = get_redis()
    logger.info("Agent worker started — listening on tariff:tasks")

    # Start Prometheus metrics server
    try:
        start_http_server(9100)
        logger.info("Prometheus metrics server started on :9100")
    except Exception as e:
        logger.warning("Could not start metrics server: %s", e)

    while True:
        try:
            # Blocking pop with 5s timeout
            item = r.brpop("tariff:tasks", timeout=5)
            if item is None:
                continue

            _, raw = item
            task_data = json.loads(raw)
            run_id = task_data.get("run_id", "unknown")

            logger.info("Processing task", extra={"run_id": run_id})

            # Update status to processing
            run_meta_raw = r.get(f"run:{run_id}")
            if run_meta_raw:
                run_meta = json.loads(run_meta_raw)
                run_meta["status"] = "processing"
                run_meta["started_at"] = datetime.now(timezone.utc).isoformat()
                r.setex(f"run:{run_id}", 86400, json.dumps(run_meta))

            t_start = time.time()
            try:
                result = process_task_with_retry(task_data)
                status = result.get("routing_decision", "completed")
                final_status = "escalated" if "escalated" in status else "completed"

                # Store result
                r.setex(f"result:{run_id}", 86400, json.dumps(result, default=str))

                # Update run metadata
                if run_meta_raw:
                    run_meta["status"] = final_status
                    run_meta["completed_at"] = datetime.now(timezone.utc).isoformat()
                    r.setex(f"run:{run_id}", 86400, json.dumps(run_meta))

                latency = time.time() - t_start
                TASK_TOTAL.labels(status="success").inc()
                TASK_LATENCY.observe(latency)
                logger.info(
                    "Task completed",
                    extra={"run_id": run_id, "status": final_status, "latency_s": round(latency, 2)},
                )

            except Exception as e:
                logger.error("Task failed permanently: %s", e, extra={"run_id": run_id})
                TASK_TOTAL.labels(status="failed").inc()

                error_result = {
                    "run_id": run_id,
                    "error": str(e),
                    "routing_decision": "escalated-to-human",
                }
                r.setex(f"result:{run_id}", 86400, json.dumps(error_result))

                if run_meta_raw:
                    run_meta = json.loads(run_meta_raw)
                    run_meta["status"] = "failed"
                    run_meta["error"] = str(e)
                    r.setex(f"run:{run_id}", 86400, json.dumps(run_meta))

        except KeyboardInterrupt:
            logger.info("Worker shutting down")
            break
        except Exception as e:
            logger.error("Worker loop error: %s", e)
            time.sleep(1)


if __name__ == "__main__":
    worker_loop()
