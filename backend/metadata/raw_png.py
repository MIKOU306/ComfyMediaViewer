"""Bounded, streaming collection of PNG textual chunks.

The collector accepts bytes or an already-open binary stream. It never opens a
path, writes to storage, interprets application metadata, or combines duplicate
keywords. Original chunk payloads are base64 encoded while within the budgets.
"""

from __future__ import annotations

import base64
import io
import struct
import zlib
from typing import BinaryIO

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
TEXT_TYPES = {b"tEXt", b"zTXt", b"iTXt"}
DEFAULT_MAX_SINGLE_TEXT_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_TOTAL_TEXT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_DECOMPRESSED_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_SINGLE_EXIF_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_TOTAL_EXIF_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_CHUNK_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_CHUNKS = 4096
READ_BLOCK_BYTES = 64 * 1024


def read_png_text(
    source: bytes | bytearray | memoryview | BinaryIO,
    *,
    max_single_text_bytes: int = DEFAULT_MAX_SINGLE_TEXT_BYTES,
    max_total_text_bytes: int = DEFAULT_MAX_TOTAL_TEXT_BYTES,
    max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES,
    max_single_exif_bytes: int = DEFAULT_MAX_SINGLE_EXIF_BYTES,
    max_total_exif_bytes: int = DEFAULT_MAX_TOTAL_EXIF_BYTES,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> dict:
    """Collect ordered PNG text chunks and eXIf payloads from a byte source.

    ``raw_payload_b64`` is the exact PNG chunk data. Compressed text also gets
    ``text_bytes_b64`` after bounded decompression. eXIf payloads are retained
    separately and parsed through the shared EXIF UserComment reader. Budgets apply to PNG chunk
    data bytes and total decompressed text bytes. Non-text chunk payloads,
    including IDAT, are streamed past without retention.
    """
    limits = {
        "max_single_text_bytes": max_single_text_bytes,
        "max_total_text_bytes": max_total_text_bytes,
        "max_decompressed_bytes": max_decompressed_bytes,
        "max_chunk_bytes": max_chunk_bytes,
        "max_single_exif_bytes": max_single_exif_bytes,
        "max_total_exif_bytes": max_total_exif_bytes,
        "max_chunks": max_chunks,
    }
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for name, value in limits.items() if name != "max_chunks") or \
            isinstance(max_chunks, bool) or not isinstance(max_chunks, int) or max_chunks < 1:
        raise ValueError("PNG byte budgets must be non-negative integers")
    stream = io.BytesIO(bytes(source)) if isinstance(source, (bytes, bytearray, memoryview)) else source
    if not hasattr(stream, "read"):
        raise TypeError("source must be PNG bytes or a binary stream")

    result = {
        "status": "error",
        "complete": False,
        "coverage": "none",
        "text_chunks": [],
        "exif_chunks": [],
        "dimensions": None,
        "chunks_scanned": 0,
        "text_bytes_captured": 0,
        "decompressed_bytes": 0,
        "exif_bytes_captured": 0,
        "errors": [],
        "limits": limits,
    }

    def fail(message: str, *, status: str = "partial") -> dict:
        result["errors"].append(message)
        result["status"] = status
        result["coverage"] = "partial"
        return result

    try:
        signature = _read_exact(stream, len(PNG_SIGNATURE))
    except EOFError:
        return fail("truncated PNG signature", status="error")
    if signature != PNG_SIGNATURE:
        return fail("invalid PNG signature", status="error")

    reached_iend = False
    saw_text = False
    oversized = False
    saw_header = False
    text_bytes_seen = 0
    exif_bytes_seen = 0
    offset = len(PNG_SIGNATURE)
    while True:
        if result["chunks_scanned"] >= max_chunks:
            result["status"] = "too_large"
            result["coverage"] = "partial"
            result["errors"].append("PNG chunk count exceeds max_chunks")
            break
        chunk_offset = offset
        try:
            length_bytes = _read_exact(stream, 4)
        except EOFError:
            fail("truncated chunk length")
            break
        offset += 4
        length = struct.unpack(">I", length_bytes)[0]
        try:
            chunk_type = _read_exact(stream, 4)
        except EOFError:
            fail("truncated chunk type")
            break
        offset += 4
        result["chunks_scanned"] += 1
        if length > max_chunk_bytes:
            if chunk_type in TEXT_TYPES:
                saw_text = True
                result["text_chunks"].append(_chunk_record(
                    chunk_offset, chunk_type, length, "unavailable", None,
                    capture_error="chunk exceeds max_chunk_bytes"))
            elif chunk_type == b"eXIf":
                result["exif_chunks"].append(_exif_record(
                    chunk_offset, length, "unavailable", None,
                    capture_error="chunk exceeds max_chunk_bytes"))
            result["errors"].append(f"chunk {chunk_type.decode('latin1')} exceeds max_chunk_bytes")
            result["status"] = "too_large"
            result["coverage"] = "partial"
            oversized = True
            break

        is_text = chunk_type in TEXT_TYPES
        is_exif = chunk_type == b"eXIf"
        saw_text = saw_text or is_text
        capture = is_text and length <= max_single_text_bytes and \
            text_bytes_seen + length <= max_total_text_bytes
        capture_exif = is_exif and length <= max_single_exif_bytes and \
            exif_bytes_seen + length <= max_total_exif_bytes
        if is_text:
            text_bytes_seen += length
        if is_exif:
            exif_bytes_seen += length
        is_header = chunk_type == b"IHDR"
        data = bytearray() if capture or capture_exif or (is_header and length == 13) else None
        crc = zlib.crc32(chunk_type)
        remaining = length
        truncated = False
        while remaining:
            piece = stream.read(min(READ_BLOCK_BYTES, remaining))
            if not piece:
                truncated = True
                break
            if len(piece) > remaining:
                piece = piece[:remaining]
            remaining -= len(piece)
            offset += len(piece)
            crc = zlib.crc32(piece, crc)
            if data is not None:
                data.extend(piece)
        if truncated:
            if is_text:
                result["text_chunks"].append(_chunk_record(
                    chunk_offset, chunk_type, length, "unavailable", bytes(data) if data is not None else None,
                    capture_error="truncated chunk data"))
            elif is_exif:
                result["exif_chunks"].append(_exif_record(
                    chunk_offset, length, "unavailable", bytes(data) if data is not None else None,
                    capture_error="truncated chunk data"))
            fail(f"truncated {chunk_type.decode('latin1')} chunk data")
            break
        try:
            crc_bytes = _read_exact(stream, 4)
        except EOFError:
            if is_text:
                result["text_chunks"].append(_chunk_record(
                    chunk_offset, chunk_type, length, "unavailable", bytes(data) if data is not None else None,
                    capture_error="truncated chunk CRC"))
            elif is_exif:
                result["exif_chunks"].append(_exif_record(
                    chunk_offset, length, "unavailable", bytes(data) if data is not None else None,
                    capture_error="truncated chunk CRC"))
            fail(f"truncated {chunk_type.decode('latin1')} chunk CRC")
            break
        offset += 4
        expected_crc = struct.unpack(">I", crc_bytes)[0]
        crc_status = "valid" if (crc & 0xFFFFFFFF) == expected_crc else "invalid"
        if is_header:
            if saw_header or result["chunks_scanned"] != 1 or length != 13 or crc_status != "valid":
                result["dimensions"] = None
                result["errors"].append('Invalid, repeated or misplaced IHDR; dimensions unavailable')
            else:
                width, height = struct.unpack_from('>II', data)
                if 1 <= width <= 2147483647 and 1 <= height <= 2147483647:
                    result["dimensions"] = {"width": width, "height": height, "source": "IHDR"}
                else:
                    result["errors"].append('Invalid IHDR dimensions')
            saw_header = True

        if is_text:
            if not capture:
                reason = "single text chunk budget exceeded" if length > max_single_text_bytes \
                    else "total text payload budget exceeded"
                result["text_chunks"].append(_chunk_record(
                    chunk_offset, chunk_type, length, crc_status, None,
                    capture_error=reason))
                result["errors"].append(reason)
                oversized = True
            else:
                payload = bytes(data)
                result["text_bytes_captured"] += length
                record, decoded_length, record_errors, decode_too_large = _decode_text_chunk(
                    chunk_offset, chunk_type, length, crc_status, payload,
                    max_decompressed_bytes - result["decompressed_bytes"],
                )
                result["text_chunks"].append(record)
                result["decompressed_bytes"] += decoded_length
                result["errors"].extend(record_errors)
                oversized = oversized or decode_too_large
        elif is_exif:
            if not capture_exif:
                reason = "single eXIf chunk budget exceeded" if length > max_single_exif_bytes \
                    else "total eXIf payload budget exceeded"
                result["exif_chunks"].append(_exif_record(
                    chunk_offset, length, crc_status, None, capture_error=reason))
                result["errors"].append(reason)
                oversized = True
            else:
                payload = bytes(data)
                record = _exif_record(chunk_offset, length, crc_status, payload)
                result["exif_chunks"].append(record)
                result["exif_bytes_captured"] += length
                if crc_status == "invalid":
                    result["errors"].append(f"eXIf at {chunk_offset}: CRC mismatch")

        if chunk_type == b"IEND":
            reached_iend = True
            break

    if oversized:
        result["status"] = "too_large"
        result["coverage"] = "partial"
    elif result["errors"]:
        result["status"] = "partial"
        result["coverage"] = "partial"
    elif reached_iend:
        result["complete"] = True
        result["coverage"] = "complete"
        result["status"] = "complete" if saw_text else "no_metadata"
    else:
        result["status"] = "partial"
        result["coverage"] = "partial"
    return result


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    parts = bytearray()
    while len(parts) < length:
        piece = stream.read(length - len(parts))
        if not piece:
            raise EOFError
        parts.extend(piece)
    return bytes(parts)


def _chunk_record(offset: int, chunk_type: bytes, length: int, crc_status: str,
                  payload: bytes | None, *, capture_error: str | None = None) -> dict:
    return {
        "offset": offset,
        "type": chunk_type.decode("ascii", errors="replace"),
        "length": length,
        "crc_status": crc_status,
        "keyword_bytes_b64": None,
        "keyword": None,
        "language_bytes_b64": None,
        "translated_keyword_bytes_b64": None,
        "translated_keyword": None,
        "raw_payload_b64": base64.b64encode(payload).decode("ascii") if payload is not None else None,
        "text_bytes_b64": None,
        "text": None,
        "encoding": None,
        "capture_error": capture_error,
    }


def _exif_record(offset: int, length: int, crc_status: str, payload: bytes | None,
                 *, capture_error: str | None = None) -> dict:
    record = {
        "offset": offset,
        "type": "eXIf",
        "length": length,
        "crc_status": crc_status,
        "status": "captured" if payload is not None and capture_error is None else "unavailable",
        "raw_payload_b64": base64.b64encode(payload).decode("ascii") if payload is not None else None,
        "capture_error": capture_error,
        "semantics": None,
    }
    if payload is not None and capture_error is None:
        from .jpeg_semantics import parse_jpeg_app1

        semantics = parse_jpeg_app1(b"Exif\x00\x00" + payload, offset)
        source = semantics.get("source")
        if isinstance(source, dict):
            source["chunk_offset"] = source.pop("app1_offset", None)
            source.pop("marker", None)
            source["container"] = "png"
        record["semantics"] = semantics
    return record


def _decode_text_chunk(offset: int, chunk_type: bytes, length: int, crc_status: str,
                       payload: bytes, decompressed_budget: int) -> tuple[dict, int, list[str], bool]:
    record = _chunk_record(offset, chunk_type, length, crc_status, payload)
    errors: list[str] = []
    too_large = False
    decompression_cost = 0
    nul = payload.find(b"\0")
    if nul < 0:
        record["capture_error"] = "missing keyword terminator"
        errors.append(f"{record['type']} at {offset}: missing keyword terminator")
        return record, 0, errors, False
    keyword_bytes = payload[:nul]
    record["keyword_bytes_b64"] = base64.b64encode(keyword_bytes).decode("ascii")
    record["keyword"] = keyword_bytes.decode("latin1")

    try:
        if chunk_type == b"tEXt":
            text_bytes = payload[nul + 1:]
            encoding = "latin-1"
        elif chunk_type == b"zTXt":
            if len(payload) < nul + 2 or payload[nul + 1] != 0:
                raise ValueError("invalid zTXt compression method")
            text_bytes, over = _inflate(payload[nul + 2:], decompressed_budget)
            too_large = too_large or over
            decompression_cost = len(text_bytes)
            encoding = "latin-1"
        else:
            cursor = nul + 1
            if cursor + 2 > len(payload):
                raise ValueError("truncated iTXt compression fields")
            compression_flag, compression_method = payload[cursor], payload[cursor + 1]
            cursor += 2
            if compression_flag not in (0, 1) or compression_method != 0:
                raise ValueError("invalid iTXt compression fields")
            language_end = payload.find(b"\0", cursor)
            if language_end < 0:
                raise ValueError("missing iTXt language terminator")
            translated_end = payload.find(b"\0", language_end + 1)
            if translated_end < 0:
                raise ValueError("missing iTXt translated keyword terminator")
            raw_text = payload[translated_end + 1:]
            language_bytes = payload[cursor:language_end]
            translated_bytes = payload[language_end + 1:translated_end]
            record["language_bytes_b64"] = base64.b64encode(language_bytes).decode("ascii")
            record["translated_keyword_bytes_b64"] = base64.b64encode(translated_bytes).decode("ascii")
            try:
                record["translated_keyword"] = translated_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                record["capture_error"] = "translated keyword is not valid UTF-8"
                errors.append(f"iTXt at {offset}: translated keyword is not valid UTF-8; original bytes retained")
            if compression_flag:
                text_bytes, over = _inflate(raw_text, decompressed_budget)
                too_large = too_large or over
                decompression_cost = len(text_bytes)
            else:
                text_bytes = raw_text
                if len(text_bytes) > decompressed_budget:
                    text_bytes = text_bytes[:decompressed_budget]
                    too_large = True
            encoding = "utf-8"

        record["encoding"] = encoding
        record["text_bytes_b64"] = base64.b64encode(text_bytes).decode("ascii")
        try:
            record["text"] = text_bytes.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            record["capture_error"] = "text is not valid UTF-8"
            errors.append(f"iTXt at {offset}: text is not valid UTF-8; original bytes retained")
        if crc_status == "invalid":
            errors.append(f"{record['type']} at {offset}: CRC mismatch")
        if too_large:
            record["capture_error"] = "decompressed text budget exceeded"
            errors.append(f"{record['type']} at {offset}: decompressed text budget exceeded")
        return record, decompression_cost, errors, too_large
    except (ValueError, zlib.error) as exc:
        record["capture_error"] = str(exc)
        errors.append(f"{record['type']} at {offset}: {exc}")
        if crc_status == "invalid":
            errors.append(f"{record['type']} at {offset}: CRC mismatch")
        return record, 0, errors, too_large


def _inflate(compressed: bytes, budget: int) -> tuple[bytes, bool]:
    decoder = zlib.decompressobj()
    try:
        output = decoder.decompress(compressed, budget + 1)
        if len(output) > budget or decoder.unconsumed_tail:
            return output[:budget], True
        tail = decoder.flush(max(1, budget - len(output) + 1))
    except zlib.error:
        raise
    if len(output) + len(tail) > budget:
        return (output + tail)[:budget], True
    output += tail
    if not decoder.eof:
        raise ValueError("incomplete compressed text stream")
    return output, False

