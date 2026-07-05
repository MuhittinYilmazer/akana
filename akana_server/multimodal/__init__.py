"""MultimodalEngine F1 — multi-type file upload + provider-NATIVE preparation.

Components:

* :class:`.store.UploadStore` — ``<data_dir>/uploads/`` + ``db/multimodal.db``
  (image + text/code + pdf/docx/xlsx/pptx/zip; magic-bytes + extension +
  size validation; EXIF stripping on images; sha256 dedup; append-only with a
  ``kind`` field). ``ImageStore``/``ImageRecord`` are F0 aliases.
* :func:`.provider.prepare_for_provider` — single file → provider-native path
  reference (claude + cursor: absolute file path; both agents read the path
  THEMSELVES — empirically verified, image/pdf/docx/xlsx/text).
* :func:`.provider.prepare_files` — multiple files → :class:`PreparedFiles`
  (file_refs + unsupported), the contract chat consumes.
* REST surface: ``api/routes/uploads.py`` (POST /api/v1/uploads + GET meta/raw).

F1 CONTRACT (PHASE 2 chat binding — ANOTHER agent will touch chat.py):

1. An optional field is added to ``ChatRequest``::

       file_ids: list[str] = Field(default_factory=list, max_length=8)

2. The ``api/routes/chat.py`` flow, AFTER provider resolution (cursor/claude)::

       prepared = prepare_files(store, file_ids, provider)
       # prepared.file_refs: [{id, path, kind, provider_native, media_type}]
       # prepared.unsupported: [{id, kind, reason}]

   * **claude + cursor** → every file is native; ``file_refs[*].path`` is passed
     to the provider call. Chat adds one ``[File/Image: <path>]`` REFERENCE
     line per file to the user text; the agent reads the path itself
     (claude=Read tool, cursor=SDK file tool). The content is NOT EMBEDDED in
     the prompt.
   * **unknown provider** → ``file_refs`` empty, everything ``unsupported``; the
     turn does not reach the LLM and the user gets an honest error. It is not
     dropped silently.
   * Missing/disabled/not-on-disk record → ``prepare_files`` writes it to
     ``unsupported`` with ``reason``+``code`` (it does not drop the turn). Where
     a hard error is required, ``prepare_for_provider`` is called directly →
     :class:`UploadStoreError`.

3. Store access follows the same pattern as the routes:
   ``UploadStore.for_settings(request.app.state.settings)`` (lazy, cached on
   app.state — see ``api/routes/uploads.py`` ``_upload_store``).
"""

from akana_server.multimodal.provider import (
    PreparedFiles,
    ProviderFileRef,
    ProviderImageRef,
    prepare_files,
    prepare_for_provider,
)
from akana_server.multimodal.store import (
    ALL_ALLOWED_EXTENSIONS,
    ALLOWED_EXTENSIONS,
    IMAGE_EXTENSIONS,
    ImageRecord,
    ImageStore,
    ImageStoreError,
    UploadRecord,
    UploadStore,
    UploadStoreError,
)

__all__ = [
    "ALLOWED_EXTENSIONS",
    "ALL_ALLOWED_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "ImageRecord",
    "ImageStore",
    "ImageStoreError",
    "PreparedFiles",
    "ProviderFileRef",
    "ProviderImageRef",
    "UploadRecord",
    "UploadStore",
    "UploadStoreError",
    "prepare_files",
    "prepare_for_provider",
]
