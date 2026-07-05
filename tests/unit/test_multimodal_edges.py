"""MultimodalEngine F0 — boundary-value tests (magic-bytes, stripping, dedup, limit).

EDGE cases beyond the existing ``test_multimodal_engine.py``:
fake-extension magic-bytes rejection, size-limit exact-boundary behavior, EXIF
stripping across 3 formats + absence of GPS leakage, stripped-hash dedup,
corrupt/empty image, provider matrix (claude/cursor/unknown), a corrupt JPEG not crashing.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from akana_server.multimodal.exif import strip_location_metadata
from akana_server.multimodal.provider import prepare_files, prepare_for_provider
from akana_server.multimodal.sniff import sniff_format
from akana_server.multimodal.store import ImageStore, ImageStoreError


# -- small valid image builders -------------------------------------------------------


def _png(w: int = 4, h: int = 4) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + struct.pack(">II", w, h)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00" * 4  # CRC placeholder (sniff/dims don't look at it)
        + b"\x00\x00\x00\x00IEND\xae\x42\x60\x82"
    )


def _gif() -> bytes:
    return b"GIF89a" + struct.pack("<HH", 3, 3) + b"\x00" * 6


def _jpeg(*, with_exif: bool = False, gps: bytes = b"GPSDATA") -> bytes:
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


def _webp_with_exif(gps: bytes = b"GPS!") -> bytes:
    vp8 = b"VP8 " + struct.pack("<I", 10) + b"\x00" * 10
    exif = b"EXIF" + struct.pack("<I", len(gps)) + gps + (b"\x00" if len(gps) % 2 else b"")
    body = vp8 + exif
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


@pytest.fixture
def store(tmp_path: Path) -> ImageStore:
    return ImageStore(tmp_path)


# -- magic-bytes: extension spoofing --------------------------------------------------


def test_sahte_uzanti_icerik_png_kabul(store: ImageStore) -> None:
    # name is .jpg but content is PNG → the canonical extension from sniff becomes png.
    rec, _ = store.save(_png(), original_name="aslinda.png.jpg")
    assert rec.format == "png" and rec.ext == "png"


def test_uzanti_allowlist_disi_reddedilir(store: ImageStore) -> None:
    with pytest.raises(ImageStoreError) as e:
        store.save(_png(), original_name="kotu.exe")
    assert e.value.code == "UNSUPPORTED_EXTENSION"


def test_icerik_gorsel_degilse_reddedilir(store: ImageStore) -> None:
    # extension png but content HTML → magic-bytes mismatch.
    with pytest.raises(ImageStoreError) as e:
        store.save(b"<html>not an image</html>", original_name="sahte.png")
    assert e.value.code == "UNSUPPORTED_MEDIA"


def test_uzantisiz_ad_icerikten_kabul(store: ImageStore) -> None:
    rec, _ = store.save(_gif(), original_name="uzantisiz_ad")
    assert rec.format == "gif"


def test_sondaki_noktali_ad_uzanti_sayilmaz(store: ImageStore) -> None:
    # "resim." → no dot after rstrip('.') → the extension check is skipped.
    rec, _ = store.save(_png(), original_name="resim.")
    assert rec.format == "png"


# -- size limit exact-boundary --------------------------------------------------------


def test_bos_dosya_reddedilir(store: ImageStore) -> None:
    with pytest.raises(ImageStoreError) as e:
        store.save(b"")
    assert e.value.code == "EMPTY_FILE"


def test_boyut_siniri_tam_sinirda(tmp_path: Path) -> None:
    payload = _png()
    # max is set exactly to the payload size → ACCEPTED at the boundary.
    st = ImageStore(tmp_path, max_mb=len(payload) / (1024 * 1024))
    assert st.max_bytes == len(payload)
    rec, _ = st.save(payload)
    assert rec.size_bytes <= len(payload)
    # one byte over is rejected.
    with pytest.raises(ImageStoreError) as e:
        st.save(payload + b"\x00")
    assert e.value.code == "FILE_TOO_LARGE"


def test_max_mb_taban_en_az_bir_bayt(tmp_path: Path) -> None:
    st = ImageStore(tmp_path, max_mb=0)
    assert st.max_bytes == 1  # max(1, ...) floor


# -- EXIF stripping: 3 formats + GPS leakage ------------------------------------------


def test_jpeg_exif_soyulur_gps_sizmaz(store: ImageStore) -> None:
    rec, _ = store.save(_jpeg(with_exif=True, gps=b"40.7N74.0W"))
    assert rec.exif_stripped is True
    disk = store.file_path(rec).read_bytes()
    assert b"40.7N74.0W" not in disk
    assert b"Exif" not in disk


def test_png_exif_chunk_soyulur(tmp_path: Path) -> None:
    # build a PNG with an eXIf chunk.
    base = _png()
    exif_data = b"GPS-PNG-LEAK"
    exif_chunk = (
        struct.pack(">I", len(exif_data)) + b"eXIf" + exif_data + b"\x00\x00\x00\x00"
    )
    # insert eXIf after IHDR.
    idx = base.index(b"IHDR") - 4
    after_ihdr = idx + 12 + 13
    withexif = base[:after_ihdr] + exif_chunk + base[after_ihdr:]
    assert sniff_format(withexif) == "png"
    st = ImageStore(tmp_path)
    rec, _ = st.save(withexif)
    assert rec.exif_stripped is True
    assert b"GPS-PNG-LEAK" not in st.file_path(rec).read_bytes()


def test_webp_exif_soyulur(store: ImageStore) -> None:
    rec, _ = store.save(_webp_with_exif(gps=b"WEBP-GPS-LEAK"))
    assert rec.format == "webp"
    assert rec.exif_stripped is True
    assert b"WEBP-GPS-LEAK" not in store.file_path(rec).read_bytes()


def test_gif_soyma_noop(store: ImageStore) -> None:
    rec, _ = store.save(_gif())
    assert rec.exif_stripped is False
    assert "format carries no EXIF" in (rec.exif_note or "")


def test_exif_olmayan_jpeg_stripped_false(store: ImageStore) -> None:
    rec, _ = store.save(_jpeg(with_exif=False))
    assert rec.exif_stripped is False


# -- dedup over stripped content ------------------------------------------------------


def test_dedup_exifli_ve_exifsiz_tek_kayit(store: ImageStore) -> None:
    temiz = _jpeg(with_exif=False)
    exifli = _jpeg(with_exif=True)
    r1, d1 = store.save(temiz, original_name="a.jpg")
    r2, d2 = store.save(exifli, original_name="b.jpg")
    assert d1 is False and d2 is True
    assert r1.id == r2.id  # same stripped content → single record
    # the dedup event was logged.
    actions = [e["action"] for e in store.events(r1.id)]
    assert "created" in actions and "dedup_hit" in actions


# -- corrupt/boundary images don't crash ----------------------------------------------


def test_bozuk_jpeg_dims_none_ama_kayit_olur(store: ImageStore) -> None:
    # valid SOI but no SOF → dims can't be read, the record is still kept.
    bozuk = b"\xff\xd8\xff\xe0\x00\x04ab\xff\xd9"
    assert sniff_format(bozuk) == "jpeg"
    rec, _ = store.save(bozuk)
    assert rec.format == "jpeg"
    assert rec.width is None and rec.height is None


def test_kucuk_webp_riff_ama_kisa_reddedilir(store: ImageStore) -> None:
    # RIFF...WEBP but under 16 bytes → image sniff None; the .webp extension
    # is from the image family but the content is not an image → UNSUPPORTED_MEDIA.
    with pytest.raises(ImageStoreError) as e:
        store.save(b"RIFF1234WEBP", original_name="kisa.webp")
    assert e.value.code == "UNSUPPORTED_MEDIA"


def test_png_truncated_exif_chunk_gps_sizmaz() -> None:
    """Regression: an eXIf chunk that overruns its length (truncated) must not leak GPS.

    If the eXIf chunk is shorter than its declared length (corrupt/truncated PNG), the
    old code would ``break`` and copy the remaining bytes VERBATIM → location metadata
    leaked to disk. Privacy-first contract: a truncated EXIF must be DROPPED too.
    """
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">I", 13) + b"IHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00"
    # eXIf: length larger than the actual data (overrun) → GPS data stays truncated.
    exif = struct.pack(">I", 9999) + b"eXIf" + b"GPS_PNG_SECRET_40N74W"
    data = sig + ihdr + exif
    assert sniff_format(data) == "png"
    r = strip_location_metadata(data, "png")
    assert b"GPS_PNG_SECRET_40N74W" not in r.data
    assert r.stripped is True


# -- provider matrix ------------------------------------------------------------------


def test_provider_claude_file_path(store: ImageStore) -> None:
    rec, _ = store.save(_png())
    ref = prepare_for_provider(store, rec.id, "claude")
    assert ref.supported is True and ref.provider_native is True
    assert ref.kind == "image"  # kind is now the file type (image/text/pdf/...)
    assert ref.path == str(store.file_path(rec))
    assert Path(ref.path).is_absolute()


def test_provider_cursor_da_native(store: ImageStore) -> None:
    rec, _ = store.save(_png())
    ref = prepare_for_provider(store, rec.id, "CURSOR")  # case-insensitive
    assert ref.supported is True and ref.provider_native is True
    assert ref.path == str(store.file_path(rec))
    assert Path(ref.path).is_absolute()


def test_provider_bilinmeyen_desteklenmez(store: ImageStore) -> None:
    rec, _ = store.save(_png())
    ref = prepare_for_provider(store, rec.id, "gpt-5")
    assert ref.supported is False
    assert "unknown" in ref.note


def test_provider_kayit_yok_hata(store: ImageStore) -> None:
    with pytest.raises(ImageStoreError) as e:
        prepare_for_provider(store, "01YOK", "claude")
    assert e.value.code == "IMAGE_NOT_FOUND"


def test_provider_pasif_kayit_reddedilir(store: ImageStore) -> None:
    rec, _ = store.save(_png())
    assert store.disable(rec.id) is True
    with pytest.raises(ImageStoreError) as e:
        prepare_for_provider(store, rec.id, "claude")
    assert e.value.code == "IMAGE_DISABLED"


def test_provider_dosya_diskte_yoksa_hata(store: ImageStore) -> None:
    rec, _ = store.save(_png())
    store.file_path(rec).unlink()  # delete from disk, keep the metadata
    with pytest.raises(ImageStoreError) as e:
        prepare_for_provider(store, rec.id, "claude")
    assert e.value.code == "FILE_MISSING"


# -- gemini INLINE-native (image+PDF) — root-cause regression ------------------------
# BEFORE these tests, gemini image/PDF fell through to "unknown provider" →
# unsupported in prepare_for_provider → the _files_gate turn would SHORT-CIRCUIT
# with "gemini doesn't support images, switch to claude" (image/PDF upload was
# completely dead, _add_turn_images was never reached).


def test_provider_gemini_image_inline(store: ImageStore) -> None:
    """gemini: image → INLINE-native (supported, inline=True, path=None).
    No PATH line is produced; the bytes are embedded as inline_data via ``_add_turn_images``."""
    rec, _ = store.save(_png(), original_name="r.png")
    ref = prepare_for_provider(store, rec.id, "gemini")
    assert ref.supported is True
    assert ref.provider_native is True
    assert ref.inline is True
    assert ref.path is None


def test_provider_gemini_pdf_inline(store: ImageStore) -> None:
    """gemini: PDF → INLINE-native (embedded as inline_data, like an image)."""
    rec, _ = store.save(b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF", original_name="r.pdf")
    assert rec.media_type == "application/pdf"
    ref = prepare_for_provider(store, rec.id, "gemini")
    assert ref.supported is True and ref.inline is True and ref.path is None


def test_provider_gemini_text_desteklenmez(store: ImageStore) -> None:
    """gemini: text (not image/PDF) → unsupported (inline=False, supported=False)."""
    rec, _ = store.save(b"plain ascii text, not an image or a pdf at all", original_name="a.txt")
    ref = prepare_for_provider(store, rec.id, "gemini")
    assert ref.supported is False and ref.inline is False


def test_prepare_files_gemini_image_pdf_inline_not_unsupported(store: ImageStore) -> None:
    """ROOT-CAUSE REGRESSION: gemini image+PDF fall into ``inline_refs``, NOT
    ``unsupported`` → _files_gate does not short-circuit, the turn flows normally and inline_data is embedded."""
    img, _ = store.save(_png(), original_name="r.png")
    pdf, _ = store.save(b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF", original_name="r.pdf")
    prepared = prepare_files(store, [img.id, pdf.id], "gemini")
    assert len(prepared.inline_refs) == 2
    assert prepared.file_refs == []
    assert prepared.unsupported == []


def test_prepare_files_gemini_image_plus_text_ayrisir(store: ImageStore) -> None:
    """gemini mixed set: image → inline_refs, text → unsupported (no short-circuit)."""
    img, _ = store.save(_png(), original_name="r.png")
    txt, _ = store.save(b"some plain text content, clearly not an image file", original_name="a.txt")
    prepared = prepare_files(store, [img.id, txt.id], "gemini")
    assert len(prepared.inline_refs) == 1
    assert len(prepared.unsupported) == 1
    assert prepared.file_refs == []


def test_prepare_files_claude_unchanged_path_based(store: ImageStore) -> None:
    """claude path UNCHANGED: image goes to file_refs (path-based), inline_refs empty."""
    rec, _ = store.save(_png(), original_name="r.png")
    prepared = prepare_files(store, [rec.id], "claude")
    assert len(prepared.file_refs) == 1 and prepared.file_refs[0]["path"]
    assert prepared.inline_refs == []


# -- openai INLINE-native (image only) — vision parity --------------------------------


def test_provider_openai_image_inline(store: ImageStore) -> None:
    """openai: image → INLINE-native (vision image_url); inline=True, path=None."""
    rec, _ = store.save(_png(), original_name="r.png")
    ref = prepare_for_provider(store, rec.id, "openai")
    assert ref.supported is True and ref.provider_native is True
    assert ref.inline is True and ref.path is None


def test_provider_openai_pdf_inline(store: ImageStore) -> None:
    """openai: PDF → INLINE-native (like gemini — the OpenAI Chat API now accepts a PDF
    inline via a ``file`` content part with a ``file_data`` data-URI). supported=True,
    inline=True, path=None → _files_gate does not short-circuit."""
    rec, _ = store.save(b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF", original_name="r.pdf")
    assert rec.media_type == "application/pdf"
    ref = prepare_for_provider(store, rec.id, "openai")
    assert ref.supported is True and ref.inline is True and ref.path is None


def test_prepare_files_openai_image_pdf_inline_not_unsupported(store: ImageStore) -> None:
    """openai mixed set: image AND PDF → both go to inline_refs (parity with gemini);
    since PDF is now supported it does NOT fall into unsupported, file_refs stays empty."""
    img, _ = store.save(_png(), original_name="r.png")
    pdf, _ = store.save(b"%PDF-1.4\n%%EOF", original_name="r.pdf")
    prepared = prepare_files(store, [img.id, pdf.id], "openai")
    assert len(prepared.inline_refs) == 2
    assert prepared.unsupported == []
    assert prepared.file_refs == []


def test_provider_openai_text_desteklenmez(store: ImageStore) -> None:
    """openai: text (not image/PDF) → STILL unsupported (inline=False,
    supported=False) — the PDF parity covers only image+PDF, not other types."""
    rec, _ = store.save(b"plain ascii text, not an image or a pdf at all", original_name="a.txt")
    ref = prepare_for_provider(store, rec.id, "openai")
    assert ref.supported is False and ref.inline is False
