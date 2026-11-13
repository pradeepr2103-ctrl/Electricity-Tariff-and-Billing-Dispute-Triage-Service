"""
Classifier Agent: classifies the dispute type and extracts key entities.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict

import google.generativeai as genai

from guardrails import validate_agent_input, sanitize_llm_output
from cost_tracker import CostTracker

logger = logging.getLogger(__name__)

PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v2")
PROMPTS_BASE = os.getenv("PROMPTS_BASE", "/app/prompts")


def _load_prompt(agent: str) -> str:
    path = os.path.join(PROMPTS_BASE, PROMPT_VERSION, f"{agent}.txt")
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("Prompt file not found: %s, using fallback", path)
        return _fallback_prompt()


def _fallback_prompt() -> str:
    return """You are a billing dispute classifier for Karnataka electricity billing.

Given the dispute details below, classify the dispute and extract key entities.

Dispute Details:
{dispute_summary}

Respond with a JSON object containing:
- dispute_type: one of [tariff_category, meter_accuracy, load_violation, arrears, disconnection, other]
- premises_classification: the type of premises (domestic, commercial, industrial, agricultural, mixed)
- is_mixed_use: true/false - whether there is any indication of mixed use
- home_based_occupation: true/false - whether it appears to be a home-based business
- key_entities: list of relevant entities extracted
- confidence: high/medium/low
- reasoning: brief explanation

Respond ONLY with valid JSON."""


def classify_dispute(dispute: Dict[str, Any], cost_tracker: CostTracker) -> Dict[str, Any]:
    """
    Classify the dispute type and extract entities.

    Args:
        dispute: The dispute request dict
        cost_tracker: CostTracker for this run

    Returns:
        Classification result dict
    """
    # Validate inputs
    description = dispute.get("description", "")
    is_safe, reason = validate_agent_input(description)
    if not is_safe:
        logger.warning("Unsafe input to classifier: %s", reason)
        return {
            "dispute_type": "other",
            "premises_classification": dispute.get("premises_type", "domestic"),
            "is_mixed_use": False,
            "home_based_occupation": False,
            "key_entities": [],
            "confidence": "low",
            "reasoning": f"Input blocked by guardrails: {reason}",
            "blocked": True,
        }

    dispute_summary = f"""
Consumer ID: {dispute.get('consumer_id', 'N/A')}
Premises Type: {dispute.get('premises_type', 'N/A')}
Connected Load: {dispute.get('connected_load_kw', 'N/A')} kW
Billing Month: {dispute.get('billing_month', 'N/A')}
Disputed Amount: INR {dispute.get('disputed_amount', 'N/A')}
Tariff Category Applied: {dispute.get('tariff_category_applied', 'N/A')}
Dispute Reason: {dispute.get('dispute_reason', 'N/A')}
Description: {description}
Additional Premises Use: {dispute.get('additional_premises_use', 'None')}
""".strip()

    prompt_template = _load_prompt("classifier")
    prompt = prompt_template.replace("{dispute_summary}", dispute_summary)

    t_start = time.time()
    try:
        # Check if using LLM stub
        if os.getenv("USE_LLM_STUB", "false").lower() == "true":
            return _stub_classify(dispute)

        genai.configure(api_key=os.getenv("GEMINI_API_KEY", ""))
        model = genai.GenerativeModel(os.getenv("GEMINI_MODEL", "gemini-1.5-flash"))
        response = model.generate_content(prompt)
        latency_ms = int((time.time() - t_start) * 1000)

        # Count tokens
        prompt_tokens = model.count_tokens(prompt).total_tokens
        completion_text = response.text
        completion_tokens = len(completion_text.split()) * 2  # Approximate

        cost_tracker.record_step("classifier", prompt_tokens, completion_tokens, latency_ms)

        # Parse JSON response
        clean = sanitize_llm_output(completion_text)
        # Extract JSON from markdown code blocks if present
        if "```json" in clean:
            clean = clean.split("```json")[1].split("```")[0]
        elif "```" in clean:
            clean = clean.split("```")[1].split("```")[0]

        result = json.loads(clean.strip())
        result["prompt_version"] = PROMPT_VERSION
        return result

    except json.JSONDecodeError as e:
        logger.error("Classifier JSON parse error: %s", e)
        return _fallback_classify(dispute)
    except Exception as e:
        logger.error("Classifier error: %s", e)
        return _fallback_classify(dispute)


def _fallback_classify(dispute: Dict[str, Any]) -> Dict[str, Any]:
    """Rule-based fallback classifier."""
    additional_use = (dispute.get("additional_premises_use") or "").lower()
    description = (dispute.get("description") or "").lower()
    premises_type = dispute.get("premises_type", "domestic")
    dispute_reason = dispute.get("dispute_reason", "other")

    is_mixed = (
        premises_type == "mixed"
        or bool(additional_use)
        or any(kw in description for kw in ["shop", "business", "office", "commercial", "tailoring"])
    )

    home_based = any(
        kw in additional_use or kw in description
        for kw in ["tailoring", "stitching", "sewing", "tuition", "parlour", "home-based", "home based"]
    )

    return {
        "dispute_type": dispute_reason,
        "premises_classification": "mixed" if is_mixed else premises_type,
        "is_mixed_use": is_mixed,
        "home_based_occupation": home_based,
        "key_entities": [
            dispute.get("consumer_id", ""),
            dispute.get("tariff_category_applied", ""),
            str(dispute.get("connected_load_kw", "")),
        ],
        "confidence": "medium",
        "reasoning": "Rule-based fallback classification (LLM unavailable)",
        "prompt_version": PROMPT_VERSION,
    }


def _stub_classify(dispute: Dict[str, Any]) -> Dict[str, Any]:
    """Stub response for load testing."""
    additional_use = (dispute.get("additional_premises_use") or "").lower()
    is_mixed = bool(additional_use) or dispute.get("premises_type") == "mixed"
    return {
        "dispute_type": dispute.get("dispute_reason", "tariff_category"),
        "premises_classification": "mixed" if is_mixed else dispute.get("premises_type", "domestic"),
        "is_mixed_use": is_mixed,
        "home_based_occupation": "tailoring" in additional_use or "stitching" in additional_use,
        "key_entities": ["stub_entity"],
        "confidence": "high",
        "reasoning": "Stub classification for load testing",
        "prompt_version": PROMPT_VERSION,
    }
