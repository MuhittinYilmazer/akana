"""Shared attachment-embedding iterator for the native-vision providers.

gemini (``_add_turn_images`` → ``inline_data``) and openai (``_image_parts`` →
``image_url``/``file``) both read this turn's uploaded attachments from the
``UploadStore`` and embed the ones the model can natively see (images + PDFs), under a
cumulative byte budget, silently skipping anything unreadable / disabled /
wrong-type / over-budget. That selection + budget + defensive-skip logic was
copy-pasted between the two providers (with the same 18 MB constant declared twice);
only the final part SHAPE differs. :func:`iter_embeddable_attachments` is that shared
selection loop — it yields ``(record, data_bytes)`` pairs and each provider keeps only
its own part-shaping.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from akana_server.config import Settings

#: Native-vision request size practical limit is ~20 MB for both gemini (``inline_data``
#: total) and openai (vision); we leave headroom and cut off at 18 MB. An attachment
#: that would exceed this CUMULATIVE budget is SILENTLY skipped — not sending it is
#: better UX than breaking the turn with the provider's 400 "request too large".
MAX_INLINE_TOTAL_BYTES = 18 * 1024 * 1024


def _is_embeddable(rec: Any) -> bool:
    """Native vision accepts ONLY images and PDFs; every other type is skipped.

    ``UploadRecord`` has no ``is_pdf`` flag → a PDF is distinguished via ``media_type``
    (``kind == "pdf"`` → ``"application/pdf"``; see multimodal/store)."""
    return bool(rec.is_image) or rec.media_type == "application/pdf"


def iter_embeddable_attachments(
    settings: Settings,
    file_ids: list[str] | None,
    *,
    max_total_bytes: int = MAX_INLINE_TOTAL_BYTES,
) -> Iterator[tuple[Any, bytes]]:
    """Yield ``(UploadRecord, data)`` for each embeddable attachment, under the budget.

    Reads each ``file_id`` from the ``UploadStore`` and yields only the records the
    model can natively see (image or PDF), whose bytes are non-empty and fit within the
    CUMULATIVE ``max_total_bytes`` budget. DEFENSIVE at every step: if the store can't
    be set up, nothing is yielded; a record that is missing / disabled / not-image-or-
    PDF / unreadable / over-budget is silently skipped, and a single attachment error
    does NOT stop the others (mirrors the per-attachment tolerance both providers had).
    The caller shapes each yielded pair into its own provider part (``inline_data`` vs
    ``image_url``/``file``)."""
    if not file_ids:
        return
    try:
        from akana_server.multimodal.store import UploadStore

        store = UploadStore.for_settings(settings)
    except Exception:  # pragma: no cover - if the store can't be set up, continue without images
        return
    total_bytes = 0
    for fid in file_ids:
        try:
            rec = store.get(str(fid))
            if rec is None or rec.disabled or not _is_embeddable(rec):
                continue
            data = store.file_path(rec).read_bytes()
            if not data or total_bytes + len(data) > max_total_bytes:
                continue
            total_bytes += len(data)
            yield rec, data
        except Exception:  # pragma: no cover - a single attachment error must not break the turn
            continue


__all__ = ["MAX_INLINE_TOTAL_BYTES", "iter_embeddable_attachments"]
