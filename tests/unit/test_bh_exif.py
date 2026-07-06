"""Bug-hunt regression: EXIF/GPS egress via a verbatim tail copy in exif.py.

Root cause (one bug, two instances) in ``akana_server/multimodal/exif.py``:
``_strip_jpeg`` and ``_strip_png`` walked segments/chunks and dropped the
APP1/eXIf (Exif/GPS) blocks for privacy — but when a NON-Exif segment/chunk
declared a length that OVERRAN the buffer, the code copied the remainder
verbatim (Python silently clamps an over-long slice to EOF). A real Exif/GPS
block physically AFTER the malformed one thus survived stripping and was written
to disk + handed to LLM providers. ``stripped`` also stayed ``False``.

Fix: bound every segment/chunk to the buffer; on a malformed/overrunning
length, stop WITHOUT emitting a verbatim tail (mirror ``_strip_webp``'s
rebuild-from-recognized-chunks approach). These tests craft the overrun-before-
Exif layout for both formats and assert the GPS/Exif bytes are absent, plus a
sanity check that normal valid images pass through intact.
"""

from __future__ import annotations

import struct

from akana_server.multimodal.exif import strip_location_metadata
from akana_server.multimodal.sniff import sniff_format


# -- valid-image builders (mirror tests/unit/test_multimodal_edges.py) ----------------


def _valid_jpeg(*, with_exif: bool = False, gps: bytes = b"GPSDATA") -> bytes:
    soi = b"\xff\xd8"
    sof_p = b"\x08" + struct.pack(">HH", 8, 8) + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    sof = b"\xff\xc0" + struct.pack(">H", len(sof_p) + 2) + sof_p
    sos = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
    parts = [soi]
    if with_exif:
        ep = b"Exif\x00\x00" + gps
        parts.append(b"\xff\xe1" + struct.pack(">H", len(ep) + 2) + ep)
    parts += [sof, sos, b"\x00\x00"]
    return b"".join(parts)


def _valid_png(w: int = 4, h: int = 4) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + struct.pack(">II", w, h)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00" * 4  # CRC placeholder (sniff/dims do not check it)
        + b"\x00\x00\x00\x00IEND\xae\x42\x60\x82"
    )


# -- JPEG: oversized non-APP1 segment BEFORE an APP1 Exif segment ---------------------


def test_jpeg_oversized_segment_before_exif_no_gps_leak() -> None:
    """An APP0 whose declared length overruns EOF must not drag a following
    APP1 Exif/GPS segment into the stripped output."""
    gps = b"40.7128N,74.0060W"
    exif = b"Exif\x00\x00" + gps
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
    # APP0 (0xE0, non-APP1) declares the max 16-bit length → end overruns EOF.
    bad_app0 = b"\xff\xe0" + struct.pack(">H", 0xFFFF)
    data = b"\xff\xd8" + bad_app0 + app1
    assert sniff_format(data) == "jpeg"

    r = strip_location_metadata(data, "jpeg")

    assert gps not in r.data
    assert b"Exif" not in r.data


def test_jpeg_broken_marker_before_exif_no_gps_leak() -> None:
    """A mis-sized preceding segment that leaves the cursor on a non-0xFF byte
    must not fall through to a verbatim tail copy that contains the Exif block."""
    gps = b"48.8566N,2.3522E"
    exif = b"Exif\x00\x00" + gps
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
    # Valid SOI + a well-formed APP0 (so the bytes sniff as JPEG: ff d8 ff e0…),
    # then a non-0xFF byte where the NEXT marker is expected, then the real
    # Exif/GPS segment. Pre-fix, the walker fell through to a verbatim tail copy
    # at the broken marker, dragging the Exif block into the "stripped" output.
    app0 = b"\xff\xe0" + struct.pack(">H", 4) + b"\x00\x00"
    data = b"\xff\xd8" + app0 + b"\x00\x00" + app1
    assert sniff_format(data) == "jpeg"

    r = strip_location_metadata(data, "jpeg")

    assert gps not in r.data
    assert b"Exif" not in r.data


# -- PNG: overrunning non-eXIf chunk BEFORE an eXIf chunk -----------------------------


def test_png_overrunning_non_exif_chunk_before_exif_no_gps_leak() -> None:
    """A tEXt chunk whose declared length overruns EOF must not drag a following
    eXIf chunk into the stripped output (the overrun tail-drop must fire for ANY
    ctype, not only eXIf)."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">I", 13) + b"IHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00"
    # Non-eXIf chunk (tEXt) with a length that overruns EOF.
    bad = struct.pack(">I", 0xFFFFFFFF) + b"tEXt" + b"x"
    gps = b"GPS_40N_74W!"
    exif = struct.pack(">I", len(gps)) + b"eXIf" + gps + b"\x00\x00\x00\x00"
    data = sig + ihdr + bad + exif
    assert sniff_format(data) == "png"

    r = strip_location_metadata(data, "png")

    assert gps not in r.data
    assert b"eXIf" not in r.data


# -- JPEG: Exif segment appended AFTER the SOS scan data ------------------------------


def test_jpeg_exif_after_sos_no_gps_leak() -> None:
    """An APP1 Exif/GPS segment appended after the SOS entropy data must be
    dropped — the post-SOS remainder must not be copied verbatim. The entropy
    (pixel) stream itself is still preserved."""
    gps = b"40.7128N,74.0060W"
    exif = b"Exif\x00\x00" + gps
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
    data = _valid_jpeg(with_exif=False) + app1  # …SOS + entropy + trailing APP1
    assert sniff_format(data) == "jpeg"

    r = strip_location_metadata(data, "jpeg")

    assert gps not in r.data
    assert b"Exif" not in r.data
    assert r.stripped is True
    assert b"\xff\xda" in r.data  # SOS (entropy data) preserved


# -- PNG: eXIf chunk placed AFTER the IEND terminator ---------------------------------


def test_png_exif_after_iend_no_gps_leak() -> None:
    """An eXIf/GPS chunk placed after IEND must be dropped — the walker must not
    copy the post-IEND remainder verbatim once it has emitted IEND."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">I", 13) + b"IHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00"
    iend = struct.pack(">I", 0) + b"IEND" + b"\xae\x42\x60\x82"
    gps = b"40.7128N,74.0060W"
    exif = struct.pack(">I", len(gps)) + b"eXIf" + gps + b"\x00\x00\x00\x00"
    data = sig + ihdr + iend + exif
    assert sniff_format(data) == "png"

    r = strip_location_metadata(data, "png")

    assert gps not in r.data
    assert b"eXIf" not in r.data
    assert r.stripped is True
    assert b"IEND" in r.data  # the real terminator is still emitted


# -- sanity: valid images pass through intact -----------------------------------------


def test_valid_jpeg_without_exif_unchanged() -> None:
    data = _valid_jpeg(with_exif=False)
    r = strip_location_metadata(data, "jpeg")
    assert r.data == data
    assert r.stripped is False


def test_valid_jpeg_with_exif_pixels_preserved_gps_gone() -> None:
    gps = b"51.5074N,0.1278W"
    data = _valid_jpeg(with_exif=True, gps=gps)
    r = strip_location_metadata(data, "jpeg")
    # Exif/GPS dropped, but pixel/entropy tail (SOS) preserved and still valid.
    assert gps not in r.data
    assert b"Exif" not in r.data
    assert r.stripped is True
    assert r.data.startswith(b"\xff\xd8")  # SOI intact
    assert b"\xff\xda" in r.data  # SOS (entropy data) preserved


def test_valid_png_unchanged() -> None:
    data = _valid_png()
    r = strip_location_metadata(data, "png")
    assert r.data == data
    assert r.stripped is False


# -- PNG: XMP location metadata (GPS in an iTXt chunk) ---------------------------------


def _png_itxt_xmp(xmp_text: bytes) -> bytes:
    # iTXt: keyword\0 + compression_flag + compression_method + language_tag\0
    #       + translated_keyword\0 + text
    chunk_data = (
        b"XML:com.adobe.xmp\x00"
        + b"\x00\x00"  # not compressed
        + b"\x00"  # empty language tag
        + b"\x00"  # empty translated keyword
        + xmp_text
    )
    return struct.pack(">I", len(chunk_data)) + b"iTXt" + chunk_data + b"\x00\x00\x00\x00"


def test_png_itxt_xmp_gps_stripped() -> None:
    """Lightroom/Photoshop store XMP GPS in a PNG iTXt 'XML:com.adobe.xmp' chunk;
    it must be dropped like JPEG APP1 and WebP 'XMP ' — not kept as free text."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">I", 13) + b"IHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00"
    xmp = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF>'
        b"<exif:GPSLatitude>40,42.768N</exif:GPSLatitude>"
        b"<exif:GPSLongitude>74,0.360W</exif:GPSLongitude>"
        b"</rdf:RDF></x:xmpmeta>"
    )
    iend = struct.pack(">I", 0) + b"IEND" + b"\xae\x42\x60\x82"
    data = sig + ihdr + _png_itxt_xmp(xmp) + iend
    assert sniff_format(data) == "png"

    r = strip_location_metadata(data, "png")

    assert b"GPSLatitude" not in r.data
    assert b"40,42.768N" not in r.data
    assert b"XML:com.adobe.xmp" not in r.data
    assert r.stripped is True
    assert b"IHDR" in r.data  # image structure preserved
    assert b"IEND" in r.data


def test_png_non_xmp_itxt_kept() -> None:
    """A non-XMP iTXt (ordinary free-text metadata) must NOT be dropped —
    only the XMP location packet is targeted."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">I", 13) + b"IHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00"
    kw = b"Comment\x00" + b"\x00\x00" + b"\x00" + b"\x00" + b"hello world"
    itxt = struct.pack(">I", len(kw)) + b"iTXt" + kw + b"\x00\x00\x00\x00"
    iend = struct.pack(">I", 0) + b"IEND" + b"\xae\x42\x60\x82"
    data = sig + ihdr + itxt + iend

    r = strip_location_metadata(data, "png")

    assert b"hello world" in r.data
    assert r.stripped is False
