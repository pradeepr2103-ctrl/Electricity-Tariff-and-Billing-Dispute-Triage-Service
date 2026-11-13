"""
Corpus ingestion pipeline: chunk, embed, and index tariff documents.
Idempotent: re-running with the same corpus produces the same index.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from chunker import chunk_documents
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
MANIFEST_FILE = "ingestion_manifest.json"


def compute_corpus_hash(corpus_dir: Path) -> str:
    """Compute a hash of all corpus files for idempotency check."""
    h = hashlib.md5()
    for f in sorted(corpus_dir.glob("*.txt")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def load_manifest(index_path: Path) -> dict:
    manifest_path = index_path / MANIFEST_FILE
    if manifest_path.exists():
        try:
            return json.loads(manifest_path.read_text())
        except Exception:
            pass
    return {}


def save_manifest(index_path: Path, corpus_hash: str, doc_count: int, chunk_count: int):
    manifest = {
        "corpus_hash": corpus_hash,
        "doc_count": doc_count,
        "chunk_count": chunk_count,
        "embedding_model": EMBEDDING_MODEL,
    }
    (index_path / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2))


def ingest(corpus_dir: Path, index_path: Path, force: bool = False):
    """
    Main ingestion function.
    - Checks if corpus has changed (idempotency)
    - Chunks documents
    - Embeds with sentence-transformers
    - Builds FAISS index
    """
    index_path.mkdir(parents=True, exist_ok=True)

    corpus_hash = compute_corpus_hash(corpus_dir)
    manifest = load_manifest(index_path)

    if not force and manifest.get("corpus_hash") == corpus_hash:
        logger.info(
            "Corpus unchanged (hash: %s). Skipping re-ingestion. "
            "Use --force to override.",
            corpus_hash[:8],
        )
        return

    logger.info("Starting corpus ingestion from: %s", corpus_dir)

    # Load and chunk
    txt_files = list(corpus_dir.glob("*.txt"))
    if not txt_files:
        logger.error("No .txt files found in %s", corpus_dir)
        sys.exit(1)

    logger.info("Found %d corpus files", len(txt_files))
    all_chunks = chunk_documents(txt_files)
    logger.info("Total chunks: %d", len(all_chunks))

    if not all_chunks:
        logger.error("No chunks produced — check corpus files")
        sys.exit(1)

    # Embed
    logger.info("Loading embedding model: %s", EMBEDDING_MODEL)
    embedder = HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)

    # Build FAISS index
    logger.info("Building FAISS index...")
    texts = [c.page_content for c in all_chunks]
    metadatas = [c.metadata for c in all_chunks]

    vectorstore = FAISS.from_texts(texts, embedder, metadatas=metadatas)

    # Save index
    vectorstore.save_local(str(index_path))
    logger.info("FAISS index saved to: %s", index_path)

    # Save manifest
    save_manifest(index_path, corpus_hash, len(txt_files), len(all_chunks))
    logger.info(
        "Ingestion complete. Documents: %d, Chunks: %d",
        len(txt_files),
        len(all_chunks),
    )


def main():
    parser = argparse.ArgumentParser(description="Ingest tariff corpus into FAISS index")
    parser.add_argument("--corpus-dir", type=Path, default=Path("/app/corpus"))
    parser.add_argument("--index-path", type=Path, default=Path("/app/faiss_index"))
    parser.add_argument("--force", action="store_true", help="Force re-ingestion even if corpus unchanged")
    args = parser.parse_args()

    if not args.corpus_dir.exists():
        logger.error("Corpus directory not found: %s", args.corpus_dir)
        sys.exit(1)

    ingest(args.corpus_dir, args.index_path, force=args.force)


if __name__ == "__main__":
    main()
