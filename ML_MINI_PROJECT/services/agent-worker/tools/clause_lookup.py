"""
Clause lookup tool: retrieve a specific clause by its ID from the corpus.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CORPUS_DIR = Path(os.getenv("CORPUS_DIR", "/app/corpus"))

# In-memory index built from corpus files at startup
_clause_index: dict[str, dict] = {}


def _build_clause_index():
    """Scan corpus files and build a clause_id -> text mapping."""
    global _clause_index
    if _clause_index:
        return

    # Look for corpus in multiple locations
    search_dirs = [CORPUS_DIR, Path("/app/corpus"), Path("corpus")]
    corpus_dir = None
    for d in search_dirs:
        if d.is_dir():
            corpus_dir = d
            break

    if corpus_dir is None:
        logger.warning("Corpus directory not found, clause index empty")
        return

    clause_pattern = re.compile(
        r"((?:Clause|Section|Schedule|Article|Regulation)\s+[\d\.]+[a-zA-Z]?)",
        re.IGNORECASE,
    )

    for txt_file in corpus_dir.glob("*.txt"):
        source = txt_file.stem
        text = txt_file.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()

        current_clause = None
        current_text = []

        for line in lines:
            match = clause_pattern.match(line.strip())
            if match:
                # Save previous clause
                if current_clause and current_text:
                    _clause_index[current_clause.upper()] = {
                        "clause_id": current_clause,
                        "source": source,
                        "text": "\n".join(current_text).strip(),
                    }
                current_clause = match.group(1)
                current_text = [line]
            elif current_clause:
                current_text.append(line)

        # Save last clause
        if current_clause and current_text:
            _clause_index[current_clause.upper()] = {
                "clause_id": current_clause,
                "source": source,
                "text": "\n".join(current_text).strip(),
            }

    logger.info("Clause index built: %d clauses indexed", len(_clause_index))


def clause_lookup(clause_id: str) -> Optional[dict]:
    """
    Look up a specific clause by its identifier.

    Args:
        clause_id: Clause identifier such as 'Clause 8.2', 'Schedule 8', 'Section 4.1'

    Returns:
        Dict with clause_id, source, text — or None if not found
    """
    _build_clause_index()

    if not clause_id:
        return None

    # Try exact match (normalized)
    key = clause_id.strip().upper()
    if key in _clause_index:
        return _clause_index[key]

    # Try partial match
    for k, v in _clause_index.items():
        if key in k or k in key:
            return v

    logger.debug("Clause not found: %s", clause_id)
    return None


def list_clauses(source_filter: Optional[str] = None) -> list[str]:
    """List all indexed clause IDs, optionally filtered by source document."""
    _build_clause_index()
    if source_filter:
        return [
            v["clause_id"]
            for v in _clause_index.values()
            if source_filter.lower() in v["source"].lower()
        ]
    return [v["clause_id"] for v in _clause_index.values()]
