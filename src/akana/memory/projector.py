"""GraphProjector — mirrors semantic facts into the graph via the event seam.

It's "just another subscriber" (like the durable ledger): it listens to the
façade's mutation stream and keeps the :class:`~akana.memory.graph.GraphStore`
in sync — projecting ``fact`` events as ``HAS_VALUE`` edges and purging them on
``fact_invalidated``. A projection failure is logged, never raised, so a graph
hiccup can't break a memory write (legacy F2 contract). It only consumes events,
so wiring it in can't loop.
"""

from __future__ import annotations

import logging

from akana.memory.events import MemoryEvent
from akana.memory.graph import GraphStore

log = logging.getLogger(__name__)


class GraphProjector:
    """Keep the graph synced with semantic facts as they mutate."""

    def __init__(self, graph: GraphStore) -> None:
        self._graph = graph

    def on_event(self, event: MemoryEvent) -> None:
        try:
            if event.kind == "fact":
                key = event.data.get("key")
                value = event.data.get("value")
                if key and value:
                    # A 'fact' event can be a RE-emit (correct_fact rewrites the value under
                    # the same id; a dedup re-assert re-emits the unchanged fact), so the old
                    # edge(s) must be dropped before inserting the new HAS_VALUE edge, else the
                    # old value survives as a live neighbor / identical edges pile up.
                    fact_id = event.data.get("fact_id")
                    if fact_id:
                        # relink = purge + link in ONE transaction, scoped orphan prune
                        # (audit C18/C19): a link failure rolls back the purge (no torn
                        # projection) and there's no global per-mutation node scan.
                        self._graph.relink_fact(
                            fact_id=str(fact_id), key=str(key), value=str(value)
                        )
                    else:
                        self._graph.link_fact(key=str(key), value=str(value), mem_id=None)
            elif event.kind == "fact_invalidated":
                fact_id = event.data.get("fact_id")
                if fact_id:
                    self._graph.purge_fact(
                        str(fact_id),
                        key=event.data.get("key"),
                        value=event.data.get("value"),
                    )
        except Exception:  # a graph hiccup must never break the memory write
            log.exception("graph projection failed for %s event", event.kind)
