"""VectorStore — fact embeddings living in the same ``memory.db`` (K11).

One ``embeddings`` table keyed by fact id, vectors stored as raw float32
blobs. Search is numpy brute-force cosine over all rows — honest and exact,
and comfortably fast for a personal assistant's fact count (thousands, not
millions).

F3 migration seam (``sqlite-vec`` fallback plan)
------------------------------------------------
* **Today (F2):** brute-force cosine in Python — zero extra native deps.
* **F3.1 target:** load ``vec0`` extension on connection bootstrap; add
  ``vec_entities`` / ``vec_turns`` virtual tables (``FLOAT[dim]``) alongside
  the legacy ``embeddings`` BLOB table during a dual-write window.
* **F3.4 query:** ``vector_search`` tries ``sqlite-vec`` ANN first; on missing
  extension / load failure / dim mismatch → **fallback** to the current
  full-table scan (log once at WARNING, never 500).
* **Backfill (F3.6):** ``VectorIndexer`` + ``python akana.py memory reindex``
  populate both stores; callers keep using :meth:`search` unchanged.

Same concurrency pattern as :class:`~akana.memory.semantic.SemanticStore`:
WAL journal, short-lived connections, a ``threading.Lock`` around each
transaction.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from akana.memory.embed import Embedder

__all__ = ["VectorStore"]

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS embeddings (
    fact_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL
);
"""


class VectorStore:
    """Embedding rows + brute-force cosine search over ``memory.db``.

    TODO(F3.1): ``_try_load_sqlite_vec(conn)`` + ``vec_entities`` DDL.
    TODO(F3.4): branch :meth:`search` → ANN query with numpy fallback below.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path.resolve()
        self._lock = threading.Lock()
        self._init_db()

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> VectorStore:
        db_dir = data_dir / "db"
        db_dir.mkdir(parents=True, exist_ok=True)
        return cls(db_dir / "memory.db")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
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

    # -- writes -----------------------------------------------------------------

    def index_fact(self, fact_id: str, text: str, embedder: Embedder) -> None:
        """Embed ``text`` and upsert it under ``fact_id`` (replace on re-index)."""
        vec = np.asarray(embedder.embed([text])[0], dtype=np.float32)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings (fact_id, model, dim, vec) "
                    "VALUES (?, ?, ?, ?)",
                    (fact_id, embedder.name, int(vec.shape[0]), vec.tobytes()),
                )
                conn.commit()
            finally:
                conn.close()

    def index_many(self, items: list[tuple[str, str]], embedder: Embedder) -> int:
        """Embed ``(fact_id, text)`` pairs with one embed call, upsert in one tx.

        The backfill seam: one HTTP round-trip per batch instead of per fact.
        Embedding failures propagate to the caller (which owns the degrade
        decision); the store itself never writes a partial batch.
        """
        if not items:
            return 0
        vectors = embedder.embed([text for _, text in items])
        if len(vectors) != len(items):
            raise ValueError(
                f"embedder returned {len(vectors)} vectors for {len(items)} texts"
            )
        rows = []
        for (fact_id, _), v in zip(items, vectors):
            vec = np.asarray(v, dtype=np.float32)
            rows.append((fact_id, embedder.name, int(vec.shape[0]), vec.tobytes()))
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO embeddings (fact_id, model, dim, vec) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()
        return len(rows)

    def delete(self, fact_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM embeddings WHERE fact_id = ?", (fact_id,))
                conn.commit()
                return int(cur.rowcount) > 0
            finally:
                conn.close()

    def prune_orphans(self) -> int:
        """Delete embeddings whose fact is gone or invalidated (U6). Returns rows removed.

        Heals embeddings that leaked before the indexer-independent cascade shipped, and
        any orphaned by a crash between the fact commit and the cascade. The contract
        matches the live indexer: the table tracks only currently-VALID facts, so rows
        for hard-deleted AND soft-invalidated facts are pruned. Guarded by a check that
        the ``facts`` table exists — a standalone VectorStore (unit tests, a fresh
        memory.db with no SemanticStore yet) has an ``embeddings`` table but no ``facts``
        table; there we cannot tell orphans apart, so we prune nothing rather than wipe.
        """
        with self._lock:
            conn = self._connect()
            try:
                has_facts = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='facts'"
                ).fetchone()
                if has_facts is None:
                    return 0
                cur = conn.execute(
                    "DELETE FROM embeddings WHERE fact_id NOT IN "
                    "(SELECT id FROM facts WHERE invalidated_at IS NULL)"
                )
                conn.commit()
                return int(cur.rowcount)
            finally:
                conn.close()

    def clear(self) -> int:
        """Delete all embeddings (before a rebuild when the embedder model changes).
        Returns the number of rows deleted."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM embeddings")
                conn.commit()
                return int(cur.rowcount)
            finally:
                conn.close()

    # -- reads ------------------------------------------------------------------

    def count(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()
            finally:
                conn.close()
        return int(row["n"])

    def distinct_models(self) -> list[str]:
        """Distinct embedder model names in the index — for detecting a model
        change (e.g. when an ``ollama:bge-m3`` remnant exists, a query with the new
        ``fastembed:...`` won't match → the caller triggers a reindex)."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT DISTINCT model FROM embeddings").fetchall()
            finally:
                conn.close()
        return [str(r["model"]) for r in rows if r["model"]]

    def search(
        self, vec: list[float], *, limit: int = 10, model: str | None = None
    ) -> list[tuple[str, float]]:
        """Top-``limit`` ``(fact_id, cosine)`` pairs, best first.

        ``model`` restricts the scan to one embedder's rows (pass
        ``embedder.name``) — different models can share a dimension without
        sharing a space, so unfiltered search would compare incomparables.
        ``None`` keeps the legacy whole-table scan. Rows whose dimension does
        not match the query (a model swap mid-life) are skipped rather than
        crashing — a re-index heals them.
        """
        query = np.asarray(vec, dtype=np.float32)
        qn = float(np.linalg.norm(query))
        if query.size == 0 or qn == 0.0:
            return []
        query = query / qn
        sql = "SELECT fact_id, dim, vec FROM embeddings"
        params: tuple[str, ...] = ()
        if model is not None:
            sql += " WHERE model = ?"
            params = (model,)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        ids: list[str] = []
        mats: list[np.ndarray] = []
        for r in rows:
            if int(r["dim"]) != query.shape[0]:
                continue
            ids.append(str(r["fact_id"]))
            mats.append(np.frombuffer(r["vec"], dtype=np.float32))
        if not ids:
            return []
        matrix = np.vstack(mats)
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0.0] = 1.0  # zero vectors score 0, not NaN
        scores = (matrix @ query) / norms
        order = np.argsort(-scores)[: max(1, limit)]
        return [(ids[i], float(scores[i])) for i in order]
