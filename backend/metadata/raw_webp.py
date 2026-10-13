"""Bounded RIFF/WEBP metadata capture without decoding image or video data."""
from __future__ import annotations

import base64
import io
import struct
from typing import BinaryIO

from backend.metadata.jpeg_semantics import parse_jpeg_app1

READ_BLOCK_BYTES = 64 * 1024
METADATA_CHUNKS = {b"EXIF", b"XMP "}


def read_webp_metadata(source: bytes | bytearray | memoryview | BinaryIO, *,
                       max_metadata_bytes: int = 8 *  1024 * 1024,
                       max_chunk_bytes: int = 64 * 1024 * 1024,
                       max_chunks: int = 4096) -> dict:
    """Return ordered raw EXIF/XMP chunks and WebP dimensions when available.

    ``offset`` is the chunk-header offset and ``declared_length`` is the RIFF
    payload length, excluding the eight-byte chunk header and optional pad.
    Large image/video chunks are skipped by seeking where possible; retained
    metadata is bounded by ``max_metadata_bytes``.
    """
    for name, value, minimum in (("max_metadata_bytes", max_metadata_bytes, 0),
                                 ("max_chunk_bytes", max_chunk_bytes, 0),
                                 ("max_chunks", max_chunks, 1)):
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} must be a non-negative integer" if minimum == 0
                             else f"{name} must be a positive integer")
    stream = io.BytesIO(bytes(source)) if isinstance(source, (bytes, bytearray, memoryview)) else source
    if not hasattr(stream, "read"):
        raise TypeError("source must be WebP bytes or a binary stream")
    result = {"status": "error", "complete": False, "coverage": "none",
              "chunks": [], "dimensions": None, "chunks_scanned": 0,
              "metadata_bytes_captured": 0, "errors": [],
              "limits": {"max_metadata_bytes": max_metadata_bytes,
                         "max_chunk_bytes": max_chunk_bytes, "max_chunks": max_chunks}}

    def fail(message: str, status: str = "error") -> dict:
        result["errors"].append(message)
        result["status"] = status
        result["coverage"] = "partial"
        return result

    def exact(length: int) -> bytes:
        out = bytearray()
        while len(out) < length:
            part = stream.read(length - len(out))
            if not part:
                raise EOFError("truncated RIFF/WEBP data")
            out.extend(part)
        return bytes(out)

    def skip(length: int) -> None:
        if length <= 0:
            return
        try:
            if stream.seekable():
                stream.seek(length, io.SEEK_CUR)
                return
        except (AttributeError, OSError, io.UnsupportedOperation):
            pass
        remaining = length
        while remaining:
            piece = stream.read(min(READ_BLOCK_BYTES, remaining))
            if not piece:
                raise EOFError("truncated RIFF/WEBP chunk")
            remaining -= len(piece)

    try:
        header = exact(12)
        if header[:4] != b"RIFF" or header[8:] != b"WEBP":
            return fail("invalid RIFF/WEBP signature")
        riff_size = struct.unpack_from("<I", header, 4)[0]
        if riff_size < 4:
            return fail("invalid RIFF size")
        riff_end = 8 + riff_size
        # Check the physical size without reading a large video payload.
        try:
            if stream.seekable():
                current = stream.tell()
                stream.seek(0, io.SEEK_END)
                physical_end = stream.tell()
                stream.seek(current)
                if riff_end > physical_end:
                    return fail("RIFF size exceeds source length")
        except (AttributeError, OSError, io.UnsupportedOperation):
            pass
        position = 12
        metadata_seen = 0
        dimensions_seen = False
        image_header_seen = False
        for _ in range(max_chunks):
            if position == riff_end:
                result["complete"] = True
                result["coverage"] = "complete"
                if not image_header_seen:
                    result["status"] = "unsupported"
                    result["complete"] = False
                    result["errors"].append("No supported VP8, VP8L or VP8X image header was found")
                else:
                    result["status"] = "complete" if metadata_seen else "no_metadata"
                return result
            if position + 8 > riff_end:
                return fail("truncated RIFF chunk header")
            chunk_offset = position
            chunk_header = exact(8)
            chunk_type = chunk_header[:4]
            length = struct.unpack_from("<I", chunk_header, 4)[0]
            position += 8
            result["chunks_scanned"] += 1
            padded_length = length + (length & 1)
            if chunk_type in METADATA_CHUNKS and length > max_chunk_bytes:
                metadata_seen += 1
                result["chunks"].append({"offset": chunk_offset,
                    "type": chunk_type.decode("ascii"), "declared_length": length,
                    "raw_payload_b64": None, "status": "unavailable",
                    "capture_error": "chunk exceeds max_chunk_bytes"})
                return fail(f"{chunk_type.decode('ascii', 'replace')} chunk exceeds max_chunk_bytes", "too_large")
            if position + padded_length > riff_end:
                return fail(f"{chunk_type.decode('ascii', 'replace')} chunk length exceeds RIFF bounds")
            if chunk_type in METADATA_CHUNKS:
                metadata_seen += 1
                record = {"offset": chunk_offset, "type": chunk_type.decode("ascii"),
                          "declared_length": length, "raw_payload_b64": None,
                          "status": "unavailable", "capture_error": None}
                result["chunks"].append(record)
                if result["metadata_bytes_captured"] + length > max_metadata_bytes:
                    record["capture_error"] = "metadata byte budget exceeded"
                    result["errors"].append("WebP metadata byte budget exceeded")
                    result["status"] = "too_large"
                    result["coverage"] = "partial"
                    return result
                payload = exact(length)
                record["raw_payload_b64"] = base64.b64encode(payload).decode("ascii")
                record["status"] = "captured"
                result["metadata_bytes_captured"] += length
                if chunk_type == b"EXIF":
                    semantic_payload = payload if payload.startswith(b"Exif\0\0") else b"Exif\0\0" + payload
                    record["semantics"] = _webp_semantics(parse_jpeg_app1(semantic_payload, chunk_offset))
                else:
                    from backend.metadata.jpeg_semantics import XMP_ID
                    semantic_payload = payload if payload.startswith(XMP_ID) else XMP_ID + payload
                    record["semantics"] = _webp_semantics(parse_jpeg_app1(semantic_payload, chunk_offset))
            elif chunk_type == b"VP8X":
                image_header_seen = True
                payload = exact(length) if length <= 10 else None
                if payload is None:
                    skip(length)
                elif length != 10:
                    return fail("invalid VP8X chunk length")
                else:
                    width = 1 + int.from_bytes(payload[4:7], "little")
                    height = 1 + int.from_bytes(payload[7:10], "little")
                    result["dimensions"] = {"width": width, "height": height, "source": "VP8X"}
                    dimensions_seen = True
            elif chunk_type == b"VP8 " and not dimensions_seen:
                image_header_seen = True
                payload = exact(min(length, 10))
                if length > 10:
                    skip(length - 10)
                if length < 10 or payload[3:6] != b"\x9d\x01\x2a":
                    return fail("invalid VP8 frame header")
                width, height = struct.unpack_from("<HH", payload, 6)
                result["dimensions"] = {"width": width & 0x3fff,
                    "height": height & 0x3fff, "source": "VP8"}
                dimensions_seen = True
            elif chunk_type == b"VP8L" and not dimensions_seen:
                image_header_seen = True
                payload = exact(min(length, 5))
                if length > 5:
                    skip(length - 5)
                if length < 5 or payload[0] != 0x2f or payload[4] >> 4:
                    return fail("invalid VP8L frame header")
                width = 1 + payload[1] + ((payload[2] & 0x3f) << 8)
                height = 1 + (payload[2] >> 6) + (payload[3] << 2) + ((payload[4] & 0x0f) << 10)
                result["dimensions"] = {"width": width, "height": height, "source": "VP8L"}
                dimensions_seen = True
            else:
                skip(length)
            if length & 1:
                exact(1)  # RIFF padding is included in bounds, but its value is unrestricted.
            position += padded_length
        result["status"] = "too_large"
        result["coverage"] = "partial"
        result["errors"].append("RIFF chunk count exceeds max_chunks")
        return result
    except (EOFError, OSError, ValueError, struct.error) as exc:
        return fail(str(exc))


def _webp_semantics(value: dict) -> dict:
    """Retain shared JPEG EXIF/XMP interpretation with WebP-specific locations."""
    source = value.get("source")
    if isinstance(source, dict):
        source["chunk_offset"] = source.pop("app1_offset", None)
        source.pop("marker", None)
        source["container"] = "webp"
    return value


__all__ = ["read_webp_metadata"]
