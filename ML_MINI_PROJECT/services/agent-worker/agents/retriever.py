"""
Retriever Agent: performs RAG-based retrieval over the tariff corpus.
Uses 3 tools: corpus_search, clause_lookup, field_lookup.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List

import google.generativeai as genai

from guardrails import validate_agent_input, sanitize_llm_output
from cost_tracker import CostTracker
from tools.corpus_search import corpus_search
from tools.clause_lookup import clause_lookup
from tools.field_lookup import field_lookup, get_dispute_summary

logger = logging.getLogger(__name__)

PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v2")
PROMPTS_BASE = os.getenv("PROMPTS_BASE", "/app/prompts")
MAX_RETRIEVAL_ROUNDS = 3


def _load_prompt(agent: str) -> str:
    path = os.path.join(PROMPTS_BASE, PROMPT_VERSION, f"{agent}.txt")
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return _fallback_retriever_prompt()


def _fallback_retriever_prompt() -> str:
    return """You are a tariff retrieval specialist for Karnataka electricity regulations (KERC).

Given the dispute classification and dispute details, identify the most relevant tariff provisions.

Dispute Summary:
{dispute_summary}

Classification Result:
{classification}

Retrieved Passages:
{passages}

Your task:
1. Identify which KERC tariff category applies
2. Note any mixed-use provisions that apply
3. Extract relevant clause numbers

Respond with a JSON object containing:
- relevant_tariff_category: e.g. "LT-1 (Domestic)", "LT-2 (Non-Domestic)"
- applicable_clauses: list of clause IDs found
- key_provisions: list of relevant text excerpts
- needs_more_info: true/false
- suggested_queries: list of additional search queries if more info needed
- reasoning: your analysis

Respond ONLY with valid JSON."""


def retrieve_tariff_info(
    dispute: Dict[str, Any],
    classification: Dict[str, Any],
    cost_tracker: CostTracker,
) -> Dict[str, Any]:
    """
    Retrieve relevant tariff provisions using corpus_search, clause_lookup, field_lookup.
    """
    # Tool 1: field_lookup — get key dispute fields
    premises_type = field_lookup(dispute, "premises_type")
    load_kw = field_lookup(dispute, "connected_load_kw")
    additional_use = field_lookup(dispute, "additional_premises_use")
    tariff_applied = field_lookup(dispute, "tariff_category_applied")
    dispute_reason = field_lookup(dispute, "dispute_reason")

    # Tool 2: corpus_search — build targeted queries
    queries = _build_queries(classification, premises_type, additional_use, dispute_reason, tariff_applied)

    all_passages = []
    seen_content = set()

    for query in queries[:MAX_RETRIEVAL_ROUNDS]:
        results = corpus_search(query, k=4)
        for r in results:
            # Deduplicate
            snippet = r["content"][:100]
            if snippet not in seen_content:
                seen_content.add(snippet)
                all_passages.append(r)

    # Tool 3: clause_lookup — look up specific clauses based on classification
    specific_clauses = []
    if classification.get("is_mixed_use"):
        for clause_id in ["Clause 8.1", "Clause 8.2", "Clause 8.3", "Schedule 8"]:
            result = clause_lookup(clause_id)
            if result:
                specific_clauses.append(result)

    if dispute_reason == "meter_accuracy":
        for clause_id in ["Clause 12", "Clause 13", "Section 5"]:
            result = clause_lookup(clause_id)
            if result:
                specific_clauses.append(result)

    # Prepare passage text for LLM
    passages_text = _format_passages(all_passages, specific_clauses)
    dispute_summary = get_dispute_summary(dispute)

    if os.getenv("USE_LLM_STUB", "false").lower() == "true":
        return _stub_retrieve(all_passages, specific_clauses, classification)

    prompt_template = _load_prompt("retriever")
    prompt = (
        prompt_template
        .replace("{dispute_summary}", dispute_summary)
        .replace("{classification}", json.dumps(classification, indent=2))
        .replace("{passages}", passages_text)
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

        cost_tracker.record_step("retriever", prompt_tokens, completion_tokens, latency_ms)

        clean = sanitize_llm_output(completion_text)
        if "```json" in clean:
            clean = clean.split("```json")[1].split("```")[0]
        elif "```" in clean:
            clean = clean.split("```")[1].split("```")[0]

        result = json.loads(clean.strip())
        result["raw_passages"] = all_passages
        result["clause_texts"] = specific_clauses
        result["prompt_version"] = PROMPT_VERSION
        return result

    except Exception as e:
        logger.error("Retriever error: %s", e)
        return _fallback_retrieve(all_passages, specific_clauses, classification)


def _build_queries(
    classification: Dict,
    premises_type: str,
    additional_use: Any,
    dispute_reason: str,
    tariff_applied: str,
) -> List[str]:
    """Build targeted search queries based on classification."""
    queries = []

    if classification.get("is_mixed_use") or additional_use:
        queries.append("mixed use premises domestic commercial tariff category home-based occupation")
        queries.append(f"home-based occupation {additional_use or ''} tariff classification")

    if premises_type:
        queries.append(f"{premises_type} tariff category definition connected load supply code")

    if tariff_applied:
        queries.append(f"tariff category {tariff_applied} definition eligibility criteria")

    if dispute_reason == "meter_accuracy":
        queries.append("meter accuracy testing replacement consumer complaint procedure")

    if dispute_reason == "load_violation":
        queries.append("connected load violation excess load penalty billing")

    if dispute_reason == "arrears":
        queries.append("arrears outstanding dues payment billing dispute procedure")

    # Default broad query
    queries.append("tariff category billing dispute resolution KERC regulation")
    return queries[:MAX_RETRIEVAL_ROUNDS]


def _format_passages(passages: List[dict], clause_texts: List[dict]) -> str:
    lines = []
    for i, p in enumerate(passages[:8], 1):
        lines.append(f"[Passage {i}] Source: {p.get('source', 'unknown')} | Score: {p.get('score', 0):.2f}")
        lines.append(p.get("content", "")[:500])
        lines.append("")

    for i, c in enumerate(clause_texts, 1):
        lines.append(f"[Clause {i}] ID: {c.get('clause_id', '')} | Source: {c.get('source', '')}")
        lines.append(c.get("text", "")[:500])
        lines.append("")

    return "\n".join(lines)


def _fallback_retrieve(passages: List[dict], clauses: List[dict], classification: Dict) -> Dict:
    """Rule-based fallback when LLM is unavailable."""
    is_mixed = classification.get("is_mixed_use", False)
    home_based = classification.get("home_based_occupation", False)

    if home_based:
        category = "LT-1 (Domestic) with mixed-use assessment required"
    elif is_mixed:
        category = "LT-2 (Non-Domestic/Commercial) — pending site inspection"
    else:
        category = classification.get("premises_classification", "LT-1 (Domestic)")

    return {
        "relevant_tariff_category": category,
        "applicable_clauses": [c["clause_id"] for c in clauses],
        "key_provisions": [p["content"][:200] for p in passages[:3]],
        "needs_more_info": is_mixed,
        "suggested_queries": [],
        "reasoning": "Fallback retrieval — LLM unavailable",
        "raw_passages": passages,
        "clause_texts": clauses,
        "prompt_version": PROMPT_VERSION,
    }


def _stub_retrieve(passages: List[dict], clauses: List[dict], classification: Dict) -> Dict:
    return {
        "relevant_tariff_category": "LT-1 (Domestic)",
        "applicable_clauses": ["Clause 8.1", "Clause 8.3"],
        "key_provisions": ["Stub provision for load testing"],
        "needs_more_info": False,
        "suggested_queries": [],
        "reasoning": "Stub retrieval for load testing",
        "raw_passages": passages,
        "clause_texts": clauses,
        "prompt_version": PROMPT_VERSION,
    }
