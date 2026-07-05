"""MultimodalEngine F0 — ImageStore (magic-bytes, size, EXIF, dedup) + provider matrix.

Test images are handcrafted with the stdlib (PIL is not in the repo): the
header/segment structures that the sniff/exif layers read are built exactly.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from akana_server.multimodal import (
    ImageStore,
    ImageStoreError,
    prepare_files,
    prepare_for_provider,
)
from akana_server.multimodal.exif import strip_location_metadata
from akana_server.multimodal.sniff import image_dimensions, sniff_format

# --------------------------------------------------------------------------- #
# Handcrafted images
# --------------------------------------------------------------------------- #


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + ctype
        + data
        + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    )


def make_png(width: int = 2, height: int = 1, *, with_exif: bool = False) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xab\xcd\xef" * width for _ in range(height))
    body = _png_chunk(b"IHDR", ihdr)
    if with_exif:
        body += _png_chunk(b"eXIf", b"MM\x00*" + b"\x00" * 16)  # fake TIFF/GPS
    body += _png_chunk(b"IDAT", zlib.compress(raw)) + _png_chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + body


def make_jpeg(width: int = 4, height: int = 3, *, with_exif: bool = False) -> bytes:
    out = b"\xff\xd8"  # SOI
    out += b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    if with_exif:
        payload = b"Exif\x00\x00MM\x00*" + b"\x00" * 24  # GPS IFD stand-in
        out += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    out += (
        b"\xff\xc0"
        + struct.pack(">H", 11)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x01\x01\x11\x00"
    )
    out += b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00"
    out += b"\x12\x34"  # entropy filler
    out += b"\xff\xd9"  # EOI
    return out


def make_gif(width: int = 5, height: int = 7) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00" + b"\x3b"


def _webp_chunk(fourcc: bytes, payload: bytes) -> bytes:
    chunk = fourcc + struct.pack("<I", len(payload)) + payload
    if len(payload) % 2:
        chunk += b"\x00"
    return chunk


def make_webp(width: int = 6, height: int = 2, *, with_exif: bool = False) -> bytes:
    flags = 0x08 if with_exif else 0x00
    vp8x = (
        bytes([flags])
        + b"\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    body = _webp_chunk(b"VP8X", vp8x)
    if with_exif:
        body += _webp_chunk(b"EXIF", b"II*\x00" + b"\x00" * 9)  # odd length → padded
    riff = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body
    return riff


@pytest.fixture
def store(tmp_path: Path) -> ImageStore:
    return ImageStore(tmp_path, max_mb=1.0)


# --------------------------------------------------------------------------- #
# Magic-bytes + extension + size
# --------------------------------------------------------------------------- #


def test_magic_bytes_reddi_png_uzantili_html(store: ImageStore) -> None:
    """Extension is .png but content is HTML → content wins, the record is rejected."""
    html = b"<!DOCTYPE html><html><body>zararli</body></html>"
    with pytest.raises(ImageStoreError) as exc:
        store.save(html, original_name="masum.png")
    assert exc.value.code == "UNSUPPORTED_MEDIA"
    assert list(store._uploads_dir.iterdir()) == []  # nothing touched the disk


def test_izinsiz_uzanti_reddi(store: ImageStore) -> None:
    with pytest.raises(ImageStoreError) as exc:
        store.save(make_png(), original_name="resim.svg")
    assert exc.value.code == "UNSUPPORTED_EXTENSION"


def test_bos_dosya_reddi(store: ImageStore) -> None:
    with pytest.raises(ImageStoreError) as exc:
        store.save(b"", original_name="bos.png")
    assert exc.value.code == "EMPTY_FILE"


def make_big_png(payload_bytes: int = 4096) -> bytes:
    """PNG with an incompressible (random) IDAT — for size-limit tests."""
    import random

    ihdr = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    noise = zlib.compress(bytes(random.Random(0).randbytes(payload_bytes)))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", noise)
        + _png_chunk(b"IEND", b"")
    )


def test_boyut_siniri_asilirsa_red(tmp_path: Path) -> None:
    kucuk = ImageStore(tmp_path, max_mb=0.001)  # ~1048 bytes
    buyuk_png = make_big_png()
    assert len(buyuk_png) > kucuk.max_bytes
    with pytest.raises(ImageStoreError) as exc:
        kucuk.save(buyuk_png, original_name="buyuk.png")
    assert exc.value.code == "FILE_TOO_LARGE"


def test_sniff_format_matrisi() -> None:
    assert sniff_format(make_png()) == "png"
    assert sniff_format(make_jpeg()) == "jpeg"
    assert sniff_format(make_gif()) == "gif"
    assert sniff_format(make_webp()) == "webp"
    assert sniff_format(b"PK\x03\x04zip") is None


# --------------------------------------------------------------------------- #
# Meta extraction (dimensions) + record
# --------------------------------------------------------------------------- #


def test_meta_cikarimi_boyutlar() -> None:
    assert image_dimensions(make_png(2, 1), "png") == (2, 1)
    assert image_dimensions(make_jpeg(4, 3), "jpeg") == (4, 3)
    assert image_dimensions(make_gif(5, 7), "gif") == (5, 7)
    assert image_dimensions(make_webp(6, 2), "webp") == (6, 2)


def test_kayit_meta_ve_dosya(store: ImageStore) -> None:
    record, dedup = store.save(make_jpeg(4, 3), original_name="Foto Bir.JPG")
    assert not dedup
    assert record.format == "jpeg"
    assert record.ext == "jpg"  # extension from the sniff, not from the name
    assert (record.width, record.height) == (4, 3)
    assert record.original_name == "Foto Bir.JPG"
    path = store.file_path(record)
    assert path.is_file()
    assert path.name == f"{record.id}.jpg"
    assert record.size_bytes == path.stat().st_size


# --------------------------------------------------------------------------- #
# EXIF stripping
# --------------------------------------------------------------------------- #


def test_jpeg_exif_soyulur(store: ImageStore) -> None:
    record, _ = store.save(make_jpeg(with_exif=True), original_name="gps.jpg")
    assert record.exif_stripped
    stored = store.file_path(record).read_bytes()
    assert b"Exif" not in stored
    assert b"\xff\xe1" not in stored  # no APP1 left
    # pixel structure intact: dimensions are still readable
    assert image_dimensions(stored, "jpeg") == (4, 3)


def test_png_exif_chunk_soyulur(store: ImageStore) -> None:
    record, _ = store.save(make_png(with_exif=True), original_name="gps.png")
    assert record.exif_stripped
    stored = store.file_path(record).read_bytes()
    assert b"eXIf" not in stored
    assert stored == make_png(with_exif=False)  # remaining bytes are exact


def test_webp_exif_chunk_soyulur_ve_vp8x_bayragi_temizlenir(store: ImageStore) -> None:
    record, _ = store.save(make_webp(with_exif=True), original_name="gps.webp")
    assert record.exif_stripped
    stored = store.file_path(record).read_bytes()
    assert b"EXIF" not in stored
    assert stored == make_webp(with_exif=False)  # flag + RIFF size corrected
    assert image_dimensions(stored, "webp") == (6, 2)


def test_gif_exif_tasimaz_noop() -> None:
    data = make_gif()
    result = strip_location_metadata(data, "gif")
    assert result.data == data
    assert not result.stripped


def test_exifsiz_jpeg_stripped_false(store: ImageStore) -> None:
    record, _ = store.save(make_jpeg(with_exif=False), original_name="temiz.jpg")
    assert not record.exif_stripped
    assert record.exif_note == "jpeg: no Exif segment"


# --------------------------------------------------------------------------- #
# Dedup + append-only
# --------------------------------------------------------------------------- #


def test_dedup_ayni_icerik_tek_kayit(store: ImageStore) -> None:
    r1, d1 = store.save(make_png(), original_name="a.png")
    r2, d2 = store.save(make_png(), original_name="kopya.png")
    assert not d1 and d2
    assert r1.id == r2.id
    assert len(list(store._uploads_dir.iterdir())) == 1


def test_dedup_exifli_ve_exifsiz_ayni_kayda_duser(store: ImageStore) -> None:
    """Hash is over the STRIPPED content: an EXIF copy does not open a new record."""
    r1, _ = store.save(make_png(with_exif=False), original_name="temiz.png")
    r2, dedup = store.save(make_png(with_exif=True), original_name="gpsli.png")
    assert dedup
    assert r1.id == r2.id


def test_append_only_olay_logu(store: ImageStore) -> None:
    record, _ = store.save(make_gif(), original_name="a.gif")
    store.save(make_gif(), original_name="b.gif")  # dedup
    assert store.disable(record.id)
    assert not store.disable(record.id)  # idempotent no-op
    actions = [e["action"] for e in store.events(record.id)]
    assert actions == ["created", "dedup_hit", "disabled"]
    # the row was not deleted, it was deactivated; the file stays on disk
    rec = store.get(record.id)
    assert rec is not None and rec.disabled
    assert store.file_path(rec).is_file()


# --------------------------------------------------------------------------- #
# Provider preparation matrix
# --------------------------------------------------------------------------- #


def test_prepare_claude_dosya_yolu(store: ImageStore) -> None:
    record, _ = store.save(make_png(), original_name="a.png")
    ref = prepare_for_provider(store, record.id, "claude")
    assert ref.supported and ref.provider_native
    assert ref.kind == "image"
    assert ref.path == str(store.file_path(record))
    assert Path(ref.path).is_absolute() and Path(ref.path).is_file()
    assert ref.media_type == "image/png"


def test_prepare_cursor_da_native_yol(store: ImageStore) -> None:
    # Empirically verified: the cursor SDK agent also reads the file from an
    # absolute path → native like claude (the old "unsupported" assumption was wrong).
    record, _ = store.save(make_png(), original_name="a.png")
    ref = prepare_for_provider(store, record.id, "cursor")
    assert ref.supported and ref.provider_native
    assert ref.path == str(store.file_path(record))
    assert Path(ref.path).is_absolute() and Path(ref.path).is_file()
    assert ref.media_type == "image/png"


def test_prepare_bilinmeyen_provider_desteklenmiyor(store: ImageStore) -> None:
    record, _ = store.save(make_png(), original_name="a.png")
    ref = prepare_for_provider(store, record.id, "ollama")
    assert not ref.supported
    assert "unknown provider" in ref.note


def test_prepare_kayit_yok_ve_pasif(store: ImageStore) -> None:
    with pytest.raises(ImageStoreError) as exc:
        prepare_for_provider(store, "yok-boyle-id", "claude")
    assert exc.value.code == "IMAGE_NOT_FOUND"

    record, _ = store.save(make_png(), original_name="a.png")
    store.disable(record.id)
    with pytest.raises(ImageStoreError) as exc2:
        prepare_for_provider(store, record.id, "claude")
    assert exc2.value.code == "IMAGE_DISABLED"


def test_prepare_dosya_diskten_silinmisse(store: ImageStore) -> None:
    record, _ = store.save(make_png(), original_name="a.png")
    store.file_path(record).unlink()
    with pytest.raises(ImageStoreError) as exc:
        prepare_for_provider(store, record.id, "claude")
    assert exc.value.code == "FILE_MISSING"


# --------------------------------------------------------------------------- #
# F1 — multi-type acceptance (text/code/pdf/docx/xlsx/zip)
# --------------------------------------------------------------------------- #


def _make_zip(*, member: str = "word/document.xml") -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", b"<Types/>")
        zf.writestr(member, b"<doc/>")
    return buf.getvalue()


def test_kabul_metin_txt(store: ImageStore) -> None:
    record, _ = store.save(b"merhaba dunya\nikinci satir", original_name="not.txt")
    assert record.kind == "text"
    assert record.ext == "txt"
    assert record.exif_stripped is False
    assert store.file_path(record).read_bytes() == b"merhaba dunya\nikinci satir"


def test_kabul_kod_py(store: ImageStore) -> None:
    record, _ = store.save(b"def f():\n    return 1\n", original_name="m.py")
    assert record.kind == "text" and record.ext == "py"


def test_kabul_csv_ve_json(store: ImageStore) -> None:
    r1, _ = store.save(b"a,b,c\n1,2,3\n", original_name="veri.csv")
    r2, _ = store.save(b'{"x": 1}', original_name="conf.json")
    assert r1.kind == "text" and r1.ext == "csv"
    assert r2.kind == "text" and r2.ext == "json"


def test_kabul_pdf_magic(store: ImageStore) -> None:
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
    record, _ = store.save(pdf, original_name="rapor.pdf")
    assert record.kind == "pdf" and record.ext == "pdf"
    assert record.media_type == "application/pdf"


def test_kabul_docx_zip_magic(store: ImageStore) -> None:
    record, _ = store.save(_make_zip(), original_name="belge.docx")
    assert record.kind == "docx"
    assert "wordprocessingml" in record.media_type


def test_kabul_xlsx_zip_magic(store: ImageStore) -> None:
    record, _ = store.save(_make_zip(member="xl/workbook.xml"), original_name="tablo.xlsx")
    assert record.kind == "xlsx"
    assert "spreadsheetml" in record.media_type


def test_kabul_zip_arsiv(store: ImageStore) -> None:
    record, _ = store.save(_make_zip(), original_name="paket.zip")
    assert record.kind == "zip" and record.ext == "zip"


def test_red_izinsiz_uzanti_exe(store: ImageStore) -> None:
    with pytest.raises(ImageStoreError) as exc:
        store.save(b"MZ\x90\x00binari", original_name="virus.exe")
    assert exc.value.code == "UNSUPPORTED_EXTENSION"


def test_red_pdf_uzanti_ama_pdf_degil(store: ImageStore) -> None:
    with pytest.raises(ImageStoreError) as exc:
        store.save(b"not a pdf at all", original_name="sahte.pdf")
    assert exc.value.code == "UNSUPPORTED_MEDIA"


def test_red_txt_uzanti_ama_binari_nul(store: ImageStore) -> None:
    # extension is .txt but content has a NUL byte → a binary sneaking in is rejected.
    with pytest.raises(ImageStoreError) as exc:
        store.save(b"metin\x00gizli\x00binari", original_name="sahte.txt")
    assert exc.value.code == "UNSUPPORTED_MEDIA"


def test_red_docx_uzanti_ama_zip_degil(store: ImageStore) -> None:
    with pytest.raises(ImageStoreError) as exc:
        store.save(b"plain text", original_name="sahte.docx")
    assert exc.value.code == "UNSUPPORTED_MEDIA"


def test_dedup_metin_dosyasi(store: ImageStore) -> None:
    r1, d1 = store.save(b"ayni icerik", original_name="a.txt")
    r2, d2 = store.save(b"ayni icerik", original_name="b.txt")
    assert not d1 and d2 and r1.id == r2.id


# --------------------------------------------------------------------------- #
# F1 — provider-native matrix (claude + cursor = path; both read from the path)
# --------------------------------------------------------------------------- #


def test_prepare_metin_claude_native(store: ImageStore) -> None:
    record, _ = store.save(b"kod", original_name="m.py")
    ref = prepare_for_provider(store, record.id, "claude")
    assert ref.supported and ref.provider_native
    assert ref.kind == "text"
    assert ref.path == str(store.file_path(record))


def test_prepare_pdf_claude_native(store: ImageStore) -> None:
    record, _ = store.save(b"%PDF-1.4\n%%EOF", original_name="r.pdf")
    ref = prepare_for_provider(store, record.id, "claude")
    assert ref.supported and ref.provider_native and ref.kind == "pdf"


def test_prepare_files_claude_karisik(store: ImageStore) -> None:
    img, _ = store.save(make_png(), original_name="a.png")
    txt, _ = store.save(b"merhaba", original_name="n.txt")
    prepared = prepare_files(store, [img.id, txt.id, "yok-id"], "claude")
    assert prepared.provider == "claude"
    assert len(prepared.file_refs) == 2
    kinds = {r["kind"] for r in prepared.file_refs}
    assert kinds == {"image", "text"}
    assert all(r["provider_native"] for r in prepared.file_refs)
    # a missing id falls into unsupported (the turn does not fail)
    assert len(prepared.unsupported) == 1
    assert prepared.unsupported[0]["id"] == "yok-id"


def test_prepare_files_cursor_da_native(store: ImageStore) -> None:
    # Empirically verified: cursor also reads files from an absolute path → same
    # as claude (the old "all unsupported" assumption was wrong).
    img, _ = store.save(make_png(), original_name="a.png")
    txt, _ = store.save(b"merhaba", original_name="n.txt")
    prepared = prepare_files(store, [img.id, txt.id], "cursor")
    assert prepared.provider == "cursor"
    assert len(prepared.file_refs) == 2
    assert {r["kind"] for r in prepared.file_refs} == {"image", "text"}
    assert all(r["provider_native"] for r in prepared.file_refs)
    assert prepared.unsupported == []
