"""
Document chunking strategy for the tariff corpus.
Uses clause-aware splitting: preserves clause boundaries when possible.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter


CHUNK_SIZE = 512
CHUNK_OVERLAP = 64

# Patterns that indicate a clause/section boundary
CLAUSE_BOUNDARY_PATTERNS = [
    r"^(Clause|Section|Regulation|Schedule|Appendix|Article|Part)\s+[\d\.]+",
    r"^Category\s+LT-\d",
    r"^(CATEGORY|PART|CHAPTER|SCHEDULE|APPENDIX)\s+",
    r"^={10,}",
    r"^-{10,}",
]


def is_clause_boundary(line: str) -> bool:
    """Check if a line is a clause/section boundary."""
    stripped = line.strip()
    if not stripped:
        return False
    for pattern in CLAUSE_BOUNDARY_PATTERNS:
        if re.match(pattern, stripped, re.IGNORECASE):
            return True
    return False


def extract_clause_id(line: str) -> str:
    """Extract a clause identifier from a line."""
    patterns = [
        r"((?:Clause|Section|Regulation|Schedule|Article)\s+[\d\.]+[a-zA-Z]?)",
        r"(Category\s+LT-\d[A-Z]?)",
        r"((?:PART|CHAPTER)\s+[IVX\d]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, line, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def clause_aware_split(text: str, source: str) -> List[Document]:
    """
    Split text on clause boundaries first, then apply size-based chunking.
    Preserves clause metadata.
    """
    lines = text.splitlines()
    segments = []
    current_clause = ""
    current_lines = []

    for line in lines:
        if is_clause_boundary(line) and current_lines:
            # Save current segment
            segments.append({
                "clause_id": current_clause,
                "text": "\n".join(current_lines).strip(),
            })
            current_lines = []
            current_clause = extract_clause_id(line) or current_clause

        current_lines.append(line)
        if not current_clause:
            current_clause = extract_clause_id(line)

    # Save final segment
    if current_lines:
        segments.append({
            "clause_id": current_clause,
            "text": "\n".join(current_lines).strip(),
        })

    # Apply recursive splitting to large segments
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    docs = []
    for seg in segments:
        if not seg["text"]:
            continue
        if len(seg["text"]) <= CHUNK_SIZE:
            docs.append(
                Document(
                    page_content=seg["text"],
                    metadata={
                        "source": source,
                        "clause_id": seg["clause_id"],
                    },
                )
            )
        else:
            sub_docs = splitter.create_documents(
                [seg["text"]],
                metadatas=[{"source": source, "clause_id": seg["clause_id"]}],
            )
            docs.extend(sub_docs)

    return docs


def chunk_documents(files: List[Path]) -> List[Document]:
    """Chunk all corpus files."""
    all_docs = []
    for f in files:
        source = f.stem
        text = f.read_text(encoding="utf-8", errors="ignore")
        docs = clause_aware_split(text, source)
        all_docs.extend(docs)
    return all_docs
