"""Bounded extraction of ordered raw EBML tag elements from WebM/Matroska."""

from __future__ import annotations

import base64
import io
from typing import BinaryIO

DEFAULT_MAX_METADATA_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_CHUNK_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_ELEMENTS = 4096
DEFAULT_MAX_DEPTH = 16
_SEGMENT, _TAGS, _TAG, _SIMPLE_TAG = 0x18538067, 0x1254C367, 0x7373, 0x67C8
_TARGETS = 0x63C0
_TAG_NAME, _TAG_STRING, _TAG_BINARY = 0x45A3, 0x4487, 0x4485
_MASTER_NAMES = {_SEGMENT: "Segment", _TAGS: "Tags", _TAG: "Tag",
                 _SIMPLE_TAG: "SimpleTag", _TARGETS: "Targets"}


def _vint(stream: BinaryIO, *, identifier: bool = False) -> tuple[int, int, bool] | None:
    first = stream.read(1)
    if not first:
        return None
    b = first[0]
    mask, width = 0x80, 1
    while width <= 8 and not (b & mask):
        mask >>= 1
        width += 1
    if width > (4 if identifier else 8):
        raise ValueError("invalid EBML VINT")
    rest = stream.read(width - 1)
    if len(rest) != width - 1:
        raise EOFError("truncated EBML VINT")
    value = b if identifier else b & (mask - 1)
    for byte in rest:
        value = (value << 8) | byte
    unknown = not identifier and value == (1 << (7 * width)) - 1
    return value, width, unknown


def read_ebml_metadata(
    source: bytes | bytearray | memoryview | BinaryIO,
    *, max_metadata_bytes: int = DEFAULT_MAX_METADATA_BYTES,
    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES,
    max_elements: int = DEFAULT_MAX_ELEMENTS,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> dict:
    """Capture exact leaf payloads under EBML Segment/Tags/Tag/SimpleTag.

    Element payloads are never retained beyond the byte budgets. Unknown-size
    Segment is treated as extending to EOF; unknown-size nested elements are
    reported as unsupported because their boundary cannot be inferred safely.
    """
    limits = {"max_metadata_bytes": max_metadata_bytes, "max_chunk_bytes": max_chunk_bytes,
              "max_elements": max_elements, "max_depth": max_depth}
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in limits.values()):
        raise ValueError("EBML limits must be non-negative integers")
    stream = io.BytesIO(bytes(source)) if isinstance(source, (bytes, bytearray, memoryview)) else source
    if not hasattr(stream, "read"):
        raise TypeError("source must be EBML bytes or a binary stream")
    out = {"status": "error", "complete": False, "coverage": "none", "elements": [],
           "text_candidates": [], "elements_scanned": 0, "metadata_bytes_captured": 0,
           "errors": [], "limits": limits}
    try:
        # Seekable inputs permit exact offsets and cheap skipping of large media payloads.
        if not hasattr(stream, "tell"):
            raise ValueError("source must support tell/seek for bounded EBML traversal")
        start = stream.tell()
        stream.seek(0, 2)
        file_end = stream.tell()
        stream.seek(start)
        if stream.read(4) != b"\x1a\x45\xdf\xa3":
            raise ValueError("invalid EBML header signature")
        stream.seek(start)
        saw_segment = False
        stopped = False

        def scan(parent_end: int, parent_path: list[str], depth: int, allow_unknown_segment: bool = False) -> None:
            nonlocal stopped, saw_segment
            tag_names: list[str | None] = []
            scoped_candidates: list[dict] = []
            if depth > max_depth:
                out["errors"].append("maximum EBML nesting depth exceeded")
                stopped = True
                return
            while stream.tell() < parent_end and not stopped:
                offset = stream.tell()
                if out["elements_scanned"] >= max_elements:
                    out["errors"].append("maximum EBML element count exceeded")
                    out["status"] = "too_large"
                    stopped = True
                    return
                try:
                    id_v = _vint(stream, identifier=True)
                    if id_v is None:
                        if offset < parent_end:
                            out["errors"].append("truncated EBML element identifier")
                            stopped = True
                        return
                    element_id, id_len, _ = id_v
                    size_v = _vint(stream)
                    if size_v is None:
                        raise EOFError("truncated EBML element size")
                    size, size_len, unknown = size_v
                except (ValueError, EOFError) as exc:
                    out["errors"].append(str(exc))
                    stopped = True
                    return
                payload_at = stream.tell()
                out["elements_scanned"] += 1
                name = _MASTER_NAMES.get(element_id)
                is_segment = element_id == _SEGMENT
                header_end = stream.tell()
                stream.seek(offset)
                raw_header = stream.read(header_end - offset)
                stream.seek(header_end)
                if unknown and not (is_segment and allow_unknown_segment):
                    out["errors"].append(f"unknown-size element 0x{element_id:X} has no safely inferable boundary")
                    stopped = True
                    return
                end = file_end if unknown else payload_at + size
                if end > parent_end or end > file_end:
                    out["errors"].append(f"element 0x{element_id:X} exceeds parent or file boundary")
                    stopped = True
                    return
                if is_segment:
                    saw_segment = True
                    out["elements"].append({"offset": offset, "payload_offset": payload_at,
                        "id": element_id, "id_hex": f"0x{element_id:X}",
                        "path": parent_path + ["Segment"], "declared_size": None if unknown else size,
                        "unknown_size": unknown, "raw_header_b64": base64.b64encode(raw_header).decode("ascii"),
                        "raw_payload_b64": None, "utf8": None, "decode_error": None})
                    scan(end, parent_path + ["Segment"], depth + 1)
                    if unknown:
                        return
                elif element_id in (_TAGS, _TAG, _SIMPLE_TAG, _TARGETS):
                    out["elements"].append({"offset": offset, "payload_offset": payload_at,
                        "id": element_id, "id_hex": f"0x{element_id:X}",
                        "path": parent_path + [name], "declared_size": size,
                        "unknown_size": False, "raw_header_b64": base64.b64encode(raw_header).decode("ascii"),
                        "raw_payload_b64": None, "utf8": None, "decode_error": None})
                    scan(end, parent_path + [name], depth + 1)
                elif "Tags" in parent_path and not unknown:
                    record = {"offset": offset, "payload_offset": payload_at, "id": element_id,
                              "id_hex": f"0x{element_id:X}", "path": parent_path + [f"0x{element_id:X}"],
                              "declared_size": size, "unknown_size": False,
                              "raw_header_b64": base64.b64encode(raw_header).decode("ascii"),
                              "raw_payload_b64": None,
                              "utf8": None, "decode_error": None, "capture_error": None}
                    out["elements"].append(record)
                    if size > max_chunk_bytes:
                        out["errors"].append(f"element 0x{element_id:X} exceeds max_chunk_bytes")
                        out["status"] = "partial"
                        record["capture_error"] = "element exceeds max_chunk_bytes"
                    elif size > max_metadata_bytes - out["metadata_bytes_captured"]:
                        out["errors"].append("metadata byte budget exceeded")
                        out["status"] = "partial"
                        record["capture_error"] = "metadata byte budget exceeded"
                    else:
                        raw = stream.read(size)
                        if len(raw) != size:
                            out["errors"].append(f"truncated element 0x{element_id:X} payload")
                            stopped = True
                            return
                        record["raw_payload_b64"] = base64.b64encode(raw).decode("ascii")
                        out["metadata_bytes_captured"] += size
                        if element_id in (_TAG_NAME, _TAG_STRING):
                            try:
                                record["utf8"] = raw.decode("utf-8", errors="strict")
                            except UnicodeDecodeError as exc:
                                record["decode_error"] = f"invalid UTF-8 at byte {exc.start}"
                            if element_id == _TAG_NAME and parent_path[-1] == "SimpleTag":
                                tag_names.append(record["utf8"])
                            if element_id == _TAG_STRING and parent_path[-1] == "SimpleTag":
                                scoped_candidates.append({"text": record["utf8"], "tag_name": None,
                                    "key": None, "offset": offset, "payload_offset": payload_at,
                                    "path": list(record["path"]), "source_id": element_id,
                                    "status": "malformed",
                                    "capture_error": record["capture_error"] or record["decode_error"]})
                    if (element_id == _TAG_STRING and parent_path[-1] == "SimpleTag"
                            and record["raw_payload_b64"] is None):
                        scoped_candidates.append({"text": None, "tag_name": None, "key": None,
                            "offset": offset, "payload_offset": payload_at,
                            "path": list(record["path"]), "source_id": element_id,
                            "status": "malformed", "capture_error": record["capture_error"]})
                    if stream.tell() < end:
                        stream.seek(end)
                else:
                    stream.seek(end)
                if stream.tell() != end:
                    if stream.tell() > end:
                        out["errors"].append("element parser crossed declared boundary")
                        stopped = True
                        return
                    stream.seek(end)
            if parent_path and parent_path[-1] == "SimpleTag":
                name_is_unambiguous = len(tag_names) == 1 and tag_names[0] is not None
                for candidate in scoped_candidates:
                    candidate["tag_name"] = tag_names[0] if name_is_unambiguous else None
                    candidate["key"] = candidate["tag_name"]
                    candidate["source"] = {"container_type": "ebml",
                        "element_offset": candidate["offset"],
                        "element_path": list(candidate["path"]),
                        "payload_offset": candidate["payload_offset"],
                        "element_id": candidate["source_id"]}
                    if candidate["text"] is not None and name_is_unambiguous:
                        candidate["status"] = "ok"
                    elif candidate["capture_error"] is None:
                        candidate["capture_error"] = (
                            "TagName is missing or ambiguous" if not name_is_unambiguous
                            else "TagString is not valid UTF-8")
                out["text_candidates"].extend(scoped_candidates)

        scan(file_end, [], 0, allow_unknown_segment=True)
        if not saw_segment:
            out["errors"].append("EBML Segment element not found")
        out["complete"] = not stopped and not out["errors"] and saw_segment
        out["coverage"] = "complete" if out["complete"] else ("partial" if out["elements"] else "none")
        if out["complete"]:
            has_payload = any(row["id"] not in _MASTER_NAMES for row in out["elements"])
            out["status"] = "complete" if has_payload else "no_metadata"
        elif out["status"] == "error":
            out["status"] = "partial" if out["elements"] or saw_segment else "error"
    except Exception as exc:
        out["errors"].append(str(exc))
        out["status"] = "error"
    return out
