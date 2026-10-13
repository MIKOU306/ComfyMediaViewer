"""Bounded raw MP4/QuickTime metadata box capture."""
from __future__ import annotations

import base64
import io
import struct
from typing import BinaryIO

DEFAULT_MAX_METADATA_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_CHUNK_BYTES = 1024 * 1024
DEFAULT_MAX_BOXES = 4096
DEFAULT_MAX_DEPTH = 16
SUPPORTED_BRANDS = {b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6",
                    b"mp41", b"mp42", b"avc1", b"M4V ", b"qt  ",
                    b"3gp4", b"3gp5", b"3gp6"}


class _Malformed(ValueError):
    pass


class _Limit(ValueError):
    pass


def read_mp4_metadata(source: bytes | bytearray | memoryview | BinaryIO, *,
                      max_metadata_bytes: int = DEFAULT_MAX_METADATA_BYTES,
                      max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES,
                      max_boxes: int = DEFAULT_MAX_BOXES,
                      max_depth: int = DEFAULT_MAX_DEPTH) -> dict:
    """Capture exact metadata atoms and separately derive sourced text values.

    ``source`` must be seekable: media payload boxes are skipped by offset, so
    large ``mdat`` bodies are never loaded into memory by this collector.
    """
    for name, value, minimum in (("max_metadata_bytes", max_metadata_bytes, 0),
                                 ("max_chunk_bytes", max_chunk_bytes, 0),
                                 ("max_boxes", max_boxes, 1),
                                 ("max_depth", max_depth, 1)):
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} must be a non-negative integer" if minimum == 0
                             else f"{name} must be a positive integer")
    stream = io.BytesIO(bytes(source)) if isinstance(source, (bytes, bytearray, memoryview)) else source
    if not hasattr(stream, "read") or not hasattr(stream, "seek"):
        raise TypeError("source must be MP4 bytes or a seekable binary stream")
    try:
        if not stream.seekable():
            raise TypeError("source must be a seekable binary stream")
        original_pos = stream.tell()
        stream.seek(0, io.SEEK_END)
        file_size = stream.tell()
        stream.seek(original_pos)
    except (AttributeError, OSError, io.UnsupportedOperation) as exc:
        raise TypeError("source must be a seekable binary stream") from exc

    result = {"status": "error", "complete": False, "coverage": "none",
              "container_type": None, "records": [], "text_candidates": [],
              "boxes_scanned": 0, "metadata_bytes_captured": 0, "errors": [],
              "limits": {"max_metadata_bytes": max_metadata_bytes,
                         "max_chunk_bytes": max_chunk_bytes,
                         "max_boxes": max_boxes, "max_depth": max_depth}}
    raw_records: dict[int, dict] = {}
    meta_handlers: dict[int, str | None] = {}
    key_tables: dict[int, dict[int, bytes]] = {}
    key_offsets: dict[int, int] = {}
    boxes_scanned = 0

    def fail(message: str, status: str = "error") -> dict:
        result["errors"].append(message)
        result["status"] = status
        result["coverage"] = "partial"
        return result

    def read_exact(count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            part = stream.read(count - len(data))
            if not part:
                raise _Malformed("truncated MP4 box")
            data.extend(part)
        return bytes(data)

    def read_header(parent_end: int, parent_path: tuple[str, ...], *, count_box: bool = True) -> dict | None:
        nonlocal boxes_scanned
        offset = stream.tell()
        if offset == parent_end:
            return None
        if offset > parent_end or parent_end - offset < 8:
            raise _Malformed(f"truncated box header at offset {offset}")
        size32, kind = struct.unpack(">I4s", read_exact(8))
        header_size = 8
        if size32 == 1:
            if parent_end - stream.tell() < 8:
                raise _Malformed(f"truncated extended size at offset {offset}")
            size = struct.unpack(">Q", read_exact(8))[0]
            header_size = 16
        elif size32 == 0:
            size = parent_end - offset
        else:
            size = size32
        if size < header_size:
            raise _Malformed(f"invalid {kind.decode('latin1')} box length at offset {offset}")
        end = offset + size
        if end > parent_end or end > file_size:
            raise _Malformed(f"{kind.decode('latin1')} box exceeds parent bounds at offset {offset}")
        if count_box:
            boxes_scanned += 1
            result["boxes_scanned"] = boxes_scanned
            if boxes_scanned > max_boxes:
                raise _Limit("MP4 box count exceeds max_boxes")
        name = kind.decode("latin1")
        return {"offset": offset, "kind": kind, "size": size,
                "header_size": header_size, "payload_start": offset + header_size,
                "end": end, "path": parent_path + (name,)}

    def children(start: int, end: int, parent_path: tuple[str, ...], *, count_box: bool = True):
        stream.seek(start)
        while stream.tell() < end:
            box = read_header(end, parent_path, count_box=count_box)
            if box is None:
                break
            yield box
            stream.seek(box["end"])

    def read_range(offset: int, count: int) -> bytes:
        stream.seek(offset)
        return read_exact(count)

    def capture(box: dict) -> dict:
        size = box["size"]
        record = {"offset": box["offset"], "type": box["kind"].decode("latin1"),
                  "declared_length": size, "box_path": list(box["path"]),
                  "raw_box_b64": None, "raw_payload_b64": None,
                  "capture_status": "unavailable", "capture_error": None}
        if size > max_chunk_bytes:
            record["capture_error"] = "box exceeds max_chunk_bytes"
            raw_records.setdefault(box["offset"], record)
            raise _Limit(f"{record['type']} box exceeds max_chunk_bytes")
        if result["metadata_bytes_captured"] + size > max_metadata_bytes:
            record["capture_error"] = "metadata byte budget exceeded"
            raw_records.setdefault(box["offset"], record)
            raise _Limit("MP4 metadata byte budget exceeded")
        raw = read_range(box["offset"], size)
        payload = raw[box["header_size"]:]
        record.update(raw_box_b64=base64.b64encode(raw).decode("ascii"),
                      raw_payload_b64=base64.b64encode(payload).decode("ascii"),
                      capture_status="captured")
        raw_records[box["offset"]] = record
        result["metadata_bytes_captured"] += size
        return record

    def strict_text(raw: bytes, label: str, offset: int) -> tuple[str | None, str | None]:
        try:
            return raw.decode("utf-8", "strict"), None
        except UnicodeDecodeError as exc:
            message = f"{label} at offset {offset} is not valid UTF-8: {exc}"
            result["errors"].append(message)
            return None, message

    def memory_boxes(data: bytes, start: int, end: int, absolute: int,
                     path: tuple[str, ...], depth: int):
        nonlocal boxes_scanned
        if depth > max_depth:
            raise _Limit("MP4 metadata nesting exceeds max_depth")
        pos = start
        while pos < end:
            if end - pos < 8:
                raise _Malformed(f"truncated nested box header at offset {absolute + pos}")
            size32, kind = struct.unpack_from(">I4s", data, pos)
            head = 8
            if size32 == 1:
                if end - pos < 16:
                    raise _Malformed(f"truncated extended nested size at offset {absolute + pos}")
                size = struct.unpack_from(">Q", data, pos + 8)[0]
                head = 16
            elif size32 == 0:
                size = end - pos
            else:
                size = size32
            if size < head or pos + size > end:
                raise _Malformed(f"nested {kind.decode('latin1')} box exceeds parent at {absolute + pos}")
            boxes_scanned += 1
            result["boxes_scanned"] = boxes_scanned
            if boxes_scanned > max_boxes:
                raise _Limit("MP4 box count exceeds max_boxes")
            yield {"offset": absolute + pos, "kind": kind, "size": size,
                   "header_size": head, "end": pos + size, "memory_pos": pos,
                   "path": path + (kind.decode("latin1"),)}
            pos += size

    def parse_keys(box: dict, meta_offset: int, payload: bytes) -> None:
        if len(payload) < 8:
            raise _Malformed(f"truncated keys box at offset {box['offset']}")
        count = struct.unpack_from(">I", payload, 4)[0]
        pos = 8
        table: dict[int, bytes] = {}
        for index in range(1, count + 1):
            if pos + 8 > len(payload):
                raise _Malformed(f"truncated keys entry {index} at {box['offset']}")
            entry_size = struct.unpack_from(">I", payload, pos)[0]
            if entry_size < 8 or pos + entry_size > len(payload):
                raise _Malformed(f"invalid keys entry {index} at {box['offset']}")
            namespace = payload[pos + 4:pos + 8]
            key_bytes = payload[pos + 8:pos + entry_size]
            if namespace != b"mdta":
                result["errors"].append(f"keys entry {index} uses unsupported namespace {namespace!r}")
            table[index] = key_bytes
            strict_text(key_bytes, f"mdta key {index}", box["offset"])
            pos += entry_size
        if pos != len(payload):
            raise _Malformed(f"trailing bytes in keys box at offset {box['offset']}")
        key_tables[meta_offset] = table
        key_offsets[meta_offset] = box["offset"]

    def parse_handler(box: dict, meta_offset: int, payload: bytes) -> None:
        if len(payload) < 12:
            raise _Malformed(f"truncated hdlr box at offset {box['offset']}")
        handler = payload[8:12].decode("latin1")
        if handler not in ("mdta", "mdir"):
            result["errors"].append(f"unsupported metadata handler {handler!r} at offset {box['offset']}")
            meta_handlers[meta_offset] = None
        else:
            meta_handlers[meta_offset] = handler

    def add_candidate(item: dict, item_raw: bytes, data_box: dict, meta_offset: int,
                      key: str | None, key_bytes: bytes | None, key_index: int | None,
                      key_error: str | None, *, freeform: dict | None = None) -> None:
        start = data_box["memory_pos"] + data_box["header_size"]
        end = data_box["end"]
        payload = item_raw[start:end]
        handler = meta_handlers.get(meta_offset)
        source_info = {"container_type": result["container_type"], "handler": handler,
                       "item_offset": item["offset"], "data_offset": data_box["offset"],
                       "item_type_bytes_b64": base64.b64encode(item["kind"]).decode("ascii"),
                       "item_path": list(item["path"]), "data_path": list(data_box["path"])}
        if key_index is not None:
            source_info["key_index"] = key_index
            source_info["keys_box_offset"] = key_offsets.get(meta_offset)
        if freeform:
            source_info.update(freeform)
        candidate = {"key": key, "text": None, "source": source_info,
                     "status": "unsupported", "capture_error": key_error,
                     "raw_value_b64": None, "data_type": None, "locale": None}
        if len(payload) < 8:
            candidate.update(status="malformed", capture_error=f"truncated data atom at {data_box['offset']}")
            result["errors"].append(candidate["capture_error"])
        else:
            data_type, locale = struct.unpack_from(">II", payload)
            raw_value = payload[8:]
            candidate.update(raw_value_b64=base64.b64encode(raw_value).decode("ascii"),
                             data_type=data_type, locale=locale)
            if key_error:
                candidate["status"] = "malformed"
            elif data_type != 1:
                candidate["capture_error"] = f"data type {data_type} is not UTF-8 text"
            else:
                text, error = strict_text(raw_value, "metadata text", data_box["offset"])
                candidate.update(text=text, status="malformed" if error else "ok",
                                 capture_error=error)
        result["text_candidates"].append(candidate)

    def scan_ilst(ilst: dict, meta_offset: int, depth: int) -> None:
        if depth > max_depth:
            raise _Limit("MP4 metadata nesting exceeds max_depth")
        table = key_tables.get(meta_offset, {})
        handler = meta_handlers.get(meta_offset)
        for item in children(ilst["payload_start"], ilst["end"], ilst["path"]):
            record = capture(item)  # Complete item retains nested data/mean/name bytes.
            item_raw = base64.b64decode(record["raw_box_b64"])
            item_type = item["kind"]
            if handler not in ("mdta", "mdir"):
                continue  # Preserve the atom, but never infer its tag semantics.
            index = struct.unpack(">I", item_type)[0]
            key_bytes = table.get(index) if handler == "mdta" else None
            key_error = None
            if handler == "mdta":
                if key_bytes is None:
                    key_error = f"mdta item index {index} has no keys entry"
                    result["errors"].append(f"{key_error} at offset {item['offset']}")
                    key = None
                else:
                    key, key_error = strict_text(key_bytes, f"mdta key {index}", item["offset"])
            elif handler == "mdir":
                key = item_type.decode("latin1")
            else:
                key = None
            nested = list(memory_boxes(item_raw, item["header_size"], len(item_raw),
                                       item["offset"], item["path"], depth + 1))
            if handler == "mdir" and item_type == b"----":
                scan_freeform(item, item_raw, nested, meta_offset, key_error)
            else:
                for data_box in nested:
                    if data_box["kind"] == b"data":
                        add_candidate(item, item_raw, data_box, meta_offset,
                                      key, key_bytes, index if handler == "mdta" else None,
                                      key_error)

    def scan_freeform(item: dict, item_raw: bytes, nested: list[dict],
                      meta_offset: int, prior_error: str | None) -> None:
        texts: dict[str, tuple[str | None, bytes, str | None]] = {}
        data_boxes = []
        for box in nested:
            if box["kind"] in (b"mean", b"name"):
                payload = item_raw[box["memory_pos"] + box["header_size"]:box["end"]]
                if len(payload) < 4:
                    error = f"truncated freeform {box['kind'].decode()} FullBox at {box['offset']}"
                    result["errors"].append(error)
                    texts[box["kind"].decode()] = (None, b"", error)
                else:
                    raw = payload[4:]
                    value, error = strict_text(raw, f"freeform {box['kind'].decode()}", box["offset"])
                    texts[box["kind"].decode()] = (value, raw, error)
            elif box["kind"] == b"data":
                data_boxes.append(box)
        mean, mean_bytes, mean_error = texts.get("mean", (None, b"", None))
        name, name_bytes, name_error = texts.get("name", (None, b"", None))
        key = "----" + (f":{mean}" if mean is not None else "") + (f":{name}" if name is not None else "")
        provenance = {"freeform_mean": mean, "freeform_name": name,
                      "freeform_mean_bytes_b64": base64.b64encode(mean_bytes).decode("ascii"),
                      "freeform_name_bytes_b64": base64.b64encode(name_bytes).decode("ascii")}
        for data_box in data_boxes:
            add_candidate(item, item_raw, data_box, meta_offset, key, None, None,
                          prior_error or mean_error or name_error, freeform=provenance)

    def scan_meta(meta: dict, depth: int) -> None:
        if depth > max_depth:
            raise _Limit("MP4 metadata nesting exceeds max_depth")
        # Retain the enclosing material, including unknown children and flags.
        capture(meta)
        # ISO BMFF meta is a FullBox; QuickTime variants can omit version/flags.
        parsed_options = []
        for skip in (4, 0):
            child_start = meta["payload_start"] + skip
            if child_start > meta["end"]:
                continue
            try:
                parsed = list(children(child_start, meta["end"], meta["path"], count_box=False))
            except _Malformed:
                continue
            if parsed and any(box["kind"] in (b"hdlr", b"keys", b"ilst") for box in parsed):
                parsed_options.append((skip, parsed))
        if not parsed_options:
            if meta["payload_start"] < meta["end"]:
                result["errors"].append(f"meta children are unrecognized at offset {meta['offset']}")
            return
        skip, _preview = parsed_options[0]
        parsed_children = list(children(meta["payload_start"] + skip, meta["end"],
                                        meta["path"], count_box=True))
        for child in parsed_children:
            if child["kind"] == b"keys":
                rec = capture(child)
                parse_keys(child, meta["offset"], base64.b64decode(rec["raw_payload_b64"]))
            elif child["kind"] == b"hdlr":
                rec = capture(child)
                parse_handler(child, meta["offset"], base64.b64decode(rec["raw_payload_b64"]))
            elif child["kind"] == b"ilst":
                scan_ilst(child, meta["offset"], depth + 1)

    def scan_udta(udta: dict, depth: int) -> None:
        if depth > max_depth:
            raise _Limit("MP4 metadata nesting exceeds max_depth")
        for child in children(udta["payload_start"], udta["end"], udta["path"]):
            if child["kind"] == b"meta":
                scan_meta(child, depth + 1)
            else:
                capture(child)

    def scan_moov(moov: dict) -> None:
        if 1 > max_depth:
            raise _Limit("MP4 metadata nesting exceeds max_depth")
        for child in children(moov["payload_start"], moov["end"], moov["path"]):
            if child["kind"] == b"udta":
                scan_udta(child, 2)
            elif child["kind"] == b"meta":
                scan_meta(child, 2)

    try:
        if file_size < 8:
            return fail("truncated MP4 file")
        found_ftyp = False
        supported_brand = False
        stream.seek(0)
        while stream.tell() < file_size:
            box = read_header(file_size, ())
            if box is None:
                break
            if box["kind"] == b"ftyp":
                found_ftyp = True
                payload_length = box["size"] - box["header_size"]
                if payload_length < 8:
                    raise _Malformed("truncated ftyp box")
                payload = read_range(box["payload_start"], min(payload_length, 4096))
                brands = [payload[:4]] + [payload[pos:pos + 4] for pos in range(8, len(payload) - 3, 4)]
                supported_brand = any(brand in SUPPORTED_BRANDS for brand in brands)
                result["container_type"] = "quicktime" if b"qt  " in brands else "iso_bmff"
            elif box["kind"] == b"moov":
                scan_moov(box)
            stream.seek(box["end"])

        result["records"] = sorted(raw_records.values(), key=lambda row: row["offset"])
        if not found_ftyp:
            return fail("MP4/QuickTime ftyp box is absent", "unsupported")
        if not supported_brand:
            result["status"] = "unsupported"
            result["coverage"] = "complete"
            result["errors"].append("ftyp brands are outside the supported MP4/QuickTime set")
        elif result["errors"]:
            result["status"] = "partial"
            result["coverage"] = "partial"
        else:
            result["status"] = "complete" if result["records"] else "no_metadata"
            result["coverage"] = "complete"
            result["complete"] = True
        return result
    except _Limit as exc:
        result["records"] = sorted(raw_records.values(), key=lambda row: row["offset"])
        return fail(str(exc), "too_large")
    except (_Malformed, OSError, struct.error) as exc:
        result["records"] = sorted(raw_records.values(), key=lambda row: row["offset"])
        return fail(str(exc))


__all__ = ["read_mp4_metadata"]
