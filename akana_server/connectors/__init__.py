"""ConnectorEngine — external channel bridges.

One channel: Telegram (long-poll, ``telegram.py``).
The architecture is channel-agnostic: ``base.Connector`` protocol +
``registry.ConnectorRegistry`` (shared inbound queue, ``send_to`` service) +
``router.InboundRouter`` (policy → conversation → LLM → egress filter).
"""

from akana_server.connectors.base import (
    Connector,
    ConnectorSendError,
    InboundMessage,
    OutboundMessage,
)
from akana_server.connectors.egress_filter import (
    EgressFilterResult,
    filter_outbound,
)
from akana_server.connectors.registry import ConnectorRegistry, build_registry
from akana_server.connectors.router import InboundRouter
from akana_server.connectors.service import start_connectors, stop_connectors

__all__ = [
    "Connector",
    "ConnectorRegistry",
    "ConnectorSendError",
    "EgressFilterResult",
    "InboundMessage",
    "InboundRouter",
    "OutboundMessage",
    "build_registry",
    "filter_outbound",
    "start_connectors",
    "stop_connectors",
]
