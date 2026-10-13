"""Extract explicitly named Workflow fields from archived JPEG/WebP XMP.

The XMP carrier bytes and XML-decoded field text are separate evidence. The
candidate text bytes below are UTF-8 encodings of parsed XML values, never a
claim that the carrier bytes were preserved as that text.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from collections import Counter
from collections.abc import Mapping
from typing import Any

from .jpeg_semantics import XMP_ID, parse_jpeg_app1

MAX_XMP_CANDIDATES = 256
MAX_XMP_CARRIER_BYTES = 8 * 1024 * 1024
_TARGET_FIELDS = frozenset(("workflow", "prompt"))  # exact, case-sensitive XML local names


class WorkflowXmpSourceError(ValueError):
    """The archived container snapshot cannot safely support XMP extraction."""


def _decode_carrier(value: Any, remaining: int) -> bytes:
    if not isinstance(value, str):
        raise WorkflowXmpSourceError("XMP carrier bytes were not retained")
    if len(value) > ((remaining + 2) // 3) * 4 + 4:
        raise WorkflowXmpSourceError("XMP carrier byte budget exceeded")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WorkflowXmpSourceError("XMP carrier base64 is malformed") from exc
    if base64.b64encode(raw).decode("ascii") != value:
        raise WorkflowXmpSourceError("XMP carrier base64 is not canonical")
    if len(raw) > remaining:
        raise WorkflowXmpSourceError("XMP carrier byte budget exceeded")
    return raw


def _webp_semantics(value: dict[str, Any]) -> dict[str, Any]:
    """Match raw_webp._webp_semantics without importing its private helper."""
    result = {**value, "source": dict(value.get("source") or {})}
    source = result["source"]
    if isinstance(source, dict):
        source["chunk_offset"] = source.pop("app1_offset", None)
        source.pop("marker", None)
        source["container"] = "webp"
    return result


def _carriers(snapshot: Mapping[str, Any], result: Mapping[str, Any]):
    fmt = snapshot.get("source_format")
    rows = result.get("segments") if fmt == "jpeg" else result.get("chunks") if fmt == "webp" else None
    if not isinstance(rows, list):
        raise WorkflowXmpSourceError(f"unsupported source format: {fmt!r}")
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        if fmt == "jpeg":
            if row.get("marker") != "FFE1":
                continue
            offset, payload_b64 = row.get("offset"), row.get("raw_payload_b64")
            carrier = {"container": "jpeg", "kind": "app1", "index": index,
                       "offset": offset, "marker": row.get("marker"), "declaredLength": row.get("length")}
        else:
            if row.get("type") != "XMP ":
                continue
            offset, payload_b64 = row.get("offset"), row.get("raw_payload_b64")
            carrier = {"container": "webp", "kind": "xmp_chunk", "index": index,
                       "offset": offset, "chunkType": row.get("type"),
                       "declaredLength": row.get("declared_length")}
        if row.get("capture_error") or payload_b64 is None:
            raise WorkflowXmpSourceError("XMP carrier is unavailable or exceeded collector limits")
        raw = _decode_carrier(payload_b64, MAX_XMP_CARRIER_BYTES)
        if fmt == "webp" and not raw.startswith(XMP_ID):
            # raw_webp adds the standard XMP identifier before semantic parsing;
            # preserve/hash the exact chunk payload separately below.
            parser_payload = XMP_ID + raw
        else:
            parser_payload = raw
        parsed = parse_jpeg_app1(parser_payload, int(offset or 0))
        expected = _webp_semantics(parsed) if fmt == "webp" else parsed
        stored = row.get("semantics")
        if not isinstance(stored, Mapping) or dict(stored) != expected:
            raise WorkflowXmpSourceError("Stored XMP semantics disagree with the archived carrier bytes")
        yield index, carrier, raw, parsed


def collect_xmp_workflow_candidates(snapshot: Mapping[str, Any], *,
                                    max_candidates: int = MAX_XMP_CANDIDATES,
                                    max_text_bytes: int = 8 * 1024 * 1024) -> dict[str, Any]:
    """Return ordered named XMP candidates and carrier-derived evidence.

    Each candidate has the common workflow source fields (kind/text/status/
    reason/text_b64/encoding/source_locator) plus explicit carrier-byte and
    XML-field-byte hashes/provenance. ``text_b64`` contains UTF-8 bytes of the
    XML-decoded field value; the raw XMP carrier remains separately retrievable
    in the metadata snapshot at ``source_locator.carrier``.
    """
    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot must be a mapping")
    if type(max_candidates) is not int or not 1 <= max_candidates <= 1024:
        raise ValueError("max_candidates must be an integer from 1 through 1024")
    if type(max_text_bytes) is not int or max_text_bytes < 0:
        raise ValueError("max_text_bytes must be a non-negative integer")
    fmt = snapshot.get("source_format")
    result = snapshot.get("result")
    if not isinstance(result, Mapping):
        raise WorkflowXmpSourceError("snapshot result is unavailable")
    if (snapshot.get("coverage") != "complete" or snapshot.get("state") != "complete"
            or result.get("coverage") != "complete" or result.get("complete") is not True
            or result.get("full_recovery") is not True):
        raise WorkflowXmpSourceError("snapshot or collector coverage is incomplete")
    if fmt not in ("jpeg", "webp"):
        raise WorkflowXmpSourceError(f"unsupported source format: {fmt!r}")

    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    carrier_bytes_total = 0
    carrier_count = 0
    field_bytes_total = 0
    observed_counts = Counter()
    for index, carrier, raw, parsed in _carriers(snapshot, result):
        carrier_count += 1
        carrier_bytes_total += len(raw)
        if carrier_bytes_total > MAX_XMP_CARRIER_BYTES:
            raise WorkflowXmpSourceError("total XMP carrier byte budget exceeded")
        if parsed.get("status") != "ok":
            errors.append(f"XMP carrier {index}: {parsed.get('error') or parsed.get('status')}")
            continue
        fields = parsed.get("fields")
        if not isinstance(fields, list):
            continue
        for ordinal, field in enumerate(fields):
            if not isinstance(field, Mapping) or field.get("name") not in _TARGET_FIELDS:
                continue
            if field.get("kind") not in ("text", "attribute") or not isinstance(field.get("value"), str):
                continue
            observed_counts[field["name"]] += 1
            if len(candidates) >= max_candidates:
                if "XMP workflow candidate count exceeds limit" not in errors:
                    errors.append("XMP workflow candidate count exceeds limit")
                continue
            value = field["value"]
            if len(value) > max_text_bytes - field_bytes_total:
                errors.append("XMP decoded field text byte budget exceeded")
                continue
            field_bytes = value.encode("utf-8")
            if len(field_bytes) > max_text_bytes - field_bytes_total:
                errors.append("XMP decoded field text byte budget exceeded")
                continue
            field_bytes_total += len(field_bytes)
            carrier_hash = hashlib.sha256(raw).hexdigest()
            field_hash = hashlib.sha256(field_bytes).hexdigest()
            candidate = {
                "kind": field["name"],
                "text": value,
                "status": "ok",
                "reason": None,
                "text_b64": base64.b64encode(field_bytes).decode("ascii"),
                "encoding": "utf-8",
                "source_bytes_sha256": carrier_hash,
                "source_bytes_length": len(raw),
                "source_bytes_provenance": "archived_xmp_carrier_raw_payload",
                "field_bytes_sha256": field_hash,
                "field_bytes_provenance": "xml_decoded_field_utf8",
                "field_namespace": field.get("namespace"),
                "field_name": field["name"],
                "field_kind": field["kind"],
                "field_ordinal": ordinal,
                "source_locator": {
                    "snapshotUuid": snapshot.get("uuid"),
                    "sourceFormat": fmt,
                    "collectorVersion": snapshot.get("collector_version"),
                    "carrier": carrier,
                    "fieldNamespace": field.get("namespace"),
                    "fieldName": field["name"],
                    "fieldKind": field["kind"],
                    "fieldOrdinal": ordinal,
                },
            }
            candidates.append(candidate)
    for candidate in candidates:
        candidate["ambiguous"] = observed_counts[candidate["kind"]] > 1
        if candidate["ambiguous"]:
            candidate["reason"] = "multiple explicitly named XMP fields; select a source explicitly"
    return {
        "snapshot_uuid": snapshot.get("uuid"),
        "source_format": fmt,
        "collector_version": snapshot.get("collector_version"),
        "status": "partial" if errors else ("complete" if candidates else "no_candidates"),
        "coverage": "partial" if errors else "complete",
        "candidate_count": len(candidates),
        "carrier_count": carrier_count,
        "source_bytes_captured": carrier_bytes_total,
        "candidates": candidates,
        "errors": errors,
        "name_matching": "exact case-sensitive XML local names workflow/prompt",
    }
