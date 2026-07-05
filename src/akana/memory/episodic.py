"""Episodic memory — durable conversation turns + FTS5 keyword search.

The "what happened" layer of memory: every user/assistant/tool turn is stored
verbatim and is keyword-searchable. Ported from the proven legacy store
(``akana_server.memory.episodic``); pure stdlib, no project imports, so
it is trivially testable against a ``tmp_path`` database.

Design notes:

* **Single file.** ``for_data_dir`` points at ``<data_dir>/db/memory.db`` — the
  unified store the vision asks for (K11). The semantic store shares the same
  file as a separate set of tables; SQLite + WAL handles the concurrent
  short-lived connections.
* **FTS5 with a LIKE fallback.** Some SQLite builds ship without FTS5; the
  keyword search degrades to ``LIKE`` rather than failing.
* **Caller-supplied ids.** ``turn_id`` is minted by the caller (so an SSE meta
  event and the stored row can agree). An UPSERT (``ON CONFLICT(id) DO UPDATE``)
  keeps re-writes of the same id idempotent. ``INSERT OR REPLACE`` is *not* used:
  its implicit delete bypasses the ``AFTER DELETE`` FTS trigger and duplicates
  rows in ``turns_fts``; the UPSERT's UPDATE path fires ``turns_fts_au`` instead.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from akana.memory._time import iso_now

log = logging.getLogger(__name__)

_FTS_MIN_TERM_LEN = 2

# "error" marks a FAILED turn (LLM unavailable / empty response): stored so the UI can
# re-render the error card after a reload, but EXCLUDED from the LLM history window
# (see conversations.recent_llm_messages — it filters to user/assistant only).
Role = Literal["user", "assistant", "system", "tool", "error"]

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    lang TEXT,
    importance REAL,
    tool_call_id TEXT,
    duration_ms INTEGER,
    tool_calls TEXT,
    file_ids TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_conv_ts ON turns(conversation_id, ts);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);
"""

#: When read from conversation-list v2, the /messages reload returns the tool cards
#: (tool_calls), attachments (file_ids) and token/cost info (usage) from these columns.
#: On old memory.db files that lack them, they are added via an idempotent ALTER
#: (ADD COLUMN is cheap, rows become NULL). "usage" is stored on assistant turns with
#: the {prompt, completion, cost_usd?} contract; on user turns it stays NULL.
_TURN_JSON_COLUMNS = ("tool_calls", "file_ids", "usage")


@dataclass(frozen=True, slots=True)
class EpisodicTurn:
    """One stored conversation turn (a ``TurnRef`` in vision terms).

    ``tool_calls``/``file_ids`` are carried only for the /messages reload
    (conversation history) — so tool cards and attached files can be restored.
    Recall/search/explain do not use them.

    ``usage`` is stored on assistant turns as {prompt, completion, cost_usd?}
    (contract v2 item 4); on user turns it is ``None``. On the /messages reload the
    frontend reads this field to preserve token/cost info across a page refresh.
    """

    id: str
    conversation_id: str
    ts: str
    role: Role
    text: str
    lang: str | None = None
    importance: float | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    file_ids: list[str] = field(default_factory=list)
    usage: dict[str, Any] | None = None


class EpisodicStore:
    """SQLite-backed append + keyword-search log of conversation turns."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path.resolve()
        self._lock = threading.Lock()
        #: Last AUTO-assigned turn timestamp (lock-guarded). ``ulid.new()`` is NOT monotonic
        #: within a millisecond, so ``(ts, id)`` ordering would scramble rapid same-ms turns;
        #: keeping auto timestamps strictly increasing per instance keeps turns ordered by ts
        #: in creation order.
        self._last_auto_ts: str | None = None
        self._init_db()

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> EpisodicStore:
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
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                self._migrate_turn_columns(conn)
                self._ensure_fts(conn)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _migrate_turn_columns(conn: sqlite3.Connection) -> None:
        """Add the missing ``tool_calls``/``file_ids`` columns (for old DBs) and
        drop the now-removed ``island`` column (best-effort)."""
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(turns)")}
        for col in _TURN_JSON_COLUMNS:
            if col not in existing:
                conn.execute(f"ALTER TABLE turns ADD COLUMN {col} TEXT")
        # Dropped column migration: the island concept was removed (recall is now global).
        # Best-effort drop — if the column is absent or SQLite <3.35 doesn't know DROP
        # COLUMN, the OperationalError is swallowed and open() never blows up.
        if "island" in existing:
            try:
                conn.execute("ALTER TABLE turns DROP COLUMN island")
            except sqlite3.OperationalError:
                pass  # column already absent or DROP COLUMN unsupported (SQLite <3.35)

    def _ensure_fts(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turns_fts'",
        ).fetchone()
        if row:
            # Existing DBs may predate some sync triggers. Originally only the UPDATE
            # trigger was backfilled here (audit C21): a DB with turns_fts but missing
            # the INSERT/DELETE triggers never indexed new appends (keyword search
            # missed them) and left orphan FTS rows on delete. Backfill ALL THREE with
            # IF NOT EXISTS so any partially-degraded schema is fully repaired.
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS turns_fts_ai AFTER INSERT ON turns BEGIN
                    INSERT INTO turns_fts(turn_id, conversation_id, text)
                    VALUES (new.id, new.conversation_id, new.text);
                END;

                CREATE TRIGGER IF NOT EXISTS turns_fts_ad AFTER DELETE ON turns BEGIN
                    DELETE FROM turns_fts WHERE turn_id = old.id;
                END;

                CREATE TRIGGER IF NOT EXISTS turns_fts_au AFTER UPDATE ON turns BEGIN
                    DELETE FROM turns_fts WHERE turn_id = old.id;
                    INSERT INTO turns_fts(turn_id, conversation_id, text)
                    VALUES (new.id, new.conversation_id, new.text);
                END;
                """
            )
            return
        conn.executescript(
            """
            CREATE VIRTUAL TABLE turns_fts USING fts5(
                turn_id UNINDEXED,
                conversation_id UNINDEXED,
                text,
                tokenize='unicode61'
            );

            CREATE TRIGGER turns_fts_ai AFTER INSERT ON turns BEGIN
                INSERT INTO turns_fts(turn_id, conversation_id, text)
                VALUES (new.id, new.conversation_id, new.text);
            END;

            CREATE TRIGGER turns_fts_ad AFTER DELETE ON turns BEGIN
                DELETE FROM turns_fts WHERE turn_id = old.id;
            END;

            CREATE TRIGGER turns_fts_au AFTER UPDATE ON turns BEGIN
                DELETE FROM turns_fts WHERE turn_id = old.id;
                INSERT INTO turns_fts(turn_id, conversation_id, text)
                VALUES (new.id, new.conversation_id, new.text);
            END;
            """
        )
        conn.execute(
            """
            INSERT INTO turns_fts(turn_id, conversation_id, text)
            SELECT id, conversation_id, text FROM turns
            """
        )
        log.info("episodic FTS5 index created and backfilled")

    @staticmethod
    def _fts_match_query(query: str) -> str | None:
        terms = [
            t
            for t in re.findall(r"[\wğüşöçıİĞÜŞÖÇ]+", query, flags=re.IGNORECASE)
            if len(t) >= _FTS_MIN_TERM_LEN
        ]
        if not terms:
            return None
        return " OR ".join(f'"{t}"' for t in terms[:16])

    @staticmethod
    def _json_list(raw: Any) -> list[Any]:
        """JSON TEXT column → list (corrupt/None → empty list, never raises)."""
        if not raw:
            return []
        try:
            val = json.loads(raw)
            return val if isinstance(val, list) else []
        except (ValueError, TypeError):
            return []

    @staticmethod
    def _json_dict(raw: Any) -> dict[str, Any] | None:
        """JSON TEXT column → dict (corrupt/None → None, never raises)."""
        if not raw:
            return None
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else None
        except (ValueError, TypeError):
            return None

    @classmethod
    def _row_to_turn(cls, r: sqlite3.Row) -> EpisodicTurn:
        # tool_calls/file_ids/usage are selected only in some SELECTs (list_conversation);
        # for queries that don't select them, we fall back to the default when the column is absent.
        keys = r.keys()
        return EpisodicTurn(
            id=r["id"],
            conversation_id=r["conversation_id"],
            ts=r["ts"],
            role=r["role"],  # type: ignore[arg-type]
            text=r["text"],
            lang=r["lang"],
            importance=r["importance"],
            tool_calls=cls._json_list(r["tool_calls"]) if "tool_calls" in keys else [],
            file_ids=cls._json_list(r["file_ids"]) if "file_ids" in keys else [],
            usage=cls._json_dict(r["usage"]) if "usage" in keys else None,
        )

    @staticmethod
    def _iso_now() -> str:
        return iso_now()

    @staticmethod
    def _iso_after(prev_iso: str) -> str:
        """An ISO ms timestamp strictly AFTER ``prev_iso`` — ``max(now, prev + 1ms)``.

        ``list_conversation`` orders by ``(ts, id)``; a same-millisecond user+assistant pair
        would otherwise be tie-broken by id, and a non-monotonic id pair could REVERSE the
        visible order. Stamping the reply strictly after the user turn keeps the pair ordered
        by ts alone, deterministically, regardless of ids or clock resolution."""
        now = datetime.now(UTC)
        try:
            prev = datetime.fromisoformat(prev_iso.replace("Z", "+00:00"))
        except ValueError:  # pragma: no cover - ts is always our own iso format
            return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        # ``prev`` is millisecond-truncated (it came from an isoformat(timespec="milliseconds")
        # string), but ``now`` carries microseconds. Comparing raw ``now`` against ``prev`` lets
        # a ``now`` in the SAME millisecond but with nonzero microseconds slip past (``now > prev``)
        # yet render to the identical ms string → a colliding, non-increasing timestamp. Truncate
        # ``now`` to milliseconds first so the "strictly after" guarantee holds at the ms grain we emit.
        now = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        if now <= prev:
            now = prev + timedelta(milliseconds=1)
        return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def append_turn(
        self,
        *,
        turn_id: str,
        conversation_id: str,
        role: Role,
        text: str,
        lang: str | None = None,
        importance: float | None = None,
        tool_call_id: str | None = None,
        duration_ms: int | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        file_ids: list[str] | None = None,
        usage: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> EpisodicTurn:
        tc_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        fi_json = json.dumps(file_ids, ensure_ascii=False) if file_ids else None
        # usage is meaningful only on assistant turns ({prompt, completion, cost_usd?});
        # an empty dict or None → store NULL (no needless JSON carrying).
        us_json = json.dumps(usage, ensure_ascii=False) if usage else None
        with self._lock:
            if ts:
                ts_val = ts  # caller-supplied ts is respected verbatim
            else:
                # AUTO ts: strictly increasing per instance so rapid same-ms turns stay ordered
                # by ts (ulid.new() ids are not monotonic within a ms → can't be the tiebreaker).
                ts_val = self._iso_now()
                if self._last_auto_ts is not None and ts_val <= self._last_auto_ts:
                    ts_val = self._iso_after(self._last_auto_ts)
                self._last_auto_ts = ts_val
            conn = self._connect()
            try:
                # UPSERT, not INSERT OR REPLACE: REPLACE's implicit delete does
                # not fire the AFTER DELETE FTS trigger, so re-writing the same
                # turn_id would leave a duplicate (stale) row in turns_fts.
                conn.execute(
                    """
                    INSERT INTO turns
                    (id, conversation_id, ts, role, text, lang, importance,
                     tool_call_id, duration_ms, tool_calls, file_ids, usage)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        conversation_id = excluded.conversation_id,
                        ts = excluded.ts,
                        role = excluded.role,
                        text = excluded.text,
                        lang = excluded.lang,
                        importance = excluded.importance,
                        tool_call_id = excluded.tool_call_id,
                        duration_ms = excluded.duration_ms,
                        tool_calls = excluded.tool_calls,
                        file_ids = excluded.file_ids,
                        usage = excluded.usage
                    """,
                    (
                        turn_id,
                        conversation_id,
                        ts_val,
                        role,
                        text,
                        lang,
                        importance,
                        tool_call_id,
                        duration_ms,
                        tc_json,
                        fi_json,
                        us_json,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return EpisodicTurn(
            id=turn_id,
            conversation_id=conversation_id,
            ts=ts_val,
            role=role,
            text=text,
            lang=lang,
            importance=importance,
            tool_calls=list(tool_calls or []),
            file_ids=list(file_ids or []),
            usage=dict(usage) if usage else None,
        )

    def get_turn(self, turn_id: str) -> EpisodicTurn | None:
        """Fetch one turn by id."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT id, conversation_id, ts, role, text, lang, importance,
                           tool_calls, file_ids, usage
                    FROM turns WHERE id = ?
                    """,
                    (turn_id,),
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_turn(row) if row else None

    def list_conversation_recent(
        self,
        conversation_id: str,
        *,
        limit: int = 200,
        before_ts: str | None = None,
        before_id: str | None = None,
        roles: tuple[str, ...] | None = None,
    ) -> list[EpisodicTurn]:
        """Return the NEWEST ``limit`` turns (in chronological/ASC order); if
        ``before_ts`` is given, fetch the window BEFORE that moment (pagination).

        ``list_conversation`` took the OLDEST 1000 with ``ts ASC LIMIT 1000`` and then
        sliced ``[-limit:]`` in Python → in a 1000+ turn conversation the newest messages
        NEVER arrived, and ``before_ts`` wasn't passed to SQL (R2-B4). Here we fetch the
        window in SQL with ``ts DESC LIMIT ?`` (+ optional ``ts < ?``) and then flip it to
        ASC → a correct "newest N" + cheap pagination.

        PAGINATION CURSOR (R4-C #2): if ``before_id`` is also given, a keyset cursor
        ``(ts < ?) OR (ts = ? AND id < ?)`` is used — EXACTLY consistent with
        ``ORDER BY ts DESC, id DESC``. With ``before_ts`` alone (``ts < ?``), if turns
        with the same ms ``ts`` are split by the ``LIMIT`` at a page boundary, the
        sibling at the boundary would be DROPPED without appearing on ANY page (a
        data-visibility loss). The keyset closes this.
        """
        lim = max(1, min(limit, 1000))
        where = "WHERE conversation_id = ?"
        params: list[object] = [conversation_id]
        if roles:
            # audit C24: window by role IN SQL so a caller wanting the newest N
            # user/assistant turns isn't starved by a long tail of tool/system turns.
            where += f" AND role IN ({','.join('?' * len(roles))})"
            params += list(roles)
        if before_ts and before_id:
            where += " AND (ts < ? OR (ts = ? AND id < ?))"
            params += [before_ts, before_ts, before_id]
        elif before_ts:
            where += " AND ts < ?"
            params.append(before_ts)
        params.append(lim)
        # LOCK-FREE READ (WAL snapshot): the per-turn LLM context read
        # (``recent_llm_messages``) + ``/messages`` pagination go through this path. The
        # global lock only serializes WRITES (``append_turn``); this read must NOT wait on
        # streaming-time writes — a locked version brought back the ~2s stall (switching to
        # a conversation while streaming) that was the very reason ``list_conversation`` was
        # made lock-free (R4-C). A separate connection + WAL → a consistent snapshot with a concurrent writer.
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT id, conversation_id, ts, role, text, lang, importance,
                       tool_calls, file_ids, usage
                FROM turns
                {where}
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        finally:
            conn.close()
        # We fetched DESC (newest N / before_ts window) → hand the consumer chronological order.
        return [self._row_to_turn(r) for r in reversed(rows)]

    def newest_turn(self, conversation_id: str) -> EpisodicTurn | None:
        """Fetch the newest turn in a SINGLE row (``ts DESC, id DESC LIMIT 1``).

        The conversation list (sidebar) derives a preview + last_message_at for each
        row; doing this via ``list_conversation`` meant fetching 1000 rows per
        conversation (+ JSON parsing) and taking ``[-1]`` → an N×M (N conversations × M
        turns) N+1 problem. This query reads exactly 1 row per conversation.

        ``id`` as a secondary sort: for turns with the same ms ``ts`` (e.g. concurrent
        writes) it selects EXACTLY the same row as ``list_conversation``'s ``[-1]``
        (``ts ASC, id ASC``) (ULID is monotonic → id order = creation order). Without the
        tiebreaker, the DESC-first and the ASC-last could be different rows.
        """
        # LOCK-FREE READ (WAL snapshot): the sidebar calls this for EVERY row → a locked
        # version would queue the N-conversation list behind concurrent writes. A separate
        # connection + WAL gives a consistent snapshot (same rationale as ``list_conversation``).
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT id, conversation_id, ts, role, text, lang, importance,
                       tool_calls, file_ids, usage
                FROM turns
                WHERE conversation_id = ?
                ORDER BY ts DESC, id DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_turn(row) if row else None

    def search_keyword(
        self,
        query: str,
        *,
        conversation_id: str | None = None,
        limit: int = 20,
    ) -> list[EpisodicTurn]:
        q = query.strip()
        if not q:
            return []
        lim = max(1, min(limit, 100))
        fts_q = self._fts_match_query(q)
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_fts(conn)
                rows: list[sqlite3.Row] = []
                # LIKE is the *error* fallback (FTS5 missing/broken), plus the
                # only path when the query has no FTS-usable token. A legitimate
                # zero-hit FTS result stays zero — no second LIKE table scan.
                fallback_to_like = fts_q is None
                if fts_q:
                    try:
                        if conversation_id:
                            rows = conn.execute(
                                """
                                SELECT t.id, t.conversation_id, t.ts, t.role, t.text,
                                       t.lang, t.importance
                                FROM turns_fts f
                                INNER JOIN turns t ON t.id = f.turn_id
                                WHERE turns_fts MATCH ?
                                  AND f.conversation_id = ?
                                ORDER BY bm25(turns_fts)
                                LIMIT ?
                                """,
                                (fts_q, conversation_id, lim),
                            ).fetchall()
                        else:
                            rows = conn.execute(
                                """
                                SELECT t.id, t.conversation_id, t.ts, t.role, t.text,
                                       t.lang, t.importance
                                FROM turns_fts f
                                INNER JOIN turns t ON t.id = f.turn_id
                                WHERE turns_fts MATCH ?
                                ORDER BY bm25(turns_fts)
                                LIMIT ?
                                """,
                                (fts_q, lim),
                            ).fetchall()
                    except sqlite3.OperationalError as e:
                        log.debug("FTS search failed, falling back to LIKE: %s", e)
                        fallback_to_like = True
                        rows = []
                if fallback_to_like:
                    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    pattern = f"%{escaped}%"
                    if conversation_id:
                        rows = conn.execute(
                            """
                            SELECT id, conversation_id, ts, role, text, lang, importance
                            FROM turns
                            WHERE conversation_id = ? AND text LIKE ? ESCAPE '\\'
                            ORDER BY ts DESC
                            LIMIT ?
                            """,
                            (conversation_id, pattern, lim),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            """
                            SELECT id, conversation_id, ts, role, text, lang, importance
                            FROM turns
                            WHERE text LIKE ? ESCAPE '\\'
                            ORDER BY ts DESC
                            LIMIT ?
                            """,
                            (pattern, lim),
                        ).fetchall()
            finally:
                conn.close()
        return [self._row_to_turn(r) for r in rows]

    def list_conversation_ids(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT conversation_id, MAX(ts) AS last_ts, COUNT(*) AS turn_count
                    FROM turns
                    GROUP BY conversation_id
                    ORDER BY last_ts DESC
                    LIMIT ?
                    """,
                    (max(1, min(limit, 200)),),
                ).fetchall()
            finally:
                conn.close()
        return [
            {
                "conversation_id": str(r["conversation_id"]),
                "last_ts": str(r["last_ts"]),
                "turn_count": int(r["turn_count"]),
            }
            for r in rows
        ]

    def count_conversations(self) -> int:
        """Total distinct conversations — uncapped COUNT (audit C27).

        ``list_conversation_ids`` clamps to 200 rows for the dashboard list; stats
        needs the true total, so this counts distinct conversation_ids directly.
        """
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT conversation_id) AS n FROM turns"
                ).fetchone()
            finally:
                conn.close()
        return int(row["n"]) if row else 0

    def count_turns(self) -> int:
        """Total turn rows across every conversation — uncapped COUNT (audit C27)."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT COUNT(*) AS n FROM turns").fetchone()
            finally:
                conn.close()
        return int(row["n"]) if row else 0

    def delete_conversation(self, conversation_id: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "DELETE FROM turns WHERE conversation_id = ?",
                    (conversation_id,),
                )
                conn.commit()
                return int(cur.rowcount)
            finally:
                conn.close()
