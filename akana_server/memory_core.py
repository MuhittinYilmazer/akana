"""A single in-process ``akana.memory.Memory`` core (the new-layer bridge).

The server's persist paths (turn_writer, chat persist sites) AND
``api/routes/memory.py`` all reach the ``memory.db`` in ``src/akana/memory``
through this module — ``api/routes/memory.py: _ensure_memory_stack`` calls
``get_memory_core`` too (B2.4 is done), so there is one instance per
``data_dir`` (lazy + ``threading.Lock``), one sqlite writer set, never a
second store/connection built on the side.
"""

from __future__ import annotations

# `import akana` (below, lazily) resolves to src/akana via the SINGLE bootstrap
# in akana_server/__init__.py — which runs before this submodule is imported.
# No per-module sys.path surgery here (that scattered "PERMANENT" preamble is
# gone; see _akana_src_bootstrap for the one central mechanism).
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type hint only; runtime lazy import
    from akana.memory import Memory

_LOCK = threading.Lock()
_INSTANCES: dict[Path, "Memory"] = {}


def get_memory_core(data_dir: Path) -> "Memory":
    """Return a single in-process ``akana.memory.Memory`` per ``data_dir``.

    Lazy setup: on the first call it is built with ``Memory.for_data_dir`` and
    cached; subsequent calls get the same instance. The ``akana.memory`` import
    is also deferred to call time so that importing this module itself
    (turn_writer/chat) never blows up because of the new layer.
    """
    key = Path(data_dir).expanduser().resolve()
    inst = _INSTANCES.get(key)
    if inst is not None:
        return inst
    with _LOCK:
        inst = _INSTANCES.get(key)
        if inst is None:
            from akana.memory import Memory  # lazy — resolves via the central bootstrap

            inst = Memory.for_data_dir(key)
            _INSTANCES[key] = inst
        return inst


__all__ = ["get_memory_core"]
