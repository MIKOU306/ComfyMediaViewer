"""Bounded semantic readers for EXIF UserComment and standard JPEG XMP APP1.

The caller owns and must retain the original APP1 payload. Results here are
derived text only; they never replace or normalize the source bytes.
"""
from __future__ import annotations

import codecs
import re
import struct
import xml.etree.ElementTree as ET

EXIF_ID = b"Exif\x00\x00"
XMP_ID = b"http://ns.adobe.com/xap/1.0/\x00"
DEFAULT_MAX_APP1_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_TIFF_ENTRIES = 4096
DEFAULT_MAX_XMP_DEPTH = 64
DEFAULT_MAX_XMP_ELEMENTS = 20000


class _Malformed(ValueError):
    pass


def _record(source: dict, *, declared=None, actual=None, text=None, fields=None,
            status="ok", error=None, byte_order_source=None,
            byte_order_inferred=False) -> dict:
    result = {"source": source, "declared_encoding": declared,
              "actual_encoding": actual, "text": text, "fields": fields or [],
              "status": status, "error": error}
    if byte_order_source is not None:
        result["byte_order_source"] = byte_order_source
    if byte_order_inferred:
        result["byte_order_inferred"] = True
    return result


# Swapping UTF-16 endianness always "succeeds" but maps CJK into the Extension A
# block (U+3400-U+4DBF) and similar rare ranges, while a correct reading lands in
# the common CJK block (U+4E00-U+9FFF). That contrast is the only reliable signal,
# so score both candidates on it instead of trusting the file byte order.
_COMMON_CJK = re.compile(r"[\u4e00-\u9fff]")
_RARE_CJK = re.compile(r"[\u3400-\u4dbf\uf900-\ufaff]")


def _cjk_score(text: str) -> int:
    """Higher means more likely to be the intended reading of a comment.

    Two independent signals, because neither works alone:
      * CJK blocks separate the right endianness for CJK text;
      * printable-ASCII density catches mixed text like "a prompt <CJK>", where
        the wrong endian turns the ASCII part into rare CJK-extension glyphs.
    """
    if not text or "\ufffd" in text:
        return -1
    cjk = 2 * len(_COMMON_CJK.findall(text)) - len(_RARE_CJK.findall(text))
    visible = [character for character in text if not character.isspace()]
    if not visible:
        return 0
    printable = sum(1 for character in visible if " " <= character <= "~")
    return cjk + printable


def _decode_comment(raw: bytes, endian: str, source: dict) -> dict:
    byte_order_inferred = False
    byte_order_source = None
    if len(raw) < 8:
        return _record(source, status="malformed", error="UserComment is shorter than its 8-byte charset designator")
    designator, body = raw[:8], raw[8:]
    if designator == b"ASCII\x00\x00\x00":
        declared, encoding = "ASCII", "ascii"
    elif designator == b"JIS\x00\x00\x00\x00\x00":
        declared, encoding = "JIS", "iso2022_jp"
    elif designator == b"UNICODE\x00":
        declared = "UNICODE"
        if body.startswith(codecs.BOM_UTF16_LE):
            encoding, body = "utf-16-le", body[2:]
        elif body.startswith(codecs.BOM_UTF16_BE):
            encoding, body = "utf-16-be", body[2:]
        else:
            # No BOM: EXIF 2.32 falls back to the TIFF byte order, but that byte
            # order describes the file, not the comment text, so it is not evidence.
            #
            # Pure ASCII is decided by shape, not by the CJK score: exactly one
            # endian yields printable ASCII, the other yields rare CJK-extension
            # glyphs. Both directions must be tried — assuming little-endian
            # mis-reads big-endian ASCII.
            if len(body) % 2 == 0 and body:
                for candidate in ("utf-16-le", "utf-16-be"):
                    try:
                        probe = body.decode(candidate)
                    except UnicodeDecodeError:
                        continue
                    if probe and all(character == "\n" or " " <= character <= "~"
                                     for character in probe):
                        return _record(source, declared=declared, actual=candidate,
                                       text=probe,
                                       byte_order_source="ascii_byte_order_agnostic",
                                       byte_order_inferred=True)
            # Otherwise decode both ways and keep the reading that looks like text.
            # Ranking, in order:
            #   1. fewer CJK-extension-A / compatibility glyphs — the clearest tell;
            #   2. lower mean code point — swapping the endian of CJK moves the high
            #      byte into the low position, so wrong readings land systematically
            #      higher (U+4F60 '你' mis-reads as U+604F). This breaks ties on
            #      short strings where both readings are all "common" CJK;
            #   3. more printable ASCII — catches mixed ASCII/CJK comments.
            best = None
            for candidate, label in (("utf-16-le", "little_endian_preferred"),
                                     ("utf-16-be", "big_endian_preferred")):
                try:
                    probe = body.decode(candidate)
                except UnicodeDecodeError:
                    continue
                if "\ufffd" in probe:
                    continue
                cjk_points = [ord(character) for character in probe
                              if ord(character) > 0x2E80]
                mean_point = (sum(cjk_points) / len(cjk_points)) if cjk_points else 0.0
                rank = (len(_RARE_CJK.findall(probe)),
                        mean_point,
                        -sum(1 for character in probe if " " <= character <= "~"))
                if best is None or rank < best[0]:
                    best = (rank, candidate, label)
            byte_order_inferred = True
            if best is None:
                encoding, byte_order_source = "utf-16-le", "assumed_little_endian"
            elif best[0][0] == 0:
                # No rare glyphs on either side: a clean reading.
                encoding, byte_order_source = best[1], best[2]
            else:
                # Both readings contain rare CJK-extension glyphs, so the bytes are
                # not a plain-endian encoding of readable text. With no BOM the byte
                # order is not recoverable, so say so instead of guessing.
                return _record(source, declared=declared, actual=None, text=None,
                               status="unsupported", byte_order_source="ambiguous_no_bom",
                               byte_order_inferred=True,
                               error=("UserComment has no byte-order mark and neither "
                                      "endian decodes to plain text, so no text is reported"))
    else:
        # The EXIF-defined all-zero designator means undefined character code.
        if designator == b"\x00" * 8:
            return _record(source, declared="undefined", status="unsupported",
                           error="EXIF UserComment has no declared character encoding")
        return _record(source, declared="unknown", status="unsupported",
                       error="Unknown EXIF UserComment charset designator")
    try:
        value = body.decode(encoding, errors="strict")
    except (UnicodeDecodeError, LookupError) as exc:
        return _record(source, declared=declared, actual=encoding,
                       status="malformed", error=f"UserComment decode failed: {exc}")
    if byte_order_source == "assumed_little_endian":
        # Neither endian could even decode: the bytes are not usable text.
        # Report that instead of presenting a guess as the comment.
        return _record(source, declared=declared, actual=encoding, text=None,
                       status="malformed", byte_order_source=byte_order_source,
                       byte_order_inferred=True,
                       error=("UserComment has no byte-order mark and the bytes are "
                              "not valid UTF-16 in either endian, so no text is reported"))
    return _record(source, declared=declared, actual=encoding, text=value,
                   byte_order_source=byte_order_source,
                   byte_order_inferred=byte_order_inferred)


def _tiff_user_comment(data: bytes, offset: int, entry_budget: int) -> dict:
    source = {"kind": "exif_user_comment", "app1_offset": offset,
              "marker": "FFE1", "tag": "ExifIFD:0x9286"}
    tiff = data[len(EXIF_ID):]
    if len(tiff) < 8:
        raise _Malformed("EXIF TIFF header is truncated")
    order = tiff[:2]
    if order == b"II":
        endian = "<"
    elif order == b"MM":
        endian = ">"
    else:
        raise _Malformed("Invalid TIFF byte order")

    def u16(pos):
        if pos < 0 or pos + 2 > len(tiff):
            raise _Malformed("TIFF 16-bit value is out of bounds")
        return struct.unpack_from(endian + "H", tiff, pos)[0]

    def u32(pos):
        if pos < 0 or pos + 4 > len(tiff):
            raise _Malformed("TIFF 32-bit value is out of bounds")
        return struct.unpack_from(endian + "I", tiff, pos)[0]

    if u16(2) != 42:
        raise _Malformed("Invalid TIFF magic")
    first_ifd = u32(4)
    visited: set[int] = set()
    count_seen = 0

    def read_ifd(ifd_offset: int):
        nonlocal count_seen
        if ifd_offset in visited:
            raise _Malformed("TIFF IFD cycle detected")
        visited.add(ifd_offset)
        count = u16(ifd_offset)
        count_seen += count
        if count_seen > entry_budget:
            raise _Malformed("TIFF IFD entry budget exceeded")
        start = ifd_offset + 2
        end = start + count * 12
        if end + 4 > len(tiff):
            raise _Malformed("TIFF IFD entries exceed APP1 bounds")
        rows = []
        for index in range(count):
            pos = start + index * 12
            tag, typ, amount = struct.unpack_from(endian + "HHI", tiff, pos)
            value_field = tiff[pos + 8:pos + 12]
            rows.append((tag, typ, amount, value_field))
        return rows, u32(end)

    ifd0, _ = read_ifd(first_ifd)
    exif_ptrs = [entry for entry in ifd0 if entry[0] == 0x8769]
    if not exif_ptrs:
        return _record(source, status="not_found", error="EXIF IFD pointer is absent")
    if len(exif_ptrs) != 1:
        raise _Malformed("Duplicate ExifIFD pointers")
    tag, typ, amount, value_field = exif_ptrs[0]
    if typ != 4 or amount != 1:
        raise _Malformed("ExifIFD pointer has invalid TIFF type/count")
    exif_offset = struct.unpack(endian + "I", value_field)[0]
    exif_ifd, _ = read_ifd(exif_offset)
    comments = [entry for entry in exif_ifd if entry[0] == 0x9286]
    if not comments:
        return _record(source, status="not_found", error="EXIF UserComment tag is absent")
    if len(comments) != 1:
        raise _Malformed("Duplicate EXIF UserComment tags")
    _, typ, amount, value_field = comments[0]
    # UNDEFINED is the standard type; some writers incorrectly mark it BYTE.
    type_sizes = {1: 1, 7: 1}
    if typ not in type_sizes:
        raise _Malformed("EXIF UserComment has unsupported TIFF type")
    if amount > len(tiff):
        raise _Malformed("EXIF UserComment length exceeds APP1 bounds")
    if amount <= 4:
        raw = value_field[:amount]
    else:
        value_offset = struct.unpack(endian + "I", value_field)[0]
        if value_offset < 0 or value_offset + amount > len(tiff):
            raise _Malformed("EXIF UserComment value exceeds APP1 bounds")
        raw = tiff[value_offset:value_offset + amount]
    return _decode_comment(raw, endian, source)


def _xmp(data: bytes, offset: int, max_depth: int, max_elements: int) -> dict:
    source = {"kind": "xmp", "app1_offset": offset, "marker": "FFE1",
              "packet": "standard_xmp"}
    packet = data[len(XMP_ID):]
    declared = None
    actual = "utf-8"
    if packet.startswith(codecs.BOM_UTF8):
        declared, packet, actual = "UTF-8 BOM", packet[3:], "utf-8"
    elif packet.startswith(codecs.BOM_UTF16_LE):
        declared, packet, actual = "UTF-16LE BOM", packet[2:], "utf-16-le"
    elif packet.startswith(codecs.BOM_UTF16_BE):
        declared, packet, actual = "UTF-16BE BOM", packet[2:], "utf-16-be"
    else:
        prefix = packet[:256]
        match = re.search(br"<\?xml[^>]*encoding\s*=\s*['\"]([^'\"]+)['\"]", prefix, re.I)
        if match:
            try:
                declared = match.group(1).decode("ascii", "strict")
                actual = codecs.lookup(declared).name
            except (UnicodeDecodeError, LookupError) as exc:
                return _record(source, declared="unknown", status="unsupported",
                               error=f"Unsupported XMP XML encoding declaration: {exc}")
    try:
        xml_text = packet.decode(actual, "strict")
        # Decode before checking so UTF-16 markup cannot bypass the declaration guard.
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml_text, re.I):
            return _record(source, declared=declared, actual=actual, status="malformed",
                           error="DTD and entity declarations are forbidden in XMP")
        parser = ET.XMLPullParser(events=("start", "end"))
        root = None
        depth = 0
        count = 0
        for start in range(0, len(xml_text), 4096):
            parser.feed(xml_text[start:start + 4096])
            for event, _element in parser.read_events():
                if event == "start":
                    if root is None:
                        root = _element
                    depth += 1
                    count += 1
                    if depth > max_depth:
                        return _record(source, declared=declared, actual=actual,
                                       status="too_large", error="XMP nesting depth budget exceeded")
                    if count > max_elements:
                        return _record(source, declared=declared, actual=actual,
                                       status="too_large", error="XMP element budget exceeded")
                else:
                    depth -= 1
        parser.close()
        if root is None:
            raise ET.ParseError('XMP root element is absent')
    except (UnicodeDecodeError, ET.ParseError, LookupError) as exc:
        return _record(source, declared=declared, actual=actual,
                       status="malformed", error=f"XMP XML parse failed: {exc}")
    fields = []
    def expanded(name: str) -> tuple[str | None, str]:
        if name.startswith("{") and "}" in name:
            namespace, local = name[1:].split("}", 1)
            return namespace, local
        return None, name

    stack = [("enter", root)]
    while stack:
        action, element = stack.pop()
        if action == "tail":
            if element.tail is not None and element.tail.strip():
                fields.append({"kind": "text", "namespace": None,
                               "name": "#tail", "value": element.tail})
            continue
        for name, value in element.attrib.items():
            namespace, local = expanded(name)
            fields.append({"kind": "attribute", "namespace": namespace,
                           "name": local, "value": value})
        if element.text is not None and element.text.strip():
            namespace, local = expanded(element.tag)
            fields.append({"kind": "text", "namespace": namespace,
                           "name": local, "value": element.text})
        # Reverse-pushed actions emit each repeated field in document order.
        children = list(element)
        stack.append(("tail", element))
        for child in reversed(children):
            stack.append(("enter", child))
    return _record(source, declared=declared or "XML default (UTF-8)", actual=actual,
                   fields=fields)


def parse_jpeg_app1(payload: bytes, offset: int, *,
                    max_payload_bytes: int = DEFAULT_MAX_APP1_BYTES,
                    max_tiff_entries: int = DEFAULT_MAX_TIFF_ENTRIES,
                    max_xmp_depth: int = DEFAULT_MAX_XMP_DEPTH,
                    max_xmp_elements: int = DEFAULT_MAX_XMP_ELEMENTS) -> dict:
    """Parse one APP1 payload, returning semantics while leaving raw bytes to caller.

    ``payload`` excludes the JPEG marker and two-byte segment length. The caller
    remains responsible for archiving those exact bytes, including on errors.
    """
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    for name, value, minimum in (("offset", offset, 0),
                                 ("max_payload_bytes", max_payload_bytes, 0),
                                 ("max_tiff_entries", max_tiff_entries, 1),
                                 ("max_xmp_depth", max_xmp_depth, 1),
                                 ("max_xmp_elements", max_xmp_elements, 1)):
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} has an invalid value")
    if len(payload) > max_payload_bytes:
        return _record({"kind": "app1", "app1_offset": offset, "marker": "FFE1"},
                       status="too_large", error="APP1 payload byte budget exceeded")
    try:
        if payload.startswith(EXIF_ID):
            return _tiff_user_comment(payload, offset, max_tiff_entries)
        if payload.startswith(XMP_ID):
            return _xmp(payload, offset, max_xmp_depth, max_xmp_elements)
        return _record({"kind": "app1", "app1_offset": offset, "marker": "FFE1"},
                       status="unsupported", error="APP1 identifier is not EXIF or standard XMP")
    except _Malformed as exc:
        kind = "exif_user_comment" if payload.startswith(EXIF_ID) else "xmp"
        return _record({"kind": kind, "app1_offset": offset, "marker": "FFE1"},
                       status="malformed", error=str(exc))


__all__ = ["parse_jpeg_app1", "DEFAULT_MAX_APP1_BYTES", "DEFAULT_MAX_TIFF_ENTRIES",
           "DEFAULT_MAX_XMP_DEPTH", "DEFAULT_MAX_XMP_ELEMENTS"]
