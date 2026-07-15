"""Semantic memory — durable facts with evidence, trust and temporal validity.

The "what is true" layer. Ported from the legacy store
(``akana_server.memory.semantic``) and extended with the metadata the
memory vision treats as non-negotiable, so the data model is right from day one
rather than retrofitted later:

* **Evidence (P5).** Every fact may carry ``source_turn_id`` + ``quote`` +
  ``extractor`` — the receipt that says *why we believe this*. M3 (Curator) is
  what populates it from chat; M1 just gives it a home.
* **Trust ladder (P6).** ``user_statement > inferred > tool_output > synthesis``.
  Recall (M2) filters on a ``min_trust`` floor; the store is policy-free and
  defaults new facts to ``inferred`` (K15).
* **Temporal validity (P6/P8).** ``valid_from`` + ``invalidated_at``. Facts are
  never silently overwritten — superseding invalidates the old row and inserts a
  new one, so the history survives for replay.

Self-contained: pure stdlib + ``ulid``, no project imports, so it is trivially
testable against a ``tmp_path`` database. Search uses a minimal local tokenizer;
the richer Turkish recall tokenizer arrives with the M2 Recall layer.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import ulid

from akana.memory._time import iso_now
from akana.memory.terms import escape_like, fold_text, recall_search_terms

log = logging.getLogger(__name__)

Trust = Literal["user_statement", "inferred", "tool_output", "synthesis"]

# Higher rank == more trustworthy. A ``min_trust`` floor admits everything at or
# above it (P6). The trust ladder's SINGLE canonical definition — every consumer
# (recall floor, fusion, UI ordering) compares through :func:`trust_rank`.
TRUST_RANK: dict[str, int] = {
    "user_statement": 3,
    "inferred": 2,
    "tool_output": 1,
    "synthesis": 0,
}
_DEFAULT_TRUST: Trust = "inferred"  # K15

# Upper bound on the search limit — the list_facts route promises up to 500; the
# requested limit is honored up to this value (the old fixed 50 clamp broke that promise).
_SEARCH_LIMIT_MAX = 500

# Provenance (citation-native): where a memory record came from. The origin
# enum mirrors the trust ladder values; ``legacy`` marks rows written before
# the source columns existed (migration default — never used on new writes).
LEGACY_ORIGIN = "legacy"
SOURCE_ORIGINS: tuple[str, ...] = (*TRUST_RANK, LEGACY_ORIGIN)


def trust_rank(trust: str | None) -> int:
    """Comparable ladder position; unknown/None ranks below everything."""
    return TRUST_RANK.get(trust or "", -1)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    ts_first TEXT NOT NULL,
    ts_last TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL,
    importance REAL,
    anchored INTEGER DEFAULT 0,
    decay_rate REAL DEFAULT 0.01,
    trust TEXT NOT NULL DEFAULT 'inferred',
    source_turn_id TEXT,
    quote TEXT,
    extractor TEXT,
    valid_from TEXT,
    invalidated_at TEXT,
    source_origin TEXT,
    source_detail TEXT,
    observed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(key);
CREATE INDEX IF NOT EXISTS idx_facts_valid ON facts(invalidated_at);
"""


@dataclass(frozen=True, slots=True)
class SemanticFact:
    """One durable fact plus its evidence, trust and validity window."""

    id: str
    key: str
    value: str
    ts_first: str
    ts_last: str
    confidence: float
    importance: float
    anchored: bool
    trust: Trust = _DEFAULT_TRUST
    source_turn_id: str | None = None
    quote: str | None = None
    extractor: str | None = None
    valid_from: str | None = None
    invalidated_at: str | None = None
    # Salience (§13/M3.1): how often recall returned this fact and the LLM
    # actually used it (hit) vs ignored it (miss).
    hit_count: int = 0
    miss_count: int = 0
    last_hit_at: str | None = None
    # Provenance (citation-native): where this record came from. Mandatory on
    # every write path (derived from trust/extractor when not explicit);
    # ``legacy`` only ever comes from the migration backfill.
    source_origin: str = LEGACY_ORIGIN
    source_detail: str | None = None
    observed_at: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.invalidated_at is None

    @property
    def source(self) -> dict[str, str | None]:
        """The §provenance contract: ``{origin, detail, observed_at}``."""
        return {
            "origin": self.source_origin,
            "detail": self.source_detail,
            "observed_at": self.observed_at,
        }


def _trust_allowset(min_trust: str) -> list[str]:
    floor = TRUST_RANK.get(min_trust, TRUST_RANK[_DEFAULT_TRUST])
    return [t for t, rank in TRUST_RANK.items() if rank >= floor]


class SemanticStore:
    """SQLite-backed store of durable facts (shares ``memory.db`` with episodic)."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path.resolve()
        self._lock = threading.Lock()
        self._init_db()

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> SemanticStore:
        db_dir = data_dir / "db"
        db_dir.mkdir(parents=True, exist_ok=True)
        return cls(db_dir / "memory.db")

    def _connect(self, *, immediate: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        # Two processes (server + MCP subprocess) share memory.db; without a
        # busy timeout a write-txn in the other process raises a raw
        # "database is locked" instead of waiting.
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        if immediate:
            # Manual transaction control so a read-then-write critical section can
            # `BEGIN IMMEDIATE` — grabbing the RESERVED write lock at txn START, which
            # serializes the two processes sharing memory.db for that whole section
            # (audit C0/C4/C14). Without it, sqlite's default deferred isolation lets
            # both processes' dedup SELECTs miss the same row and both INSERT.
            conn.isolation_level = None
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SCHEMA)
                self._ensure_norm_columns(conn)
                self._ensure_salience_columns(conn)
                self._ensure_source_columns(conn)
                self._drop_island_column(conn)
                # After the norm columns exist + are backfilled: enforce "one valid
                # row per folded (key_norm,value_norm)" as a DB invariant (audit
                # C0/C4/C5/C14). Must run last — it dedups against the backfilled cols.
                self._ensure_unique_valid_index(conn)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _drop_island_column(conn: sqlite3.Connection) -> None:
        """Drop the now-removed ``island`` column + its index (best-effort).

        The island concept was removed (recall is now global). The same idempotent
        try/except pattern: if the column/index is absent or SQLite <3.35 doesn't know
        DROP COLUMN, the OperationalError is swallowed and open() never blows up.
        """
        try:
            conn.execute("DROP INDEX IF EXISTS idx_facts_island")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE facts DROP COLUMN island")
        except sqlite3.OperationalError:
            pass  # column already absent or DROP COLUMN unsupported (SQLite <3.35)

    @staticmethod
    def _ensure_norm_columns(conn: sqlite3.Connection) -> None:
        """Add + backfill ``key_norm``/``value_norm`` shadow columns.

        SQLite's ``LIKE`` only case-folds ASCII, so a stored ``'İstanbul'``
        never matches ``'%istanbul%'``. Searches run against these columns,
        filled with :func:`fold_text` (Turkish-aware fold). ``ALTER TABLE`` is
        idempotent via try/except so existing DBs migrate in place.
        """
        for col in ("key_norm", "value_norm"):
            try:
                conn.execute(f"ALTER TABLE facts ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.create_function("akana_fold", 1, fold_text, deterministic=True)
        conn.execute(
            "UPDATE facts SET key_norm = akana_fold(key), value_norm = akana_fold(value) "
            "WHERE key_norm IS NULL OR value_norm IS NULL"
        )

    @staticmethod
    def _ensure_salience_columns(conn: sqlite3.Connection) -> None:
        """Add + backfill the salience counters (§13/M3.1).

        Same idempotent try/except ``ALTER`` pattern as the norm columns, so
        existing DBs migrate in place. SQLite applies the constant ``DEFAULT 0``
        to existing rows on ``ADD COLUMN``; the backfill ``UPDATE`` covers rows
        a future partial migration might leave ``NULL``.
        """
        for col, decl in (
            ("hit_count", "INTEGER DEFAULT 0"),
            ("miss_count", "INTEGER DEFAULT 0"),
            ("last_hit_at", "TEXT"),
        ):
            try:
                conn.execute(f"ALTER TABLE facts ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.execute(
            "UPDATE facts SET hit_count = IFNULL(hit_count, 0), "
            "miss_count = IFNULL(miss_count, 0) "
            "WHERE hit_count IS NULL OR miss_count IS NULL"
        )

    @staticmethod
    def _ensure_unique_valid_index(conn: sqlite3.Connection) -> None:
        """Enforce 'at most one VALID row per folded (key_norm, value_norm)' (audit
        C0/C4/C5/C14) via a partial UNIQUE index — the cross-process arbiter that a
        double-insert cannot slip past.

        The ``CREATE UNIQUE INDEX`` fails if valid duplicates already exist, so dedup
        FIRST: keep the newest/most-important survivor per folded group and INVALIDATE
        (never DELETE) the losers, so history/replay survive. Idempotent — a second
        open collapses nothing and ``IF NOT EXISTS`` skips the create. Guarded so an
        ancient SQLite (no window functions / no partial indexes) degrades gracefully
        to the in-lock + ``BEGIN IMMEDIATE`` serialization instead of bricking open().
        """
        try:
            cur = conn.execute(
                """
                UPDATE facts SET invalidated_at = ts_last
                WHERE invalidated_at IS NULL AND rowid NOT IN (
                    SELECT rowid FROM (
                        SELECT rowid, ROW_NUMBER() OVER (
                            PARTITION BY IFNULL(key_norm, key), IFNULL(value_norm, value)
                            ORDER BY importance DESC, ts_last DESC, rowid DESC
                        ) AS rn
                        FROM facts WHERE invalidated_at IS NULL
                    ) WHERE rn = 1
                )
                """
            )
            if cur.rowcount:
                log.warning(
                    "semantic: collapsed %d duplicate valid fact row(s) before the "
                    "unique-valid index (older duplicates invalidated; history kept)",
                    int(cur.rowcount),
                )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_valid_uniq "
                "ON facts(key_norm, value_norm) WHERE invalidated_at IS NULL"
            )
        except sqlite3.OperationalError:
            log.warning(
                "semantic: unique-valid index unavailable (old SQLite?) — relying on "
                "in-lock + BEGIN IMMEDIATE serialization only",
                exc_info=True,
            )

    @staticmethod
    def _ensure_source_columns(conn: sqlite3.Connection) -> None:
        """Add + backfill the provenance columns (citation-native source).

        Same idempotent try/except ``ALTER`` pattern as the norm/salience
        columns. Pre-existing rows get ``source_origin='legacy'`` (their true
        origin is unknown) and ``observed_at`` falls back to ``ts_first`` —
        the closest honest stand-in for when the record was observed.
        """
        for col in ("source_origin", "source_detail", "observed_at"):
            try:
                conn.execute(f"ALTER TABLE facts ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.execute(
            "UPDATE facts SET source_origin = ?, "
            "source_detail = COALESCE(source_detail, extractor), "
            "observed_at = COALESCE(observed_at, ts_first) "
            "WHERE source_origin IS NULL",
            (LEGACY_ORIGIN,),
        )

    @staticmethod
    def _iso_now() -> str:
        return iso_now()

    @staticmethod
    def _next_ms(iso_ts: str) -> str:
        """Smallest canonical millisecond strictly greater than ``iso_ts``.

        Used to keep a superseded fact's validity window non-empty when the
        supersede lands in the same millisecond as the fact's ``valid_from`` (see
        :meth:`supersede_fact`). Input is always an :meth:`_iso_now`-shaped string.
        """
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00")) + timedelta(milliseconds=1)
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _canon_ts(ts: str) -> str:
        """Re-emit any ISO-8601 instant in canonical millisecond-``Z`` UTC form (audit C8).

        Temporal compares (:meth:`_next_ms`, ``facts_as_of``) assume the
        :meth:`_iso_now` shape (ms precision, ``Z`` suffix). A caller-supplied
        ``valid_from``/``observed_at`` like ``'2026-01-01T00:00:00+00:00'`` would
        sort wrong lexicographically (``'+'`` < ``'Z'``); normalize it to the same
        shape. Unparseable input is returned unchanged (best-effort).
        """
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def upsert_fact(
        self,
        *,
        fact_id: str,
        key: str,
        value: str,
        confidence: float = 0.85,
        importance: float = 0.7,
        anchored: bool = False,
        trust: Trust = _DEFAULT_TRUST,
        source_turn_id: str | None = None,
        quote: str | None = None,
        extractor: str | None = None,
        valid_from: str | None = None,
        source_origin: str | None = None,
        source_detail: str | None = None,
        observed_at: str | None = None,
    ) -> SemanticFact:
        with self._lock:
            conn = self._connect()
            try:
                fact = self._upsert_in_conn(
                    conn,
                    fact_id=fact_id,
                    key=key,
                    value=value,
                    confidence=confidence,
                    importance=importance,
                    anchored=anchored,
                    trust=trust,
                    source_turn_id=source_turn_id,
                    quote=quote,
                    extractor=extractor,
                    valid_from=valid_from,
                    source_origin=source_origin,
                    source_detail=source_detail,
                    observed_at=observed_at,
                )
                conn.commit()
            finally:
                conn.close()
        return fact

    def assert_fact(
        self,
        *,
        key: str,
        value: str,
        trust: Trust = _DEFAULT_TRUST,
        confidence: float = 0.85,
        importance: float = 0.7,
        anchored: bool = False,
        source_turn_id: str | None = None,
        quote: str | None = None,
        extractor: str | None = None,
        source_origin: str | None = None,
        source_detail: str | None = None,
        observed_at: str | None = None,
        supersede: bool = True,
        fact_id: str | None = None,
    ) -> tuple[list[SemanticFact], SemanticFact]:
        """Contradiction-aware durable write in ONE atomic transaction (audit C0/C4/C14).

        Folds find-contradictions + invalidate + dedup-upsert into a single
        ``BEGIN IMMEDIATE`` transaction under the store lock, so the check-then-act is
        atomic in-process (the lock) AND across the two processes sharing memory.db
        (the RESERVED write lock + the partial unique-valid index). This replaces the
        old get→find→write→retry dance in ``Curator.promote`` / ``mutate.remember(direct)``
        that raced two concurrent writers into two conflicting valid rows.

        With ``supersede`` on, EVERY currently-valid row under the folded ``key`` whose
        value differs from ``value`` is invalidated (so at most one value stays valid per
        key), then ``value`` is upserted with valid_from = the supersede instant so the
        windows tile gaplessly (audit C7). Returns ``(closed, new)`` — ``closed`` is the
        list of invalidated contradictions (empty when there was no conflict); the caller
        emits vector/graph/ledger events AFTER this returns.
        """
        key_n = key.strip()[:256]
        val_n = value.strip()[:8000]
        key_fold = fold_text(key_n)
        val_fold = fold_text(val_n)
        with self._lock:
            conn = self._connect(immediate=True)
            try:
                conn.execute("BEGIN IMMEDIATE")
                closed: list[SemanticFact] = []
                supersede_ts: str | None = None
                if supersede:
                    rows = conn.execute(
                        "SELECT * FROM facts WHERE IFNULL(key_norm, key) = ? "
                        "AND IFNULL(value_norm, value) != ? AND invalidated_at IS NULL",
                        (key_fold, val_fold),
                    ).fetchall()
                    for r in rows:
                        old = self._row_to_fact(r)
                        ts = self._iso_now()
                        old_vf = old.valid_from or old.ts_first
                        if old_vf and ts <= old_vf:  # keep a non-empty validity window
                            ts = self._next_ms(old_vf)
                        conn.execute(
                            "UPDATE facts SET invalidated_at = ?, ts_last = ? WHERE id = ?",
                            (ts, ts, old.id),
                        )
                        closed.append(replace(old, invalidated_at=ts, ts_last=ts))
                        if supersede_ts is None or ts > supersede_ts:
                            supersede_ts = ts
                new = self._upsert_in_conn(
                    conn,
                    fact_id=fact_id or str(ulid.new()),
                    key=key_n,
                    value=val_n,
                    confidence=confidence,
                    importance=importance,
                    anchored=anchored,
                    trust=trust,
                    source_turn_id=source_turn_id,
                    quote=quote,
                    extractor=extractor,
                    valid_from=supersede_ts,
                    set_valid_from=supersede_ts is not None,
                    source_origin=source_origin,
                    source_detail=source_detail,
                    observed_at=observed_at,
                )
                conn.commit()
                return closed, new
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _upsert_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        fact_id: str,
        key: str,
        value: str,
        confidence: float = 0.85,
        importance: float = 0.7,
        anchored: bool = False,
        trust: Trust = _DEFAULT_TRUST,
        source_turn_id: str | None = None,
        quote: str | None = None,
        extractor: str | None = None,
        valid_from: str | None = None,
        set_valid_from: bool = False,
        source_origin: str | None = None,
        source_detail: str | None = None,
        observed_at: str | None = None,
    ) -> SemanticFact:
        """Dedup + insert/update on a caller-owned connection (no lock/commit).

        Shared by :meth:`upsert_fact` and the atomic :meth:`supersede_fact` so
        both go through the same dedup semantics; the caller commits.

        Provenance is mandatory: when the caller is not explicit, ``origin``
        derives from the trust ladder value, ``detail`` from the extractor (or
        the evidence turn) and ``observed_at`` from the write moment — a new
        row can never land without a source.
        """
        if source_origin is not None and source_origin not in SOURCE_ORIGINS:
            raise ValueError(
                f"source_origin must be one of {SOURCE_ORIGINS}, got {source_origin!r}"
            )
        key_n = key.strip()[:256]
        val_n = value.strip()[:8000]
        quote_n = quote.strip()[:2000] if quote else None
        # Normalize an unknown/invalid trust AT WRITE TIME. A value outside TRUST_RANK
        # (a typo, "explicit"/"high"...) is in no _trust_allowset, so the fact is
        # INVISIBLE in recall even at the loosest floor (a silent loss). Bring it down to
        # a valid ladder value + warn. (R2-B4: an invalid trust = a dead fact.)
        if trust not in TRUST_RANK:
            log.warning(
                "semantic: bilinmeyen trust %r → %r normalize edildi (key=%s)",
                trust, _DEFAULT_TRUST, key_n,
            )
            trust = _DEFAULT_TRUST
        ts = self._iso_now()
        origin = source_origin or trust
        detail = (source_detail or "").strip()[:512] or extractor or (
            f"turn:{source_turn_id}" if source_turn_id else None
        )
        observed = self._canon_ts(observed_at) if observed_at else ts
        # Canonicalize a caller-supplied valid_from up front (audit C8) so every
        # downstream compare/store uses the same ms-Z shape.
        vf_canon = self._canon_ts(valid_from) if valid_from else None
        # Only de-dup against *currently valid* rows; re-asserting a fact
        # that was invalidated correctly creates a fresh row.
        #
        # Matching is Turkish-folded (consistent with _is_known):
        # a raw ``key = ? AND value = ?`` exact-match could not map a re-assertion of
        # "İstanbul" then "istanbul" (or key "Email" vs "email") onto the SAME row and would
        # open a SECOND valid row → two valid facts under the same logical key, both
        # surfacing in recall. The comparison goes through the folded shadow columns
        # (key_norm/value_norm); IFNULL covers rows an older process did not fill. The
        # STORED value keeps its original casing; only the MATCH is folded.
        row = conn.execute(
            """
            SELECT id, key, value, ts_first, valid_from, trust, source_origin,
                   source_detail, confidence, source_turn_id, quote, extractor
            FROM facts
            WHERE IFNULL(key_norm, key) = ? AND IFNULL(value_norm, value) = ?
              AND invalidated_at IS NULL
            """,
            (fold_text(key_n), fold_text(val_n)),
        ).fetchone()
        if row:
            fact_id = str(row["id"])
            # A dedup-hit leaves value/value_norm (and key) untouched in the UPDATEs
            # below ("the STORED value keeps its original casing"), so the returned
            # fact — and every downstream consumer (_emit_fact → graph relink, the
            # vector sidecar hash, the API/timeline record) — must echo the STORED
            # spelling, not the fold-equal INCOMING one ('İstanbul' vs 'istanbul'),
            # or sqlite and the projections diverge.
            key_eff = str(row["key"])
            value_eff = str(row["value"])
            ts_first = str(row["ts_first"])
            existing_vf = str(row["valid_from"]) if row["valid_from"] else ts_first
            # Upgrade-only trust ladder (P6): a re-assertion that dedups onto an existing
            # valid row must never DOWNGRADE its trust/provenance/confidence. Approving an
            # 'inferred' inbox duplicate of a 'user_statement' fact would otherwise silently
            # demote it below a min_trust recall floor. When the incoming write ranks LOWER,
            # keep the stored trust/origin/detail and the higher confidence.
            stored_trust = str(row["trust"] or _DEFAULT_TRUST)
            if trust_rank(trust) < trust_rank(stored_trust):
                trust = stored_trust  # type: ignore[assignment]
                if row["source_origin"]:
                    origin = str(row["source_origin"])
                if row["source_detail"]:
                    detail = str(row["source_detail"])
                if row["confidence"] is not None:
                    confidence = max(confidence, float(row["confidence"]))
                # Provenance travels as one unit with trust: the UPDATEs below would
                # otherwise stamp the lower-trust duplicate's turn/quote/extractor onto
                # a row whose trust/origin/detail we just chose to keep — a mixed-
                # provenance row (user_statement trust, inferred quote).
                if row["source_turn_id"]:
                    source_turn_id = str(row["source_turn_id"])
                if row["quote"]:
                    quote_n = str(row["quote"])
                if row["extractor"]:
                    extractor = str(row["extractor"])
            if set_valid_from and vf_canon:
                # C7: a supersede whose replacement dedups onto a DIFFERENT pre-existing
                # valid row must carry its supersede instant onto that row — otherwise the
                # passed valid_from is dropped and the [valid_from, invalidated_at) windows
                # stop tiling gaplessly. Plain upserts (set_valid_from=False) never touch it.
                # Only ever move valid_from EARLIER: tiling needs the row valid AT the
                # supersede instant, which min() already guarantees, and moving it forward
                # would erase validity the row genuinely had (facts_as_of would return
                # nothing for a period the fact was valid). existing_vf/vf_canon are both
                # canonical ms-Z, so lexicographic min == temporal min.
                valid_from_eff = min(existing_vf, vf_canon)
                conn.execute(
                    """
                    UPDATE facts SET ts_last = ?, confidence = ?, importance = ?,
                        anchored = ?, trust = ?, source_turn_id = ?, quote = ?,
                        extractor = ?, source_origin = ?, source_detail = ?,
                        observed_at = ?, valid_from = ?
                    WHERE id = ?
                    """,
                    (
                        ts, confidence, importance, 1 if anchored else 0, trust,
                        source_turn_id, quote_n, extractor, origin, detail, observed,
                        valid_from_eff, fact_id,
                    ),
                )
            else:
                valid_from_eff = existing_vf
                conn.execute(
                    """
                    UPDATE facts SET ts_last = ?, confidence = ?, importance = ?,
                        anchored = ?, trust = ?, source_turn_id = ?, quote = ?,
                        extractor = ?, source_origin = ?, source_detail = ?,
                        observed_at = ?
                    WHERE id = ?
                    """,
                    (
                        ts, confidence, importance, 1 if anchored else 0, trust,
                        source_turn_id, quote_n, extractor, origin, detail, observed,
                        fact_id,
                    ),
                )
        else:
            key_eff = key_n
            value_eff = val_n
            ts_first = ts
            # C6: canonicalized valid_from, clamped so a FUTURE valid_from can never
            # push the window past 'now' (which would hide the fact from facts_as_of(now)).
            # EXCEPT under set_valid_from: that is the internal supersede instant, which
            # FIX 1 may have bumped 1ms past 'now' precisely so the old row keeps a
            # non-empty window — clamping it back would give the new row the SAME
            # valid_from as the old one and make facts_as_of see both at that instant.
            # It must be carried verbatim so the windows tile (the dedup-hit branch
            # above already trusts it unconditionally).
            valid_from_eff = vf_canon or ts
            if valid_from_eff > ts and not set_valid_from:
                valid_from_eff = ts
            conn.execute(
                """
                INSERT INTO facts
                (id, ts_first, ts_last, key, value, confidence, importance,
                 anchored, trust, source_turn_id, quote, extractor,
                 valid_from, invalidated_at, key_norm, value_norm,
                 source_origin, source_detail, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    fact_id,
                    ts_first,
                    ts,
                    key_n,
                    val_n,
                    confidence,
                    importance,
                    1 if anchored else 0,
                    trust,
                    source_turn_id,
                    quote_n,
                    extractor,
                    valid_from_eff,
                    fold_text(key_n),
                    fold_text(val_n),
                    origin,
                    detail,
                    observed,
                ),
            )
        return SemanticFact(
            id=fact_id,
            key=key_eff,
            value=value_eff,
            ts_first=ts_first,
            ts_last=ts,
            confidence=confidence,
            importance=importance,
            anchored=anchored,
            trust=trust,
            source_turn_id=source_turn_id,
            quote=quote_n,
            extractor=extractor,
            valid_from=valid_from_eff,
            invalidated_at=None,
            source_origin=origin,
            source_detail=detail,
            observed_at=observed,
        )

    def list_all_facts(self) -> list[SemanticFact]:
        """Return all valid facts for projection rebuild (not UI-paginated)."""
        return self.list_facts(limit=50_000)

    def list_facts(
        self,
        *,
        min_trust: str | None = None,
        include_invalidated: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SemanticFact]:
        clauses, params = self._base_filters(
            min_trust=min_trust, include_invalidated=include_invalidated
        )
        where = " AND ".join(clauses) if clauses else "1=1"
        sql = (
            f"SELECT * FROM facts WHERE {where} "  # noqa: S608 - clauses are static, params bound
            # rowid DESC tiebreaker: ULIDs are not monotonic within one ms, so
            # equal ts_last falls back to deterministic insert order.
            "ORDER BY importance DESC, ts_last DESC, rowid DESC LIMIT ? OFFSET ?"
        )
        params.append(max(1, min(limit, 50_000)))
        params.append(max(0, offset))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        return [self._row_to_fact(r) for r in rows]

    def count_facts(
        self,
        *,
        min_trust: str | None = None,
        include_invalidated: bool = False,
    ) -> int:
        """Total facts matching the same filters as :meth:`list_facts` (for pagination)."""
        clauses, params = self._base_filters(
            min_trust=min_trust, include_invalidated=include_invalidated
        )
        where = " AND ".join(clauses) if clauses else "1=1"
        sql = f"SELECT COUNT(*) FROM facts WHERE {where}"  # noqa: S608 - clauses static, params bound
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(sql, params).fetchone()
            finally:
                conn.close()
        return int(row[0]) if row else 0

    def facts_for_key(
        self,
        key: str,
        *,
        include_invalidated: bool = False,
    ) -> list[SemanticFact]:
        """All facts under one key (newest first) — powers dup + contradiction checks.

        Key matching is Turkish-fold-insensitive (``key_norm``): ``'Şehir'`` and
        ``'şehir'`` are the same key. Rows not yet backfilled fall back to the
        raw ``key`` column via ``IFNULL``.
        """
        clauses = ["IFNULL(key_norm, key) = ?"]
        params: list[object] = [fold_text(key.strip()[:256])]
        if not include_invalidated:
            clauses.append("invalidated_at IS NULL")
        where = " AND ".join(clauses)
        sql = f"SELECT * FROM facts WHERE {where} ORDER BY ts_last DESC, rowid DESC"  # noqa: S608
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        return [self._row_to_fact(r) for r in rows]

    def get_fact(self, fact_id: str) -> SemanticFact | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
            finally:
                conn.close()
        return self._row_to_fact(row) if row else None

    def correct_fact(
        self,
        fact_id: str,
        *,
        new_value: str,
        importance: float | None = None,
    ) -> SemanticFact | None:
        """In-place value fix (typo/cleanup) — *not* a temporal supersede."""
        fact = self.get_fact(fact_id)
        if not fact:
            return None
        val_n = new_value.strip()[:8000]
        imp = importance if importance is not None else fact.importance
        ts = self._iso_now()
        with self._lock:
            conn = self._connect()
            try:
                # Guard on validity (like invalidate_fact/supersede_fact): correcting an
                # already-invalidated fact would re-emit a 'fact' event that re-indexes the
                # dead row into the vector/graph stores as if live (its earlier
                # fact_invalidated already purged it, and no future event ever will). rowcount
                # < 1 here → caller gets None and the façade skips the event.
                # C5: refuse a correction that would collide with ANOTHER valid row
                # sharing the resulting folded (key_norm, value_norm) — that would create
                # two undedupable valid duplicates (and, with the unique-valid index, raise
                # IntegrityError). The NOT EXISTS makes rowcount 0 in that case → return
                # None so the façade skips the event; a clean correction is unaffected.
                key_fold = fold_text(fact.key)
                val_fold = fold_text(val_n)
                cur = conn.execute(
                    "UPDATE facts SET value = ?, value_norm = ?, ts_last = ?, importance = ? "
                    "WHERE id = ? AND invalidated_at IS NULL AND NOT EXISTS ("
                    "  SELECT 1 FROM facts f2 WHERE IFNULL(f2.key_norm, f2.key) = ? "
                    "  AND IFNULL(f2.value_norm, f2.value) = ? AND f2.invalidated_at IS NULL "
                    "  AND f2.id != ?)",
                    (val_n, val_fold, ts, imp, fact_id, key_fold, val_fold, fact_id),
                )
                conn.commit()
                if int(cur.rowcount) < 1:
                    return None
            finally:
                conn.close()
        return self.get_fact(fact_id)

    def invalidate_fact(self, fact_id: str, *, at: str | None = None) -> SemanticFact | None:
        """Close a fact's validity window (temporal delete). Idempotent-safe."""
        ts = at or self._iso_now()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE facts SET invalidated_at = ?, ts_last = ? "
                    "WHERE id = ? AND invalidated_at IS NULL",
                    (ts, ts, fact_id),
                )
                conn.commit()
                if int(cur.rowcount) < 1:
                    return None
            finally:
                conn.close()
        return self.get_fact(fact_id)

    def supersede_fact(
        self,
        fact_id: str,
        *,
        new_value: str,
        new_key: str | None = None,
        trust: Trust | None = None,
        source_turn_id: str | None = None,
        quote: str | None = None,
        extractor: str | None = None,
        source_origin: str | None = None,
        source_detail: str | None = None,
        observed_at: str | None = None,
    ) -> tuple[SemanticFact, SemanticFact] | None:
        """Temporally replace a fact: invalidate the old, insert a new one.

        Returns ``(old, new)`` snapshots where ``old`` reflects the closed window
        (``invalidated_at`` set). The old row is preserved for replay (P8).

        Invalidate + insert run in **one transaction** on one connection: a
        crash between the two steps can no longer lose the fact (either both
        happen or neither does). On error the transaction is rolled back and
        the exception propagates — the old fact stays valid.
        """
        ts = self._iso_now()
        with self._lock:
            # BEGIN IMMEDIATE (audit C4): grab the RESERVED lock for the whole
            # read→invalidate→insert critical section so the other process cannot
            # interleave a write between the SELECT and the supersede.
            conn = self._connect(immediate=True)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
                if row is None:
                    return None
                old = self._row_to_fact(row)
                if not old.is_valid:
                    return None
                # The supersede instant must fall STRICTLY AFTER the old fact's
                # valid_from. On a fast host the upsert and this supersede can land
                # in the SAME millisecond, which would collapse the old fact's
                # half-open window [valid_from, invalidated_at) to zero width and
                # make facts_as_of(old.valid_from) skip it (returning the new value
                # at the very instant the old one began). Bumping ts by 1ms keeps a
                # real validity window; ts still equals the new row's valid_from so
                # the windows continue to tile gaplessly (FIX 1).
                old_vf = old.valid_from or old.ts_first
                if old_vf and ts <= old_vf:
                    ts = self._next_ms(old_vf)
                cur = conn.execute(
                    "UPDATE facts SET invalidated_at = ?, ts_last = ? "
                    "WHERE id = ? AND invalidated_at IS NULL",
                    (ts, ts, fact_id),
                )
                if int(cur.rowcount) < 1:  # lost a race; treat as no-op
                    conn.rollback()
                    return None
                new_fact = self._upsert_in_conn(
                    conn,
                    fact_id=str(ulid.new()),
                    key=(new_key or old.key),
                    value=new_value,
                    confidence=old.confidence,
                    importance=old.importance,
                    anchored=old.anchored,
                    trust=trust or old.trust,
                    source_turn_id=source_turn_id,
                    quote=quote,
                    extractor=extractor,
                    # The new row's valid_from matches the old one's invalidated_at EXACTLY:
                    # so the half-open windows [valid_from, invalidated_at) tile without a
                    # gap. Otherwise _upsert_in_conn would generate valid_from from its own
                    # (slightly later) ts and, at exactly that millisecond
                    # (as_of == invalidated_at), facts_as_of would return neither the old nor
                    # the new — a coverage hole.
                    valid_from=ts,
                    set_valid_from=True,  # C7: carry the supersede ts even on a dedup-hit
                    source_origin=source_origin,
                    source_detail=source_detail,
                    observed_at=observed_at,
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        closed = replace(old, invalidated_at=ts, ts_last=ts)
        return (closed, new_fact)

    def delete_fact(self, fact_id: str) -> bool:
        """Hard delete (drops history). Prefer :meth:`invalidate_fact` for supersede."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
                conn.commit()
                return int(cur.rowcount) > 0
            finally:
                conn.close()

    def clear_all(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM facts")
                conn.commit()
                return int(cur.rowcount)
            finally:
                conn.close()

    def search(
        self,
        query: str,
        *,
        min_trust: str | None = None,
        include_invalidated: bool = False,
        limit: int = 15,
    ) -> list[SemanticFact]:
        return self._term_search(
            query,
            min_trust=min_trust,
            include_invalidated=include_invalidated,
            limit=limit,
        )

    def facts_as_of(
        self,
        terms_query: str,
        as_of: str,
        *,
        min_trust: str | None = None,
        limit: int = 15,
    ) -> list[SemanticFact]:
        """Time-travel search (P8/M3.1): facts as they were valid *at* ``as_of``.

        :meth:`search` with ``include_invalidated=True`` plus the validity
        window ``IFNULL(valid_from, ts_first) <= as_of AND (invalidated_at IS
        NULL OR invalidated_at > as_of)`` applied in SQL. Timestamps are the
        stores' millisecond-Z ISO strings, so lexicographic comparison is a
        correct time comparison.
        """
        return self._term_search(
            terms_query,
            min_trust=min_trust,
            include_invalidated=True,
            limit=limit,
            extra_clauses=[
                "IFNULL(valid_from, ts_first) <= ?",
                "(invalidated_at IS NULL OR invalidated_at > ?)",
            ],
            extra_params=[as_of, as_of],
        )

    def _term_search(
        self,
        query: str,
        *,
        min_trust: str | None,
        include_invalidated: bool,
        limit: int,
        extra_clauses: list[str] | None = None,
        extra_params: list[object] | None = None,
    ) -> list[SemanticFact]:
        """Shared per-term LIKE loop behind :meth:`search` / :meth:`facts_as_of`."""
        terms = recall_search_terms(query)
        if not terms:
            return []
        cap = max(1, min(limit, _SEARCH_LIMIT_MAX))
        seen_ids: set[str] = set()
        out: list[SemanticFact] = []
        with self._lock:
            conn = self._connect()
            try:
                for term in terms:
                    if len(out) >= cap:
                        break
                    clauses, params = self._base_filters(
                        min_trust=min_trust,
                        include_invalidated=include_invalidated,
                    )
                    if extra_clauses:
                        clauses.extend(extra_clauses)
                        params.extend(extra_params or [])
                    # Match on the Turkish-folded shadow columns: SQLite LIKE
                    # only case-folds ASCII, so 'İstanbul' vs '%istanbul%' would
                    # never hit on the raw columns. IFNULL covers rows written
                    # by an older process that did not fill the norm columns.
                    pattern = f"%{escape_like(fold_text(term))}%"
                    clauses.append(
                        "(IFNULL(key_norm, key) LIKE ? ESCAPE '\\' "
                        "OR IFNULL(value_norm, value) LIKE ? ESCAPE '\\')"
                    )
                    params.extend([pattern, pattern])
                    where = " AND ".join(clauses)
                    sql = (
                        f"SELECT * FROM facts WHERE {where} "  # noqa: S608 - static clauses
                        "ORDER BY importance DESC, ts_last DESC, rowid DESC LIMIT ?"
                    )
                    params.append(cap - len(out))
                    for r in conn.execute(sql, params).fetchall():
                        fid = str(r["id"])
                        if fid in seen_ids:
                            continue
                        seen_ids.add(fid)
                        out.append(self._row_to_fact(r))
            finally:
                conn.close()
        return out

    @staticmethod
    def _base_filters(
        *,
        min_trust: str | None,
        include_invalidated: bool,
    ) -> tuple[list[str], list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if min_trust:
            allow = _trust_allowset(min_trust)
            placeholders = ",".join("?" * len(allow))
            clauses.append(f"trust IN ({placeholders})")
            params.extend(allow)
        if not include_invalidated:
            clauses.append("invalidated_at IS NULL")
        return clauses, params

    @staticmethod
    def _row_to_fact(r: sqlite3.Row) -> SemanticFact:
        keys = r.keys()
        return SemanticFact(
            id=str(r["id"]),
            key=str(r["key"]),
            value=str(r["value"]),
            ts_first=str(r["ts_first"]),
            ts_last=str(r["ts_last"]),
            confidence=float(r["confidence"] or 0.0),
            importance=float(r["importance"] or 0.0),
            anchored=bool(r["anchored"]),
            trust=(r["trust"] if "trust" in keys and r["trust"] else _DEFAULT_TRUST),
            source_turn_id=r["source_turn_id"] if "source_turn_id" in keys else None,
            quote=r["quote"] if "quote" in keys else None,
            extractor=r["extractor"] if "extractor" in keys else None,
            valid_from=r["valid_from"] if "valid_from" in keys else None,
            invalidated_at=r["invalidated_at"] if "invalidated_at" in keys else None,
            hit_count=int(r["hit_count"] or 0) if "hit_count" in keys else 0,
            miss_count=int(r["miss_count"] or 0) if "miss_count" in keys else 0,
            last_hit_at=r["last_hit_at"] if "last_hit_at" in keys else None,
            source_origin=(
                str(r["source_origin"])
                if "source_origin" in keys and r["source_origin"]
                else LEGACY_ORIGIN
            ),
            source_detail=r["source_detail"] if "source_detail" in keys else None,
            observed_at=r["observed_at"] if "observed_at" in keys else None,
        )
