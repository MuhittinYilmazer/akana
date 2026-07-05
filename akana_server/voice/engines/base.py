"""TTS engine protocol + process-wide registry.

Engines are cheap, stateless adapters constructed on demand with ``Settings``;
heavy resources (e.g. Piper voice models) stay cached inside their backing
modules. New engines plug in via :func:`register` — no core changes needed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from akana_server.voice.tts import TtsError

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VoiceSelection:
    """Engine name + engine-specific voice id.

    Piper voices are ``.onnx`` paths; edge voices are neural voice names
    (e.g. ``tr-TR-EmelNeural``).
    """

    engine: str
    voice: str


@runtime_checkable
class TtsEngine(Protocol):
    """Contract every TTS engine adapter implements."""

    name: str

    def available(self) -> bool:
        """Cheap readiness probe (imports/files; no heavy work)."""
        ...

    def synthesize(self, text: str, voice: str) -> tuple[bytes, str]:
        """Blocking synth: returns ``(audio_bytes, mime_type)``.

        Raises :class:`TtsError` on failure. Callers on an event loop must
        off-load via a worker thread (``anyio.to_thread.run_sync``).
        """
        ...

    def list_voices(self) -> list[dict[str, Any]]:
        """Voice catalog entries (``id``/``name``/``lang`` keys expected)."""
        ...


EngineFactory = Callable[["Settings"], TtsEngine]

_FACTORIES: dict[str, EngineFactory] = {}
_AUTO_ORDER: list[str] = []  # registration order == "auto" resolve priority


def register(name: str, factory: EngineFactory, *, auto: bool = True) -> None:
    """Register an engine factory. Registration order sets ``auto`` priority.

    ``auto=False``: the engine is resolved ONLY via an explicit ``tts_engine=<name>``
    selection (``resolve`` calls ``get`` directly) — it does NOT enter the ``auto``
    chain (edge→piper). Rationale: XTTS is opt-in; ``available()`` only checks the
    ``torch``+``TTS`` import, so once those packages are installed it would shadow
    Piper in the auto chain and break the "if none available, last engine = Piper
    offline guarantee" contract (#tts-auto).
    """
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("engine name must be non-empty")
    if auto and key not in _AUTO_ORDER:
        _AUTO_ORDER.append(key)
    _FACTORIES[key] = factory


def registered_engines() -> list[str]:
    """All registered engine names (registration order) — UI/diagnostics list.

    The auto-resolution priority is kept SEPARATE (``_AUTO_ORDER``): opt-in engines
    (``auto=False``, e.g. XTTS) DO appear here (so the user can select them
    explicitly) but do not enter the auto chain.
    """
    return list(_FACTORIES)


def get(name: str, settings: Settings) -> TtsEngine:
    """Construct the engine registered under *name* (raises 400 TtsError)."""
    key = (name or "").strip().lower()
    factory = _FACTORIES.get(key)
    if factory is None:
        raise TtsError(
            f"unknown TTS engine: {name!r} (registered: {', '.join(_FACTORIES) or 'none'})",
            status_code=400,
        )
    return factory(settings)


def resolve(preference: str | None, settings: Settings) -> TtsEngine:
    """Map an engine preference to a concrete engine.

    ``auto`` (or empty) → first :meth:`TtsEngine.available` engine in
    registration order; if none probes available, the last registered engine
    (the offline guarantee, Piper) is returned so synth errors carry its
    install hint. An unknown explicit name logs a warning and degrades to
    ``auto`` instead of breaking the caller.
    """
    pref = (preference or "auto").strip().lower()
    if pref != "auto":
        if pref in _FACTORIES:
            return get(pref, settings)
        log.warning("unknown TTS engine preference %r — using auto", preference)
    engines = [get(key, settings) for key in _AUTO_ORDER]
    if not engines:
        raise TtsError("no TTS engines registered", status_code=503)
    for engine in engines:
        try:
            if engine.available():
                return engine
        except Exception as e:  # noqa: BLE001 - probe must never break resolution
            log.warning("TTS engine %s availability probe failed: %s", engine.name, e)
    return engines[-1]


__all__ = [
    "EngineFactory",
    "TtsEngine",
    "VoiceSelection",
    "get",
    "register",
    "registered_engines",
    "resolve",
]
