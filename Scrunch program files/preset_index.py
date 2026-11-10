"""Read-only preset discovery, metadata parsing and local semantic search.

Discovery has two phases. ``inspect_formats`` observes extensions and file
signatures first; ``build_index`` then parses only formats accepted for the
observed device family. Opaque vendor payloads are never guessed or rewritten.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import os
import plistlib
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import engine
from transaction_store import Store

INDEX_ROOT = engine.DATA / "knowledge" / "presets"
INDEX_PATH = INDEX_ROOT / "vector-store.json"
OVERNIGHT_LOG = engine.ROOT / "evidence" / "overnight-run.log"
MAX_TEXT_BYTES = 2_000_000
VECTOR_DIMENSIONS = 512

DEFAULT_ROOTS = (
    {"id": "system-audio-presets", "kind": "standard", "path": "/Library/Audio/Presets"},
    {"id": "user-audio-presets", "kind": "standard", "path": str(Path.home() / "Library/Audio/Presets")},
    {"id": "ableton-core-library", "kind": "standard", "path": "/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library"},
    {"id": "ableton-user-library", "kind": "standard", "path": str(Path.home() / "Music/Ableton/User Library/Presets")},
    {"id": "t7-primary", "kind": "external", "path": "/Volumes/T7/SAMPLE PACKS  PRESETS"},
    {"id": "t7-nexus-secondary", "kind": "external", "path": "/Volumes/T7/SAMPLE PACKS  PRESETS nexus "},
)

# These are candidates, not claims that a device uses every format. The
# inspector confirms the actual extension and signature before parsing.
PRESET_EXTENSIONS = frozenset({
    ".serumpreset", ".h2p", ".vpreset", ".aupreset", ".spacepreset",
    ".adv", ".adg", ".fxp", ".fxb", ".vstpreset", ".spf", ".nmsv",
    ".nki", ".nkm", ".nksn", ".labx", ".pgtx",
})

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event: str, **fields: Any) -> None:
    OVERNIGHT_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"timestamp": _now(), "event": event, **fields}, ensure_ascii=False, sort_keys=True)
    fd = os.open(OVERNIGHT_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _root_rows(roots: Sequence[dict[str, str]] | None) -> list[dict[str, str]]:
    rows = [dict(row) for row in (roots or DEFAULT_ROOTS)]
    for row in rows:
        if set(row) != {"id", "kind", "path"} or row["kind"] not in {"standard", "external"}:
            raise ValueError("Preset roots must contain id, kind and path.")
    return rows


def _files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [name for name in names if not name.startswith(".")]
        for name in files:
            if name.startswith("._") or name == ".DS_Store":
                continue
            yield Path(directory) / name


def _read_prefix(path: Path, size: int = 8192) -> bytes:
    with path.open("rb") as stream:
        return stream.read(size)


def _h2p_product(prefix: bytes) -> str:
    match = re.search(r"(?m)^#AM=([^\r\n]+)", prefix.decode("utf-8", "replace"))
    return match.group(1).strip() if match else "u-he unknown"


def _fxp_product(prefix: bytes) -> str:
    if len(prefix) < 28 or prefix[:4] != b"CcnK" or prefix[8:12] not in {b"FxCk", b"FPCh", b"FxBk", b"FBCh"}:
        return "VST2 unknown"
    plugin_id = prefix[16:20].decode("latin-1", "replace").strip("\x00 ")
    return {"syl1": "Sylenth1", "NEXU": "NEXUS", "XfsX": "Serum"}.get(plugin_id, f"VST2 {plugin_id or 'unknown'}")


def _xfer_product(prefix: bytes) -> str:
    if len(prefix) < 17 or not prefix.startswith(b"XferJson\0"):
        return ""
    length = int.from_bytes(prefix[9:17], "little")
    if not 0 < length <= 65536 or len(prefix) < 17 + length:
        return ""
    try:
        metadata = json.loads(prefix[17:17 + length])
    except (UnicodeError, json.JSONDecodeError):
        return ""
    return str(metadata.get("product") or "")


def _xml_product(prefix: bytes) -> str:
    match = re.search(br"<([A-Za-z_][A-Za-z0-9_.:-]*)(?:\s|>)", prefix)
    return match.group(1).decode("ascii", "replace") if match else ""


def _family(path: Path, prefix: bytes) -> tuple[str, str, str] | None:
    ext = path.suffix.lower()
    parts = path.parts
    joined = "/".join(parts).casefold()
    if ext == ".serumpreset" and prefix.startswith(b"XferJson\0"):
        product_id = _xfer_product(prefix)
        if product_id == "Serum2":
            return "xfer_serum", "Serum 2", "xfer-json"
        if product_id == "SerumFX":
            return "xfer_serum_fx", "Serum FX", "xfer-json"
        return None
    if ext == ".h2p":
        product = _h2p_product(prefix)
        return "u-he-" + re.sub(r"[^a-z0-9]+", "-", product.casefold()).strip("-"), product, "u-he-h2p"
    if ext in {".vpreset", ".spacepreset"} and prefix.lstrip().startswith(b"<"):
        product = _xml_product(prefix)
        if not product:
            return None
        if ext == ".spacepreset" and product == "Parameters":
            product = "SPACECONTROL"
        family = re.sub(r"[^a-z0-9]+", "-", product.casefold()).strip("-")
        return family, product, "xml-attributes"
    if ext == ".aupreset" and b"<plist" in prefix:
        product = path.parent.name
        return "audio-unit-" + re.sub(r"[^a-z0-9]+", "-", product.casefold()).strip("-"), product, "audio-unit-plist"
    if ext in {".adv", ".adg"} and prefix.startswith(b"\x1f\x8b"):
        return "ableton", "Ableton Live", "ableton-gzip-xml"
    if ext in {".fxp", ".fxb"}:
        product = _fxp_product(prefix)
        return "vst2-" + re.sub(r"[^a-z0-9]+", "-", product.casefold()).strip("-"), product, "vst2-fxp"
    if ext == ".spf":
        return "reveal-sound-spire", "Spire", "spire-spf"
    if ext == ".nmsv":
        return "native-instruments-massive", "Massive", "massive-nmsv"
    if ext in {".nki", ".nkm", ".nksn"}:
        return "native-instruments-kontakt", "Kontakt", "kontakt-binary"
    if ext in {".labx", ".pgtx"} and prefix.startswith(b"PK"):
        product = "Analog Lab" if ext == ".labx" else "Pigments"
        return "arturia-" + product.casefold().replace(" ", "-"), product, "arturia-archive"
    if ext == ".vstpreset":
        return "vst3-unknown", "VST3 unknown", "vst3-preset"
    if "nexus" in joined and path.name.casefold() == "tags.csv":
        return "refx-nexus", "NEXUS", "nexus-tags-csv"
    return None


def inspect_formats(roots: Sequence[dict[str, str]] | None = None) -> dict[str, Any]:
    root_rows = _root_rows(roots)
    families: dict[tuple[str, str, str, str], int] = Counter()
    ignored = Counter()
    root_status = []
    for row in root_rows:
        root = Path(row["path"])
        status = {**row, "available": root.is_dir(), "candidateFiles": 0}
        if root.is_dir():
            for path in _files(root):
                ext = path.suffix.lower()
                is_nexus_catalogue = path.name.casefold() == "tags.csv" and "nexus" in str(path).casefold()
                if ext not in PRESET_EXTENSIONS and not is_nexus_catalogue:
                    ignored[ext or "<none>"] += 1
                    continue
                try:
                    detected = _family(path, _read_prefix(path))
                except OSError as exc:
                    log_event("format-inspection-error", path=str(path), error=str(exc))
                    continue
                if detected is None:
                    ignored[ext] += 1
                    continue
                family, product, parser = detected
                families[(family, product, ext, parser)] += 1
                status["candidateFiles"] += 1
        root_status.append(status)
    return {
        "schemaVersion": 1,
        "inspectedAt": _now(),
        "roots": root_status,
        "formats": [
            {"family": key[0], "product": key[1], "extension": key[2], "parser": key[3], "count": count}
            for key, count in sorted(families.items())
        ],
        "ignoredExtensionCounts": dict(ignored.most_common()),
    }


def _base_record(path: Path, root: dict[str, str], family: str, product: str, parser: str) -> dict[str, Any]:
    relative = str(path.relative_to(Path(root["path"])))
    categories = [part for part in Path(relative).parts[:-1] if part not in {"Presets", "Factory", "User"}]
    ident = hashlib.sha256(f"{root['id']}:{relative}".encode("utf-8")).hexdigest()[:24]
    return {
        "presetId": ident,
        "family": family,
        "product": product,
        "name": path.stem,
        "categories": categories[-5:],
        "tags": [],
        "description": "",
        "author": "",
        "sourcePath": str(path),
        "sourceRoot": root["id"],
        "sourceKind": root["kind"],
        "fileExtension": path.suffix.lower(),
        "format": parser,
        "parameterStateStatus": "opaque",
        "parameterStates": {},
        "loadVerified": False,
        "metadataBasis": "File path and verified format signature; sound not auditioned",
        "schemaVersion": 1,
    }


def _parse_serum(path: Path, record: dict[str, Any]) -> None:
    with path.open("rb") as stream:
        prefix = stream.read(17)
        if len(prefix) != 17 or prefix[:9] != b"XferJson\0":
            raise ValueError("Unsupported Serum preset header.")
        length = int.from_bytes(prefix[9:17], "little")
        if not 0 < length <= 65536:
            raise ValueError("Invalid Serum metadata length.")
        raw = stream.read(length)
    metadata = json.loads(raw)
    product_id = metadata.get("product")
    if metadata.get("fileType") != "SerumPreset" or product_id not in {"Serum2", "SerumFX"}:
        raise ValueError("Preset is not verified as a supported Serum product.")
    record.update(
        family="xfer_serum" if product_id == "Serum2" else "xfer_serum_fx",
        product="Serum 2" if product_id == "Serum2" else "Serum FX",
        name=str(metadata.get("presetName") or record["name"])[:500],
        author=str(metadata.get("presetAuthor") or "")[:500],
        description=str(metadata.get("presetDescription") or "")[:1000],
        tags=[str(tag)[:100] for tag in metadata.get("tags", [])[:40] if isinstance(tag, str)],
        metadataBasis="XferJson vendor metadata; compressed parameter payload remains opaque",
    )


def _quoted_meta(text: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*\n'([^']*)'", text)
    return match.group(1).strip() if match else ""


def _coerce(value: str) -> str | int | float:
    value = value.strip()
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except ValueError:
        return value[:300]


def _parse_h2p(path: Path, record: dict[str, Any]) -> None:
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("h2p preset exceeds the text parser limit.")
    text = path.read_text(encoding="utf-8", errors="replace")
    product = _h2p_product(text[:8192].encode())
    if product == "u-he unknown":
        raise ValueError("h2p product marker is missing.")
    states = {}
    for key, value in re.findall(r"(?m)^([A-Za-z][A-Za-z0-9_]*)=([^\r\n]*)$", text):
        if len(states) >= 4096:
            raise ValueError("h2p parameter count exceeds the safety limit.")
        states[key] = _coerce(value)
    record.update(
        family="u-he-" + re.sub(r"[^a-z0-9]+", "-", product.casefold()).strip("-"),
        product=product,
        author=_quoted_meta(text, "Author"),
        tags=[tag for tag in _quoted_meta(text, "Categories").split(":") if tag],
        parameterStateStatus="parsed",
        parameterStates=states,
        metadataBasis="u-he text metadata and explicit key/value state; sound not auditioned",
    )


def _parse_xml(path: Path, record: dict[str, Any]) -> None:
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("XML preset exceeds the parser limit.")
    root = ET.fromstring(path.read_bytes())
    states = {key: _coerce(value) for key, value in root.attrib.items() if key not in {"presetName", "pluginVersion", "version"}}
    for child in root.iter():
        key = child.attrib.get("id")
        if key and "value" in child.attrib:
            states[key] = _coerce(child.attrib["value"])
    product = record["product"] if path.suffix.lower() == ".spacepreset" and root.tag == "Parameters" else root.tag
    record.update(
        product=product,
        name=root.attrib.get("presetName", record["name"]),
        parameterStateStatus="parsed" if states else "unavailable",
        parameterStates=states,
        metadataBasis="Verified XML attributes and parameter elements; sound not auditioned",
    )


def _parse_aupreset(path: Path, record: dict[str, Any]) -> None:
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError("Audio Unit preset exceeds the parser limit.")
    data = plistlib.loads(path.read_bytes())
    record["name"] = str(data.get("name") or record["name"])[:500]
    state = data.get("jucePluginState")
    states = {}
    if isinstance(state, bytes):
        start = state.find(b"<")
        end = state.rfind(b">")
        if 0 <= start < end:
            root = ET.fromstring(state[start:end + 1])
            states = {key: _coerce(value) for key, value in root.attrib.items() if key.casefold().startswith("parameter")}
    record.update(
        parameterStateStatus="parsed" if states else "opaque",
        parameterStates=states,
        metadataBasis="Audio Unit plist metadata; generic JUCE parameter slots parsed when present",
    )


def _parse_fxp(path: Path, record: dict[str, Any]) -> None:
    prefix = _read_prefix(path, 60)
    if len(prefix) < 56 or prefix[:4] != b"CcnK":
        raise ValueError("Invalid VST2 preset header.")
    magic = prefix[8:12]
    if magic not in {b"FxCk", b"FPCh", b"FxBk", b"FBCh"}:
        raise ValueError("Unknown VST2 preset kind.")
    name = prefix[28:56].split(b"\0", 1)[0].decode("latin-1", "replace").strip()
    record["name"] = name or record["name"]
    record["metadataBasis"] = "VST2 header; chunk payload remains opaque"
    if magic == b"FxCk":
        count = struct.unpack(">I", prefix[24:28])[0]
        if count > 8192 or path.stat().st_size < 56 + count * 4:
            raise ValueError("Invalid VST2 parameter array length.")
        raw = path.read_bytes()[56:56 + count * 4]
        record["parameterStates"] = {f"parameter_{i}": value[0] for i, value in enumerate(struct.iter_unpack(">f", raw))}
        record["parameterStateStatus"] = "parsed"


def _parse_ableton(path: Path, record: dict[str, Any]) -> None:
    with gzip.open(path, "rb") as stream:
        prefix = stream.read(65536)
    if not re.search(br"<Ableton(?:\s|>)", prefix):
        raise ValueError("Unsupported Ableton preset root.")
    record.update(
        parameterStateStatus="opaque",
        metadataBasis="Verified Ableton gzip/XML container; semantic parameter mapping is unavailable",
    )


def _parse_arturia(path: Path, record: dict[str, Any]) -> None:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
    record["description"] = f"Arturia bank containing {len(names)} archived files"
    record["metadataBasis"] = "Verified ZIP bank directory; internal parameter serialization remains opaque"


def _parse(path: Path, record: dict[str, Any]) -> None:
    parser = record["format"]
    if parser == "xfer-json":
        _parse_serum(path, record)
    elif parser == "u-he-h2p":
        _parse_h2p(path, record)
    elif parser == "xml-attributes":
        _parse_xml(path, record)
    elif parser == "audio-unit-plist":
        _parse_aupreset(path, record)
    elif parser == "vst2-fxp":
        _parse_fxp(path, record)
    elif parser == "ableton-gzip-xml":
        _parse_ableton(path, record)
    elif parser == "arturia-archive":
        _parse_arturia(path, record)


def _nexus_rows(path: Path, root: dict[str, str]) -> Iterator[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["NAME", "TAGS"]:
            raise ValueError("NEXUS tags.csv has an unsupported header.")
        for number, row in enumerate(reader, 2):
            name = (row.get("NAME") or "").strip()
            if not name:
                continue
            parts = Path(name).parts
            ident = hashlib.sha256(f"{root['id']}:{path}:row:{number}:{name}".encode()).hexdigest()[:24]
            yield {
                "presetId": ident, "family": "refx-nexus", "product": "NEXUS",
                "name": parts[-1], "categories": list(parts[:-1])[-5:],
                "tags": [str(row.get("TAGS") or "")], "description": "", "author": "",
                "sourcePath": f"{path}#row={number}", "sourceRoot": root["id"], "sourceKind": root["kind"],
                "fileExtension": ".csv", "format": "nexus-tags-csv",
                "parameterStateStatus": "unavailable", "parameterStates": {}, "loadVerified": False,
                "metadataBasis": "reFX NEXUS catalogue row; no loadable preset file or parameter state verified",
                "schemaVersion": 1,
            }


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def _expanded_tokens(text: str) -> list[str]:
    tokens = _tokens(text)
    return tokens


def _vector(text: str) -> dict[str, float]:
    counts = Counter(_expanded_tokens(text))
    buckets = Counter()
    for token, count in counts.items():
        bucket = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big") % VECTOR_DIMENSIONS
        buckets[str(bucket)] += 1.0 + math.log(count)
    norm = math.sqrt(sum(value * value for value in buckets.values())) or 1.0
    return {key: round(value / norm, 8) for key, value in buckets.items()}


def _search_text(record: dict[str, Any]) -> str:
    return " ".join([
        record["name"], record["product"], record["description"], record["author"],
        *record["categories"], *record["tags"],
    ])


def build_index(roots: Sequence[dict[str, str]] | None = None, destination: Path = INDEX_PATH) -> dict[str, Any]:
    root_rows = _root_rows(roots)
    inspection = inspect_formats(root_rows)
    accepted = {(row["family"], row["extension"], row["parser"]) for row in inspection["formats"]}
    records = []
    errors = []
    for root in root_rows:
        base = Path(root["path"])
        if not base.is_dir():
            continue
        for path in _files(base):
            ext = path.suffix.lower()
            is_nexus_catalogue = path.name.casefold() == "tags.csv" and "nexus" in str(path).casefold()
            if ext not in PRESET_EXTENSIONS and not is_nexus_catalogue:
                continue
            try:
                detected = _family(path, _read_prefix(path))
                if detected is None or (detected[0], ext, detected[2]) not in accepted:
                    continue
                if detected[2] == "nexus-tags-csv":
                    records.extend(_nexus_rows(path, root))
                    continue
                record = _base_record(path, root, *detected)
                try:
                    _parse(path, record)
                except (OSError, ValueError, TypeError, json.JSONDecodeError, ET.ParseError, plistlib.InvalidFileException, zipfile.BadZipFile) as exc:
                    record["parameterStateStatus"] = "unparsed"
                    record["parameterStates"] = {}
                    record["metadataBasis"] = "Safe filename fallback after parser error; no state inferred"
                    error = {"path": str(path), "family": detected[0], "error": f"{type(exc).__name__}: {exc}"}
                    errors.append(error)
                    log_event("preset-parser-error", **error)
                records.append(record)
            except (OSError, ValueError, TypeError, UnicodeError) as exc:
                error = {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}
                errors.append(error)
                log_event("preset-scan-error", **error)
    # NEXUS ships both a broad catalogue and a smaller set of actual FXP files.
    # Prefer the concrete FXP when both describe the same product/name/root.
    actual_keys = {
        (record["sourceRoot"], record["product"].casefold(), record["name"].casefold())
        for record in records if record["fileExtension"] != ".csv"
    }
    retained = []
    duplicate_count = 0
    for record in records:
        key = (record["sourceRoot"], record["product"].casefold(), record["name"].casefold())
        if record["fileExtension"] == ".csv" and key in actual_keys:
            duplicate_count += 1
            continue
        retained.append(record)
    records = retained
    records.sort(key=lambda row: (row["product"].casefold(), row["name"].casefold(), row["presetId"]))
    vectors = [_vector(_search_text(record)) for record in records]
    payload = {
        "schemaVersion": 1,
        "builtAt": _now(),
        "readOnly": True,
        "embedding": {"model": "local-hashed-audio-semantics-v1", "dimensions": VECTOR_DIMENSIONS, "cloudCalls": False},
        "inspection": inspection,
        "duplicateRecordsCollapsed": duplicate_count,
        "recordCount": len(records),
        "records": records,
        "vectors": vectors,
        "errors": errors,
    }
    destination = Path(destination)
    Store(destination.parent).write(destination, payload)
    log_event("preset-index-complete", records=len(records), errors=len(errors), destination=str(destination))
    return payload


def load_index(path: Path = INDEX_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Preset index is unavailable or unreadable.") from exc
    if payload.get("schemaVersion") != 1 or not payload.get("readOnly") or len(payload.get("records", [])) != len(payload.get("vectors", [])):
        raise ValueError("Preset index is invalid or inconsistent.")
    return payload


def search(query: str, limit: int = 12, product: str | None = None, index: dict[str, Any] | None = None) -> dict[str, Any]:
    terms = _expanded_tokens(query)
    if not terms:
        raise ValueError("Enter a sound description to search presets.")
    index = index or load_index()
    query_vector = _vector(" ".join(terms))
    matches = []
    for record, vector in zip(index["records"], index["vectors"]):
        if product and record["product"].casefold() != product.casefold():
            continue
        score = sum(value * vector.get(bucket, 0.0) for bucket, value in query_vector.items())
        exact = len(set(_tokens(query)) & set(_tokens(_search_text(record))))
        ranking = score + exact * 0.08
        if ranking > 0:
            matches.append({"score": round(ranking, 8), **{k: record[k] for k in (
                "presetId", "product", "name", "categories", "tags", "description", "author",
                "sourcePath", "format", "parameterStateStatus", "loadVerified", "metadataBasis")}})
    matches.sort(key=lambda row: (row["score"], row["name"].casefold()), reverse=True)
    bounded = max(1, min(int(limit), 50))
    return {
        "query": query,
        "productFilter": product,
        "matches": matches[:bounded],
        "totalMatches": len(matches),
        "catalogueCount": index["recordCount"],
        "readOnly": True,
        "warning": "Metadata match only. Sound and preset loading are not verified.",
    }


if __name__ == "__main__":
    result = build_index()
    print(json.dumps({"records": result["recordCount"], "errors": len(result["errors"]), "formats": result["inspection"]["formats"]}, indent=2))
