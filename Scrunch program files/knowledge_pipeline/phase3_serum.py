"""Phase 3: page-aware Serum manual ingestion and fail-closed retrieval.

The index uses local NumPy latent semantic analysis (LSA). It never calls a
cloud embedding service. Manufacturer facts, pilot material, and user
preferences retain separate provenance and are filtered before answering.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy
from pypdf import PdfReader

import engine
from transaction_store import Store
from knowledge_pipeline.phase2_pilot import _query_preference_context

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "knowledge_base/serum/Serum_2_User_Guide_Manual_1.0.3.pdf"
PILOT = ROOT / "knowledge_base/serum/SCRUNCH_SERUM_PILOT_REFERENCE.txt"
INDEX_ROOT = engine.DATA / "knowledge/phase3/serum"
PREFERENCES = engine.DATA / "knowledge/phase2/serum-pilot/user_preference_records/user_preference_records.json"
DEVICE_ID = "xfer_serum"
SOURCE_TYPE = "manufacturer_manual"
EMBEDDING_MODEL = "numpy-lsa-64-v1"
CHUNK_SIZE_TOKENS = 180
CHUNK_OVERLAP_TOKENS = 30
LSA_DIMENSIONS = 64
MIN_SIMILARITY = 0.10
MIN_QUERY_COVERAGE = 0.50
REFUSAL = "I do not have verified manual coverage to answer this question accurately."
STOPWORDS = frozenset("a an and are as at be by can do does for from how i in is it me of on or please serum that the this to what when where which who why with you your adjust explain tell".split())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", text.lower())


def _query_terms(text: str) -> set[str]:
    return {token for token in _tokens(text) if token not in STOPWORDS and len(token) > 1}


def _clean_page(text: str, page_number: int) -> str:
    lines=[]
    for raw in text.replace("\u00ad", "").splitlines():
        line=" ".join(raw.split()).strip()
        if not line or re.fullmatch(rf"Serum 2 User Guide\s+{page_number}", line):
            continue
        if line == str(page_number):
            continue
        lines.append(line)
    return "\n".join(lines)


def _section_hint(text: str) -> str:
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "Unknown section"
    headings=[]
    for line in lines[:5]:
        if len(line) <= 70 and not line.endswith((".", ":", ";")):
            headings.append(line)
        if len(headings) == 2:
            break
    return " > ".join(headings) if headings else lines[0][:100]


def _page_chunks(text: str) -> list[str]:
    # Keep the manual's punctuation and capitalization in stored/displayed text.
    # Vector tokenization happens separately and never mutates the source excerpt.
    tokens=text.split()
    chunks=[]; cursor=0
    while cursor < len(tokens):
        end=min(len(tokens), cursor + CHUNK_SIZE_TOKENS)
        chunks.append(" ".join(tokens[cursor:end]))
        if end == len(tokens): break
        cursor=end - CHUNK_OVERLAP_TOKENS
    return chunks


def _manual_metadata(reader: PdfReader) -> tuple[str, str]:
    opening="\n".join((reader.pages[i].extract_text() or "") for i in range(min(5,len(reader.pages))))
    device=re.search(r"(?m)^\s*Version:?\s*([0-9.]+)", opening)
    manual=re.search(r"(?m)^\s*Manual Version:?\s*([0-9.]+)", opening)
    if not device or not manual:
        raise ValueError("Could not verify Serum device and manual versions from the manufacturer PDF.")
    return device.group(1), manual.group(1)


def _record_chunks(source: Path, document_id: str, manual_version: str, source_type: str) -> list[dict[str,Any]]:
    records=[]
    if source.suffix.lower() == ".pdf":
        reader=PdfReader(str(source))
        for page_number,page in enumerate(reader.pages,1):
            text=_clean_page(page.extract_text() or "",page_number)
            if not _tokens(text): continue
            section=_section_hint(text)
            for within_page,chunk_text in enumerate(_page_chunks(text)):
                chunk_id=f"{document_id}-p{page_number:03d}-c{within_page:02d}"
                records.append({"chunkId":chunk_id,"documentId":document_id,"chunkIndex":len(records),
                    "text":chunk_text,"embeddingRef":f"{EMBEDDING_MODEL}:{chunk_id}",
                    "pageHint":str(page_number),"pageNumber":page_number,"sectionHint":section,
                    "sourceLocator":f"{source.name}#page={page_number}","parserVersion":"pypdf-page-v1",
                    "device_id":DEVICE_ID,"manual_version":manual_version,"source_type":source_type,"schemaVersion":1})
    else:
        text=source.read_text(encoding="utf-8")
        for i,chunk_text in enumerate(_page_chunks(text)):
            chunk_id=f"{document_id}-c{i:03d}"
            records.append({"chunkId":chunk_id,"documentId":document_id,"chunkIndex":i,
                "text":chunk_text,"embeddingRef":f"{EMBEDDING_MODEL}:{chunk_id}",
                "pageHint":"pilot","sectionHint":"Phase 2 pilot reference",
                "sourceLocator":f"{source.name}#chunk={i}","parserVersion":"text-page-v1",
                "device_id":DEVICE_ID,"manual_version":manual_version,"source_type":source_type,"schemaVersion":1})
    return records


def _write_record(path: Path, payload: dict[str,Any]) -> None:
    Store(path.parent).write(path,payload)


def _lsa(chunks: Sequence[dict[str,Any]]) -> dict[str,Any]:
    token_rows=[_tokens(row["text"]) for row in chunks]
    vocab={token:i for i,token in enumerate(sorted({token for row in token_rows for token in row}))}
    matrix=numpy.zeros((len(chunks),len(vocab)),dtype=numpy.float32)
    document_frequency=numpy.zeros(len(vocab),dtype=numpy.float32)
    for row_index,tokens in enumerate(token_rows):
        counts=Counter(tokens)
        for token,count in counts.items(): matrix[row_index,vocab[token]]=count
        for token in counts: document_frequency[vocab[token]]+=1
    idf=numpy.log((1+len(chunks))/(1+document_frequency))+1
    matrix*=idf
    norms=numpy.linalg.norm(matrix,axis=1,keepdims=True);norms[norms==0]=1
    matrix/=norms
    gram=matrix@matrix.T
    values,vectors=numpy.linalg.eigh(gram)
    order=numpy.argsort(values)[::-1]
    dimensions=min(LSA_DIMENSIONS,max(1,int(numpy.sum(values>1e-8))))
    values=numpy.maximum(values[order[:dimensions]],1e-8)
    left=vectors[:,order[:dimensions]]
    singular=numpy.sqrt(values)
    embedded=left*singular
    embedded_norm=numpy.linalg.norm(embedded,axis=1,keepdims=True);embedded_norm[embedded_norm==0]=1
    embedded/=embedded_norm
    sparse=[]
    for row in matrix:
        indexes=numpy.nonzero(row)[0]
        sparse.append({str(int(i)):round(float(row[i]),8) for i in indexes})
    return {"vocab":vocab,"idf":idf.tolist(),"documentTermVectors":sparse,
        "leftSingularVectors":left.tolist(),"singularValues":singular.tolist(),
        "chunkVectors":embedded.tolist(),"dimensions":dimensions}


def build_index(manual: Path=MANUAL, pilot: Path=PILOT, out: Path=INDEX_ROOT) -> dict[str,Any]:
    if not manual.exists() or not pilot.exists():
        raise ValueError("The Serum manufacturer PDF and Phase 2 pilot reference must both exist.")
    reader=PdfReader(str(manual)); device_version,manual_version=_manual_metadata(reader)
    sources=[(pilot,"pilot","pilot_reference","other"),(manual,manual_version,SOURCE_TYPE,"manual")]
    documents=[]; chunks=[]
    for source,version,source_type,schema_source_type in sources:
        digest=sha256(source.read_bytes()).hexdigest();document_id=digest[:16]
        document={"documentId":document_id,"title":"Serum 2 User Guide" if source==manual else source.name,
            "sourceType":schema_source_type,"pluginAssociation":{"vendor":"Xfer Records","deviceType":"Serum 2",
            "deviceId":DEVICE_ID,"version":device_version},"ingestedAt":_now(),"fileHash":digest,
            "filePath":str(source.resolve()),"fileType":source.suffix[1:].lower(),"rightsStatus":"private",
            "device_id":DEVICE_ID,"manual_version":version,"source_type":source_type,"schemaVersion":1}
        documents.append(document); chunks.extend(_record_chunks(source,document_id,version,source_type))
        _write_record(out/"document_records"/f"{document_id}.json",document)
    for chunk in chunks:_write_record(out/"chunk_records"/f"{chunk['chunkId']}.json",chunk)
    vectors=_lsa(chunks)
    payload={"schemaVersion":1,"device_id":DEVICE_ID,"deviceVersion":device_version,
        "manual_version":manual_version,"source_type":SOURCE_TYPE,"builtAt":_now(),
        "chunking":{"chunkSizeTokens":CHUNK_SIZE_TOKENS,"overlapTokens":CHUNK_OVERLAP_TOKENS,
        "pageBoundaryPolicy":"chunks never cross PDF pages"},
        "embedding":{"library":"numpy","model":EMBEDDING_MODEL,"algorithm":"TF-IDF plus local latent semantic analysis",
        "dimensions":vectors["dimensions"],"cloudCalls":False},
        "documents":documents,"chunks":chunks,**vectors}
    _write_record(out/"vector-store.json",payload)
    state={"deviceId":DEVICE_ID,"vendor":"Xfer Records","product":"Serum 2","version":device_version,
        "manualStatus":"indexed","pluginMapStatus":"verified-semantic","presetParseStatus":"partial",
        "lastVerifiedAt":_now(),"notes":f"Manufacturer manual {manual_version} indexed page-by-page; retrieval acceptance pending.","schemaVersion":1}
    _write_record(out/"device-knowledge-state.json",state)
    return payload


def load_index(root: Path=INDEX_ROOT) -> dict[str,Any]:
    try:return json.loads((root/"vector-store.json").read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError) as exc:raise ValueError("Serum manual index is unavailable or unreadable.") from exc


def _query_embedding(index: dict[str,Any],query: str) -> tuple[numpy.ndarray,set[str]]:
    terms=_query_terms(query);vector=numpy.zeros(len(index["vocab"]),dtype=float)
    counts=Counter(token for token in _tokens(query) if token in index["vocab"] and token not in STOPWORDS)
    for token,count in counts.items():vector[index["vocab"][token]]=count*index["idf"][index["vocab"][token]]
    norm=numpy.linalg.norm(vector)
    if norm:vector/=norm
    lexical=numpy.array([sum(vector[int(i)]*weight for i,weight in row.items()) for row in index["documentTermVectors"]])
    left=numpy.array(index["leftSingularVectors"]);singular=numpy.array(index["singularValues"])
    semantic=(lexical@left)/singular
    semantic_norm=numpy.linalg.norm(semantic)
    if semantic_norm:semantic/=semantic_norm
    return semantic,terms


def retrieve(query: str,device_id: str=DEVICE_ID,source_type: str=SOURCE_TYPE,top_k: int=3,index: dict[str,Any]|None=None) -> list[dict[str,Any]]:
    if device_id != DEVICE_ID:return []
    index=index or load_index();query_vector,terms=_query_embedding(index,query)
    if not terms or not numpy.linalg.norm(query_vector):return []
    results=[]
    for i,(chunk,vector) in enumerate(zip(index["chunks"],index["chunkVectors"])):
        if chunk.get("device_id")!=device_id or chunk.get("source_type")!=source_type:continue
        # Pages 3-12 are the table of contents. They are indexed for complete
        # provenance but cannot serve as answer evidence.
        if chunk.get("pageNumber",0) <= 12:continue
        chunk_terms=set(_tokens(chunk["text"]));coverage=len(terms&chunk_terms)/len(terms)
        score=float(numpy.dot(query_vector,numpy.array(vector)))
        lower=chunk["text"].lower()
        definition_bonus=sum(bool(re.search(rf"\b{re.escape(term)}\b.{{0,35}}\b(?:use|sets?|controls?|allows?|determines?)\b",lower)) for term in terms)/len(terms)
        results.append({"chunkId":chunk["chunkId"],"score":score,"queryCoverage":coverage,
            "rankingScore":score+0.20*coverage+0.25*definition_bonus,
            "pageNumber":chunk.get("pageNumber"),"sectionHint":chunk.get("sectionHint"),
            "sourceLocator":chunk["sourceLocator"],"text":chunk["text"]})
    results.sort(key=lambda row:(row["rankingScore"],row["score"]),reverse=True)
    return results[:top_k]


def _preferences(query: str) -> list[dict[str,Any]]:
    if not PREFERENCES.exists():return []
    records=json.loads(PREFERENCES.read_text(encoding="utf-8"))
    return _query_preference_context(query,records,top_k=3)


def _trace(query: str,answer_id: str,results: Sequence[dict[str,Any]],decision: str,coverage: float,used: Sequence[str],root: Path) -> dict[str,Any]:
    trace={"traceId":uuid.uuid4().hex,"answerId":answer_id,"modelProvider":"local-retrieval-gate",
        "query":query,"retrievedChunkIds":[r["chunkId"] for r in results],"chunkIdsUsed":list(used),
        "similarityScores":[{"chunkId":r["chunkId"],"score":round(r["score"],8)} for r in results],
        "coverageScore":round(max(0,min(1,coverage)),8),"requestedAt":_now(),
        "missingContextFlags":[] if decision=="answered" else ["manual_coverage_missing"],
        "usedInAnswer":decision=="answered","finalDecision":decision,"schemaVersion":1}
    _write_record(root/"retrieval_traces"/f"{trace['traceId']}.json",trace)
    return trace


def answer(query: str,device_id: str=DEVICE_ID,index: dict[str,Any]|None=None,root: Path=INDEX_ROOT) -> dict[str,Any]:
    answer_id=uuid.uuid4().hex
    if device_id != DEVICE_ID:
        trace=_trace(query,answer_id,[],"refused_missing_device",0,[],root)
        return {"answered":False,"answerId":answer_id,"text":REFUSAL,"citations":[],"preferenceContext":[],"retrievalTrace":trace}
    results=retrieve(query,device_id=device_id,index=index)
    best=results[0] if results else None
    coverage=best["queryCoverage"] if best else 0
    if not best or best["score"]<MIN_SIMILARITY or coverage<MIN_QUERY_COVERAGE:
        trace=_trace(query,answer_id,results,"refused_low_coverage",coverage,[],root)
        return {"answered":False,"answerId":answer_id,"text":REFUSAL,"citations":[],"preferenceContext":[],"retrievalTrace":trace}
    citation={"title":"Serum 2 User Guide","pageNumber":best["pageNumber"],"sourceLocator":best["sourceLocator"],"chunkId":best["chunkId"]}
    preferences=_preferences(query)
    excerpt=best["text"][:900].strip()
    response=excerpt+f"\n\n[Source: Serum 2 User Guide, Page {best['pageNumber']}]"
    if preferences:
        response+="\n\nJohn's relevant preferences: "+"; ".join(f"{p['preferenceType']}: {p['value']}" for p in preferences)
    trace=_trace(query,answer_id,results,"answered",coverage,[best["chunkId"]],root)
    return {"answered":True,"answerId":answer_id,"text":response,"manualText":excerpt,
        "citations":[citation],"preferenceContext":preferences,"retrievalTrace":trace}


def device_for_question(text: str) -> str|None:
    lower=text.lower()
    if "serum" in lower or "warp mode" in lower:return DEVICE_ID
    for term,device in (("diva","uhe_diva"),("jup","arturia_jup_8000"),("massive","native_instruments_massive"),("pigments","arturia_pigments")):
        if term in lower:return device
    return None


def is_knowledge_question(text: str) -> bool:
    lower=text.strip().lower()
    if not device_for_question(lower):return False
    # Spoken requests often contain "what I want" or end in a question mark.
    # Those still belong to the action planner, including multi-step requests.
    if re.search(r"\b(?:i want you to|i need you to|can you|could you|would you|keep it|keep that|until i say stop)\b",lower):return False
    lower=re.sub(r"^(?:(?:please|scrunch)[, ]+)+","",lower)
    return bool(re.match(r"(?:how (?:do|does|is|are|can i)|what (?:is|are|does)|where (?:is|are|can i)|why (?:is|are|does)|which\b|explain\b|tell me about\b)",lower))


def answer_question(text: str) -> dict[str,Any]|None:
    if not is_knowledge_question(text):return None
    return answer(text,device_id=device_for_question(text) or "unknown")


if __name__ == "__main__":
    built=build_index()
    print(json.dumps({"chunks":len(built["chunks"]),"documents":len(built["documents"]),"manualVersion":built["manual_version"],"embedding":built["embedding"]},indent=2))
