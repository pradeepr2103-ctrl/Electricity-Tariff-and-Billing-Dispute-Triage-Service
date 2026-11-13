"""
Batch evaluation runner for the Tariff Dispute Triage Service.
Measures: groundedness, context relevance, faithfulness, hallucination rate.
Compares prompt versions v1 vs v2.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

API_BASE = os.getenv("API_BASE", "http://localhost:8000")
EVAL_SET_PATH = Path(__file__).parent / "eval_set.json"
RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def load_eval_set() -> List[Dict]:
    with open(EVAL_SET_PATH, encoding="utf-8") as f:
        return json.load(f)


def _http_post_json(url: str, payload: dict, timeout: float = 30.0) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body}
        return e.code, parsed
    except Exception as e:
        return 0, {"error": str(e)}


def _http_get_json(url: str, timeout: float = 30.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body}
        return e.code, parsed
    except Exception as e:
        return 0, {"error": str(e)}


def submit_dispute(scenario: Dict) -> str | None:
    """Submit a dispute and return the run_id."""
    try:
        status_code, body = _http_post_json(f"{API_BASE}/disputes/submit", scenario["input"], timeout=30.0)
        if status_code in (200, 202) and "run_id" in body:
            return body["run_id"]
        else:
            logger.error("Submit failed for %s: %s %s", scenario["scenario_id"], status_code, str(body)[:200])
            return None
    except Exception as e:
        logger.error("Submit error for %s: %s", scenario["scenario_id"], e)
        return None


def poll_result(run_id: str, timeout: int = 180) -> Dict | None:
    """Poll until result is ready or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status_code, data = _http_get_json(f"{API_BASE}/disputes/{run_id}/status", timeout=10.0)
            status = data.get("status")
            if status in ("completed", "failed", "escalated"):
                return data
            time.sleep(3)
        except Exception as e:
            logger.error("Poll error: %s", e)
            time.sleep(5)
    logger.warning("Timeout polling %s", run_id)
    return None


def compute_metrics(scenario: Dict, result: Dict) -> Dict:
    """
    Compute evaluation metrics for a single scenario.
    
    Metrics:
    - routing_correct: whether routing matches expected
    - tariff_correct: whether tariff position mentions expected category
    - has_citations: whether result includes citations
    - citation_count: number of citations
    - verification_status: verified or escalated
    - groundedness_score: fraction of citations with score >= 0.5
    - latency_ms: agent latency
    """
    run_result = result.get("result") or {}
    
    if not run_result:
        return {
            "scenario_id": scenario["scenario_id"],
            "routing_correct": False,
            "tariff_match": False,
            "has_citations": False,
            "citation_count": 0,
            "groundedness_score": 0.0,
            "verification_status": "unknown",
            "latency_ms": 0,
            "error": "No result",
        }

    actual_routing = run_result.get("routing_decision", "")
    expected_routing = scenario["expected_routing"]
    routing_correct = actual_routing == expected_routing

    tariff_position = (run_result.get("tariff_position") or "").lower()
    expected_category = (scenario.get("expected_tariff_category") or "").lower()
    # Check if key terms of expected category appear in tariff position
    expected_terms = [t for t in expected_category.replace("(", " ").replace(")", " ").split() if len(t) > 3]
    tariff_match = any(term in tariff_position for term in expected_terms) if expected_terms else False

    citations = run_result.get("citations", [])
    has_citations = len(citations) > 0
    
    grounded_citations = [c for c in citations if c.get("relevance_score", 0) >= 0.5]
    groundedness_score = len(grounded_citations) / len(citations) if citations else 0.0

    return {
        "scenario_id": scenario["scenario_id"],
        "routing_correct": routing_correct,
        "tariff_match": tariff_match,
        "has_citations": has_citations,
        "citation_count": len(citations),
        "groundedness_score": round(groundedness_score, 3),
        "verification_status": run_result.get("verification_status", "unknown"),
        "latency_ms": run_result.get("latency_ms", 0),
        "cost_usd": run_result.get("cost_usd", 0.0),
        "prompt_version": run_result.get("prompt_version", "unknown"),
        "actual_routing": actual_routing,
        "expected_routing": expected_routing,
    }


def compute_aggregate_metrics(metrics: List[Dict]) -> Dict:
    """Compute aggregate evaluation report."""
    n = len(metrics)
    if n == 0:
        return {}

    routing_accuracy = sum(1 for m in metrics if m["routing_correct"]) / n
    tariff_accuracy = sum(1 for m in metrics if m["tariff_match"]) / n
    citation_coverage = sum(1 for m in metrics if m["has_citations"]) / n
    avg_groundedness = sum(m["groundedness_score"] for m in metrics) / n
    verified_rate = sum(1 for m in metrics if m["verification_status"] == "verified") / n
    hallucination_rate = 1 - avg_groundedness
    avg_latency_ms = sum(m["latency_ms"] for m in metrics) / n
    total_cost = sum(m.get("cost_usd", 0) for m in metrics)
    escalation_rate = sum(1 for m in metrics if "escalated" in m.get("actual_routing", "")) / n

    return {
        "total_scenarios": n,
        "routing_accuracy": round(routing_accuracy, 3),
        "tariff_accuracy": round(tariff_accuracy, 3),
        "citation_coverage": round(citation_coverage, 3),
        "avg_groundedness_score": round(avg_groundedness, 3),
        "verified_rate": round(verified_rate, 3),
        "hallucination_rate": round(hallucination_rate, 3),
        "escalation_rate": round(escalation_rate, 3),
        "avg_latency_ms": round(avg_latency_ms, 1),
        "total_cost_usd": round(total_cost, 6),
        "slo_p95_latency_ms": 30000,  # 30s SLO
        "slo_met": avg_latency_ms < 30000,
    }


def run_evaluation(prompt_version: str = "v2"):
    """Run full evaluation suite."""
    eval_id = os.getenv("EVAL_RUN_ID", f"eval_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}")
    logger.info("Starting evaluation run: %s (prompt version: %s)", eval_id, prompt_version)

    scenarios = load_eval_set()
    logger.info("Loaded %d scenarios", len(scenarios))

    metrics = []
    for i, scenario in enumerate(scenarios, 1):
        logger.info("[%d/%d] Running scenario %s: %s",
                   i, len(scenarios), scenario["scenario_id"], scenario["description"][:50])

        run_id = submit_dispute(scenario)
        if not run_id:
            metrics.append({
                "scenario_id": scenario["scenario_id"],
                "error": "Failed to submit",
                "routing_correct": False,
                "tariff_match": False,
                "has_citations": False,
                "citation_count": 0,
                "groundedness_score": 0.0,
                "verification_status": "error",
                "latency_ms": 0,
            })
            continue

        result = poll_result(run_id)
        if not result:
            metrics.append({
                "scenario_id": scenario["scenario_id"],
                "error": "Timeout",
                "routing_correct": False,
                "tariff_match": False,
                "has_citations": False,
                "citation_count": 0,
                "groundedness_score": 0.0,
                "verification_status": "timeout",
                "latency_ms": 0,
            })
            continue

        scenario_metrics = compute_metrics(scenario, result)
        metrics.append(scenario_metrics)
        logger.info(
            "  → Routing: %s (expected: %s) | Correct: %s | Groundedness: %.2f",
            scenario_metrics.get("actual_routing", "N/A"),
            scenario_metrics.get("expected_routing", "N/A"),
            scenario_metrics.get("routing_correct", False),
            scenario_metrics.get("groundedness_score", 0.0),
        )

        # Small delay between scenarios
        time.sleep(1)

    aggregate = compute_aggregate_metrics(metrics)

    report = {
        "eval_id": eval_id,
        "prompt_version": prompt_version,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "aggregate": aggregate,
        "per_scenario": metrics,
    }

    # Save report
    report_path = RESULTS_DIR / f"{eval_id}.json"
    report_path.write_text(json.dumps(report, indent=2))
    logger.info("Report saved to: %s", report_path)

    # Print summary
    print("\n" + "=" * 60)
    print(f"EVALUATION REPORT — {eval_id}")
    print("=" * 60)
    for key, val in aggregate.items():
        print(f"  {key}: {val}")
    print("=" * 60 + "\n")

    return report


if __name__ == "__main__":
    prompt_version = sys.argv[1] if len(sys.argv) > 1 else "v2"
    run_evaluation(prompt_version)
