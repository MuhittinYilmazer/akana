"""Skill hybrid retrieval layers — FTS5 (SkillEngine F2).

Two layers, one fusion:

* **FTS5** (:class:`SkillFtsIndex`) — skill metadata (name/title/description/
  trigger/tag) + L2 body summary, in ``<data_dir>/db/skills.db``; rebuilt from
  scratch on registry reload (cheap for 26 skills, consistency for free).
* **RRF fusion** (:func:`rrf_fuse`) — ``score = Σ 1/(k+rank)``, ``k=60``.

The patterns originate from ``src/akana/memory`` but are **copies, not imports**:
by the package boundary ``akana_server`` may not depend on ``src/akana``.

* :func:`fold_text` — copy of fold_text from ``src/akana/memory/terms.py``
* RRF ``k=60`` — pattern A from ``src/akana/memory/vector_recall.py`` §11.3
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path

__all__ = [
    "RRF_K_CONST",
    "SkillFtsIndex",
    "fold_text",
    "rrf_fuse",
]

log = logging.getLogger(__name__)

RRF_K_CONST = 60  # same constant as pattern A in src/akana/memory/vector_recall.py §11.3

_FTS_MIN_TERM_LEN = 2
_MAX_FTS_TERMS = 16
_WORD_RE = re.compile(r"[\wğüşöçıİĞÜŞÖÇ]+", flags=re.IGNORECASE)


def fold_text(text: str) -> str:
    """Turkish-aware case folding (copy of ``src/akana/memory/terms.py``).

    Python's locale-blind ``lower()`` mangles Turkish: ``"İ".lower()`` produces
    ``i + U+0307`` (a combining dot), and ``"I".lower()`` yields ``i`` instead of
    ``ı`` — so ``"İzmir" vs "İZMİR"`` does not match. Fold: NFKC → İ→i, I→ı → lower
    → clean up any leftover combining dot. Never use a bare ``lower()`` for skill
    matching; use this instead.
    """
    s = unicodedata.normalize("NFKC", text or "")
    s = s.replace("İ", "i").replace("I", "ı")
    s = s.lower()
    return s.replace("i̇", "i")


# -- FTS5 layer -----------------------------------------------------------------


class SkillFtsIndex:
    """SQLite FTS5 skill index (``<data_dir>/db/skills.db``).

    :meth:`rebuild` is called on every registry reload: the table is dropped and
    repopulated with folded documents (no incremental-sync headache — the skill
    count is small, a full build takes milliseconds). If FTS5 is unavailable or
    SQLite errors, the index disables itself (``available=False``) and
    :meth:`search` returns empty — the substring layer keeps carrying the search.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._available = False  # disabled until rebuild succeeds

    @property
    def available(self) -> bool:
        return self._available

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        return conn

    def rebuild(self, docs: list[tuple[str, str]]) -> bool:
        """Builds the index from scratch from a ``(skill_id, doc)`` list.

        The doc text is folded here (:func:`fold_text`) — the query goes through the
        same folding, so ``İzmir``/``İZMİR`` are the same term.
        """
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                conn = self._connect()
                try:
                    conn.execute("DROP TABLE IF EXISTS skills_fts")
                    conn.execute(
                        "CREATE VIRTUAL TABLE skills_fts USING fts5("
                        "skill_id UNINDEXED, doc, tokenize='unicode61')"
                    )
                    conn.executemany(
                        "INSERT INTO skills_fts(skill_id, doc) VALUES (?, ?)",
                        [(sid, fold_text(doc)) for sid, doc in docs],
                    )
                    conn.commit()
                finally:
                    conn.close()
                self._available = True
            except (sqlite3.Error, OSError) as e:
                # OSError covers self._path.parent.mkdir() failures (e.g. the target
                # exists as a file, or is unwritable) — the docstring/class contract
                # promises rebuild() itself never raises; degrade to substring-only.
                if self._available:  # log once on transition
                    log.warning(
                        "skill FTS index build failed (%s); substring search continues", e
                    )
                self._available = False
        return self._available

    @staticmethod
    def _match_query(query: str) -> str | None:
        terms = [
            t
            for t in _WORD_RE.findall(fold_text(query))
            if len(t) >= _FTS_MIN_TERM_LEN
        ]
        if not terms:
            return None
        return " OR ".join(f'"{t}"' for t in terms[:_MAX_FTS_TERMS])

    def search(self, query: str, *, limit: int = 20) -> list[str]:
        """Matching skill ids in bm25 order; any error returns an empty list (degrade)."""
        if not self._available:
            return []
        match = self._match_query(query)
        if match is None:
            return []
        with self._lock:
            try:
                conn = self._connect()
                try:
                    rows = conn.execute(
                        "SELECT skill_id FROM skills_fts WHERE skills_fts MATCH ? "
                        "ORDER BY rank LIMIT ?",
                        (match, max(1, limit)),
                    ).fetchall()
                finally:
                    conn.close()
            except sqlite3.Error as e:
                log.warning("skill FTS query failed (%s); falling back to substring search", e)
                return []
        return [str(r["skill_id"]) for r in rows]


# -- RRF fusion ---------------------------------------------------------------------


def rrf_fuse(
    rankings: list[list[str]], *, k_const: int = RRF_K_CONST
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: ``score(id) = Σ_ranking 1/(k + rank)``.

    A copy of pattern A from ``src/akana/memory/vector_recall.py`` §11.3. Empty
    rankings pass through without contributing — the same code path runs whether or
    not the vector layer is active. On a score tie, ids sort alphabetically
    (deterministic ordering).
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, ident in enumerate(ranking, start=1):
            scores[ident] = scores.get(ident, 0.0) + 1.0 / (k_const + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
