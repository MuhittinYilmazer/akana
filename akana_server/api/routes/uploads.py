"""Multi-type file upload REST surface — /api/v1/uploads (MultimodalEngine F1).

* ``POST /uploads`` — a single multipart file; ALL allowed types (image +
  text/code + pdf/docx/xlsx/pptx/zip). magic-bytes + extension + size
  validation and (for images) EXIF stripping live inside :class:`UploadStore`.
  The feature flag ``AKANA_UPLOADS_ENABLED=0`` disables POST (existing records
  remain readable).
* ``GET /uploads/{id}`` — meta (JSON): ``kind``, ``path`` (the server disk path —
  the absolute path the claude Read tool will read) and ``provider_native`` info.
* ``GET /uploads/{id}/raw`` — the raw file; served behind bearer (router-level)
  and with ``Content-Disposition: attachment`` + ``X-Content-Type-Options: nosniff``
  (no inline-execution surface in the browser). The file name is a
  server-generated ULID name — user input never leaks into the header.
* No ``DELETE`` — records are append-only; deactivation is in the store API
  (``UploadStore.disable``) and not exposed over REST.

For the F1 chat contract (provider-native, NO text embedding) see
:mod:`akana_server.multimodal`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from akana_server.api.deps import get_image_store, require_akana_bearer
from akana_server.api.errors import http_error
from akana_server.api.services import AppServices, get_services
from akana_server.multimodal.store import (
    UploadRecord,
    UploadStore,
    UploadStoreError,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["uploads"], dependencies=[Depends(require_akana_bearer)])

#: ImageStoreError.code → HTTP status mapping.
_ERROR_STATUS = {
    "EMPTY_FILE": 422,
    "FILE_TOO_LARGE": 413,
    "UNSUPPORTED_EXTENSION": 415,
    "UNSUPPORTED_MEDIA": 415,
    "IMAGE_NOT_FOUND": 404,
    "IMAGE_DISABLED": 410,
    "FILE_MISSING": 410,
}


def _store_error(e: UploadStoreError) -> HTTPException:
    return http_error(_ERROR_STATUS.get(e.code, 422), e.code, e.message)


def _record_payload(store: UploadStore, record: UploadRecord) -> dict[str, Any]:
    payload = record.to_payload()
    payload["media_type"] = record.media_type
    payload.pop("file_name", None)  # the ULID disk name does not leak to the API surface
    # provider-native info: both agents read the file themselves from the absolute
    # PATH (claude=Read tool, cursor=SDK file tool) — empirically verified (a path
    # outside cwd is also read; image→vision, pdf/docx/xlsx/text→text).
    payload["path"] = str(store.file_path(record))
    # gemini native: image + PDF (embedded via inline_data); openai native (vision):
    # image (image_url data-URI) + PDF (a file content part, embedded inline via a
    # file_data data-URI). For other types (docx/xlsx/text) gemini/openai have no
    # file-reading tool → False.
    gemini_native = record.is_image or record.media_type == "application/pdf"
    openai_native = record.is_image or record.media_type == "application/pdf"
    payload["provider_native"] = {
        "claude": True,
        "cursor": True,
        "gemini": bool(gemini_native),
        "openai": bool(openai_native),
    }
    return payload


@router.post("/uploads")
async def upload_image(
    file: Annotated[UploadFile, File()],
    services: AppServices = Depends(get_services),
    store: UploadStore = Depends(get_image_store),
) -> dict[str, Any]:
    from akana_server.runtime_settings import get_runtime

    if not get_runtime("uploads_enabled", services.settings):
        raise http_error(
            403,
            "UPLOADS_DISABLED",
            "file upload is disabled (Settings → Runtime or AKANA_UPLOADS_ENABLED=0)",
        )
    # Read limit + 1 byte: oversize content is rejected without being fully loaded into memory.
    data = await file.read(store.max_bytes + 1)
    try:
        record, dedup = await asyncio.to_thread(
            store.save, data, original_name=file.filename
        )
    except UploadStoreError as e:
        raise _store_error(e) from e
    return {"image": _record_payload(store, record), "dedup": dedup}


@router.get("/uploads/{image_id}")
async def get_upload_meta(
    image_id: str, store: UploadStore = Depends(get_image_store)
) -> dict[str, Any]:
    record = await asyncio.to_thread(store.get, image_id)
    if record is None:
        raise http_error(404, "IMAGE_NOT_FOUND", f"no file record: {image_id}")
    return {"image": _record_payload(store, record)}


@router.get("/uploads/{image_id}/raw")
async def get_upload_raw(
    image_id: str, store: UploadStore = Depends(get_image_store)
) -> FileResponse:
    record = await asyncio.to_thread(store.get, image_id)
    if record is None:
        raise http_error(404, "IMAGE_NOT_FOUND", f"no file record: {image_id}")
    if record.disabled:
        raise http_error(410, "IMAGE_DISABLED", f"file is disabled: {image_id}")
    path = store.file_path(record)
    if not path.is_file():
        raise http_error(410, "FILE_MISSING", f"file is not on disk: {image_id}")
    # The file name is server-generated (<ulid>.<ext>) — no header-injection surface.
    return FileResponse(
        path,
        media_type=record.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{record.file_name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
