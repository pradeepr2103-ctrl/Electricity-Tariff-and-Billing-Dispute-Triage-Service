"""
Guardrails: prompt injection detection, toxicity filtering, output sanitization.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Injection Patterns
# ---------------------------------------------------------------------------
INJECTION_PATTERNS = [
    r"ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions",
    r"disregard\s+(?:all\s+)?(?:previous|above|prior)",
    r"forget\s+(?:all\s+)?(?:previous|above|prior)\s+instructions",
    r"you\s+are\s+now\s+(?:a\s+)?(?:different|new)",
    r"system\s*:\s*you\s+are",
    r"act\s+as\s+(?:if\s+you\s+are|a\s+different)",
    r"new\s+instructions?\s*:",
    r"<\s*/?(?:script|iframe|object|embed|img)\s*[^>]*>",
    r"javascript\s*:",
    r"data\s*:\s*text/html",
    r"\beval\s*\(",
    r"__import__\s*\(",
    r"exec\s*\(",
    r"os\.system\s*\(",
]

# Simple keyword-based toxicity wordlist (production: use a model)
TOXICITY_KEYWORDS = [
    "fuck", "shit", "bitch", "asshole", "bastard", "cunt", "damn",
    "kill", "murder", "bomb", "terrorist", "rape",
]

TOXICITY_THRESHOLD = float(os.getenv("TOXICITY_THRESHOLD", "0.8"))


def check_injection(text: str) -> Tuple[bool, str]:
    """
    Returns (is_injection, matched_pattern).
    """
    if not text:
        return False, ""
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            logger.warning("Prompt injection detected: pattern=%s", pattern)
            return True, pattern
    return False, ""


def check_toxicity(text: str) -> Tuple[bool, float]:
    """
    Heuristic toxicity check. Returns (is_toxic, score).
    Production: replace with a proper toxicity model.
    """
    if not text:
        return False, 0.0
    text_lower = text.lower()
    matches = sum(1 for kw in TOXICITY_KEYWORDS if kw in text_lower)
    score = min(1.0, matches / 3.0)
    return score >= TOXICITY_THRESHOLD, score


def sanitize_llm_input(text: str) -> Tuple[str, bool]:
    """
    Sanitize text before sending to LLM.
    Returns (sanitized_text, was_modified).
    """
    original = text
    # Remove null bytes
    text = re.sub(r"\x00", "", text)
    # Truncate
    max_len = int(os.getenv("MAX_DESCRIPTION_LENGTH", "2000"))
    if len(text) > max_len:
        text = text[:max_len] + "...[truncated]"
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text, text != original


def sanitize_llm_output(text: str) -> str:
    """
    Filter LLM output — remove any unintended system-level content.
    """
    if not text:
        return text
    # Remove any injected SYSTEM: lines
    text = re.sub(r"(?i)^system\s*:.*$", "", text, flags=re.MULTILINE)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def validate_agent_input(text: str) -> Tuple[bool, str]:
    """
    Full validation pipeline for any text entering the agent.
    Returns (is_safe, reason).
    """
    if not text:
        return True, ""

    is_injection, pattern = check_injection(text)
    if is_injection:
        return False, f"Prompt injection detected (pattern: {pattern})"

    is_toxic, score = check_toxicity(text)
    if is_toxic:
        return False, f"Toxic content detected (score: {score:.2f})"

    return True, ""
