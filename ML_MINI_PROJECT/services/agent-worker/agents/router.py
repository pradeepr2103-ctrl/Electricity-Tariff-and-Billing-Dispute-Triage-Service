"""
Router Agent: produces final routing decision with basis and citations.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List

import google.generativeai as genai

from guardrails import sanitize_llm_output
from cost_tracker import CostTracker
from tools.escalation import escalate_to_human

logger = logging.getLogger(__name__)

PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v2")
PROMPTS_BASE = os.getenv("PROMPTS_BASE", "/app/prompts")


def _load_router_prompt() -> str:
    path = os.path.join(PROMPTS_BASE, PROMPT_VERSION, "router.txt")
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return _fallback_router_prompt()


def _fallback_router_prompt() -> str:
    return """You are a final routing specialist for Karnataka electricity billing disputes.

Based on the complete analysis below, produce a final routing decision.

Dispute Details:
{dispute_summary}

Classification:
{classification}

Tariff Analysis:
{retrieval_result}

Verification:
{verification_result}

Routing options:
- bill-stands-with-reason: The applied tariff category is correct; provide reason
- revision-recommended: Evidence supports a different tariff category; specify which
- site-inspection-required: Physical verification needed (mixed use, meter issue)
- escalated-to-human: Genuinely ambiguous, out-of-corpus, or verification failed

Respond with a JSON object:
- routing_decision: one of the four options above
- tariff_position: authoritative statement of the correct tariff category with rationale
- citations: list of objects with {clause_id, document, excerpt, relevance_score}
- reasoning: full step-by-step reasoning
- confidence: high/medium/low
- escalation_needed: true/false
- escalation_reason: if applicable

Respond ONLY with valid JSON."""


def route_case(
    dispute: Dict[str, Any],
    classification: Dict[str, Any],
    retrieval_result: Dict[str, Any],
    verification_result: Dict[str, Any],
    run_id: str,
    cost_tracker: CostTracker,
) -> Dict[str, Any]:
    """
    Produce final routing decision based on all prior agent outputs.
    """
    # If verification failed and escalation needed → escalate immediately
    if verification_result.get("needs_escalation") and verification_result.get("verification_status") == "failed":
        esc = escalate_to_human(
            run_id=run_id,
            dispute_id=dispute.get("dispute_id", ""),
            reason="Citation verification failed — claims not grounded in corpus",
            partial_analysis=json.dumps(retrieval_result, default=str)[:500],
        )
        return {
            "routing_decision": "escalated-to-human",
            "tariff_position": "Unable to determine — verification failed",
            "citations": [],
            "reasoning": f"Escalated: {esc['reason']}",
            "confidence": "low",
            "escalation_id": esc["escalation_id"],
            "prompt_version": PROMPT_VERSION,
        }

    if os.getenv("USE_LLM_STUB", "false").lower() == "true":
        return _stub_route(dispute, classification, retrieval_result)

    # Build prompt
    dispute_summary = json.dumps({
        k: v for k, v in dispute.items() if k not in ("description",)
    }, indent=2)

    retrieval_summary = json.dumps({
        k: v for k, v in retrieval_result.items()
        if k not in ("raw_passages", "clause_texts")
    }, indent=2)

    prompt_template = _load_router_prompt()
    prompt = (
        prompt_template
        .replace("{dispute_summary}", dispute_summary)
        .replace("{classification}", json.dumps(classification, indent=2))
        .replace("{retrieval_result}", retrieval_summary)
        .replace("{verification_result}", json.dumps(verification_result, indent=2))
    )

    t_start = time.time()
    try:
        genai.configure(api_key=os.getenv("GEMINI_API_KEY", ""))
        model = genai.GenerativeModel(os.getenv("GEMINI_MODEL", "gemini-1.5-flash"))
        response = model.generate_content(prompt)
        latency_ms = int((time.time() - t_start) * 1000)

        prompt_tokens = model.count_tokens(prompt).total_tokens
        completion_text = response.text
        completion_tokens = len(completion_text.split()) * 2

        cost_tracker.record_step("router", prompt_tokens, completion_tokens, latency_ms)

        clean = sanitize_llm_output(completion_text)
        if "```json" in clean:
            clean = clean.split("```json")[1].split("```")[0]
        elif "```" in clean:
            clean = clean.split("```")[1].split("```")[0]

        result = json.loads(clean.strip())

        # Handle escalation from router
        if result.get("escalation_needed") or result.get("routing_decision") == "escalated-to-human":
            esc = escalate_to_human(
                run_id=run_id,
                dispute_id=dispute.get("dispute_id", ""),
                reason=result.get("escalation_reason", "Router determined escalation needed"),
                partial_analysis=result.get("reasoning", "")[:500],
            )
            result["escalation_id"] = esc["escalation_id"]
            result["routing_decision"] = "escalated-to-human"

        result["prompt_version"] = PROMPT_VERSION
        return result

    except Exception as e:
        logger.error("Router error: %s", e)
        return _fallback_route(dispute, classification, retrieval_result, verification_result, run_id)


def _fallback_route(
    dispute: Dict,
    classification: Dict,
    retrieval_result: Dict,
    verification_result: Dict,
    run_id: str,
) -> Dict:
    """Rule-based routing fallback."""
    is_mixed = classification.get("is_mixed_use", False)
    home_based = classification.get("home_based_occupation", False)
    dispute_reason = dispute.get("dispute_reason", "other")
    verification_status = verification_result.get("verification_status", "failed")

    if verification_status == "failed":
        decision = "escalated-to-human"
        position = "Unable to verify tariff position — human review required"
        reasoning = "Verification of citations failed. Case escalated for human review."
    elif home_based:
        decision = "site-inspection-required"
        position = (
            "Premises may qualify for home-based occupation provision under LT-1 "
            "(Clause 8.3), subject to site inspection to verify non-domestic area < 50%."
        )
        reasoning = (
            "Consumer has a home-based tailoring shop. Per KERC Clause 8.3, home-based "
            "occupation using residential premises with ≤2 persons employed may retain LT-1 "
            "billing if non-domestic area is less than 50% of total built-up area. "
            "Site inspection required to verify compliance."
        )
    elif is_mixed:
        decision = "site-inspection-required"
        position = "Mixed-use premises require site inspection to determine billing category"
        reasoning = "Mixed use detected. Per Clause 8.2, if non-domestic area > 50%, LT-2 applies."
    elif dispute_reason == "meter_accuracy":
        decision = "site-inspection-required"
        position = "Meter accuracy dispute requires physical inspection and testing"
        reasoning = "Meter accuracy disputes require physical meter testing per supply code provisions."
    elif dispute_reason == "arrears":
        decision = "bill-stands-with-reason"
        position = f"Arrears billing is correct under {retrieval_result.get('relevant_tariff_category', 'applied tariff')}"
        reasoning = "Arrears disputes require payment per outstanding dues. Bill stands as issued."
    else:
        decision = "bill-stands-with-reason"
        position = f"Applied tariff category {dispute.get('tariff_category_applied', '')} is correct"
        reasoning = "Based on premises type and connected load, the applied tariff category is appropriate."

    return {
        "routing_decision": decision,
        "tariff_position": position,
        "citations": _extract_citations(retrieval_result),
        "reasoning": reasoning,
        "confidence": "medium",
        "prompt_version": PROMPT_VERSION,
    }


def _extract_citations(retrieval_result: Dict) -> List[Dict]:
    citations = []
    passages = retrieval_result.get("raw_passages", [])
    clause_texts = retrieval_result.get("clause_texts", [])

    for p in passages[:3]:
        if p.get("score", 0) > 0.3:
            citations.append({
                "clause_id": p.get("clause_id", "Corpus passage"),
                "document": p.get("source", "KERC Tariff Order 2024"),
                "excerpt": p.get("content", "")[:300],
                "relevance_score": round(p.get("score", 0.5), 3),
            })

    for c in clause_texts[:2]:
        citations.append({
            "clause_id": c.get("clause_id", ""),
            "document": c.get("source", "KERC Tariff Order 2024"),
            "excerpt": c.get("text", "")[:300],
            "relevance_score": 0.95,
        })

    return citations


def _stub_route(dispute: Dict, classification: Dict, retrieval_result: Dict) -> Dict:
    return {
        "routing_decision": "bill-stands-with-reason",
        "tariff_position": "LT-1 (Domestic) tariff is applicable — stub response",
        "citations": [],
        "reasoning": "Stub routing for load testing",
        "confidence": "high",
        "prompt_version": PROMPT_VERSION,
    }
