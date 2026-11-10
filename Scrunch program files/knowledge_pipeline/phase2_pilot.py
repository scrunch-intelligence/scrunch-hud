"""Phase 2 one-device pilot ingestion pipeline."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Sequence

import numpy

from pypdf import PdfReader


DEFAULT_EMBEDDING_LIBRARY = "numpy"
DEFAULT_EMBEDDING_MODEL = "serum-tfidf-token-v1"

# Explicit configuration for this pilot. No implicit defaults are allowed by constraint.
DEFAULT_CHUNK_SIZE_TOKENS = 200
DEFAULT_CHUNK_OVERLAP_TOKENS = 40
DEFAULT_PREFERENCE_MIN_CONFIDENCE = 0.75
DEFAULT_PREFERENCE_REPEAT_THRESHOLD = 2
USER_PREFERENCE_FILE = "user_preference_records.json"
USER_PREFERENCE_DIR = "user_preference_records"


@dataclass(frozen=True)
class LocalVectorIndex:
    """In-memory representation before writing local vector baseline store."""

    vocab: Dict[str, int]
    idf: List[float]
    vectors: Dict[str, List[float]]



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9._#+-]+", text.lower())


def _is_pdf_readable(path: Path) -> bool:
    try:
        reader = PdfReader(str(path))
    except Exception:
        return False
    return len(reader.pages) > 0


def extract_text(path: Path) -> str:
    """Extract text from source refs (PDF or text) for pilot ingestion."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        if not _is_pdf_readable(path):
            raise ValueError(f"Pilot PDF unreadable: {path}")
        reader = PdfReader(str(path))
        pages = [page.extract_text() for page in reader.pages]
        return "\n\n".join(p for p in pages if p)
    if suffix in {".txt", ".md", ".markdown", ".rst"}:
        return path.read_text(encoding="utf-8")
    raise ValueError(f"Unsupported source extension for pilot: {path.suffix}")


def chunk_text(
    text: str,
    chunk_size_tokens: int = DEFAULT_CHUNK_SIZE_TOKENS,
    overlap_tokens: int = DEFAULT_CHUNK_OVERLAP_TOKENS,
) -> List[Dict[str, Any]]:
    # Explicit chunk contract (Phase 2):
    # - chunk_size_tokens = target token length per chunk.
    # - overlap_tokens = number of overlapping tail tokens from previous chunk.
    if chunk_size_tokens <= 0:
        raise ValueError("chunk_size_tokens must be > 0")
    if overlap_tokens < 0:
        raise ValueError("overlap_tokens must be >= 0")
    if overlap_tokens >= chunk_size_tokens:
        raise ValueError("overlap_tokens must be less than chunk_size_tokens")

    tokens = _tokenize(text)
    if not tokens:
        raise ValueError("No tokens available after normalization for pilot ingestion")

    chunks: List[Dict[str, Any]] = []
    cursor = 0
    chunk_index = 0
    while cursor < len(tokens):
        end = min(len(tokens), cursor + chunk_size_tokens)
        chunk_tokens = tokens[cursor:end]
        chunks.append(
            {
                "chunkIndex": chunk_index,
                "tokenStart": cursor,
                "tokenEnd": end,
                "text": " ".join(chunk_tokens),
            }
        )
        if end == len(tokens):
            break
        cursor = end - overlap_tokens
        chunk_index += 1
    return chunks


def _build_vocab(chunks: Sequence[str]) -> Dict[str, int]:
    vocab: Dict[str, int] = {}
    for chunk in chunks:
        for token in _tokenize(chunk):
            if token not in vocab:
                vocab[token] = len(vocab)
    if not vocab:
        raise ValueError("Cannot build vocabulary: document tokens are empty")
    return vocab


def build_tfidf_vectors(chunks: Sequence[str]) -> LocalVectorIndex:
    """Local-only embedding generation (Apple Silicon-safe): pure NumPy TF-IDF."""
    vocab = _build_vocab(chunks)
    token_rows = [_tokenize(chunk) for chunk in chunks]
    doc_count = len(token_rows)
    vocab_size = len(vocab)

    tf = numpy.zeros((doc_count, vocab_size), dtype=float)
    df = numpy.zeros(vocab_size, dtype=float)
    for row, tokens in enumerate(token_rows):
        seen: set[int] = set()
        for token in tokens:
            token_index = vocab[token]
            tf[row, token_index] += 1.0
            seen.add(token_index)
        for token_index in seen:
            df[token_index] += 1.0

    idf = numpy.log((1 + doc_count) / (1 + df)) + 1.0
    tfidf = tf * idf
    norms = numpy.linalg.norm(tfidf, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vectors = tfidf / norms

    return LocalVectorIndex(
        vocab=vocab,
        idf=idf.tolist(),
        vectors={str(i): vectors[i].tolist() for i in range(len(chunks))},
    )


def _query_vector(
    query: str,
    vocab: Dict[str, int],
    idf: Sequence[float],
) -> numpy.ndarray:
    tokens = _tokenize(query)
    vector = numpy.zeros(len(vocab), dtype=float)
    for token in tokens:
        if token in vocab:
            vector[vocab[token]] += 1.0
    vector *= numpy.array(idf, dtype=float)
    norm = numpy.linalg.norm(vector)
    if norm == 0:
        return vector
    return vector / norm


def _read_preference_records(base_dir: Path) -> List[Dict[str, Any]]:
    preference_file = base_dir / USER_PREFERENCE_DIR / USER_PREFERENCE_FILE
    if not preference_file.exists():
        return []
    try:
        payload = json.loads(preference_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Could not decode preference records in {preference_file}: {exc}"
        ) from exc
    if not isinstance(payload, list):
        raise ValueError(f"Preference records payload must be a JSON array: {preference_file}")
    return payload


def _is_reliable_preference_record(record: Dict[str, Any]) -> bool:
    provenance = record.get("provenance")
    confidence = float(record.get("confidence", 0))
    if confidence < DEFAULT_PREFERENCE_MIN_CONFIDENCE:
        return False

    if provenance == "explicitly-confirmed":
        return True
    if provenance == "observed":
        return record.get("observationCount", 0) >= DEFAULT_PREFERENCE_REPEAT_THRESHOLD
    return False


def _preference_match_score(record: Dict[str, Any], query: str) -> float:
    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return 0.0

    terms: List[str] = [record.get("preferenceType", "")]
    terms.append(str(record.get("value")))
    scope = record.get("scope") or {}
    if isinstance(scope, dict):
        terms.extend([scope.get("vendor", ""), scope.get("product", ""), scope.get("deviceId", "")])
    record_text = " ".join(filter(None, map(str, terms)))
    record_tokens = set(_tokenize(record_text))

    if not record_tokens:
        return 0.0
    return len(query_tokens & record_tokens)


def _query_preference_context(
    query: str,
    preference_records: Sequence[Dict[str, Any]],
    top_k: int = 3,
) -> List[Dict[str, Any]]:
    scored = []
    for record in preference_records:
        if not _is_reliable_preference_record(record):
            continue
        score = _preference_match_score(record, query)
        if score <= 0:
            continue
        scored.append({"score": score, "record": record})

    scored.sort(key=lambda item: (item["score"], item["record"].get("confidence", 0)), reverse=True)
    return [
        {
            "preferenceId": item["record"]["preferenceId"],
            "preferenceType": item["record"]["preferenceType"],
            "value": item["record"]["value"],
            "confidence": item["record"]["confidence"],
            "provenance": item["record"]["provenance"],
            "score": item["score"],
        }
        for item in scored[:top_k]
    ]


def query_similarity_with_context(
    index_payload: Dict[str, Any],
    preference_records: Sequence[Dict[str, Any]],
    query: str,
    top_k: int = 3,
    top_preference_k: int = 2,
) -> Dict[str, Any]:
    return {
        "results": query_similarity(index_payload, query=query, top_k=top_k),
        "preferenceContext": _query_preference_context(
            query,
            preference_records=preference_records,
            top_k=top_preference_k,
        ),
    }


def query_similarity(
    index_payload: Dict[str, Any],
    query: str,
    top_k: int = 3,
) -> List[Dict[str, Any]]:
    if top_k <= 0:
        raise ValueError("top_k must be positive")

    query_vector = _query_vector(
        query=query,
        vocab=index_payload["vocab"],
        idf=index_payload["idf"],
    )
    scores: List[Dict[str, Any]] = []
    for chunk_id, chunk_vector in index_payload["chunkVectors"].items():
        vec = numpy.array(chunk_vector, dtype=float)
        scores.append({
            "chunkId": chunk_id,
            "score": float(numpy.dot(query_vector, vec)),
        })
    scores.sort(key=lambda item: item["score"], reverse=True)
    for score in scores[:top_k]:
        chunk_id = score["chunkId"]
        score["chunkIndex"] = index_payload["chunkMeta"][chunk_id]["index"]
        score["text"] = index_payload["chunkMeta"][chunk_id]["text"]
        score["sourceLocator"] = index_payload["chunkMeta"][chunk_id]["sourceLocator"]
    return scores[:top_k]


def _document_id_for(source_path: Path) -> str:
    return _sha256_bytes(source_path.read_bytes())[:16]


def write_documents_and_chunks(
    *,
    base_dir: Path,
    source_path: Path,
    source_type: str,
    vendor: str,
    product: str,
    version: str,
    document_id: str,
    chunk_records: Sequence[Dict[str, Any]],
) -> None:
    """Persist phase-2 pilot records under the current run folder."""
    records_dir = base_dir / "document_records"
    chunks_dir = base_dir / "chunk_records"
    records_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)

    document_record = {
        "documentId": document_id,
        "title": source_path.name,
        "sourceType": source_type,
        "pluginAssociation": {
            "vendor": vendor,
            "deviceType": product,
            "deviceId": f"{vendor}:{product}:{version}",
            "version": version,
        },
        "ingestedAt": _now_iso(),
        "fileHash": _sha256_bytes(source_path.read_bytes()),
        "filePath": str(source_path),
        "fileType": source_path.suffix.lower().lstrip("."),
        "rightsStatus": "private",
        "schemaVersion": 1,
    }
    (records_dir / f"{document_id}.json").write_text(
        json.dumps(document_record, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    for chunk_record in chunk_records:
        (chunks_dir / f"{chunk_record['chunkId']}.json").write_text(
            json.dumps(chunk_record, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def _preference_seed_value(preference_type: str, value: Any, scope: Dict[str, Any] | None) -> str:
    seed = json.dumps(
        {
            "preferenceType": preference_type,
            "value": value,
            "scope": scope or {},
        },
        sort_keys=True,
    )
    return sha256(seed.encode("utf-8")).hexdigest()[:16]


def record_preference_signal(
    *,
    run_dir: Path,
    preference_type: str,
    value: Any,
    provenance: str,
    confidence: float,
    scope: Dict[str, Any] | None = None,
    observation_count: int = 1,
    source: str | None = None,
) -> Dict[str, Any] | None:
    """Write a UserPreferenceRecord only when it is meaningful.

    Observed preferences are only logged after repeated signal count reaches the local
    threshold so we do not persist every single probe action as a durable preference.
    """
    if provenance not in {"observed", "explicitly-confirmed"}:
        raise ValueError("provenance must be 'observed' or 'explicitly-confirmed'")

    preference_record = {
        "preferenceId": _preference_seed_value(
            preference_type=preference_type,
            value=value,
            scope=scope,
        ),
        "preferenceType": preference_type,
        "value": value,
        "provenance": provenance,
        "confidence": confidence,
        "observationCount": observation_count,
        "timestamp": _now_iso(),
        "source": source,
        "schemaVersion": 1,
    }
    if scope:
        preference_record["scope"] = scope

    if not _is_reliable_preference_record(preference_record):
        return None

    preference_dir = run_dir / USER_PREFERENCE_DIR
    preference_dir.mkdir(parents=True, exist_ok=True)
    preference_file = preference_dir / USER_PREFERENCE_FILE

    records: List[Dict[str, Any]] = []
    if preference_file.exists():
        records = _read_preference_records(run_dir)
        if any(item.get("preferenceId") == preference_record["preferenceId"] for item in records):
            return None

    records.append(preference_record)
    preference_file.write_text(
        json.dumps(records, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return preference_record


def _build_chunk_records(
    *,
    document_id: str,
    source_path: Path,
    chunks: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    chunk_records: List[Dict[str, Any]] = []
    for chunk in chunks:
        chunk_id = f"{document_id}-c{chunk['chunkIndex']:03d}"
        chunk_records.append(
            {
                "chunkId": chunk_id,
                "documentId": document_id,
                "chunkIndex": chunk["chunkIndex"],
                "text": chunk["text"],
                "embeddingRef": f"{DEFAULT_EMBEDDING_LIBRARY}:{chunk_id}",
                "pageHint": None,
                "sectionHint": None,
                "sourceLocator": f"{source_path.name}:{chunk['chunkIndex']}",
                "parserVersion": DEFAULT_EMBEDDING_MODEL,
                "schemaVersion": 1,
            }
        )
    return chunk_records


def build_pilot_index(
    *,
    source_path: Path,
    out_root: Path,
    vendor: str = "Xfer Records",
    product: str = "Serum",
    version: str = "2.0",
    chunk_size: int = DEFAULT_CHUNK_SIZE_TOKENS,
    overlap: int = DEFAULT_CHUNK_OVERLAP_TOKENS,
) -> Dict[str, Any]:
    """Build pilot pipeline for exactly one source document."""
    source_path = source_path.resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"Pilot source not found: {source_path}")

    text = extract_text(source_path)
    chunked = chunk_text(text=text, chunk_size_tokens=chunk_size, overlap_tokens=overlap)
    chunk_texts = [chunk["text"] for chunk in chunked]
    local_vectors = build_tfidf_vectors(chunk_texts)

    run_dir = out_root / "serum-pilot"
    run_dir.mkdir(parents=True, exist_ok=True)

    document_id = _document_id_for(source_path)
    chunk_records = _build_chunk_records(
        document_id=document_id,
        source_path=source_path,
        chunks=chunked,
    )
    write_documents_and_chunks(
        base_dir=run_dir,
        source_path=source_path,
        source_type="manual",
        vendor=vendor,
        product=product,
        version=version,
        document_id=document_id,
        chunk_records=chunk_records,
    )

    chunk_vectors = {
        chunk_records[i]["chunkId"]: local_vectors.vectors[str(i)]
        for i in range(len(chunk_records))
    }
    chunk_meta = {
        chunk_records[i]["chunkId"]: {
            "index": chunk_records[i]["chunkIndex"],
            "sourceLocator": chunk_records[i]["sourceLocator"],
            "text": chunk_records[i]["text"],
        }
        for i in range(len(chunk_records))
    }

    index_payload = {
        "schemaVersion": 1,
        "pilot": True,
        "documentId": document_id,
        "sourcePath": str(source_path),
        "runRoot": str(run_dir.resolve()),
        "runAt": _now_iso(),
        "chunking": {
            "chunkSizeTokens": chunk_size,
            "overlapTokens": overlap,
            "embeddingLibrary": DEFAULT_EMBEDDING_LIBRARY,
            "embeddingModel": DEFAULT_EMBEDDING_MODEL,
            "chunkingNote": (
                f"Explicit token chunking: size={chunk_size}, overlap={overlap}"
            ),
        },
        "search": {
            "queryAlgorithm": "cosine",
            "description": "dot product over l2-normalized tf-idf vectors",
            "library": DEFAULT_EMBEDDING_LIBRARY,
        },
        "vocab": local_vectors.vocab,
        "idf": local_vectors.idf,
        "chunkVectors": chunk_vectors,
        "chunkMeta": chunk_meta,
    }

    (run_dir / "vector-store.json").write_text(
        json.dumps(index_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    return index_payload


def run_demo_queries(index_payload: Dict[str, Any], queries: Sequence[str]) -> List[Dict[str, Any]]:
    run_root = index_payload.get("runRoot")
    preference_records = _read_preference_records(Path(run_root)) if run_root else []
    return [
        {
            "query": query,
            **query_similarity_with_context(
                index_payload=index_payload,
                preference_records=preference_records,
                query=query,
                top_k=3,
                top_preference_k=3,
            ),
        }
        for query in queries
    ]


def _write_demo_log(index_payload: Dict[str, Any], output_dir: Path) -> str:
    sample_queries = [
        "serum filter cutoff and resonance",
        "where is oscillator wavetable position in serum",
        "how do i verify control assignment safety",
    ]
    results = run_demo_queries(index_payload, sample_queries)

    log_json_path = output_dir / "query-log.json"
    log_json_path.write_text(
        json.dumps(
            {
                "model": {
                    "embeddingLibrary": DEFAULT_EMBEDDING_LIBRARY,
                    "embeddingModel": DEFAULT_EMBEDDING_MODEL,
                    "queryAlgorithm": "cosine over l2-normalized tf-idf",
                },
                "runAt": _now_iso(),
                "queries": results,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    log_text_path = output_dir / "query-log.txt"
    lines = [
        "Phase 2 pilot query log (manual verification)",
        f"runAt: {index_payload['runAt']}",
        "embedding: numpy + local TF-IDF, one document only",
        "",
    ]
    for item in results:
        lines.append(f"query: {item['query']}")
        for row in item["results"]:
            snippet = row["text"][:140].replace("\n", " ")
            lines.append(
                f"  - chunk={row['chunkId']} score={row['score']:.4f} index={row['chunkIndex']}"
            )
            lines.append(f"    snippet={snippet}")
        for pref in item.get("preferenceContext", []):
            lines.append(
                "  - preference="
                f"{pref['preferenceType']} value={pref['value']} score={pref['score']:.2f} "
                f"provenance={pref['provenance']} confidence={pref['confidence']:.2f}"
            )
        lines.append("")
    log_text_path.write_text("\n".join(lines), encoding="utf-8")
    return str(log_json_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 2 pilot ingestion for one Serum document")
    parser.add_argument(
        "source",
        type=Path,
        help="Pilot source reference file (text or PDF)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/knowledge/phase2"),
        help="Output directory for records and local vector payload",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    index_payload = build_pilot_index(source_path=args.source, out_root=args.out)
    output_dir = args.out / "serum-pilot"
    log_path = _write_demo_log(index_payload, output_dir)
    print(f"Pilot index written: {output_dir / 'vector-store.json'}")
    print(f"Query log written: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
