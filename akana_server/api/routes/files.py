"""Files REST surface (FileEngine F0) — bearer-protected, READ-ONLY.

* ``GET /api/v1/files/list?path=&depth=`` — list a directory inside the allowlist.
* ``GET /api/v1/files/read?path=&max_bytes=`` — read a file inside the allowlist.

The write endpoint is INTENTIONALLY absent in F0 (wiring the approval queue is
F1's job). HTTP mapping: empty (unconfigured) allowlist → 503, path outside the
root → 403, missing path/directory → 404, invalid parameter → 400 (the code is
in ``detail.error.code``).

The service is built lazily on ``app.state.file_service``; tests may override
this field.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from akana_server.api.deps import get_file_service, require_akana_bearer
from akana_server.files.service import (
    DEFAULT_MAX_READ_BYTES,
    MAX_LIST_DEPTH,
    MAX_READ_BYTES,
    FileEngineNotConfigured,
    FileService,
)

router = APIRouter(tags=["files"])


def _http_error(e: Exception) -> HTTPException:
    if isinstance(e, FileEngineNotConfigured):
        return HTTPException(
            status_code=503,
            detail={"error": {"code": "FILES_NOT_CONFIGURED", "message": str(e)}},
        )
    if isinstance(e, PermissionError):
        return HTTPException(
            status_code=403,
            detail={"error": {"code": "PATH_FORBIDDEN", "message": str(e)}},
        )
    if isinstance(e, (FileNotFoundError, NotADirectoryError)):
        return HTTPException(
            status_code=404,
            detail={"error": {"code": "PATH_NOT_FOUND", "message": str(e)}},
        )
    if isinstance(e, ValueError):
        return HTTPException(
            status_code=400,
            detail={"error": {"code": "BAD_REQUEST", "message": str(e)}},
        )
    raise e  # unexpected error — let it surface as 500


@router.get("/files/list", dependencies=[Depends(require_akana_bearer)])
async def files_list(
    path: str = Query(min_length=1, max_length=4096),
    depth: int = Query(1, ge=1, le=MAX_LIST_DEPTH),
    svc: FileService = Depends(get_file_service),
) -> dict[str, Any]:
    try:
        # BUG FIX: svc.list_dir does synchronous filesystem IO; run it off the
        # event loop (matches the uploads.py idiom) so slow/large directory
        # walks do not stall the whole asyncio server.
        entries = await asyncio.to_thread(svc.list_dir, path, depth=depth)
    except Exception as e:  # noqa: BLE001 — _http_error re-raises the unknown
        raise _http_error(e) from e
    return {"path": path, "depth": depth, "entries": entries, "count": len(entries)}


@router.get("/files/read", dependencies=[Depends(require_akana_bearer)])
async def files_read(
    path: str = Query(min_length=1, max_length=4096),
    max_bytes: int = Query(DEFAULT_MAX_READ_BYTES, ge=1, le=MAX_READ_BYTES),
    svc: FileService = Depends(get_file_service),
) -> dict[str, Any]:
    try:
        # BUG FIX: svc.read_text does synchronous filesystem IO; run it off the
        # event loop (matches the uploads.py idiom) so slow/large file reads do
        # not stall the whole asyncio server.
        result = await asyncio.to_thread(svc.read_text, path, max_bytes=max_bytes)
    except Exception as e:  # noqa: BLE001 — _http_error re-raises the unknown
        raise _http_error(e) from e
    return result.to_payload()
