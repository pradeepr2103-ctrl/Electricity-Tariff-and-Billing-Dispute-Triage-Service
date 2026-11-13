"""
Verifier Agent: verifies all citations resolve to retrieved text.
If verification fails → triggers re-search or escalation.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List

import google.generativeai as genai

from guardrails import sanitize_llm_output
from cost_tracker import CostTracker

logger = logging.getLogger(__name__)

PROMPT_VERSION = os.getenv("PROMPT_VERSION", "v2")
PROMPTS_BASE = os.getenv("PROMPTS_BASE", "/app/prompts")
SIMILARITY_THRESHOLD = 0.5  # Minimum passage similarity to accept a citation


def _load_prompt(agent: str) -> str:
    path = os.path.join(PROMPTS_BASE, PROMPT_VERSION, f"{agent}.txt")
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return _fallback_verifier_prompt()


def _fallback_verifier_prompt() -> str:
    return """You are a verification specialist for tariff determinations.

Your job is to verify that all tariff positions and citations are grounded in the retrieved corpus passages.

Tariff Analysis:
{retrieval_result}

Retrieved Passages:
{passages}

Verify each claim and citation:
1. Does the cited clause exist in the retrieved passages?
2. Is the tariff position supported by the evidence?
3. Are there contradictions or unsupported claims?

Respond with a JSON object:
- verification_status: "verified" | "partial" | "failed"
- verified_citations: list of citation IDs that are verified
- unverified_claims: list of claims that could not be verified
- hallucination_flags: list of any apparent hallucinations
- confidence: high/medium/low
- needs_escalation: true/false
- escalation_reason: reason if escalation needed

Respond ONLY with valid JSON."""


def verify_citations(
    retrieval_result: Dict[str, Any],
    dispute: Dict[str, Any],
    cost_tracker: CostTracker,
) -> Dict[str, Any]:
    """
    Verify that all citations in the retrieval result resolve to actual corpus text.

    Returns:
        Verification result dict with verified_citations, status, etc.
    """
    passages = retrieval_result.get("raw_passages", [])
    clause_texts = retrieval_result.get("clause_texts", [])
    claimed_clauses = retrieval_result.get("applicable_clauses", [])

    # Step 1: Automated citation verification (no LLM needed)
    auto_verification = _auto_verify(claimed_clauses, passages, clause_texts)

    if os.getenv("USE_LLM_STUB", "false").lower() == "true":
        return _stub_verify(auto_verification, claimed_clauses)

    # Step 2: LLM-based deep verification
    passages_text = _format_passages_for_verification(passages, clause_texts)
    retrieval_summary = json.dumps({
        k: v for k, v in retrieval_result.items()
        if k not in ("raw_passages", "clause_texts")
    }, indent=2)

    prompt_template = _load_prompt("verifier")
    prompt = (
        prompt_template
        .replace("{retrieval_result}", retrieval_summary)
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

        cost_tracker.record_step("verifier", prompt_tokens, completion_tokens, latency_ms)

        clean = sanitize_llm_output(completion_text)
        if "```json" in clean:
            clean = clean.split("```json")[1].split("```")[0]
        elif "```" in clean:
            clean = clean.split("```")[1].split("```")[0]

        llm_result = json.loads(clean.strip())

        # Merge LLM result with auto-verification
        merged = {**auto_verification, **llm_result}
        merged["auto_verified_clauses"] = auto_verification.get("auto_verified", [])
        merged["prompt_version"] = PROMPT_VERSION
        return merged

    except Exception as e:
        logger.error("Verifier LLM error: %s", e)
        return {**auto_verification, "prompt_version": PROMPT_VERSION}


def _auto_verify(
    claimed_clauses: List[str],
    passages: List[dict],
    clause_texts: List[dict],
) -> Dict[str, Any]:
    """
    Automated verification: check if claimed clauses appear in retrieved passages.
    """
    all_corpus_text = " ".join(p.get("content", "") for p in passages)
    all_corpus_text += " ".join(c.get("text", "") for c in clause_texts)

    auto_verified = []
    unverified = []

    for clause_id in claimed_clauses:
        # Check if clause ID appears in corpus text
        normalized = clause_id.lower().strip()
        if normalized in all_corpus_text.lower():
            auto_verified.append(clause_id)
        else:
            # Try fuzzy match
            similarity = _best_similarity(normalized, all_corpus_text.lower())
            if similarity >= SIMILARITY_THRESHOLD:
                auto_verified.append(clause_id)
            else:
                unverified.append(clause_id)

    has_passages = len(passages) > 0 or len(clause_texts) > 0

    if not claimed_clauses:
        status = "verified" if has_passages else "failed"
    elif len(unverified) == 0:
        status = "verified"
    elif len(unverified) < len(claimed_clauses):
        status = "partial"
    else:
        status = "failed"

    return {
        "verification_status": status,
        "auto_verified": auto_verified,
        "unverified_claims": unverified,
        "hallucination_flags": unverified if status == "failed" else [],
        "needs_escalation": status == "failed" and len(claimed_clauses) > 0,
        "confidence": "high" if status == "verified" else ("medium" if status == "partial" else "low"),
    }


def _best_similarity(needle: str, haystack: str) -> float:
    """Find best fuzzy match for a needle in haystack."""
    words = needle.split()
    if not words:
        return 0.0
    window_size = len(needle)
    best = 0.0
    for i in range(0, len(haystack) - window_size, max(1, window_size // 2)):
        window = haystack[i:i + window_size]
        sim = SequenceMatcher(None, needle, window).ratio()
        if sim > best:
            best = sim
    return best


def _format_passages_for_verification(passages: List[dict], clauses: List[dict]) -> str:
    lines = []
    for p in passages[:6]:
        lines.append(f"[{p.get('source', 'doc')}] {p.get('clause_id', '')}:")
        lines.append(p.get("content", "")[:400])
        lines.append("")
    for c in clauses:
        lines.append(f"[{c.get('source', 'doc')}] {c.get('clause_id', '')}:")
        lines.append(c.get("text", "")[:400])
        lines.append("")
    return "\n".join(lines)


def _stub_verify(auto_verification: Dict, claimed_clauses: List[str]) -> Dict:
    return {
        **auto_verification,
        "verification_status": "verified",
        "needs_escalation": False,
        "confidence": "high",
        "prompt_version": PROMPT_VERSION,
    }
