"""Phase 2 pilot ingestion primitives for Scrunch knowledge base."""

from .phase2_pilot import (
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DEFAULT_CHUNK_SIZE_TOKENS,
    DEFAULT_EMBEDDING_LIBRARY,
    DEFAULT_EMBEDDING_MODEL,
    build_pilot_index,
    chunk_text,
    extract_text,
    build_tfidf_vectors,
    query_similarity_with_context,
    record_preference_signal,
    query_similarity,
    run_demo_queries,
    write_documents_and_chunks,
)

__all__ = [
    "DEFAULT_CHUNK_SIZE_TOKENS",
    "DEFAULT_CHUNK_OVERLAP_TOKENS",
    "DEFAULT_EMBEDDING_LIBRARY",
    "DEFAULT_EMBEDDING_MODEL",
    "build_pilot_index",
    "chunk_text",
    "extract_text",
    "build_tfidf_vectors",
    "query_similarity_with_context",
    "record_preference_signal",
    "query_similarity",
    "run_demo_queries",
    "write_documents_and_chunks",
]
