"""MemoryEvent — the unit of the append-only mutation stream (P8 replay seam).

Lives in its own leaf module (no intra-package imports) so the durable
:class:`~akana.memory.ledger.MemoryLedger` can consume events without an import
cycle with the :class:`~akana.memory.Memory` façade.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MemoryEvent:
    """An append-only record of a memory mutation (the ledger's unit)."""

    kind: str  # "turn" | "fact" | "fact_invalidated" | "conversation_reset"
    ts: str
    data: dict[str, Any] = field(default_factory=dict)


Subscriber = Callable[[MemoryEvent], None]
