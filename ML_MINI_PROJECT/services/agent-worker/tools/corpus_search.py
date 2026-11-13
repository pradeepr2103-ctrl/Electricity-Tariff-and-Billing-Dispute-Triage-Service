"""
Corpus search tool: semantic FAISS-based retrieval over the tariff corpus.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import List, Optional

import redis
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings

logger = logging.getLogger(__name__)

_vectorstore: Optional[FAISS] = None
_embedder: Optional[HuggingFaceEmbeddings] = None
_redis_client: Optional[redis.Redis] = None

FAISS_INDEX_PATH = os.getenv("FAISS_INDEX_PATH", "/app/faiss_index")
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
RETRIEVAL_CACHE_TTL = int(os.getenv("RETRIEVAL_CACHE_TTL_SECONDS", "300"))


def _get_embedder() -> HuggingFaceEmbeddings:
    global _embedder
    if _embedder is None:
        _embedder = HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)
    return _embedder


def _get_vectorstore() -> Optional[FAISS]:
    global _vectorstore
    if _vectorstore is None:
        if os.path.isdir(FAISS_INDEX_PATH):
            try:
                _vectorstore = FAISS.load_local(
                    FAISS_INDEX_PATH,
                    _get_embedder(),
                    allow_dangerous_deserialization=True,
                )
                logger.info("FAISS index loaded from %s", FAISS_INDEX_PATH)
            except Exception as e:
                logger.error("Failed to load FAISS index: %s", e)
    return _vectorstore


def _get_redis() -> Optional[redis.Redis]:
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis.from_url(
                os.getenv("REDIS_URL", "redis://redis:6379/0"),
                decode_responses=True,
            )
        except Exception as e:
            logger.warning("Redis unavailable for cache: %s", e)
    return _redis_client


def corpus_search(query: str, k: int = 5) -> List[dict]:
    """
    Semantic search over the tariff corpus.

    Args:
        query: Natural language search query
        k: Number of results to return (default 5)

    Returns:
        List of dicts with keys: content, source, clause_id, score
    """
    if not query or not query.strip():
        return []

    # Check cache
    cache_key = f"retrieval:{hashlib.md5(f'{query}:{k}'.encode()).hexdigest()}"
    r = _get_redis()
    if r:
        try:
            cached = r.get(cache_key)
            if cached:
                logger.debug("Cache hit for query: %s", query[:50])
                return json.loads(cached)
        except Exception:
            pass

    vs = _get_vectorstore()
    if vs is None:
        logger.warning("FAISS index not available — returning empty results")
        return []

    try:
        docs_with_scores = vs.similarity_search_with_score(query, k=k)
        results = []
        for doc, score in docs_with_scores:
            results.append({
                "content": doc.page_content,
                "source": doc.metadata.get("source", "unknown"),
                "clause_id": doc.metadata.get("clause_id", ""),
                "score": float(1 / (1 + score)),  # Convert distance to similarity
            })

        # Cache result
        if r:
            try:
                r.setex(cache_key, RETRIEVAL_CACHE_TTL, json.dumps(results))
            except Exception:
                pass

        return results
    except Exception as e:
        logger.error("Corpus search error: %s", e)
        return []
