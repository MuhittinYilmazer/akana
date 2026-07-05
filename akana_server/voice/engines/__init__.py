"""Pluggable TTS engines.

``auto`` priority = registration order: edge (Microsoft neural, online) first,
Piper (offline guarantee) last. XTTS is registered with ``auto=False`` → it does
NOT enter the auto chain (it is only resolved via an explicit ``tts_engine=xtts``
selection); otherwise, once torch is installed, it would shadow Piper in auto and
break the "Piper if none is ready" offline-guarantee contract. Importing this
package registers the built-ins; custom engines may call :func:`register` at any
time.
"""

from __future__ import annotations

from akana_server.voice.engines.base import (
    EngineFactory,
    TtsEngine,
    VoiceSelection,
    get,
    register,
    registered_engines,
    resolve,
)
from akana_server.voice.engines.edge import EdgeTtsEngine
from akana_server.voice.engines.piper import PiperEngine
from akana_server.voice.engines.xtts import XttsEngine

register("edge", EdgeTtsEngine)
register("piper", PiperEngine)
# XTTS opt-in: auto=False → does not enter the auto chain (only explicit `tts_engine=xtts`).
# available() only checks the torch+TTS import; if it were in auto it would shadow Piper.
register("xtts", XttsEngine, auto=False)

__all__ = [
    "EdgeTtsEngine",
    "EngineFactory",
    "PiperEngine",
    "TtsEngine",
    "VoiceSelection",
    "XttsEngine",
    "get",
    "register",
    "registered_engines",
    "resolve",
]
