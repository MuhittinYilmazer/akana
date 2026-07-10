"""Staging inbox — captured fact candidates awaiting the user's approval.

K30 ``promote_mode=inbox_only``: nothing reaches durable semantic memory
automatically. The Curator extracts candidates and *stages* them here; the user
(via a future Memory screen, or the chat ``/inbox`` command) promotes or rejects.
This keeps the trust ladder honest — an inferred guess never silently becomes a
"fact" the assistant asserts.

Shares ``memory.db`` with the other stores; pure stdlib + ``ulid``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import ulid

from akana.memory._time import iso_now
from akana.memory.terms import fold_text

log = logging.getLogger(__name__)

StagingStatus = Literal["pending", "promoted", "rejected"]

# Flood protection: the inbox never holds more than this many pending rows.
_MAX_PENDING = 500

# Retention (audit C34): promoted/rejected rows are only status-flipped, never
# deleted (except a full clear()), so the table grows unbounded over months of
# auto-capture. Opportunistically prune resolved rows older than this, every
# _PRUNE_EVERY stage() inserts (pending rows are never touched).
_RESOLVED_RETENTION_DAYS = 30
_PRUNE_EVERY = 100

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS staging (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    conversation_id TEXT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    reason TEXT,
    trust TEXT NOT NULL DEFAULT 'inferred',
    source_turn_id TEXT,
    quote TEXT,
    extractor TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    promoted_fact_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_staging_status ON staging(status, ts);
"""


@dataclass(frozen=True, slots=True)
class FactCandidate:
    """A fact an extractor proposes — not yet trusted, not yet stored."""

    key: str
    value: str
    reason: str = ""
    trust: str = "inferred"
    source_turn_id: str | None = None
    quote: str | None = None
    extractor: str | None = None
    # M3.3: durable fact ids a consolidation candidate refers to (merge sources
    # / decay target) — separate field, never smuggled into ``value``.
    source_fact_ids: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class StagedFact:
    """A candidate persisted in the inbox, with its review status."""

    id: str
    ts: str
    key: str
    value: str
    reason: str
    status: StagingStatus
    trust: str
    source_turn_id: str | None
    quote: str | None
    extractor: str | None
    conversation_id: str | None
    promoted_fact_id: str | None = None
    source_fact_ids: tuple[str, ...] | None = None


class StagingStore:
    """SQLite-backed inbox of fact candidates."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path.resolve()
        self._lock = threading.Lock()
        self._stage_calls = 0  # throttle counter for the opportunistic retention sweep
        self._init_db()

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> StagingStore:
        db_dir = data_dir / "db"
        db_dir.mkdir(parents=True, exist_ok=True)
        return cls(db_dir / "memory.db")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        # Two processes (server + MCP subprocess) share memory.db; without a
        # busy timeout a write-txn in the other process raises a raw
        # "database is locked" instead of waiting.
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        # Turkish-aware fold for inbox dedup (same helper the fact store uses for
        # key matching) → 'İsim' and 'isim' collapse to one pending candidate.
        conn.create_function("akana_fold", 1, fold_text, deterministic=True)
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                self._ensure_source_fact_ids_column(conn)
                self._drop_island_column(conn)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _drop_island_column(conn: sqlite3.Connection) -> None:
        """Drop the now-removed ``island`` column (best-effort).

        The island concept was removed (recall is now global). On old DBs the
        column is dropped; on new DBs it is already absent. SQLite <3.35 does not
        support DROP COLUMN → pass silently.
        """
        try:
            conn.execute("ALTER TABLE staging DROP COLUMN island")
        except sqlite3.OperationalError:
            pass  # column already absent or DROP COLUMN unsupported (SQLite <3.35)

    @staticmethod
    def _ensure_source_fact_ids_column(conn: sqlite3.Connection) -> None:
        """Add the ``source_fact_ids`` JSON column (M3.3 consolidation).

        Same idempotent try/except ``ALTER`` pattern as semantic's shadow
        columns; existing rows stay ``NULL`` (no backfill needed).
        """
        try:
            conn.execute("ALTER TABLE staging ADD COLUMN source_fact_ids TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists

    @staticmethod
    def _iso_now() -> str:
        return iso_now()

    @staticmethod
    def _resolved_cutoff_iso(older_than_days: int) -> str:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        # ts is stored as ms-Z ISO (see _iso_now) → lexicographic compare is correct.
        return cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _prune_resolved_in_conn(conn: sqlite3.Connection, cutoff_iso: str) -> int:
        """DELETE resolved (promoted/rejected) rows older than the cutoff on ``conn``
        (caller owns the lock/commit). Uses idx_staging_status(status, ts)."""
        cur = conn.execute(
            "DELETE FROM staging WHERE status IN ('promoted', 'rejected') AND ts < ?",
            (cutoff_iso,),
        )
        return int(cur.rowcount)

    def stage(
        self,
        candidate: FactCandidate,
        *,
        conversation_id: str | None = None,
        staged_id: str | None = None,
    ) -> StagedFact:
        """Persist a candidate in the inbox as ``pending``.

        Flood protection: at most ``_MAX_PENDING`` (500) rows may be pending.
        Staging beyond the cap auto-rejects the oldest pending row(s)
        (``status='rejected'``, logged) before accepting the new candidate —
        newest candidates win, the inbox cannot grow without bound.

        Idempotency: re-staging via ``staged_id`` onto an existing row that has
        already been decided (``promoted``/``rejected``) is a NO-OP — INSERT OR
        REPLACE does not override the decision and reset the row to pending; the
        existing row is returned as-is (a warning is logged). A still-``pending``
        row is refreshed.
        """
        sid = staged_id or str(ulid.new())
        ts = self._iso_now()
        key_n = candidate.key.strip()[:256]
        val_n = candidate.value.strip()[:8000]
        src_ids = tuple(candidate.source_fact_ids) if candidate.source_fact_ids else None
        src_json = json.dumps(list(src_ids)) if src_ids else None
        with self._lock:
            conn = self._connect()
            try:
                refresh_pending = False
                if staged_id is not None:
                    existing = conn.execute(
                        "SELECT * FROM staging WHERE id = ?", (sid,)
                    ).fetchone()
                    if existing is not None:
                        if str(existing["status"]) != "pending":
                            log.warning(
                                "stage(%s): row is already %s; keeping the decision (no-op)",
                                sid,
                                existing["status"],
                            )
                            return self._row_to_staged(existing)
                        # Refreshing an existing pending row adds no net new row —
                        # so flood protection must not reject an innocent row.
                        refresh_pending = True
                # Inbox dedup: if a CAPTURE candidate is already pending for the same
                # key, mark it 'rejected' — the newest candidate wins, so old+new don't
                # sit side by side in the inbox (a user-facing bug). The model already
                # holds one value per key (find_contradictions counts same-key-different-value
                # as a conflict) → consistent. EXEMPT: (1) consolidation candidates
                # (source_fact_ids; their own idempotency), (2) session summaries
                # (extractor='session_closer') — summaries accumulate, they are not fact
                # updates (summary dedup is a separate concern, the M5/summarization phase).
                if src_ids is None and candidate.extractor != "session_closer":
                    sup = conn.execute(
                        "UPDATE staging SET status = 'rejected' "
                        "WHERE status = 'pending' AND id != ? AND source_fact_ids IS NULL "
                        "AND akana_fold(key) = ?",
                        (sid, fold_text(key_n)),
                    )
                    if sup.rowcount:
                        log.info(
                            "stage: %d stale pending CAPTURE candidate(s) ('%s') superseded "
                            "by new candidate (inbox dedup)",
                            int(sup.rowcount), key_n,
                        )
                pending = conn.execute(
                    "SELECT COUNT(*) AS n FROM staging WHERE status = 'pending'"
                ).fetchone()
                n_pending = int(pending["n"]) if pending else 0
                if not refresh_pending and n_pending >= _MAX_PENDING:
                    overflow = n_pending - _MAX_PENDING + 1
                    cur = conn.execute(
                        """
                        UPDATE staging SET status = 'rejected'
                        WHERE id IN (
                            SELECT id FROM staging WHERE status = 'pending'
                            ORDER BY ts ASC, rowid ASC LIMIT ?
                        )
                        """,
                        (overflow,),
                    )
                    log.warning(
                        "staging inbox full (%d pending >= cap %d): auto-rejected %d oldest",
                        n_pending,
                        _MAX_PENDING,
                        int(cur.rowcount),
                    )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO staging
                    (id, ts, conversation_id, key, value, reason, trust,
                     source_turn_id, quote, extractor, status,
                     promoted_fact_id, source_fact_ids)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?)
                    """,
                    (
                        sid,
                        ts,
                        conversation_id,
                        key_n,
                        val_n,
                        candidate.reason,
                        candidate.trust,
                        candidate.source_turn_id,
                        candidate.quote,
                        candidate.extractor,
                        src_json,
                    ),
                )
                # Opportunistic retention sweep (audit C34): every _PRUNE_EVERY inserts,
                # drop resolved rows older than the retention window on this same
                # connection (we already hold the lock — a nested prune_resolved() would
                # deadlock on the non-reentrant lock). Pending rows are never touched.
                self._stage_calls += 1
                if self._stage_calls % _PRUNE_EVERY == 0:
                    self._prune_resolved_in_conn(
                        conn, self._resolved_cutoff_iso(_RESOLVED_RETENTION_DAYS)
                    )
                conn.commit()
            finally:
                conn.close()
        return StagedFact(
            id=sid,
            ts=ts,
            key=key_n,
            value=val_n,
            reason=candidate.reason,
            status="pending",
            trust=candidate.trust,
            source_turn_id=candidate.source_turn_id,
            quote=candidate.quote,
            extractor=candidate.extractor,
            conversation_id=conversation_id,
            source_fact_ids=src_ids,
        )

    def list_pending(self, *, limit: int = 50) -> list[StagedFact]:
        return self._query("status = 'pending'", (), limit=limit, order="ts ASC")

    def list_all(self, *, status: StagingStatus | None = None, limit: int = 100) -> list[StagedFact]:
        if status:
            return self._query("status = ?", (status,), limit=limit, order="ts DESC")
        return self._query("1=1", (), limit=limit, order="ts DESC")

    def get(self, staged_id: str) -> StagedFact | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM staging WHERE id = ?", (staged_id,)
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_staged(row) if row else None

    def mark_promoted(self, staged_id: str, fact_id: str) -> bool:
        return self._set_status(staged_id, "promoted", fact_id=fact_id)

    def set_promoted_fact_id(self, staged_id: str, fact_id: str) -> bool:
        """Re-point an already-promoted row's ``promoted_fact_id`` at the real durable id.

        Claim-first promotion (Curator.promote) records a provisional minted id via
        ``mark_promoted`` BEFORE the write; the atomic write may then dedup onto a
        pre-existing fact with a DIFFERENT id. This corrects the link so
        ``promoted_fact_id`` always names the durable fact (Group B review). Unlike
        ``_set_status`` it is not gated on ``status='pending'`` — the row is already
        ``promoted`` (and owned by this caller after a won claim).
        """
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE staging SET promoted_fact_id = ? "
                    "WHERE id = ? AND status = 'promoted'",
                    (fact_id, staged_id),
                )
                conn.commit()
                return int(cur.rowcount) > 0
            finally:
                conn.close()

    def revert_promotion(self, staged_id: str) -> bool:
        """Release a won ``mark_promoted`` claim after the durable write FAILED.

        Claim-first promotion (Curator.promote) flips the row to ``promoted`` with a
        provisional minted fact_id BEFORE calling ``assert_fact``. If that durable write
        raises (e.g. cross-process ``database is locked`` past the busy_timeout, disk
        I/O error), the claim must be released — otherwise the row is stuck ``promoted``
        pointing at a fact that was never written and the user-approved candidate can
        never be re-approved. Only reverts a row still ``promoted`` (idempotent).
        """
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE staging SET status = 'pending', promoted_fact_id = NULL "
                    "WHERE id = ? AND status = 'promoted'",
                    (staged_id,),
                )
                conn.commit()
                return int(cur.rowcount) > 0
            finally:
                conn.close()

    def mark_rejected(self, staged_id: str) -> bool:
        return self._set_status(staged_id, "rejected")

    def count_pending(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM staging WHERE status = 'pending'"
                ).fetchone()
            finally:
                conn.close()
        return int(row["n"]) if row else 0

    def clear(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM staging")
                conn.commit()
                return int(cur.rowcount)
            finally:
                conn.close()

    def _set_status(self, staged_id: str, status: str, *, fact_id: str | None = None) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE staging SET status = ?, promoted_fact_id = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (status, fact_id, staged_id),
                )
                conn.commit()
                return int(cur.rowcount) > 0
            finally:
                conn.close()

    def _query(
        self, where: str, params: tuple[object, ...], *, limit: int, order: str
    ) -> list[StagedFact]:
        sql = (
            f"SELECT * FROM staging WHERE {where} "  # noqa: S608 - static clauses
            f"ORDER BY {order} LIMIT ?"
        )
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, (*params, max(1, min(limit, 1000)))).fetchall()
            finally:
                conn.close()
        return [self._row_to_staged(r) for r in rows]

    @staticmethod
    def _parse_source_fact_ids(raw: object) -> tuple[str, ...] | None:
        if not raw:
            return None
        try:
            ids = json.loads(str(raw))
        except ValueError:
            return None
        return tuple(str(i) for i in ids) if isinstance(ids, list) and ids else None

    @staticmethod
    def _row_to_staged(r: sqlite3.Row) -> StagedFact:
        keys = r.keys()
        return StagedFact(
            id=str(r["id"]),
            ts=str(r["ts"]),
            key=str(r["key"]),
            value=str(r["value"]),
            reason=str(r["reason"] or ""),
            status=r["status"],  # type: ignore[arg-type]
            trust=str(r["trust"] or "inferred"),
            source_turn_id=r["source_turn_id"],
            quote=r["quote"],
            extractor=r["extractor"],
            conversation_id=r["conversation_id"],
            promoted_fact_id=r["promoted_fact_id"],
            source_fact_ids=(
                StagingStore._parse_source_fact_ids(r["source_fact_ids"])
                if "source_fact_ids" in keys
                else None
            ),
        )
