"""Magic-bytes format detection + stdlib size (width/height) extraction.

The extension is NEVER trusted: the format is detected only from the file's
first bytes (png/jpeg/gif/webp). Dimensions are also read without PIL, with pure
byte parsing — PIL is not in the repo and F0 does not take a new pip dependency.
"""

from __future__ import annotations

import struct

#: Supported format → canonical file extension.
FORMAT_EXTENSIONS: dict[str, str] = {
    "png": "png",
    "jpeg": "jpg",
    "gif": "gif",
    "webp": "webp",
}

#: Format → MIME (for raw serving + provider preparation).
FORMAT_MEDIA_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def sniff_format(data: bytes) -> str | None:
    """Format detection from content; unknown/broken header → ``None``.

    Looks only at the first bytes — an HTML file named `something.png` is
    rejected here (magic-bytes rejection).
    """
    if data.startswith(_PNG_SIGNATURE):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 16 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Height/width from the SOF segment (C0-CF; excluding C4/C8/CC)."""
    pos = 2
    n = len(data)
    while pos + 4 <= n:
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:  # padding byte
            pos += 1
            continue
        if marker in (0x01,) or 0xD0 <= marker <= 0xD9:  # standalone marker
            pos += 2
            continue
        if pos + 4 > n:
            return None
        length = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if pos + 9 > n:
                return None
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            return (width, height)
        if marker == 0xDA:  # SOS → entropy data; no SOF was seen
            return None
        pos += 2 + length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    fourcc = data[12:16]
    payload = data[20:]
    if fourcc == b"VP8X" and len(payload) >= 10:
        width = int.from_bytes(payload[4:7], "little") + 1
        height = int.from_bytes(payload[7:10], "little") + 1
        return (width, height)
    if fourcc == b"VP8 " and len(payload) >= 10:
        if payload[3:6] != b"\x9d\x01\x2a":
            return None
        width = struct.unpack("<H", payload[6:8])[0] & 0x3FFF
        height = struct.unpack("<H", payload[8:10])[0] & 0x3FFF
        return (width, height)
    if fourcc == b"VP8L" and len(payload) >= 5:
        if payload[0] != 0x2F:
            return None
        bits = struct.unpack("<I", payload[1:5])[0]
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return (width, height)
    return None


def image_dimensions(data: bytes, fmt: str) -> tuple[int, int] | None:
    """(width, height) — ``None`` if it cannot be read (the record is still kept)."""
    try:
        if fmt == "png":
            if len(data) < 24 or data[12:16] != b"IHDR":
                return None
            width, height = struct.unpack(">II", data[16:24])
            return (width, height)
        if fmt == "gif":
            if len(data) < 10:
                return None
            width, height = struct.unpack("<HH", data[6:10])
            return (width, height)
        if fmt == "jpeg":
            return _jpeg_dimensions(data)
        if fmt == "webp":
            return _webp_dimensions(data)
    except struct.error:
        return None
    return None
