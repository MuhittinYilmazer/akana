"""Pack <-> akana interface contract (L2 platform).

Single source of truth: both the pack (producer) and akana (consumer) agree over
this schema + protocol. Spec: packs/PACK_INTERFACE.md.
"""

from __future__ import annotations

from packs.contract.host import (
    ContentAdapter,
    LoadedPack,
    PackHost,
    PackRef,
    PackState,
)
from packs.contract.manifest import (
    PackManifest,
    ValidationResult,
    load_manifest,
    validate_pack_dir,
)

__all__ = [
    "ContentAdapter",
    "LoadedPack",
    "PackHost",
    "PackManifest",
    "PackRef",
    "PackState",
    "ValidationResult",
    "load_manifest",
    "validate_pack_dir",
]
