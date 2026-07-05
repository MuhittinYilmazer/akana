"""SQLite-backed persona store (``<data_dir>/db/persona.db``).

Connection pattern is the same as ``schedule/store.py``: WAL, per-connection
``busy_timeout``, ``threading.Lock`` + short-lived connection per operation.

Append-only EVENT LOG: every mutation (persona created/updated/deleted, binding
set) inserts a row into ``persona_events`` in the same transaction; rows are
never deleted from this log. The current-state tables (``personas`` / ``bindings``)
are mutable (create/update/delete, binding upsert) — history is tracked only via
the event log; the rows themselves carry the most recent state.

Only ``source="user"`` personas live here; builtin/pack personas are merged at
runtime by :class:`~akana_server.persona.registry.PersonaRegistry`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

from akana_server.persona.models import Persona, PersonaError
from akana_server.timeutil import iso_now

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS personas (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    tone TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- scope: 'channel' | 'conversation'; key: channel name / conversation_id.
CREATE TABLE IF NOT EXISTS bindings (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    persona_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS persona_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);

-- Singleton text overrides: 'base_prompt' (akana core prompt),
-- 'catalog_override' (capability catalog text). Absent = code/auto-generated default.
CREATE TABLE IF NOT EXISTS overrides (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

#: Valid binding scopes.
BINDING_SCOPES = ("channel", "conversation")


def _iso_now() -> str:
    return iso_now()


class PersonaStore:
    """User personas + channel/conversation bindings over ``db/persona.db``."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @staticmethod
    def _event(conn: sqlite3.Connection, action: str, payload: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO persona_events (timestamp, action, payload) VALUES (?, ?, ?)",
            (_iso_now(), action, json.dumps(payload, ensure_ascii=False)),
        )

    # -- user personas ------------------------------------------------------ #

    def create(self, persona: Persona) -> Persona:
        """Create a new user persona — raises :class:`PersonaError` on id conflict (append-only)."""
        with self._lock, closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT 1 FROM personas WHERE id = ?", (persona.id,)
            ).fetchone()
            if row is not None:
                raise PersonaError(f"persona already exists: {persona.id}")
            conn.execute(
                "INSERT INTO personas (id, name, system_prompt, tone, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (persona.id, persona.name, persona.system_prompt, persona.tone, _iso_now()),
            )
            self._event(conn, "persona_created", {"id": persona.id, "name": persona.name})
        return persona

    def update(self, persona: Persona) -> Persona:
        """Update an existing user persona (current-state mutation + event).

        Raises :class:`PersonaError` if not found. The event log stays append-only
        (``persona_updated``); the ``personas`` row is updated in place.
        """
        with self._lock, closing(self._connect()) as conn, conn:
            exists = conn.execute(
                "SELECT 1 FROM personas WHERE id = ?", (persona.id,)
            ).fetchone()
            if exists is None:
                raise PersonaError(f"persona not found: {persona.id}")
            conn.execute(
                "UPDATE personas SET name = ?, system_prompt = ?, tone = ? WHERE id = ?",
                (persona.name, persona.system_prompt, persona.tone, persona.id),
            )
            self._event(conn, "persona_updated", {"id": persona.id, "name": persona.name})
        return persona

    def delete(self, persona_id: str) -> bool:
        """Delete a user persona + clean up its bindings (mutation + event).

        Returns ``True`` if a record was found and deleted. Leaves no dangling
        bindings (resolve falls back to the builtin). The event log stays append-only
        (``persona_deleted``).
        """
        with self._lock, closing(self._connect()) as conn, conn:
            cur = conn.execute("DELETE FROM personas WHERE id = ?", (persona_id,))
            if cur.rowcount == 0:
                return False
            conn.execute("DELETE FROM bindings WHERE persona_id = ?", (persona_id,))
            self._event(conn, "persona_deleted", {"id": persona_id})
        return True

    def get(self, persona_id: str) -> Persona | None:
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, name, system_prompt, tone FROM personas WHERE id = ?",
                (persona_id,),
            ).fetchone()
        return self._row_to_persona(row) if row is not None else None

    def list(self) -> list[Persona]:
        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, name, system_prompt, tone FROM personas ORDER BY id"
            ).fetchall()
        return [self._row_to_persona(r) for r in rows]

    @staticmethod
    def _row_to_persona(row: sqlite3.Row) -> Persona:
        return Persona(
            id=row["id"],
            name=row["name"],
            system_prompt=row["system_prompt"],
            tone=row["tone"],
            source="user",
        )

    # -- bindings ------------------------------------------------------------ #

    def set_binding(self, scope: str, key: str, persona_id: str) -> None:
        """Channel/conversation → persona binding (upsert + append-only event)."""
        if scope not in BINDING_SCOPES:
            raise PersonaError(f"unknown binding scope: {scope}")
        key = (key or "").strip()
        if not key:
            raise PersonaError("binding key cannot be empty")
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO bindings (scope, key, persona_id, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(scope, key) DO UPDATE SET"
                " persona_id = excluded.persona_id, updated_at = excluded.updated_at",
                (scope, key, persona_id, _iso_now()),
            )
            self._event(
                conn, "binding_set", {"scope": scope, "key": key, "persona_id": persona_id}
            )

    def get_binding(self, scope: str, key: str) -> str | None:
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT persona_id FROM bindings WHERE scope = ? AND key = ?",
                (scope, (key or "").strip()),
            ).fetchone()
        return row["persona_id"] if row is not None else None

    def list_bindings(self) -> list[dict[str, str]]:
        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT scope, key, persona_id, updated_at FROM bindings ORDER BY scope, key"
            ).fetchall()
        return [dict(r) for r in rows]

    # -- singleton overrides (base prompt / catalog text) ------------------- #

    def get_override(self, key: str) -> str | None:
        """Stored override text for ``key`` (None if absent → code/auto default)."""
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT value FROM overrides WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row is not None else None

    def set_override(self, key: str, value: str) -> None:
        """Upsert the override (current-state row + append-only event)."""
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO overrides (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (key, value, _iso_now()),
            )
            self._event(conn, "override_set", {"key": key, "len": len(value)})

    def clear_override(self, key: str) -> bool:
        """Delete the override → fall back to the default. Returns True if a record was found and deleted."""
        with self._lock, closing(self._connect()) as conn, conn:
            cur = conn.execute("DELETE FROM overrides WHERE key = ?", (key,))
            if cur.rowcount == 0:
                return False
            self._event(conn, "override_cleared", {"key": key})
        return True


__all__ = ["BINDING_SCOPES", "PersonaStore"]
