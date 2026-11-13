"""
Locust load tests for the Tariff Dispute Triage Service.
p95 SLO: 30 seconds (total time from submission to result)
Throughput SLO: 20 requests/minute sustained
Uses LLM stub for predictable load testing without real LLM costs.
"""
from __future__ import annotations

import json
import random
import time
import uuid

from locust import HttpUser, between, task

# ---------------------------------------------------------------------------
# Sample payloads for load testing
# ---------------------------------------------------------------------------
SAMPLE_DISPUTES = [
    {
        "consumer_id": "BES-LOAD-001",
        "consumer_name": "Load Test Domestic",
        "premises_type": "domestic",
        "connected_load_kw": 3.5,
        "billing_month": "2024-03",
        "disputed_amount": 850.0,
        "tariff_category_applied": "LT-1",
        "dispute_reason": "tariff_category",
        "description": "Load test: domestic tariff dispute for billing month March 2024",
        "additional_premises_use": None,
    },
    {
        "consumer_id": "BES-LOAD-002",
        "consumer_name": "Load Test Tailoring",
        "premises_type": "domestic",
        "connected_load_kw": 4.0,
        "billing_month": "2024-03",
        "disputed_amount": 2200.0,
        "tariff_category_applied": "LT-2",
        "dispute_reason": "tariff_category",
        "description": "Load test: home tailoring shop tariff dispute",
        "additional_premises_use": "tailoring shop with 2 sewing machines",
    },
    {
        "consumer_id": "BES-LOAD-003",
        "consumer_name": "Load Test Commercial",
        "premises_type": "commercial",
        "connected_load_kw": 15.0,
        "billing_month": "2024-03",
        "disputed_amount": 12500.0,
        "tariff_category_applied": "LT-2",
        "dispute_reason": "meter_accuracy",
        "description": "Load test: commercial meter accuracy dispute",
        "additional_premises_use": None,
    },
    {
        "consumer_id": "BES-LOAD-004",
        "consumer_name": "Load Test Arrears",
        "premises_type": "domestic",
        "connected_load_kw": 2.0,
        "billing_month": "2024-02",
        "disputed_amount": 3500.0,
        "tariff_category_applied": "LT-1",
        "dispute_reason": "arrears",
        "description": "Load test: arrears dispute for previous months",
        "additional_premises_use": None,
    },
    {
        "consumer_id": "BES-LOAD-005",
        "consumer_name": "Load Test Industrial",
        "premises_type": "industrial",
        "connected_load_kw": 45.0,
        "billing_month": "2024-03",
        "disputed_amount": 85000.0,
        "tariff_category_applied": "LT-3",
        "dispute_reason": "load_violation",
        "description": "Load test: industrial load violation dispute",
        "additional_premises_use": None,
    },
]


class TariffDisputeUser(HttpUser):
    """
    Simulates a consumer submitting billing disputes and polling for results.

    SLOs declared:
    - p95 response time (submit): < 500ms
    - p95 total time (submit + poll to completion): < 30s
    - Throughput: >= 20 requests/minute
    - Error rate: < 1%
    """

    wait_time = between(1, 3)
    host = "http://localhost:8000"

    def on_start(self):
        """Check health on start."""
        try:
            self.client.get("/health", timeout=5)
        except Exception:
            pass

    @task(5)
    def submit_dispute(self):
        """Submit a dispute and poll until complete."""
        payload = random.choice(SAMPLE_DISPUTES).copy()
        payload["consumer_id"] = f"LOAD-{uuid.uuid4().hex[:8]}"
        payload["dispute_id"] = str(uuid.uuid4())

        # Step 1: Submit
        with self.client.post(
            "/disputes/submit",
            json=payload,
            headers={"X-Correlation-ID": str(uuid.uuid4())},
            name="/disputes/submit",
            catch_response=True,
        ) as resp:
            if resp.status_code not in (200, 202):
                resp.failure(f"Submit failed: {resp.status_code}")
                return
            try:
                data = resp.json()
                run_id = data["run_id"]
                resp.success()
            except Exception as e:
                resp.failure(f"Submit parse error: {e}")
                return

        # Step 2: Poll until complete (SLO: within 30s)
        start = time.time()
        poll_deadline = 30.0
        poll_interval = 2.0
        completed = False

        while time.time() - start < poll_deadline:
            time.sleep(poll_interval)
            try:
                poll_resp = self.client.get(
                    f"/disputes/{run_id}/status",
                    name="/disputes/{run_id}/status",
                )
                if poll_resp.status_code == 200:
                    status_data = poll_resp.json()
                    if status_data.get("status") in ("completed", "failed", "escalated"):
                        completed = True
                        break
            except Exception:
                break

        total_time = time.time() - start
        if not completed:
            # Track SLO violation
            self.environment.events.request.fire(
                request_type="SLO",
                name="p95_total_time_slo",
                response_time=total_time * 1000,
                response_length=0,
                exception=Exception(f"SLO violated: {total_time:.1f}s > 30s"),
            )

    @task(2)
    def check_health(self):
        """Periodic health check."""
        self.client.get("/health", name="/health")

    @task(1)
    def submit_raw_payload(self):
        """Submit via raw /disputes endpoint (untyped)."""
        payload = random.choice(SAMPLE_DISPUTES).copy()
        payload["consumer_id"] = f"RAW-{uuid.uuid4().hex[:8]}"

        self.client.post(
            "/disputes",
            json=payload,
            headers={"Content-Type": "application/json"},
            name="/disputes (raw)",
        )

    @task(1)
    def submit_feedback(self):
        """Submit feedback for a fake run (tests feedback endpoint)."""
        fake_run_id = str(uuid.uuid4())
        self.client.post(
            f"/feedback/{fake_run_id}",
            json={
                "run_id": fake_run_id,
                "satisfactory": random.choice([True, False]),
                "comment": "Load test feedback",
                "reviewer_id": "locust-tester",
            },
            name="/feedback/{run_id}",
        )
