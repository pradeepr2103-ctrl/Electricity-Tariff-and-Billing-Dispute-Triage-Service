"""
LLM Stub Service: returns deterministic fake LLM responses for load testing.
Mimics the Google Gemini API response format.
"""
from __future__ import annotations

import json
import random
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="LLM Stub for Load Testing", version="1.0.0")

STUB_RESPONSES = {
    "classifier": {
        "dispute_type": "tariff_category",
        "premises_classification": "domestic",
        "is_mixed_use": False,
        "home_based_occupation": False,
        "key_entities": ["LT-1", "domestic", "4.5 kW"],
        "confidence": "high",
        "reasoning": "Stub: Pure domestic premises, tariff category dispute",
        "prompt_version": "stub",
    },
    "retriever": {
        "relevant_tariff_category": "LT-1 (Domestic)",
        "applicable_clauses": ["Clause 8.1", "Schedule 8"],
        "key_provisions": ["Domestic tariff applies to residential premises"],
        "needs_more_info": False,
        "suggested_queries": [],
        "reasoning": "Stub: Standard domestic tariff applies",
        "prompt_version": "stub",
    },
    "verifier": {
        "verification_status": "verified",
        "verified_citations": ["Clause 8.1"],
        "unverified_claims": [],
        "hallucination_flags": [],
        "confidence": "high",
        "needs_escalation": False,
        "escalation_reason": None,
        "prompt_version": "stub",
    },
    "router": {
        "routing_decision": "bill-stands-with-reason",
        "tariff_position": "LT-1 (Domestic) tariff is correctly applied",
        "citations": [
            {
                "clause_id": "Clause 8.1",
                "document": "kerc_tariff_order_2024",
                "excerpt": "Domestic premises billed under LT-1",
                "relevance_score": 0.92,
            }
        ],
        "reasoning": "Stub: Bill stands as LT-1 is correctly applied to domestic premises",
        "confidence": "high",
        "escalation_needed": False,
        "prompt_version": "stub",
    },
}

ROUTING_DECISIONS = [
    "bill-stands-with-reason",
    "bill-stands-with-reason",
    "bill-stands-with-reason",
    "revision-recommended",
    "site-inspection-required",
]


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "llm-stub"}


@app.post("/generate")
async def generate(request: Request):
    """Stub LLM generation endpoint."""
    body = await request.json()
    prompt = body.get("prompt", "")

    # Add simulated latency
    time.sleep(random.uniform(0.05, 0.15))

    # Detect which agent is calling based on prompt content
    if "classify" in prompt.lower() or "dispute_type" in prompt.lower():
        response_data = STUB_RESPONSES["classifier"]
    elif "retriev" in prompt.lower() or "tariff_category" in prompt.lower():
        response_data = STUB_RESPONSES["retriever"]
    elif "verif" in prompt.lower() or "verified_citations" in prompt.lower():
        response_data = STUB_RESPONSES["verifier"]
    elif "routing" in prompt.lower() or "routing_decision" in prompt.lower():
        resp = STUB_RESPONSES["router"].copy()
        resp["routing_decision"] = random.choice(ROUTING_DECISIONS)
        response_data = resp
    else:
        response_data = {"text": "Stub response", "prompt_version": "stub"}

    return JSONResponse({
        "text": json.dumps(response_data),
        "usage": {
            "prompt_tokens": len(prompt.split()),
            "completion_tokens": 50,
            "total_tokens": len(prompt.split()) + 50,
        },
    })


@app.post("/v1beta/models/{model}:generateContent")
async def gemini_generate(model: str, request: Request):
    """Mimic Gemini API response format for direct SDK calls."""
    body = await request.json()
    contents = body.get("contents", [])
    prompt = ""
    for content in contents:
        for part in content.get("parts", []):
            prompt += part.get("text", "")

    time.sleep(random.uniform(0.05, 0.2))

    if "routing_decision" in prompt:
        text = json.dumps(STUB_RESPONSES["router"])
    elif "verification_status" in prompt:
        text = json.dumps(STUB_RESPONSES["verifier"])
    elif "relevant_tariff_category" in prompt:
        text = json.dumps(STUB_RESPONSES["retriever"])
    elif "dispute_type" in prompt:
        text = json.dumps(STUB_RESPONSES["classifier"])
    else:
        text = '{"result": "stub response"}'

    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": len(prompt.split()),
            "candidatesTokenCount": 50,
            "totalTokenCount": len(prompt.split()) + 50,
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
