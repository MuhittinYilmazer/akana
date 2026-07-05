"""Provider-NATIVE file preparation — ``prepare_for_provider`` + ``prepare_files``.

Architectural decision (user request): NO text EXTRACTION / embedding into the
prompt. The file is given in a form the provider can read ITSELF: an absolute
file PATH reference. Both the claude CLI's ``Read`` tool and the cursor SDK
agent's file tool read the path themselves (image → vision; pdf/docx/xlsx/
text/code → text). The server does not chunk the content and embed it in the
prompt.

Capability matrix (no FALSE capability is advertised):

**claude** (local ``claude`` CLI, ``orchestrator/claude_provider.py``):
  the ``Read`` tool (enabled in the claude_provider ``_READONLY_TOOLS``
  allowlist) can read EVERY kind of file from an absolute path — including
  images. So for claude EVERY kind returns ``provider_native=True`` + an
  absolute path. The chat agent's cwd is ``<data_dir>/agent_chat`` and the
  uploads dir is ``<data_dir>/uploads`` — the path is ALWAYS absolute; the
  PHASE 2 chat binding gives the path to the provider (WITHOUT embedding text in
  the prompt — only a "[File: <path>]" reference may be passed, not the
  content).

**cursor** (Cursor SDK bridge, ``cursor_bridge/lib.mjs``):
  the Cursor SDK agent also has its own file-reading tool; the bridge passes
  ``local:{cwd}`` but the file tools are NOT LIMITED to the cwd — empirically
  verified (2026-06-13): Cursor reads the ``[File: <absolute path>]`` reference
  in the prompt even when the path is OUTSIDE the cwd (the ``uploads`` dir ≠ the
  ``agent_chat`` cwd): image→vision, pdf/docx/xlsx/text→text tokens came out
  correctly. So cursor, like claude, returns ``provider_native=True`` + an
  absolute path for EVERY kind. (The old note — "the bridge is text-only,
  unsupported" — was wrong; there is NO text embedding, only a path reference is
  passed and the agent reads it itself.)

Unknown provider names also return ``supported=False`` (not an error).

F1 CONTRACT (PHASE 2 chat.py — ANOTHER agent will touch it):

    refs = prepare_files(store, file_ids, provider)
    # refs.file_refs: [{id, path, kind, provider_native, media_type}]
    # refs.unsupported: [{id, kind, reason}]
    # chat.py: passes file_refs[*].path to the provider call (claude Read reads
    # it); shows unsupported entries to the user as an honest error.
    # NO TEXT EMBEDDING — only the path reference is carried.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from akana_server.multimodal.store import (
    UploadStore,
    UploadStoreError,
)

PROVIDER_CLAUDE = "claude"
PROVIDER_CURSOR = "cursor"

#: Providers that can read a file from an absolute PATH with their own agent
#: tool. Empirically verified (2026-06-13): both the claude CLI (Read tool) and
#: the cursor SDK agent read an absolute path OUTSIDE the cwd — image→vision,
#: pdf/docx/xlsx/text→text. The content is NOT EMBEDDED in the prompt; only a
#: "[Image/File: <path>]" reference is passed.
_NATIVE_FILE_PROVIDERS = frozenset({PROVIDER_CLAUDE, PROVIDER_CURSOR})

PROVIDER_GEMINI = "gemini"
PROVIDER_OPENAI = "openai"

#: INLINE-native providers → the set of KINDs each can embed INLINE. These
#: providers embed the file NOT as a PATH but as raw bytes (gemini
#: ``inline_data`` / openai ``image_url`` data-URI); the model REALLY sees the
#: content. The CRITICAL difference from claude/cursor: no "[Image: <path>]"
#: line enters the prompt.
#: - gemini: image + PDF (``gemini_provider._add_turn_images``).
#: - openai: image + PDF (``openai_provider._image_parts``; image→``image_url``
#:   data-URI, PDF→a ``file`` content part embedded inline with a ``file_data``
#:   data-URI). Unsupported kind (docx/xlsx/text in both) → ``unsupported``.
_INLINE_NATIVE_KINDS: dict[str, tuple[str, ...]] = {
    PROVIDER_GEMINI: ("image", "pdf"),
    PROVIDER_OPENAI: ("image", "pdf"),
}


def _inline_native_supported(record: Any, provider: str) -> bool:
    """Can an INLINE-native provider embed this record (provider-specific kind gate)?"""
    kinds = _INLINE_NATIVE_KINDS.get(provider, ())
    if "image" in kinds and bool(getattr(record, "is_image", False)):
        return True
    if "pdf" in kinds and getattr(record, "media_type", None) == "application/pdf":
        return True
    return False


@dataclass(frozen=True, slots=True)
class ProviderFileRef:
    """NATIVE reference of a single file for a single provider (path-based)."""

    file_id: str
    provider: str
    kind: str
    #: whether the provider can read the file ITSELF (claude Read = True; cursor = False).
    provider_native: bool
    #: provider_native=True → absolute file path; None if unsupported.
    path: str | None
    media_type: str | None
    note: str
    #: whether the provider embeds the file INLINE (bytes) instead of by PATH
    #: (gemini image/PDF). True → no path line ENTERS the prompt;
    #: ``_add_turn_images`` embeds the bytes. path stays None (with inline, the
    #: disk path is not given to the model).
    inline: bool = False

    @property
    def supported(self) -> bool:
        """Whether the provider can consume this file at all — natively (path) or inline."""
        return self.provider_native or self.inline

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PreparedFiles:
    """Prepared file set for one provider (the contract chat consumes)."""

    provider: str
    #: files the provider can read natively (path + kind + media_type).
    file_refs: list[dict[str, Any]] = field(default_factory=list)
    #: files to be embedded INLINE (gemini image/PDF) — no path line ENTERS the
    #: prompt; the provider layer (``_add_turn_images``) adds the bytes as
    #: ``inline_data``. When this list is POPULATED, the "provider cannot read the
    #: file" short-circuit is not triggered.
    inline_refs: list[dict[str, Any]] = field(default_factory=list)
    #: unsupported files (id, kind, reason) — an honest error to the user.
    unsupported: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "file_refs": self.file_refs,
            "inline_refs": self.inline_refs,
            "unsupported": self.unsupported,
        }


#: F0 backward-compat name (alias for the old "image"-focused calls).
ProviderImageRef = ProviderFileRef


def prepare_for_provider(
    store: UploadStore, file_id: str, provider: str
) -> ProviderFileRef:
    """Convert the file to a NATIVE reference for the given LLM provider.

    No record / disabled / file not on disk → :class:`UploadStoreError`
    (respectively ``IMAGE_NOT_FOUND`` / ``IMAGE_DISABLED`` / ``FILE_MISSING``;
    the F0 error codes are preserved for backward compatibility).
    """
    record = store.get(file_id)
    if record is None:
        raise UploadStoreError(f"no file record: {file_id}", code="IMAGE_NOT_FOUND")
    if record.disabled:
        raise UploadStoreError(
            f"file disabled: {file_id}", code="IMAGE_DISABLED"
        )
    path = store.file_path(record)
    if not path.is_file():
        raise UploadStoreError(
            f"file not on disk: {record.file_name}", code="FILE_MISSING"
        )

    provider_norm = (provider or "").strip().lower()
    if provider_norm in _NATIVE_FILE_PROVIDERS:
        return ProviderFileRef(
            file_id=file_id,
            provider=provider_norm,
            kind=record.kind,
            provider_native=True,
            path=str(path),
            media_type=record.media_type,
            note=(
                f"{provider_norm}: the absolute file path is passed as a reference "
                "in the prompt; the agent reads the file ITSELF (image→vision, pdf/docx/"
                "xlsx/text→text). The content is not embedded in the prompt."
            ),
        )
    if provider_norm in _INLINE_NATIVE_KINDS:
        # INLINE-native (gemini and openai: image+PDF) → bytes are EMBEDDED
        # (NOT a path); an unsupported kind cannot be read → unsupported.
        if _inline_native_supported(record, provider_norm):
            return ProviderFileRef(
                file_id=file_id,
                provider=provider_norm,
                kind=record.kind,
                provider_native=True,
                path=None,  # inline → the disk path is not given to the model
                media_type=record.media_type,
                note=(
                    f"{provider_norm}: the file is embedded INLINE (the model sees the "
                    "bytes directly); no path line enters the prompt."
                ),
                inline=True,
            )
        kinds = _INLINE_NATIVE_KINDS.get(provider_norm, ())
        readable = " and ".join("image" if k == "image" else k.upper() for k in kinds)
        return ProviderFileRef(
            file_id=file_id,
            provider=provider_norm,
            kind=record.kind,
            provider_native=False,
            path=None,
            media_type=record.media_type,
            note=(
                f"{provider_norm}: only reads {readable}; this file type "
                f"({record.kind}) is not supported."
            ),
        )

    return ProviderFileRef(
        file_id=file_id,
        provider=provider_norm or "unknown",
        kind=record.kind,
        provider_native=False,
        path=None,
        media_type=record.media_type,
        note=f"unknown provider: {provider!r}",
    )


def prepare_files(
    store: UploadStore, file_ids: list[str], provider: str
) -> PreparedFiles:
    """Prepare multiple files for a single provider (the chat contract).

    :func:`prepare_for_provider` is called for each id; native ones fall into
    ``file_refs`` and unsupported ones into ``unsupported``. Missing/disabled/
    not-on-disk records do NOT raise :class:`UploadStoreError` — they are written
    to the ``unsupported`` list with a ``reason`` here (so one file's error does
    not drop the whole turn; the caller handles a single uniform type). If a
    single file needs a hard error, call :func:`prepare_for_provider` directly.
    """
    file_refs: list[dict[str, Any]] = []
    inline_refs: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    for file_id in file_ids:
        try:
            ref = prepare_for_provider(store, file_id, provider)
        except UploadStoreError as exc:
            unsupported.append(
                {"id": file_id, "kind": None, "reason": exc.message, "code": exc.code}
            )
            continue
        if ref.provider_native and ref.path:
            file_refs.append(
                {
                    "id": ref.file_id,
                    "path": ref.path,
                    "kind": ref.kind,
                    "provider_native": True,
                    "media_type": ref.media_type,
                }
            )
        elif ref.inline:
            # INLINE (gemini image/PDF): no path — bytes are embedded in the provider layer.
            inline_refs.append(
                {
                    "id": ref.file_id,
                    "kind": ref.kind,
                    "media_type": ref.media_type,
                    "inline": True,
                }
            )
        else:
            unsupported.append(
                {"id": ref.file_id, "kind": ref.kind, "reason": ref.note}
            )
    return PreparedFiles(
        provider=(provider or "").strip().lower() or "unknown",
        file_refs=file_refs,
        inline_refs=inline_refs,
        unsupported=unsupported,
    )
