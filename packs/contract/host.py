"""Consumer (akana) interface — the CONTRACT for loading/using packs.

This file is the CONTRACT ONLY: no behaviour. The real implementation lives on
the akana side (``akana_server/packs/``) and implements the ``PackHost`` +
``ContentAdapter`` protocols. The producer side is ``PackManifest``
(``manifest.py``) plus the pack directory layout.

Spec: packs/PACK_INTERFACE.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from packs.contract.manifest import PackManifest, ValidationResult


class PackState(str, Enum):
    """The lifecycle states the reference consumer actually implements.

    A discovered pack comes up directly as ``ENABLED`` (no quarantine / approval
    gate in v0.1); ``enable``/``disable`` hot-reload its content. Removal is done
    by deleting the pack folder + ``rescan()`` — there is no persistent
    ``uninstalled`` state.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"


@dataclass(frozen=True)
class PackRef:
    """A pointer to a discovered pack."""

    pack_id: str
    root: Path


@dataclass
class LoadedPack:
    """A loaded pack plus its state."""

    manifest: PackManifest
    root: Path
    state: PackState = PackState.ENABLED
    registered: dict[str, list[str]] = field(default_factory=dict)


@runtime_checkable
class ContentAdapter(Protocol):
    """One adapter per content type. Binds pack content to the relevant engine.

    Mapping (content_type -> akana engine):
      "skills"   -> SkillEngine  (registry scan; trigger-select + turn injection)
      "tools"    -> ToolGateway  (consent-gated MCP client mount; expose to agent)
      "personas" -> PersonaEngine (system-prompt injection)
    """

    content_type: str

    def register(self, pack: LoadedPack) -> None:
        """Register this pack's content with the engine (on enable)."""
        ...

    def unregister(self, pack_id: str) -> None:
        """Withdraw this pack's content from the engine (on disable)."""
        ...


@runtime_checkable
class PackHost(Protocol):
    """The main pack-management surface akana implements.

    Typical flow:  discover -> validate -> load(ENABLED) -> enable/disable.
    In v0.1 there is no approval gate: a discovered pack is enabled directly.
    """

    def discover(self) -> list[PackRef]:
        """Scan the pack directories (e.g. ~/.akana/packs/, repo packs/)."""
        ...

    def validate(self, ref: PackRef) -> ValidationResult:
        """Validate schema + file existence (may use manifest.validate_pack_dir)."""
        ...

    def load(self, ref: PackRef) -> LoadedPack:
        """Load the manifest and prepare the pack (content registered on enable)."""
        ...

    def enable(self, pack_id: str) -> None:
        """Register the pack's content through the ContentAdapters (hot-reload)."""
        ...

    def disable(self, pack_id: str) -> None:
        """Withdraw the content; data stays, no skill/tool/persona stays active."""
        ...


__all__ = [
    "ContentAdapter",
    "LoadedPack",
    "PackHost",
    "PackRef",
    "PackState",
]
