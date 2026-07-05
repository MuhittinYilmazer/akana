"""Driver layer: provider-neutral chat backends.

There is no factory here on purpose: ``akana_server`` constructs
``OpenAIDriver``/``OllamaDriver`` concretely (see
``akana_server/orchestrator/openai_provider.py`` and ``ollama_provider.py``)
and routes Cursor through ``akana_server.orchestrator.llm_dispatch`` directly.
A prior ``CursorDriver``/``make_driver`` factory wrapped that same dispatch
module from inside this "clean core" package, which created a backwards
``akana -> akana_server`` import edge and was never used in production. It
was removed; import the concrete driver you need instead.
"""

from __future__ import annotations

from akana.driver.base import (
    ChatChunk,
    ChatResult,
    Driver,
    DriverError,
    DriverUnavailable,
    Message,
)
from akana.driver.ollama import OllamaDriver

__all__ = [
    "Message",
    "ChatChunk",
    "ChatResult",
    "Driver",
    "DriverError",
    "DriverUnavailable",
    "OllamaDriver",
]
