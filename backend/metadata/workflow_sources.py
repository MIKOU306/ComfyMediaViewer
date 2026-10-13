"""Bounded workflow/prompt candidates extracted from immutable metadata snapshots."""
from __future__ import annotations

import base64
import binascii
import hashlib
from collections import Counter
from collections.abc import Mapping
from typing import Any

DEFAULT_MAX_SOURCE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_SOURCES = 256


def _decode_b64(value: Any, remaining: int) -> bytes | None:
    if not isinstance(value, str):
        return None
    # Reject oversized encodings before allocating a decoded copy.
    if len(value) > ((remaining + 2) // 3) * 4 + 4:
        raise ValueError("source text byte budget exceeded")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("source text base64 is malformed") from exc
    if base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("source text base64 is not canonical")
    if len(raw) > remaining:
        raise ValueError("source text byte budget exceeded")
    return raw


def _locator(snapshot: Mapping[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "snapshotUuid": snapshot.get("uuid"),
        "sourceFormat": snapshot.get("source_format"),
        "collectorVersion": snapshot.get("collector_version"),
        **fields,
    }


def _png_candidates(snapshot: Mapping[str, Any], result: Mapping[str, Any]) -> list[dict[str, Any]]:
    chunks = result.get("text_chunks")
    if not isinstance(chunks, list):
        return []
    selected = []
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, Mapping) or chunk.get("keyword") not in ("workflow", "prompt"):
            continue
        selected.append({
            "kind": chunk["keyword"],
            "text": chunk.get("text") if isinstance(chunk.get("text"), str) else None,
            "status": "ok" if chunk.get("text") is not None and not chunk.get("capture_error")
                      and chunk.get("crc_status") == "valid" else "unavailable",
            "reason": (chunk.get("capture_error") or
                       ("PNG text chunk CRC is not valid" if chunk.get("crc_status") != "valid" else None)),
            "text_b64": chunk.get("text_bytes_b64"),
            "encoding": chunk.get("encoding"),
            "source_locator": _locator(snapshot, kind="png_text_chunk", index=index,
                                       offset=chunk.get("offset"), chunkType=chunk.get("type"),
                                       declaredLength=chunk.get("length"), keyword=chunk.get("keyword"),
                                       crcStatus=chunk.get("crc_status")),
        })
    return selected


def _video_candidates(snapshot: Mapping[str, Any], result: Mapping[str, Any]) -> list[dict[str, Any]]:
    from .video_candidates import generation_candidates, generation_key

    raw = result.get("text_candidates")
    if not isinstance(raw, list):
        return []
    valid = generation_candidates(snapshot)
    used: set[int] = set()
    elements = result.get("elements") if isinstance(result.get("elements"), list) else []
    output = []
    for index, record in enumerate(raw):
        if not isinstance(record, Mapping):
            continue
        kind = generation_key(record)
        if kind not in ("workflow", "prompt"):
            continue
        text = record.get("text") if isinstance(record.get("text"), str) else None
        source = record.get("source") if isinstance(record.get("source"), Mapping) else {}
        matched = False
        for valid_index, candidate in enumerate(valid):
            if valid_index in used:
                continue
            if candidate.get("key") == kind and candidate.get("text") == text and candidate.get("source") == dict(source):
                used.add(valid_index)
                matched = True
                break
        raw_text_b64 = record.get("raw_value_b64")
        encoding = "utf-8"
        if snapshot.get("source_format") == "ebml":
            offset = source.get("element_offset", record.get("offset"))
            match = next((item for item in elements if isinstance(item, Mapping)
                          and item.get("offset") == offset
                          and item.get("id") == record.get("source_id")), None)
            raw_text_b64 = match.get("raw_payload_b64") if match else None
            if match is not None:
                source = {**dict(source), "payloadOffset": match.get("payload_offset"),
                          "declaredLength": match.get("declared_size")}
        output.append({
            "kind": kind,
            "text": text,
            "status": "ok" if matched else "unavailable",
            "reason": None if matched else (record.get("capture_error") or "candidate is invalid or unsupported"),
            "text_b64": raw_text_b64,
            "encoding": encoding,
            "source_locator": _locator(snapshot, kind="container_text_candidate", index=index,
                                       containerSource=dict(source)),
        })
    return output


def extract_workflow_sources(snapshot: Mapping[str, Any], *,
                             max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
                             max_sources: int = DEFAULT_MAX_SOURCES) -> dict[str, Any]:
    """Return ordered, located workflow/prompt candidates without assigning identity.

    A candidate's decoded text is kept distinct from its captured bytes. PNG
    ``text_bytes_b64`` is the collector's decoded text payload (possibly after
    decompression); video candidates use their retained raw text value when the
    container collector supplied one. No text re-encoding is treated as source
    bytes.
    """
    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot must be a metadata snapshot mapping")
    if type(max_source_bytes) is not int or max_source_bytes < 0:
        raise ValueError("max_source_bytes must be a non-negative integer")
    if type(max_sources) is not int or not 1 <= max_sources <= 1024:
        raise ValueError("max_sources must be an integer from 1 through 1024")
    result = snapshot.get("result")
    if not isinstance(result, Mapping):
        result = {}
    source_format = snapshot.get("source_format")
    extraction_errors = []
    if source_format == "png":
        candidates = _png_candidates(snapshot, result)
    elif source_format in ("mp4", "ebml"):
        candidates = _video_candidates(snapshot, result)
    elif source_format in ("jpeg", "webp"):
        from .workflow_sources_xmp import collect_xmp_workflow_candidates, WorkflowXmpSourceError
        try:
            xmp = collect_xmp_workflow_candidates(snapshot, max_candidates=max_sources,
                                                   max_text_bytes=max_source_bytes)
            candidates = xmp["candidates"]
            extraction_errors = xmp["errors"]
        except WorkflowXmpSourceError as error:
            candidates = []
            extraction_errors = [str(error)]
    else:
        candidates = []

    counts = Counter(candidate["kind"] for candidate in candidates)
    used_bytes = 0
    errors: list[str] = list(extraction_errors)
    output: list[dict[str, Any]] = []
    truncated_sources = len(candidates) > max_sources
    for index, candidate in enumerate(candidates[:max_sources]):
        kind = candidate["kind"]
        item: dict[str, Any] = {
            "kind": kind,
            "status": "unavailable",
            "ambiguous": counts[kind] > 1 or bool(candidate.get("ambiguous")),
            "text": None,
            "reason": None,
            "source_locator": candidate["source_locator"],
            "text_encoding": candidate.get("encoding"),
            "text_bytes_sha256": None,
            "text_bytes_provenance": None,
        }
        raw = None
        derived_xmp = candidate.get("field_bytes_provenance") == "xml_decoded_field_utf8"
        if derived_xmp:
            for field in ("source_bytes_sha256", "source_bytes_length", "source_bytes_provenance",
                          "field_bytes_sha256", "field_bytes_provenance", "field_namespace",
                          "field_name", "field_kind", "field_ordinal"):
                item[field] = candidate.get(field)
        if isinstance(candidate.get("text_b64"), str):
            try:
                raw = _decode_b64(candidate["text_b64"], max_source_bytes - used_bytes)
                if raw is not None:
                    used_bytes += len(raw)
                    item["text_bytes_sha256"] = None if derived_xmp else hashlib.sha256(raw).hexdigest()
                    item["text_bytes_provenance"] = ("xml_decoded_field_utf8" if derived_xmp else
                        "collector_decoded_text_payload" if source_format == "png" else "container_text_value")
            except ValueError as exc:
                item["reason"] = str(exc)
        if item["reason"] is None:
            if candidate["status"] != "ok" or not isinstance(candidate.get("text"), str):
                item["reason"] = candidate.get("reason") or "candidate has no valid decoded text"
            elif raw is None:
                item["reason"] = "original text bytes were not retained by this collector"
            else:
                encoding = candidate.get("encoding") or "utf-8"
                try:
                    decoded = raw.decode(encoding, errors="strict")
                except (LookupError, UnicodeDecodeError) as exc:
                    item["reason"] = f"retained text bytes do not decode strictly as {encoding}: {exc}"
                else:
                    if decoded != candidate["text"]:
                        item["reason"] = "retained text bytes disagree with decoded candidate text"
                    else:
                        item.update(status="available", text=decoded)
        if item["ambiguous"] and item["status"] == "available":
            item["reason"] = "multiple candidates for this metadata key; select a source explicitly"
        if item["reason"] and not (item["ambiguous"] and item["status"] == "available"):
            errors.append(f"{kind} source {index}: {item['reason']}")
        output.append(item)

    if truncated_sources:
        errors.append(f"source count exceeds limit {max_sources}")
    # A missing supported-key candidate is not evidence that metadata is absent.
    if source_format not in ("png", "mp4", "ebml", "jpeg", "webp"):
        errors.append(f"unsupported source format: {source_format!r}")
    snapshot_complete = (snapshot.get("coverage") == "complete"
                         and snapshot.get("state") in ("complete", "no_metadata")
                         and result.get("coverage") == "complete"
                         and result.get("status") in ("complete", "no_metadata"))
    if not snapshot_complete:
        errors.append("snapshot or collector coverage is incomplete or unknown")
    coverage = "partial" if errors or truncated_sources else "complete"
    return {
        "snapshot_uuid": snapshot.get("uuid"),
        "source_format": source_format,
        "collector_version": snapshot.get("collector_version"),
        "status": "partial" if coverage == "partial" else ("complete" if output else "no_candidates"),
        "coverage": coverage,
        "source_count": len(output),
        "candidate_count": len(candidates),
        "source_bytes_decoded": used_bytes,
        "sources": output,
        "errors": errors,
    }
