"""AppServices — typed dependency container (DI — Step C, first step).

Today routes pull services via ``request.app.state.X`` (134 places; implicit
getattr → no compile-time type, the dependency is invisible in the signature, and
a real ``app`` is required in tests). This module builds a TYPED view of the core
services and injects it into routes via FastAPI ``Depends(get_services)``.

The first step is ADDITIVE + safe: storage is STILL ``app.state``; ``get_services``
builds a typed :class:`AppServices` from it. Routes are migrated one by one
(strangler) — the remaining ``app.state`` paths keep working. The real DI win is
in tests: with ``app.dependency_overrides[get_services] = lambda: fake`` a route
can be run with FAKE services without setting up a real lifespan/app.state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request

from akana_server.config import Settings
from akana_server.events import EventHub
from akana_server.llm_settings import LlmSettings
from akana_server.conversation_service import ConversationService

if TYPE_CHECKING:  # circular-import + weight: type-only (no runtime import)
    from akana_server.packs.host import AkanaPackHost


@dataclass(frozen=True)
class AppServices:
    """A typed, read-only view of the core services set up in the lifespan.

    In a properly initialized app the fields are always populated; ``get_services``
    reads defensively (``getattr(..., None)``) so early/test/partial paths don't
    blow up. Lazy services (image_store/file_service) are NOT here
    yet; when a route that uses them is migrated, they are either added to the
    container or fetched via ``Request`` from ``app.state`` (incremental).
    """

    settings: Settings
    conversation_service: ConversationService
    event_hub: EventHub
    llm_settings: LlmSettings
    pack_host: "AkanaPackHost"


def get_services(request: Request) -> AppServices:
    """Build a typed :class:`AppServices` view from ``app.state`` (FastAPI dependency).

    A test can override this dependency via ``app.dependency_overrides[get_services]``
    to supply a fake container → the route is tested in isolation, without real
    services.
    """

    s = request.app.state
    return AppServices(
        settings=getattr(s, "settings", None),
        conversation_service=getattr(s, "conversation_service", None),
        event_hub=getattr(s, "event_hub", None),
        llm_settings=getattr(s, "llm_settings", None),
        pack_host=getattr(s, "pack_host", None),
    )
