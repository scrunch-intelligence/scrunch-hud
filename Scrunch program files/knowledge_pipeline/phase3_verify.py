"""One-pass Phase 3 Serum acceptance proof; no retries or cloud calls."""
import json
from pathlib import Path

from knowledge_pipeline import phase3_serum as serum
from transaction_store import Store


def run() -> dict:
    index=serum.load_index()
    manual_chunks=[row for row in index["chunks"] if row["source_type"]==serum.SOURCE_TYPE]
    before_hash=serum.sha256(json.dumps(manual_chunks,sort_keys=True).encode()).hexdigest()

    valid=serum.answer("How do I adjust warp modes in Serum?",index=index)
    missing=serum.answer("How do I adjust warp modes in Massive?",device_id="native_instruments_massive",index=index)
    preference=serum.answer("What does the Serum filter cutoff do?",index=index)

    if not valid["answered"] or valid["citations"][0]["pageNumber"]!=49:
        raise ValueError("Valid warp query did not return verified page 49 coverage.")
    if missing["answered"] or missing["text"]!=serum.REFUSAL:
        raise ValueError("Unindexed-device query did not refuse exactly as required.")
    if not preference["answered"] or not preference["preferenceContext"]:
        raise ValueError("Preference context did not attach to a cited manual answer.")
    after=serum.load_index()["chunks"]
    after_manual=[row for row in after if row["source_type"]==serum.SOURCE_TYPE]
    after_hash=serum.sha256(json.dumps(after_manual,sort_keys=True).encode()).hexdigest()
    if before_hash!=after_hash:
        raise ValueError("Manual chunks changed while attaching preference context.")
    if any(str(p["value"]) in preference["manualText"] for p in preference["preferenceContext"]):
        raise ValueError("User preference text contaminated manufacturer manual text.")

    report={"status":"passed","singleAttempt":True,"automaticRetries":0,
        "index":{"documents":len(index["documents"]),"chunks":len(index["chunks"]),
            "manufacturerChunks":len(manual_chunks),"manualVersion":index["manual_version"],
            "deviceVersion":index["deviceVersion"],"embedding":index["embedding"]},
        "validQuery":valid,"missingCoverageQuery":missing,"preferenceQuery":preference,
        "manualChunkHashBefore":before_hash,"manualChunkHashAfter":after_hash}
    root=serum.INDEX_ROOT
    Store(root).write(root/"verification-log.json",report)
    lines=["Phase 3 Serum single-device retrieval proof: PASS",
        f"Documents: {report['index']['documents']} (pilot + manufacturer manual)",
        f"Manual: Serum 2 {report['index']['deviceVersion']}; manual {report['index']['manualVersion']}",
        f"Chunks: {report['index']['chunks']} total; {report['index']['manufacturerChunks']} manufacturer",
        f"Embedding: {report['index']['embedding']['model']} ({report['index']['embedding']['dimensions']} dimensions; local)","",
        "VALID QUERY: How do I adjust warp modes in Serum?",
        f"Decision: {valid['retrievalTrace']['finalDecision']}",
        f"Citation: [Source: Serum 2 User Guide, Page {valid['citations'][0]['pageNumber']}]",
        f"Trace: {valid['retrievalTrace']['traceId']}",f"Manual text: {valid['manualText']}","",
        "MISSING COVERAGE: How do I adjust warp modes in Massive?",
        f"Decision: {missing['retrievalTrace']['finalDecision']}",f"Response: {missing['text']}",
        f"Trace: {missing['retrievalTrace']['traceId']}","",
        "PREFERENCE CONTEXT: What does the Serum filter cutoff do?",
        f"Decision: {preference['retrievalTrace']['finalDecision']}",
        f"Citation: [Source: Serum 2 User Guide, Page {preference['citations'][0]['pageNumber']}]",
        f"Preference records: {len(preference['preferenceContext'])}",
        f"Manual immutable: {before_hash==after_hash}",f"Trace: {preference['retrievalTrace']['traceId']}"]
    (root/"verification-log.txt").write_text("\n".join(lines)+"\n",encoding="utf-8")
    state_path=root/"device-knowledge-state.json"
    state=json.loads(state_path.read_text(encoding="utf-8"));state["manualStatus"]="verified"
    state["notes"]="Manufacturer manual 1.0.3 indexed page-by-page; Phase 3 single-device retrieval acceptance passed."
    Store(root).write(state_path,state)
    return report


if __name__=="__main__":
    result=run()
    print(json.dumps({"status":result["status"],"valid":result["validQuery"]["retrievalTrace"]["finalDecision"],
        "missing":result["missingCoverageQuery"]["retrievalTrace"]["finalDecision"],
        "preferences":len(result["preferenceQuery"]["preferenceContext"])},indent=2))
