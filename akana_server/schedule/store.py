"""ScheduleEngine — persistent, atomic, corruption-tolerant schedule store.

The store is a single JSON file ``<data_dir>/schedules.json`` holding a list of
:class:`~akana_server.schedule.model.ScheduleItem` rows. It reuses the server's
shared atomic-write primitives (:mod:`akana_server.json_store`): every mutation
is a read-modify-write guarded by the per-``data_dir`` CROSS-PROCESS lock
(:func:`akana_server.json_store.cross_process_lock`) and written via
``write_json_atomic`` (unique tmp + ``os.replace``), so concurrent writers never
corrupt the file or lose an update — not only two tabs / the engine firing while
the UI edits (same interpreter), but ALSO the ``akana_schedule`` MCP child process,
which mutates the same ``schedules.json``. A MISSING file yields an empty list and
a single un-parseable ROW is skipped, but a TEMPORARILY-unreadable or wholly-corrupt
file RAISES (never "starts empty") so a transient Windows sharing violation can
never trick a mutation into overwriting real data with nothing.

Recurrence math (:func:`compute_next_run`) is plain datetime arithmetic in
Turkey local time (fixed +03:00, no DST — the project convention). There is NO
cron dependency: the four kinds (``once`` / ``interval`` / ``daily`` / ``weekly``)
are each a small, testable closed-form computation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ulid

from akana_server.json_store import cross_process_lock, write_json_atomic
from akana_server.schedule.model import (
    CREATED_BY,
    DELIVERY_MODES,
    KINDS,
    Delivery,
    ScheduleItem,
)

log = logging.getLogger(__name__)

__all__ = [
    "TR_TZ",
    "MAX_SCHEDULES",
    "MIN_INTERVAL_SECONDS",
    "SPENT_ONCE_RETENTION_DAYS",
    "ScheduleValidationError",
    "ScheduleStore",
    "now_tr",
    "to_iso",
    "parse_iso",
    "compute_next_run",
]

#: Turkey fixed offset (+03:00, no DST since 2016). All schedule wall-clock math
#: (``HH:MM``, weekday, ``next_run_at``) is done in this zone, matching
#: ``src/akana/memory/time_expressions`` and the rest of the server.
TR_TZ = timezone(timedelta(hours=3))

#: Hard ceiling on stored schedules (SAFETY: a runaway assistant / buggy client
#: must not be able to accumulate unbounded pending work). Counts only ENABLED
#: rows: a lifetime of one-shot reminders leaves behind disabled/spent rows that
#: must NOT push a live user into "too many schedules" (a slow self-DoS). Spent
#: one-shots are also pruned by the engine sweep (see :meth:`ScheduleStore.prune_spent`).
MAX_SCHEDULES = 100

#: Floor on an interval schedule's period, so a misconfigured tiny value cannot
#: turn the engine into a busy loop (each fire is a full LLM turn).
MIN_INTERVAL_SECONDS = 60

#: How long a spent (disabled) ``once`` row is kept before the engine sweep prunes
#: it. Long enough that ``schedule_list`` still shows a recently-fired reminder's
#: outcome, short enough that fired reminders never accumulate without bound.
SPENT_ONCE_RETENTION_DAYS = 7

#: The store file name inside ``data_dir``.
_FILENAME = "schedules.json"


class ScheduleValidationError(ValueError):
    """A schedule spec was rejected; the message is safe to show the user."""


# --------------------------------------------------------------------------- #
# Time helpers (all in Turkey local time, +03:00)
# --------------------------------------------------------------------------- #


def now_tr() -> datetime:
    """Current instant as a +03:00-aware datetime (Turkey local time)."""
    return datetime.now(TR_TZ)


def to_iso(dt: datetime) -> str:
    """Aware datetime → a stable ``+03:00`` ISO-8601 string (second precision).

    A naive datetime is assumed to already be Turkey local time. Second
    precision keeps the stored value human-readable and comparison-stable."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TR_TZ)
    return dt.astimezone(TR_TZ).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 datetime → a +03:00-aware datetime.

    Accepts a trailing ``Z`` (UTC) and offset-less values (assumed +03:00), and
    a bare ``YYYY-MM-DD`` date (interpreted as midnight Turkey time). Raises
    :class:`ScheduleValidationError` on anything unparseable."""
    raw = (value or "").strip()
    if not raw:
        raise ScheduleValidationError("empty datetime")
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        # Fall back to a plain date (common when the model emits YYYY-MM-DD).
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            raise ScheduleValidationError(
                f"could not parse datetime {value!r} (use ISO-8601, e.g. 2026-07-11T18:30)"
            ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TR_TZ)
    return dt.astimezone(TR_TZ)


def _parse_hhmm(value: str) -> tuple[int, int]:
    """``"HH:MM"`` → ``(hour, minute)``; raises on a bad shape/range."""
    raw = (value or "").strip()
    parts = raw.split(":")
    if len(parts) != 2:
        raise ScheduleValidationError(f"time must be HH:MM (got {value!r})")
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ScheduleValidationError(f"time must be HH:MM (got {value!r})") from exc
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ScheduleValidationError(f"time out of range: {value!r} (00:00–23:59)")
    return hh, mm


def compute_next_run(
    kind: str,
    when: str,
    *,
    weekday: int | None,
    after: datetime,
) -> datetime | None:
    """The next fire time strictly appropriate for ``after`` (Turkey local time).

    * ``once``     — the fixed target datetime (``when``). Returned as-is; the
      engine disables the item after it fires, so this is only used to seed the
      first (and only) ``next_run_at``.
    * ``interval`` — ``after + when`` seconds.
    * ``daily``    — the next ``HH:MM`` strictly after ``after`` (today if the
      time has not passed, else tomorrow).
    * ``weekly``   — the next ``weekday`` at ``HH:MM`` strictly after ``after``.

    ``after`` is normalised to +03:00. Because the recurring branches always jump
    to the next occurrence AFTER a reference instant, recomputing with
    ``after=now`` after a fire naturally rolls a long-overdue schedule forward to
    a single future occurrence — no backlog storm (see the engine's catch-up
    policy)."""
    ref = after.astimezone(TR_TZ) if after.tzinfo else after.replace(tzinfo=TR_TZ)
    if kind == "once":
        return parse_iso(when)
    if kind == "interval":
        try:
            secs = int(str(when).strip())
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError(
                f"interval seconds must be an integer (got {when!r})"
            ) from exc
        return ref + timedelta(seconds=secs)
    if kind == "daily":
        hh, mm = _parse_hhmm(when)
        candidate = ref.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= ref:
            candidate += timedelta(days=1)
        return candidate
    if kind == "weekly":
        hh, mm = _parse_hhmm(when)
        if weekday is None or not (0 <= int(weekday) <= 6):
            raise ScheduleValidationError(
                "weekly schedule needs a weekday 0–6 (0=Monday)"
            )
        candidate = ref.replace(hour=hh, minute=mm, second=0, microsecond=0)
        days_ahead = (int(weekday) - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= ref:
            candidate += timedelta(days=7)
        return candidate
    raise ScheduleValidationError(f"unknown schedule kind: {kind!r}")


# --------------------------------------------------------------------------- #
# Spec validation
# --------------------------------------------------------------------------- #


def _validate_spec(
    *,
    title: str,
    prompt: str,
    kind: str,
    when: str,
    weekday: int | None,
    delivery: Delivery,
    created_by: str,
    message: str = "",
) -> None:
    """Reject an invalid create/update spec with a user-safe message.

    A schedule carries EXACTLY ONE payload: ``prompt`` (run an LLM turn when it
    fires) OR ``message`` (deliver this text verbatim, no LLM). Supplying both is
    ambiguous — which one fires? — and supplying neither leaves nothing to do, so
    both are rejected here (shared by create AND update, so neither surface can
    persist an invalid combination)."""
    prompt_set = bool((prompt or "").strip())
    message_set = bool((message or "").strip())
    if prompt_set and message_set:
        raise ScheduleValidationError(
            "provide either 'prompt' (run an LLM turn) or 'message' (deliver verbatim), not both"
        )
    if not prompt_set and not message_set:
        raise ScheduleValidationError(
            "prompt or message is required (what should I do — or say — when it fires?)"
        )
    if not (title or "").strip():
        raise ScheduleValidationError("title is required")
    if kind not in KINDS:
        raise ScheduleValidationError(
            f"kind must be one of {sorted(KINDS)} (got {kind!r})"
        )
    if created_by not in CREATED_BY:
        raise ScheduleValidationError(f"created_by must be one of {sorted(CREATED_BY)}")
    if kind == "interval":
        try:
            secs = int(str(when).strip())
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError(
                f"interval seconds must be an integer (got {when!r})"
            ) from exc
        if secs < MIN_INTERVAL_SECONDS:
            raise ScheduleValidationError(
                f"interval must be at least {MIN_INTERVAL_SECONDS} seconds"
            )
    elif kind == "once":
        parse_iso(when)  # raises ScheduleValidationError on a bad datetime
    elif kind in ("daily", "weekly"):
        _parse_hhmm(when)  # raises on a bad HH:MM
        if kind == "weekly" and (weekday is None or not (0 <= int(weekday) <= 6)):
            raise ScheduleValidationError(
                "weekly schedule needs a weekday 0–6 (0=Monday, 6=Sunday)"
            )
    if delivery.mode not in DELIVERY_MODES:
        raise ScheduleValidationError(
            f"delivery mode must be one of {sorted(DELIVERY_MODES)}"
        )
    if delivery.mode in ("connector", "both"):
        if not delivery.channel:
            raise ScheduleValidationError(
                "connector delivery requires a channel (e.g. 'telegram')"
            )
        if not delivery.chat_id:
            raise ScheduleValidationError(
                "connector delivery requires a chat_id to send to"
            )


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class ScheduleStore:
    """``data_dir``-scoped CRUD over ``schedules.json`` (atomic + lock-guarded).

    All mutations go through :meth:`_mutate`, which takes the process-wide,
    cross-process per-``data_dir`` lock, reloads the current rows, applies a
    function, and atomically rewrites the file. Reads (:meth:`load` / :meth:`get`
    / :meth:`due`) take the SAME lock for the duration of the read, so a read never
    interleaves with a peer process's ``os.replace`` and never trips the strict
    error handling in :meth:`_read_rows`.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / _FILENAME

    def _guard(self):
        """The combined in-process + cross-process lock for one RMW (or read).

        Wraps the ENTIRE read-modify-write (and pure reads) so neither another
        thread nor the ``akana_schedule`` MCP child process can slip a write between
        our load and our atomic replace."""
        return cross_process_lock(self._data_dir, self._path)

    # -- low-level read/write (lock held by caller) --------------------------

    def _read_rows(self) -> list[dict[str, Any]]:
        """Raw row dicts from disk — or RAISE so a mutation aborts, never wipes.

        ``[]`` is returned for EXACTLY ONE case: the file does not exist yet
        (``FileNotFoundError``). EVERY other failure re-raises:

        * A transient ``OSError``/``PermissionError`` — on Windows a peer process
          mid-``os.replace`` briefly makes the file unopenable (WinError 5 / 32).
          Returning empty here and letting :meth:`_mutate` persist it would DELETE
          every schedule. Re-raising aborts the mutation, leaving the file intact.
        * Corrupt JSON (``ValueError``) — the atomic write path rules out a genuine
          half-write, so this is real corruption; do NOT overwrite it with empty.

        A single MALFORMED ROW inside otherwise-valid JSON is still tolerated (skipped
        in :meth:`_load_items`); only an unreadable/undecodable WHOLE FILE raises.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # No store yet — an empty starting point is correct here (and ONLY here).
            return []
        except ValueError:
            log.error(
                "schedules.json at %s is corrupt — aborting the mutation instead of "
                "overwriting it with an empty store", self._path, exc_info=True,
            )
            raise
        except OSError:
            log.error(
                "schedules.json at %s is temporarily unreadable — aborting the "
                "mutation to protect existing data", self._path, exc_info=True,
            )
            raise
        # Accept both the wrapped ({"schedules": [...]}) and bare-list shapes.
        if isinstance(raw, dict):
            rows = raw.get("schedules")
        else:
            rows = raw
        if not isinstance(rows, list):
            return []
        return [r for r in rows if isinstance(r, dict)]

    def _load_items(self) -> list[ScheduleItem]:
        """Parse the raw rows into items (lock-agnostic; caller holds the guard).

        A single malformed row is skipped, not fatal. Shared by the public reads and
        by :meth:`_mutate` so the file lock is acquired exactly ONCE per operation
        (``file_lock`` is not reentrant)."""
        items: list[ScheduleItem] = []
        for row in self._read_rows():
            try:
                items.append(ScheduleItem.from_dict(row))
            except Exception:  # a single malformed row must not sink the store
                log.warning("skipping malformed schedule row: %r", row, exc_info=True)
        return items

    def load(self) -> list[ScheduleItem]:
        """All schedules (a broken single row is skipped, not fatal)."""
        with self._guard():
            return self._load_items()

    def _write(self, items: list[ScheduleItem]) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(
            self._path,
            {"version": 1, "schedules": [i.to_dict() for i in items]},
        )

    def _mutate(self, fn):
        """Run ``fn(items) -> (items, result)`` under the cross-process lock, persist, return result.

        The load, the ``fn`` edit, and the atomic write all happen inside ONE
        :meth:`_guard` so the read-modify-write is atomic across threads AND
        processes. If the load raises (transient unreadability / corruption) the
        mutation aborts BEFORE writing, so a bad read can never wipe the store."""
        with self._guard():
            items = self._load_items()
            items, result = fn(items)
            self._write(items)
            return result

    # -- reads ---------------------------------------------------------------

    def get(self, schedule_id: str) -> ScheduleItem | None:
        sid = (schedule_id or "").strip()
        with self._guard():
            for item in self._load_items():
                if item.id == sid:
                    return item
        return None

    def due(self, now: datetime) -> list[ScheduleItem]:
        """Enabled items whose ``next_run_at`` is at or before ``now``.

        Sorted by ``next_run_at`` so the most overdue fires first (the engine
        runs them sequentially, one LLM turn at a time)."""
        ref = now.astimezone(TR_TZ) if now.tzinfo else now.replace(tzinfo=TR_TZ)
        out: list[ScheduleItem] = []
        with self._guard():
            items = self._load_items()
        for item in items:
            if not item.enabled or not item.next_run_at:
                continue
            try:
                nxt = parse_iso(item.next_run_at)
            except ScheduleValidationError:
                log.warning("schedule %s has an unparseable next_run_at %r — skipping",
                            item.id, item.next_run_at)
                continue
            if nxt <= ref:
                out.append(item)
        out.sort(key=lambda i: i.next_run_at)
        return out

    # -- mutations -----------------------------------------------------------

    def create(
        self,
        *,
        title: str,
        prompt: str = "",
        message: str = "",
        kind: str,
        when: str,
        weekday: int | None = None,
        delivery: Delivery | None = None,
        created_by: str = "user",
        language: str = "en",
        enabled: bool = True,
        now: datetime | None = None,
    ) -> ScheduleItem:
        """Validate, compute the first ``next_run_at``, and persist a new schedule.

        Exactly one of ``prompt`` (LLM turn) or ``message`` (verbatim delivery) is
        required. Raises :class:`ScheduleValidationError` on a bad spec or when the
        :data:`MAX_SCHEDULES` cap is hit."""
        deliver = delivery or Delivery()
        _validate_spec(
            title=title,
            prompt=prompt,
            message=message,
            kind=kind,
            when=str(when),
            weekday=weekday,
            delivery=deliver,
            created_by=created_by,
        )
        ref = now or now_tr()
        first = compute_next_run(kind, str(when), weekday=weekday, after=ref)
        assert first is not None  # only unknown kinds return None, rejected above
        item = ScheduleItem(
            id=str(ulid.new()),
            title=title.strip(),
            prompt=prompt.strip(),
            message=message.strip(),
            kind=kind,
            when=str(when).strip(),
            next_run_at=to_iso(first),
            enabled=bool(enabled),
            weekday=weekday,
            delivery=deliver,
            created_by=created_by,
            language=(language or "en").strip().lower() or "en",
            created_at=to_iso(ref),
            last_run=None,
        )

        def _add(items: list[ScheduleItem]):
            # SAFETY cap counts only ENABLED rows. Disabled/spent one-shots left
            # behind by past reminders must not consume the budget — otherwise a
            # heavy reminder user hits "too many schedules" purely from history (a
            # slow self-DoS). Spent one-shots are additionally pruned by the engine.
            active = sum(1 for i in items if i.enabled)
            if active >= MAX_SCHEDULES:
                raise ScheduleValidationError(
                    f"too many active schedules (max {MAX_SCHEDULES}); cancel or pause one first"
                )
            items.append(item)
            return items, item

        return self._mutate(_add)

    def update(
        self,
        schedule_id: str,
        *,
        title: str | None = None,
        prompt: str | None = None,
        message: str | None = None,
        kind: str | None = None,
        enabled: bool | None = None,
        when: str | None = None,
        weekday: int | None = None,
        delivery: Delivery | None = None,
        now: datetime | None = None,
    ) -> ScheduleItem | None:
        """Patch fields of an existing schedule; returns the updated item or
        ``None`` if the id is unknown. Changing ``when``/``weekday``/``kind``
        recomputes ``next_run_at``. Re-enabling a stale schedule also rolls its
        ``next_run_at`` forward so it does not immediately fire the whole
        backlog.

        Two guards keep an update from producing an instantly-firing schedule:

        * Changing ``kind`` (once↔interval↔daily↔weekly) is only allowed together
          with a new ``when``: each kind's ``when`` means a different thing (an ISO
          datetime vs. seconds vs. ``HH:MM``), so keeping the old value would either
          be nonsense or silently fire wrong. A ``kind`` change with no ``when`` is
          rejected rather than silently ignored (the pre-fix behavior reported
          success while leaving ``kind`` unchanged).
        * Re-enabling a spent one-shot (a ``once`` whose target time already passed)
          with no fresh ``when`` is rejected: recomputing would land ``next_run_at``
          in the past and the reminder would fire instantly on the next poll. The
          caller must supply a new time to re-arm it.
        """
        sid = (schedule_id or "").strip()
        ref = now or now_tr()

        def _patch(items: list[ScheduleItem]):
            for idx, item in enumerate(items):
                if item.id != sid:
                    continue
                new_title = title if title is not None else item.title
                new_prompt = prompt if prompt is not None else item.prompt
                new_message = message if message is not None else item.message
                new_when = str(when) if when is not None else item.when
                new_weekday = weekday if weekday is not None else item.weekday
                new_delivery = delivery if delivery is not None else item.delivery
                new_enabled = item.enabled if enabled is None else bool(enabled)

                # BUG 6: a kind change is meaningful only alongside a new 'when'
                # (the 'when' encoding differs per kind). Reject a lone kind change
                # instead of the old silent no-op that still reported success.
                kind_changing = kind is not None and str(kind) != item.kind
                if kind_changing and when is None:
                    raise ScheduleValidationError(
                        "changing 'kind' also requires a new 'when' (its meaning "
                        "differs per kind: datetime vs. seconds vs. HH:MM)"
                    )
                new_kind = str(kind) if kind is not None else item.kind

                # BUG 5: re-enabling a spent one-shot (target already in the past)
                # with no new time would recompute next_run_at into the past → an
                # instant re-fire. Force the caller to give a fresh 'when'.
                reenabling = enabled is not None and bool(enabled) and not item.enabled
                if reenabling and new_kind == "once" and when is None:
                    try:
                        target = parse_iso(new_when)
                    except ScheduleValidationError:
                        target = None
                    if target is None or target <= ref:
                        raise ScheduleValidationError(
                            "this one-shot reminder has already fired; give a new "
                            "'when' time to re-arm it"
                        )

                _validate_spec(
                    title=new_title,
                    prompt=new_prompt,
                    message=new_message,
                    kind=new_kind,
                    when=new_when,
                    weekday=new_weekday,
                    delivery=new_delivery,
                    created_by=item.created_by,
                )
                recompute = (
                    when is not None
                    or weekday is not None
                    or kind_changing
                    or reenabling  # re-enable → roll forward
                )
                next_run_at = item.next_run_at
                if recompute:
                    nxt = compute_next_run(
                        new_kind, new_when, weekday=new_weekday, after=ref
                    )
                    if nxt is not None:
                        next_run_at = to_iso(nxt)
                updated = ScheduleItem(
                    id=item.id,
                    title=new_title.strip(),
                    prompt=new_prompt.strip(),
                    message=new_message.strip(),
                    kind=new_kind,
                    when=new_when.strip(),
                    next_run_at=next_run_at,
                    enabled=new_enabled,
                    weekday=new_weekday,
                    delivery=new_delivery,
                    created_by=item.created_by,
                    language=item.language,
                    created_at=item.created_at,
                    last_run=item.last_run,
                )
                items[idx] = updated
                return items, updated
            return items, None

        return self._mutate(_patch)

    def cancel(self, schedule_id: str) -> bool:
        """Delete a schedule; returns ``True`` if a row was removed."""
        sid = (schedule_id or "").strip()

        def _remove(items: list[ScheduleItem]):
            kept = [i for i in items if i.id != sid]
            removed = len(kept) != len(items)
            return kept, removed

        return self._mutate(_remove)

    def mark_ran(
        self,
        schedule_id: str,
        *,
        status: str,
        error: str | None = None,
        conversation_id: str | None = None,
        now: datetime | None = None,
        roll_forward: bool = True,
    ) -> ScheduleItem | None:
        """Record a fire outcome and advance the schedule.

        A ``once`` item is disabled (it has done its single job — this holds even
        for an out-of-band manual run: it ran, so it is spent). A recurring item's
        ``next_run_at`` is normally rolled forward to the next occurrence STRICTLY
        AFTER ``now`` — so a long-overdue schedule fires exactly once and then
        resumes its normal cadence (catch-up policy, no storm). ``conversation_id``
        (when a thread was created/used) is persisted back into ``delivery`` so
        the next run appends to the same thread.

        ``roll_forward=False`` (the manual 'run now' path — see BUG 8) fires a
        recurring schedule OUT OF BAND: it records the outcome but leaves
        ``next_run_at`` untouched, so the real scheduled slot for the day is NOT
        swallowed by a test/preview run. ``once`` items still self-disable."""
        sid = (schedule_id or "").strip()
        ref = now or now_tr()

        def _record(items: list[ScheduleItem]):
            for idx, item in enumerate(items):
                if item.id != sid:
                    continue
                last_run: dict[str, Any] = {"at": to_iso(ref), "status": status}
                if error:
                    last_run["error"] = str(error)[:500]
                if conversation_id:
                    last_run["conversation_id"] = conversation_id
                delivery = item.delivery
                if conversation_id and not delivery.conversation_id:
                    delivery = Delivery(
                        mode=delivery.mode,
                        channel=delivery.channel,
                        chat_id=delivery.chat_id,
                        conversation_id=conversation_id,
                        same_chat=delivery.same_chat,
                    )
                if item.kind == "once":
                    enabled = False
                    next_run_at = item.next_run_at
                elif not roll_forward:
                    # Out-of-band run (run-now): keep the recurring item's real slot.
                    enabled = item.enabled
                    next_run_at = item.next_run_at
                else:
                    enabled = item.enabled
                    nxt = compute_next_run(
                        item.kind, item.when, weekday=item.weekday, after=ref
                    )
                    next_run_at = to_iso(nxt) if nxt is not None else item.next_run_at
                updated = ScheduleItem(
                    id=item.id,
                    title=item.title,
                    prompt=item.prompt,
                    message=item.message,
                    kind=item.kind,
                    when=item.when,
                    next_run_at=next_run_at,
                    enabled=enabled,
                    weekday=item.weekday,
                    delivery=delivery,
                    created_by=item.created_by,
                    language=item.language,
                    created_at=item.created_at,
                    last_run=last_run,
                )
                items[idx] = updated
                return items, updated
            return items, None

        return self._mutate(_record)

    def prune_spent(
        self,
        *,
        now: datetime | None = None,
        older_than_days: int = SPENT_ONCE_RETENTION_DAYS,
    ) -> int:
        """Delete disabled ``once`` rows whose last fire is older than the retention
        window; return how many were removed.

        Spent one-shots (a ``once`` that already fired → disabled) would otherwise
        accumulate forever: every reminder the user ever set leaves a tombstone row.
        They no longer fire, but they clutter ``schedule_list`` and — before the
        enabled-only cap — even counted toward :data:`MAX_SCHEDULES`. The engine
        sweep calls this each poll so history self-cleans without a manual purge.

        SAFETY: only DISABLED ``once`` rows with a parseable ``last_run.at`` older
        than the window are removed. A still-enabled once (armed but not yet fired,
        even one whose target is far in the past) and every recurring schedule are
        always kept — pruning must never drop a schedule that could still fire.
        Writes only when something is actually removed (a no-op sweep touches no I/O
        beyond the guarded read)."""
        ref = now or now_tr()
        cutoff = ref - timedelta(days=max(0, older_than_days))
        with self._guard():
            items = self._load_items()
            kept: list[ScheduleItem] = []
            removed = 0
            for item in items:
                if item.kind == "once" and not item.enabled and item.last_run:
                    at = item.last_run.get("at")
                    ran: datetime | None
                    try:
                        ran = parse_iso(at) if at else None
                    except ScheduleValidationError:
                        ran = None
                    if ran is not None and ran < cutoff:
                        removed += 1
                        continue  # drop this spent, aged-out one-shot
                kept.append(item)
            if removed:
                self._write(kept)
            return removed


def get_schedule_store(data_dir: Path | str) -> ScheduleStore:
    """Convenience constructor (mirrors the other stores' free-function style)."""
    return ScheduleStore(data_dir)
