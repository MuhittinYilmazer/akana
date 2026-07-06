"""EXIF/location metadata stripping — pure stdlib (PIL is NOT in the repo).

Limits (honest report):

* **JPEG** — APP1 segments (Exif + XMP; GPS data lives in the Exif IFD) are
  dropped entirely. Pixel data and other segments (JFIF APP0, ICC APP2,
  quantization/huffman tables) are preserved byte for byte.
* **PNG** — ``eXIf`` chunks are dropped, and ``tEXt``/``iTXt``/``zTXt`` chunks
  whose keyword is ``XML:com.adobe.xmp`` (the XMP packet, which carries
  ``exif:GPSLatitude``/``GPSLongitude`` as written by Lightroom/Photoshop) are
  dropped too. Other free-text chunks are left untouched — deleting them
  aggressively would corrupt legitimate metadata.
* **WebP** — ``EXIF`` and ``XMP `` chunks are dropped from the RIFF container,
  the EXIF (0x08) / XMP (0x04) flag bits in the ``VP8X`` header are cleared, and
  the RIFF size is recomputed.
* **GIF** — the format carries no EXIF; no-op.

What CANNOT be done with stdlib: opening the Exif block to strip only the GPS
IFD while keeping the rest (orientation, lens, date) — that requires a TIFF/IFD
rewrite. The F0 choice favors privacy: the container is dropped entirely (the
data loss is metadata only).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StripResult:
    data: bytes
    stripped: bool  # whether at least one metadata block was dropped
    note: str  # what was done / why it could not be done (written to the record)


def _strip_jpeg(data: bytes) -> StripResult:
    out = bytearray(data[:2])  # SOI
    pos = 2
    n = len(data)
    stripped = False
    in_scan = False  # inside entropy-coded scan data (after an SOS marker)
    while pos < n:
        if data[pos] != 0xFF:
            if in_scan:
                out.append(data[pos])  # entropy byte — keep the pixel stream
                pos += 1
                continue
            # BUG(EXIF/GPS egress): a broken segment stream must NOT copy the
            # remainder verbatim — a later APP1 Exif/GPS segment could ride
            # along in that tail. Rebuild from recognized segments only (as
            # _strip_webp does) and drop the unparseable remainder.
            break
        if pos + 1 >= n:  # a trailing 0xFF with no marker byte — drop it
            break
        marker = data[pos + 1]
        if marker == 0xFF:
            out.append(0xFF)
            pos += 1
            continue
        if marker == 0x00 and in_scan:  # 0xFF00 byte-stuffing inside scan data
            out += data[pos : pos + 2]
            pos += 2
            continue
        if marker in (0x01,) or 0xD0 <= marker <= 0xD9:
            out += data[pos : pos + 2]
            # RSTn (D0-D7) sit inside the scan; any other standalone ends it.
            in_scan = in_scan and 0xD0 <= marker <= 0xD7
            pos += 2
            continue
        if pos + 4 > n:  # not enough bytes for the 2-byte segment length
            break
        length = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        end = pos + 2 + length
        # BUG(EXIF/GPS egress): bound every segment to the buffer. A malformed
        # length (< the 2-byte minimum, or one that overruns EOF) would let the
        # slice below clamp to EOF and copy a following real Exif/GPS segment
        # verbatim. Treat it as broken and stop without a verbatim tail copy.
        if length < 2 or end > n:
            break
        if marker == 0xDA:  # SOS → keep the header; entropy data follows
            # BUG(EXIF/GPS egress): do NOT copy the rest verbatim — an APP1
            # Exif/GPS segment appended after the scan data would survive. Keep
            # the SOS header and walk the entropy stream so a later APP1 is
            # still dropped below.
            out += data[pos:end]
            in_scan = True
            pos = end
            continue
        if marker == 0xE1:  # APP1: Exif or XMP — dropped entirely for privacy
            stripped = True
            pos = end
            continue
        out += data[pos:end]
        pos = end
    note = "jpeg: APP1 (Exif/XMP) segments stripped" if stripped else "jpeg: no Exif segment"
    return StripResult(bytes(out), stripped, note)


#: PNG text chunks that can carry an XMP packet, and the XMP keyword. Lightroom/
#: Photoshop store the XMP block (with exif:GPSLatitude/GPSLongitude) in an iTXt
#: chunk keyed 'XML:com.adobe.xmp'; the keyword is the null-terminated head of
#: the chunk data for tEXt/iTXt/zTXt alike.
_PNG_TEXT_CHUNKS = (b"tEXt", b"iTXt", b"zTXt")
_XMP_KEYWORD = b"XML:com.adobe.xmp"


def _is_xmp_text_chunk(ctype: bytes, chunk_data: bytes) -> bool:
    if ctype not in _PNG_TEXT_CHUNKS:
        return False
    keyword = chunk_data.split(b"\x00", 1)[0]
    return keyword == _XMP_KEYWORD


def _strip_png(data: bytes) -> StripResult:
    out = bytearray(data[:8])  # imza
    pos = 8
    n = len(data)
    stripped = False
    while pos + 8 <= n:
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        end = pos + 12 + length  # len + type + data + crc
        if end > n:
            # BUG(EXIF/GPS egress): a chunk whose declared length overruns EOF
            # must ALWAYS drop the remaining raw bytes — regardless of ctype. If
            # the overrunning chunk is NOT eXIf (e.g. tEXt/IDAT), a real eXIf
            # chunk physically after it would otherwise be copied verbatim by
            # the trailing `out += data[pos:]`, leaking GPS data. Rebuild from
            # recognized chunks only (as _strip_webp does): stop without the
            # verbatim tail.
            if ctype == b"eXIf" or _is_xmp_text_chunk(
                ctype, data[pos + 8 : n]
            ):
                stripped = True
            pos = n  # prevent the trailing copy for ANY overrunning chunk
            break
        if ctype == b"eXIf" or _is_xmp_text_chunk(ctype, data[pos + 8 : end - 4]):
            stripped = True
        else:
            out += data[pos:end]
        pos = end
        # BUG(EXIF/GPS egress): do NOT break at IEND and do NOT copy a verbatim
        # tail — a real eXIf (GPS) chunk placed AFTER IEND would otherwise ride
        # along in that remainder. Keep scanning so any late eXIf is dropped;
        # rebuild from recognized, in-bounds chunks only (mirrors _strip_webp).
    note = "png: eXIf/XMP location chunk stripped" if stripped else "png: no eXIf/XMP chunk"
    return StripResult(bytes(out), stripped, note)


def _strip_webp(data: bytes) -> StripResult:
    if len(data) < 12:
        return StripResult(data, False, "webp: header too short")
    chunks: list[bytes] = []
    pos = 12
    n = len(data)
    stripped = False
    while pos + 8 <= n:
        fourcc = data[pos : pos + 8][:4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        payload_end = pos + 8 + size
        chunk_end = payload_end + (size % 2)  # align to an even byte
        if payload_end > n:
            break
        if fourcc in (b"EXIF", b"XMP "):
            stripped = True
        else:
            payload = data[pos + 8 : payload_end]
            if fourcc == b"VP8X" and payload:
                payload = bytes([payload[0] & ~0x0C]) + payload[1:]  # EXIF|XMP bits
            chunk = fourcc + struct.pack("<I", len(payload)) + payload
            if len(payload) % 2:
                chunk += b"\x00"
            chunks.append(chunk)
        pos = chunk_end
    body = b"".join(chunks)
    out = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body
    note = "webp: EXIF/XMP chunks stripped, VP8X flags cleared" if stripped else "webp: no EXIF chunk"
    return StripResult(out, stripped, note)


def strip_location_metadata(data: bytes, fmt: str) -> StripResult:
    """Strip EXIF/location metadata per format; unknown format → no-op."""
    if fmt == "jpeg":
        return _strip_jpeg(data)
    if fmt == "png":
        return _strip_png(data)
    if fmt == "webp":
        return _strip_webp(data)
    if fmt == "gif":
        return StripResult(data, False, "gif: format carries no EXIF")
    return StripResult(data, False, f"{fmt}: stripping not applied")
