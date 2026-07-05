"""Adversarial probes for the magic-bytes sniffer + PIL-free dimension readers.

``sniff.py`` is a trust boundary for uploaded files: the extension is NEVER
trusted, the format is decided purely from the first bytes, and dimensions are
parsed by hand (no PIL). Two failure modes matter:

* **prefix/format confusion** — a non-image whose header *almost* matches (RIFF
  WAV vs RIFF WEBP, an off-by-one PNG signature) must NOT be accepted as an
  image, and
* **malformed-input robustness** — a hostile JPEG (huge segment lengths, all
  ``0xFF`` fill, zero-length segment spam, truncation) must terminate and return
  ``None`` rather than hang, overrun, or crash. ``None`` is a *safe* answer here:
  the contract is "dimensions unknown, keep the record."

These exercise ``image_dimensions`` / ``_jpeg_dimensions`` / ``_webp_dimensions``
directly — the bulk of the byte-parsing surface that the engine-level tests only
touch through the happy path. A failure here is a real containment/availability
bug, not a style nit.
"""

from __future__ import annotations

import struct

import pytest

from akana_server.multimodal.sniff import (
    FORMAT_EXTENSIONS,
    FORMAT_MEDIA_TYPES,
    _jpeg_dimensions,
    _webp_dimensions,
    image_dimensions,
    sniff_format,
)


# --------------------------------------------------------------------------- #
# Byte-exact constructors for each format (only what the parser actually reads).
# --------------------------------------------------------------------------- #


def _png(w: int, h: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + struct.pack(">II", w, h)
        + b"\x08\x06\x00\x00\x00"
    )


def _gif(w: int, h: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 6


def _jpeg_sof(w: int, h: int, *, marker: int = 0xC0) -> bytes:
    """SOI + a single SOFn segment (precision, height, width, 1 component)."""
    payload = b"\x08" + struct.pack(">HH", h, w) + b"\x01\x11\x00"
    seg = bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload
    return b"\xff\xd8" + seg


def _jpeg_seg(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def _webp(fourcc: bytes, payload: bytes) -> bytes:
    body = fourcc + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


def _webp_vp8x(w: int, h: int) -> bytes:
    payload = (
        b"\x00\x00\x00\x00"
        + (w - 1).to_bytes(3, "little")
        + (h - 1).to_bytes(3, "little")
        + b"\x00"
    )
    return _webp(b"VP8X", payload)


def _webp_vp8(w: int, h: int, *, start: bytes = b"\x9d\x01\x2a") -> bytes:
    payload = b"\x00\x00\x00" + start + struct.pack("<H", w) + struct.pack("<H", h) + b"\x00" * 4
    return _webp(b"VP8 ", payload)


def _webp_vp8l(w: int, h: int, *, sig: int = 0x2F) -> bytes:
    bits = ((w - 1) & 0x3FFF) | (((h - 1) & 0x3FFF) << 14)
    payload = bytes([sig]) + struct.pack("<I", bits) + b"\x00" * 8
    return _webp(b"VP8L", payload)


# --------------------------------------------------------------------------- #
# sniff_format — the magic-bytes trust boundary. No format/prefix confusion.    #
# --------------------------------------------------------------------------- #


def test_each_format_detected_from_content() -> None:
    assert sniff_format(_png(4, 4)) == "png"
    assert sniff_format(b"\xff\xd8\xff\xe0") == "jpeg"
    assert sniff_format(b"GIF87a....") == "gif"
    assert sniff_format(b"GIF89a....") == "gif"
    assert sniff_format(_webp_vp8(8, 8)) == "webp"


def test_riff_but_not_webp_is_rejected() -> None:
    """A RIFF container that is NOT WebP (WAV/AVI) must not be mistaken for one —
    the ``WEBP`` tag at offset 8 is what disambiguates, not the ``RIFF`` prefix."""
    assert sniff_format(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None
    assert sniff_format(b"RIFF\x00\x00\x00\x00AVI LIST") is None


def test_webp_requires_sixteen_bytes() -> None:
    """The webp branch needs len>=16 (RIFF+size+WEBP+room); a 12–15 byte header,
    even with the right tags, is treated as not-an-image (fail closed)."""
    assert sniff_format(b"RIFF\x00\x00\x00\x00WEBP") is None  # exactly 12
    assert sniff_format(b"RIFF\x00\x00\x00\x00WEBPVP") is None  # 14 < 16
    assert sniff_format(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"  # 16 ✓


def test_png_signature_is_exact_no_off_by_one() -> None:
    assert sniff_format(b"\x89PNG\r\n\x1a\n") == "png"
    # last signature byte flipped → not a PNG
    assert sniff_format(b"\x89PNG\r\n\x1a\x00") is None
    # first byte flipped → not a PNG
    assert sniff_format(b"\x88PNG\r\n\x1a\n") is None


def test_jpeg_needs_three_byte_soi_marker() -> None:
    assert sniff_format(b"\xff\xd8\xff") == "jpeg"
    assert sniff_format(b"\xff\xd8") is None  # 2 bytes is not enough
    assert sniff_format(b"\xff\xd9\xff") is None  # EOI, not SOI


def test_gif_variant_must_match_exactly() -> None:
    assert sniff_format(b"GIF88a") is None  # not a real GIF version tag
    assert sniff_format(b"gif89a") is None  # case-sensitive


@pytest.mark.parametrize("blob", [b"", b"\x89", b"\xff", b"GIF", b"RIFF"])
def test_short_or_empty_input_is_not_an_image(blob: bytes) -> None:
    assert sniff_format(blob) is None


def test_spoofed_extension_content_is_html_rejected() -> None:
    """The whole point: content, not name, decides. HTML bytes → not an image."""
    assert sniff_format(b"<!DOCTYPE html><html></html>") is None
    assert sniff_format(b"<html>totally a png trust me</html>") is None


# --------------------------------------------------------------------------- #
# image_dimensions: PNG / GIF — header math, truncation tolerance.              #
# --------------------------------------------------------------------------- #


def test_png_dimensions_read_from_ihdr() -> None:
    assert image_dimensions(_png(1920, 1080), "png") == (1920, 1080)


def test_png_without_ihdr_tag_returns_none() -> None:
    # right length but the IHDR tag is corrupted → refuse to guess
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dXXXX" + struct.pack(">II", 5, 5) + b"\x00" * 5
    assert image_dimensions(blob, "png") is None


def test_png_truncated_before_dimensions_returns_none() -> None:
    assert image_dimensions(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR", "png") is None


def test_png_huge_declared_dimensions_are_returned_verbatim() -> None:
    """No sanity cap (by design — this module only reads the header integers, it
    never allocates from them, so there is no decompression-bomb surface here)."""
    assert image_dimensions(_png(0xFFFFFFFF, 0xFFFFFFFF), "png") == (0xFFFFFFFF, 0xFFFFFFFF)


def test_gif_dimensions_are_little_endian() -> None:
    assert image_dimensions(_gif(320, 240), "gif") == (320, 240)


def test_gif_truncated_returns_none() -> None:
    assert image_dimensions(b"GIF89a\x01", "gif") is None


# --------------------------------------------------------------------------- #
# image_dimensions: JPEG SOF walker — markers, truncation, hostile lengths.     #
# --------------------------------------------------------------------------- #


def test_jpeg_sof0_dimensions() -> None:
    assert image_dimensions(_jpeg_sof(100, 200), "jpeg") == (100, 200)


def test_jpeg_app0_segment_is_skipped_then_sof_found() -> None:
    app0 = _jpeg_seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    data = b"\xff\xd8" + app0 + _jpeg_sof(640, 480)[2:]
    assert image_dimensions(data, "jpeg") == (640, 480)


def test_jpeg_dht_marker_is_not_mistaken_for_sof() -> None:
    """0xC4 (DHT) sits inside the 0xC0–0xCF range but is NOT a frame header; it
    must be length-skipped, and the real SOF after it is what gives dimensions."""
    dht = _jpeg_seg(0xC4, b"\x00\x01\x02\x03")
    data = b"\xff\xd8" + dht + _jpeg_sof(50, 70)[2:]
    assert image_dimensions(data, "jpeg") == (50, 70)


@pytest.mark.parametrize("marker", [0xC4, 0xC8, 0xCC])
def test_jpeg_non_frame_c_markers_excluded(marker: int) -> None:
    """C4/C8/CC (DHT/JPG/DAC) are the documented exclusions — a segment with one
    of these and no real SOF yields no dimensions."""
    seg = _jpeg_seg(marker, b"\x00\x01\x02\x03")
    assert _jpeg_dimensions(b"\xff\xd8" + seg + b"\xff\xd9") is None


def test_jpeg_sos_before_sof_returns_none() -> None:
    """Reaching SOS (start-of-scan) means we are entering entropy data with no
    frame header seen — give up rather than parse garbage as dimensions."""
    sos = _jpeg_seg(0xDA, b"\x01\x00")
    assert _jpeg_dimensions(b"\xff\xd8" + sos + _jpeg_sof(9, 9)[2:]) is None


def test_jpeg_standalone_rst_markers_are_skipped() -> None:
    """RST0–RST7 (0xD0–0xD7) have no length payload; the walker must step over
    them by 2, not try to read a segment length."""
    rsts = b"\xff\xd0\xff\xd3\xff\xd7"
    data = b"\xff\xd8" + rsts + _jpeg_sof(11, 22)[2:]
    assert image_dimensions(data, "jpeg") == (11, 22)


def test_jpeg_no_sof_returns_none() -> None:
    assert image_dimensions(b"\xff\xd8\xff\xe0\x00\x04ab\xff\xd9", "jpeg") is None


def test_jpeg_truncated_sof_returns_none() -> None:
    # SOF marker + length claim but bytes run out before width/height
    assert _jpeg_dimensions(b"\xff\xd8\xff\xc0\x00\x11\x08") is None


# -- hostile JPEGs must terminate (no hang / overrun / crash) → None ----------- #


def test_jpeg_oversized_segment_length_does_not_overrun() -> None:
    # APP0 claims 0xFFFF bytes but only a few follow → walk past EOF, return None
    assert _jpeg_dimensions(b"\xff\xd8\xff\xe0\xff\xffPAYLOAD") is None


def test_jpeg_zero_length_segment_spam_terminates() -> None:
    # 100k zero-length APP0 segments — must not infinite-loop
    hostile = b"\xff\xd8" + b"\xff\xe0\x00\x00" * 100_000
    assert _jpeg_dimensions(hostile) is None


def test_jpeg_all_fill_bytes_terminates() -> None:
    # a sea of 0xFF padding bytes (each advances pos by 1) must still finish
    assert _jpeg_dimensions(b"\xff\xd8" + b"\xff" * 200_000) is None


def test_jpeg_marker_not_preceded_by_ff_returns_none() -> None:
    # walker requires 0xFF at each marker position; a stray non-0xFF aborts
    assert _jpeg_dimensions(b"\xff\xd8\x00\xc0\x00\x11") is None


# --------------------------------------------------------------------------- #
# image_dimensions: WebP — VP8X / VP8 (lossy) / VP8L (lossless) bit parsing.    #
# --------------------------------------------------------------------------- #


def test_webp_vp8x_canvas_dimensions() -> None:
    assert image_dimensions(_webp_vp8x(1024, 768), "webp") == (1024, 768)


def test_webp_vp8_lossy_dimensions() -> None:
    assert image_dimensions(_webp_vp8(800, 600), "webp") == (800, 600)


def test_webp_vp8_lossy_bad_start_code_returns_none() -> None:
    """The 3-byte VP8 start code ``9d 01 2a`` gates the parse; a wrong one is a
    malformed/forged frame → no dimensions."""
    assert _webp_dimensions(_webp_vp8(800, 600, start=b"\xde\xad\xbe")) is None


def test_webp_vp8l_lossless_dimensions() -> None:
    assert image_dimensions(_webp_vp8l(640, 480), "webp") == (640, 480)


def test_webp_vp8l_bad_signature_returns_none() -> None:
    """VP8L must start with the 0x2F signature byte; anything else → None."""
    assert _webp_dimensions(_webp_vp8l(640, 480, sig=0x99)) is None


def test_webp_dimension_mask_is_14_bits() -> None:
    """VP8 dims are 14-bit fields; the high 2 bits are flags and must be masked
    off, so 0x3FFF (16383) is the max and bit 15/16 noise does not leak in."""
    assert image_dimensions(_webp_vp8(0x3FFF, 0x3FFF), "webp") == (0x3FFF, 0x3FFF)


def test_webp_too_short_returns_none() -> None:
    assert _webp_dimensions(b"RIFF\x00\x00\x00\x00WEBPVP8 \x00\x00") is None


def test_webp_unknown_fourcc_returns_none() -> None:
    # a RIFF/WEBP whose chunk is neither VP8/VP8X/VP8L
    assert _webp_dimensions(_webp(b"ICCP", b"\x00" * 20)) is None


# --------------------------------------------------------------------------- #
# image_dimensions: format/argument mismatches degrade to None, never crash.    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("fmt", ["bmp", "tiff", "", "PNG", "jpg"])
def test_unknown_format_argument_returns_none(fmt: str) -> None:
    assert image_dimensions(_png(4, 4), fmt) is None


@pytest.mark.parametrize("fmt", ["png", "gif", "jpeg", "webp"])
def test_empty_data_returns_none_for_every_format(fmt: str) -> None:
    assert image_dimensions(b"", fmt) is None


def test_png_one_byte_short_of_dimensions_returns_none() -> None:
    # IHDR tag present but the 8 dimension bytes are one short (23 bytes total) —
    # the len>=24 guard refuses rather than unpacking a partial >II pair.
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00\x00\x00\x05\x00\x00\x00"
    assert len(blob) == 23
    assert image_dimensions(blob, "png") is None


# --------------------------------------------------------------------------- #
# Format tables stay in lock-step (a new format must land in both maps).        #
# --------------------------------------------------------------------------- #


def test_extension_and_media_type_tables_cover_same_formats() -> None:
    assert set(FORMAT_EXTENSIONS) == set(FORMAT_MEDIA_TYPES)
    assert set(FORMAT_EXTENSIONS) == {"png", "jpeg", "gif", "webp"}
    # every declared media type is an image/* type
    assert all(mt.startswith("image/") for mt in FORMAT_MEDIA_TYPES.values())
