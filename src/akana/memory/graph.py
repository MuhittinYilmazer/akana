"""Knowledge graph — the P2 "graph-first" substrate (SQLite projection).

A lean port of the legacy graph (F2.6), trimmed of its vis.js/Studio dashboard
framing. It holds two things:

* **nodes** — entities, fact keys, and fact values.
* **edges** — the ``fact_key --HAS_VALUE--> fact_value`` projection of semantic
  facts (kept in sync by the :class:`~akana.memory.projector.GraphProjector`).

It lives in the shared ``memory.db`` (K11), in its own tables. Like every other
store it is policy-free: the façade/projector decide *when* to write.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ulid

from akana.memory._time import iso_now

log = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_label ON nodes(label);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);

CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    src_id TEXT NOT NULL,
    dst_id TEXT NOT NULL,
    rel TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_mem_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_edges_mem ON edges(source_mem_id);
"""


@dataclass(frozen=True, slots=True)
class GraphNode:
    id: str
    label: str
    kind: str


@dataclass(frozen=True, slots=True)
class GraphEdge:
    id: str
    src_id: str
    dst_id: str
    rel: str
    source_mem_id: str | None = None


class GraphStore:
    """Entity/relation graph backed by the shared ``memory.db``."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path.resolve()
        self._lock = threading.RLock()
        self._init_db()

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> GraphStore:
        db_dir = data_dir / "db"
        db_dir.mkdir(parents=True, exist_ok=True)
        return cls(db_dir / "memory.db")  # shared file, K11

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        # Two processes (server + MCP subprocess) share memory.db; without a
        # busy timeout a write-txn in the other process raises a raw
        # "database is locked" instead of waiting.
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _iso_now() -> str:
        return iso_now()

    def _upsert_node(self, conn: sqlite3.Connection, *, label: str, kind: str) -> str:
        row = conn.execute(
            "SELECT id FROM nodes WHERE label = ? AND kind = ?",
            (label, kind),
        ).fetchone()
        if row:
            return str(row["id"])
        node_id = str(ulid.new())
        conn.execute(
            "INSERT INTO nodes (id, label, kind, created_at) VALUES (?, ?, ?, ?)",
            (node_id, label[:512], kind[:64], self._iso_now()),
        )
        return node_id

    # -- write ----------------------------------------------------------------

    def link_fact(self, *, key: str, value: str, mem_id: str | None = None) -> GraphEdge:
        """Project a semantic fact as ``fact_key --HAS_VALUE--> fact_value``."""
        key_n = key.strip()[:256]
        val_n = value.strip()[:512]
        with self._lock:
            conn = self._connect()
            try:
                src = self._upsert_node(conn, label=key_n, kind="fact_key")
                dst = self._upsert_node(conn, label=val_n, kind="fact_value")
                edge_id = str(ulid.new())
                conn.execute(
                    """
                    INSERT INTO edges (id, src_id, dst_id, rel, created_at, source_mem_id)
                    VALUES (?, ?, ?, 'HAS_VALUE', ?, ?)
                    """,
                    (edge_id, src, dst, self._iso_now(), mem_id),
                )
                conn.commit()
            finally:
                conn.close()
        return GraphEdge(id=edge_id, src_id=src, dst_id=dst, rel="HAS_VALUE", source_mem_id=mem_id)

    # -- read ------------------------------------------------------------------

    def neighbors(self, label: str, *, kind: str = "entity") -> list[GraphNode]:
        """Nodes one HAS_VALUE hop away from the node ``label``."""
        with self._lock:
            conn = self._connect()
            try:
                node = conn.execute(
                    "SELECT id FROM nodes WHERE label = ? AND kind = ? LIMIT 1",
                    (label.strip()[:512], kind),
                ).fetchone()
                if node is None:
                    return []
                nid = str(node["id"])
                rows = conn.execute(
                    """
                    SELECT DISTINCT n.id, n.label, n.kind FROM nodes n
                    WHERE n.id IN (
                        SELECT dst_id FROM edges WHERE src_id = :id
                        UNION SELECT src_id FROM edges WHERE dst_id = :id
                    )
                    """,
                    {"id": nid},
                ).fetchall()
            finally:
                conn.close()
        return [GraphNode(id=str(r["id"]), label=str(r["label"]), kind=str(r["kind"])) for r in rows]

    def snapshot(self, *, limit: int = 200) -> dict[str, Any]:
        """Plain ``{nodes, edges}`` dump for inspection/debug (no UI framing)."""
        cap = max(1, min(limit, 500))
        with self._lock:
            conn = self._connect()
            try:
                node_rows = conn.execute(
                    "SELECT id, label, kind FROM nodes ORDER BY created_at DESC LIMIT ?",
                    (cap,),
                ).fetchall()
                node_ids = {str(r["id"]) for r in node_rows}
                edges: list[dict[str, str]] = []
                if node_ids:
                    placeholders = ",".join("?" * len(node_ids))
                    edge_rows = conn.execute(
                        f"""
                        SELECT id, src_id, dst_id, rel FROM edges
                        WHERE src_id IN ({placeholders}) AND dst_id IN ({placeholders})
                        ORDER BY created_at DESC LIMIT ?
                        """,
                        (*node_ids, *node_ids, cap * 2),
                    ).fetchall()
                    edges = [
                        {"id": str(r["id"]), "src": str(r["src_id"]), "dst": str(r["dst_id"]), "rel": str(r["rel"])}
                        for r in edge_rows
                    ]
            finally:
                conn.close()
        nodes = [
            {"id": str(r["id"]), "label": str(r["label"]), "kind": str(r["kind"])}
            for r in node_rows
        ]
        return {"nodes": nodes, "edges": edges}

    # -- maintenance -----------------------------------------------------------

    def purge_fact(self, fact_id: str, *, key: str | None = None, value: str | None = None) -> int:
        """Remove graph edges for a retracted/superseded semantic fact."""
        removed = 0
        with self._lock:
            conn = self._connect()
            try:
                touched = self._fact_edge_nodes(conn, fact_id)
                cur = conn.execute("DELETE FROM edges WHERE source_mem_id = ?", (fact_id,))
                removed += int(cur.rowcount)
                if key and value:
                    src = conn.execute(
                        "SELECT id FROM nodes WHERE label = ? AND kind = 'fact_key' LIMIT 1",
                        (key.strip()[:256],),
                    ).fetchone()
                    dst = conn.execute(
                        "SELECT id FROM nodes WHERE label = ? AND kind = 'fact_value' LIMIT 1",
                        (value.strip()[:512],),
                    ).fetchone()
                    if src and dst:
                        touched.update([str(src["id"]), str(dst["id"])])
                        cur2 = conn.execute(
                            """
                            DELETE FROM edges
                            WHERE src_id = ? AND dst_id = ? AND rel = 'HAS_VALUE'
                              AND (source_mem_id IS NULL OR source_mem_id = ?)
                            """,
                            (str(src["id"]), str(dst["id"]), fact_id),
                        )
                        removed += int(cur2.rowcount)
                # audit C18: prune ONLY the nodes this fact could have orphaned — not a
                # global "DELETE FROM nodes WHERE id NOT IN (all edges)" O(nodes) scan run
                # on every single fact mutation.
                self._prune_nodes_if_orphan(conn, touched)
                conn.commit()
            finally:
                conn.close()
        return removed

    def relink_fact(self, *, fact_id: str, key: str, value: str) -> None:
        """Atomically re-project a fact: drop its existing HAS_VALUE edge(s) and insert the
        new one in ONE transaction (audit C19).

        The GraphProjector previously called ``purge_fact`` then ``link_fact`` as two
        separate committed transactions; if ``link_fact`` failed after the purge committed
        (e.g. a lock in a two-process burst), the corrected fact vanished from the graph
        with no retry. Here a link failure rolls back the purge, so the projection is never
        left torn, and the orphan prune is scoped (audit C18).
        """
        key_n = key.strip()[:256]
        val_n = value.strip()[:512]
        with self._lock:
            conn = self._connect()
            try:
                touched = self._fact_edge_nodes(conn, fact_id)
                conn.execute("DELETE FROM edges WHERE source_mem_id = ?", (fact_id,))
                src = self._upsert_node(conn, label=key_n, kind="fact_key")
                dst = self._upsert_node(conn, label=val_n, kind="fact_value")
                conn.execute(
                    "INSERT INTO edges (id, src_id, dst_id, rel, created_at, source_mem_id) "
                    "VALUES (?, ?, ?, 'HAS_VALUE', ?, ?)",
                    (str(ulid.new()), src, dst, self._iso_now(), fact_id),
                )
                touched.discard(src)  # still referenced by the freshly-inserted edge
                touched.discard(dst)
                self._prune_nodes_if_orphan(conn, touched)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def clear_all(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                for table in ("edges", "nodes"):
                    conn.execute(f"DELETE FROM {table}")
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _fact_edge_nodes(conn: sqlite3.Connection, fact_id: str) -> set[str]:
        """Node ids referenced by the edges tied to ``fact_id`` — the only nodes a purge
        of this fact could orphan (so the prune can be scoped to them)."""
        ids: set[str] = set()
        for r in conn.execute(
            "SELECT src_id, dst_id FROM edges WHERE source_mem_id = ?", (fact_id,)
        ):
            ids.add(str(r["src_id"]))
            ids.add(str(r["dst_id"]))
        return ids

    @staticmethod
    def _prune_nodes_if_orphan(conn: sqlite3.Connection, node_ids: set[str]) -> int:
        """Delete only the given nodes that no longer have ANY edge (scoped orphan prune)."""
        removed = 0
        for nid in node_ids:
            still_referenced = conn.execute(
                "SELECT 1 FROM edges WHERE src_id = ? OR dst_id = ? LIMIT 1", (nid, nid)
            ).fetchone()
            if still_referenced is None:
                conn.execute("DELETE FROM nodes WHERE id = ?", (nid,))
                removed += 1
        return removed
