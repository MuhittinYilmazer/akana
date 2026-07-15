"""ScheduleEngine store — recurrence math, atomic CRUD, due-query, roll-forward.

Hermetic: every test uses a throwaway ``tmp_path`` data dir and an INJECTED
``now`` (a fixed +03:00 datetime), so there is no wall-clock flakiness. Covers
``compute_next_run`` for all four kinds (incl. the +03:00 convention and Z→+03:00
conversion), one-shot self-disable, and past-due roll-forward (server-was-off
catch-up fires ONCE, no storm).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from akana_server.schedule.model import Delivery
from akana_server.schedule.store import (
    TR_TZ,
    ScheduleStore,
    ScheduleValidationError,
    compute_next_run,
    parse_iso,
    to_iso,
)

# A fixed reference instant (Turkey local time) — no wall clock is read.
T0 = datetime(2026, 7, 11, 10, 0, tzinfo=TR_TZ)


# --- compute_next_run (all four kinds) --------------------------------------


def test_next_run_once_is_the_target():
    dt = compute_next_run("once", "2026-07-11T18:30", weekday=None, after=T0)
    assert dt == datetime(2026, 7, 11, 18, 30, tzinfo=TR_TZ)


def test_next_run_once_z_utc_becomes_plus_three():
    # 15:30 UTC == 18:30 in Turkey (+03:00).
    dt = compute_next_run("once", "2026-07-11T15:30:00Z", weekday=None, after=T0)
    assert dt == datetime(2026, 7, 11, 18, 30, tzinfo=TR_TZ)


def test_next_run_interval_adds_seconds():
    dt = compute_next_run("interval", "3600", weekday=None, after=T0)
    assert dt == T0 + timedelta(seconds=3600)


def test_next_run_daily_later_today():
    dt = compute_next_run("daily", "18:00", weekday=None, after=T0)  # 18:00 > 10:00
    assert dt == datetime(2026, 7, 11, 18, 0, tzinfo=TR_TZ)


def test_next_run_daily_rolls_to_tomorrow_when_passed():
    dt = compute_next_run("daily", "09:00", weekday=None, after=T0)  # 09:00 < 10:00
    assert dt == datetime(2026, 7, 12, 9, 0, tzinfo=TR_TZ)


def test_next_run_weekly_next_occurrence():
    dt = compute_next_run("weekly", "08:30", weekday=0, after=T0)  # next Monday 08:30
    assert dt.weekday() == 0
    assert (dt.hour, dt.minute) == (8, 30)
    assert dt > T0
    assert dt.tzinfo.utcoffset(dt) == timedelta(hours=3)


def test_next_run_weekly_requires_weekday():
    with pytest.raises(ScheduleValidationError):
        compute_next_run("weekly", "08:30", weekday=None, after=T0)


def test_to_iso_uses_plus_three_offset():
    assert to_iso(datetime(2026, 7, 11, 18, 30, tzinfo=TR_TZ)) == "2026-07-11T18:30:00+03:00"


def test_parse_iso_bare_date_is_midnight_tr():
    assert parse_iso("2026-07-11") == datetime(2026, 7, 11, 0, 0, tzinfo=TR_TZ)


# --- CRUD -------------------------------------------------------------------


def test_create_and_get_roundtrip(tmp_path):
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="interval", when="3600", now=T0)
    assert item.id
    assert item.created_by == "user"  # default
    assert item.next_run_at == to_iso(T0 + timedelta(seconds=3600))
    got = store.get(item.id)
    assert got is not None and got.prompt == "p"


def test_create_rejects_short_interval(tmp_path):
    store = ScheduleStore(tmp_path)
    with pytest.raises(ScheduleValidationError):
        store.create(title="t", prompt="p", kind="interval", when="10", now=T0)


def test_create_requires_prompt(tmp_path):
    store = ScheduleStore(tmp_path)
    with pytest.raises(ScheduleValidationError):
        store.create(title="t", prompt="  ", kind="daily", when="09:00", now=T0)


def test_create_connector_needs_channel_and_chat(tmp_path):
    store = ScheduleStore(tmp_path)
    with pytest.raises(ScheduleValidationError):
        store.create(
            title="t", prompt="p", kind="daily", when="09:00",
            delivery=Delivery(mode="connector", channel="telegram"),  # no chat_id
            now=T0,
        )


def test_cap_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr("akana_server.schedule.store.MAX_SCHEDULES", 2)
    store = ScheduleStore(tmp_path)
    store.create(title="a", prompt="p", kind="interval", when="3600", now=T0)
    store.create(title="b", prompt="p", kind="interval", when="3600", now=T0)
    with pytest.raises(ScheduleValidationError):
        store.create(title="c", prompt="p", kind="interval", when="3600", now=T0)


def test_cap_counts_only_enabled_rows(tmp_path, monkeypatch):
    """BUG 4a: the cap counts only ENABLED rows, so paused/spent schedules do not
    consume the budget (a lifetime of one-shot reminders was a slow self-DoS)."""
    monkeypatch.setattr("akana_server.schedule.store.MAX_SCHEDULES", 2)
    store = ScheduleStore(tmp_path)
    a = store.create(title="a", prompt="p", kind="interval", when="3600", now=T0)
    store.create(title="b", prompt="p", kind="interval", when="3600", now=T0)
    # 2 enabled → at cap → a third is rejected.
    with pytest.raises(ScheduleValidationError):
        store.create(title="c", prompt="p", kind="interval", when="3600", now=T0)
    # Pause one → an enabled slot frees up → the create now succeeds.
    store.update(a.id, enabled=False, now=T0)
    c = store.create(title="c", prompt="p", kind="interval", when="3600", now=T0)
    assert c.id


def test_prune_spent_removes_old_disabled_once(tmp_path):
    """BUG 4b: prune_spent drops disabled `once` rows whose last fire is older than
    the retention window, and keeps recent ones + anything still enabled."""
    store = ScheduleStore(tmp_path)
    old = store.create(title="old", prompt="p", kind="once", when=to_iso(T0), now=T0)
    store.mark_ran(old.id, status="ok", now=T0)  # → disabled, last_run at T0
    recent = store.create(title="recent", prompt="p", kind="once", when=to_iso(T0), now=T0)
    store.mark_ran(recent.id, status="ok", now=T0 + timedelta(days=6))
    # An enabled once far in the past must NEVER be pruned (it could still fire).
    armed = store.create(title="armed", prompt="p", kind="once", when=to_iso(T0), now=T0)

    removed = store.prune_spent(now=T0 + timedelta(days=8))  # cutoff = T0 + 1 day
    assert removed == 1
    ids = {i.id for i in store.load()}
    assert old.id not in ids  # ran 8 days ago → pruned
    assert recent.id in ids   # ran 2 days ago → kept
    assert armed.id in ids    # still enabled → kept


def test_cancel(tmp_path):
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="interval", when="3600", now=T0)
    assert store.cancel(item.id) is True
    assert store.get(item.id) is None
    assert store.cancel(item.id) is False  # already gone


def test_update_recomputes_when_timing_changes(tmp_path):
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="daily", when="09:00", now=T0)
    updated = store.update(item.id, when="18:00", now=T0)
    assert updated is not None
    assert updated.when == "18:00"
    assert updated.next_run_at == to_iso(datetime(2026, 7, 11, 18, 0, tzinfo=TR_TZ))


def test_update_unknown_id_returns_none(tmp_path):
    store = ScheduleStore(tmp_path)
    assert store.update("nope", title="x") is None


def test_create_message_mode_roundtrip(tmp_path):
    """BUG 9: a schedule may carry a verbatim `message` instead of a `prompt`."""
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", message="Suyu iç", kind="daily", when="09:00", now=T0)
    assert item.message == "Suyu iç"
    assert item.prompt == ""
    got = store.get(item.id)
    assert got.message == "Suyu iç"


def test_create_rejects_both_prompt_and_message(tmp_path):
    store = ScheduleStore(tmp_path)
    with pytest.raises(ScheduleValidationError):
        store.create(
            title="t", prompt="p", message="m", kind="daily", when="09:00", now=T0
        )


def test_store_update_kind_change_requires_when(tmp_path):
    """BUG 6: changing `kind` without a matching `when` is rejected (was a silent
    no-op that reported success while leaving kind unchanged)."""
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="daily", when="09:00", now=T0)
    with pytest.raises(ScheduleValidationError):
        store.update(item.id, kind="interval", now=T0)  # no 'when'
    # With a compatible 'when' the change goes through.
    updated = store.update(item.id, kind="interval", when="3600", now=T0)
    assert updated.kind == "interval"
    assert updated.next_run_at == to_iso(T0 + timedelta(seconds=3600))


def test_store_reenable_spent_once_requires_new_when(tmp_path):
    """BUG 5: re-enabling a spent one-shot (past target) with no new time errors."""
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="once", when=to_iso(T0), now=T0)
    store.mark_ran(item.id, status="ok", now=T0)  # fired → disabled, target now past
    with pytest.raises(ScheduleValidationError):
        store.update(item.id, enabled=True, now=T0 + timedelta(hours=1))
    # A fresh future 'when' re-arms it.
    revived = store.update(
        item.id, enabled=True, when=to_iso(T0 + timedelta(days=1)),
        now=T0 + timedelta(hours=1),
    )
    assert revived.enabled is True
    assert revived.next_run_at == to_iso(T0 + timedelta(days=1))


# --- corruption tolerance ---------------------------------------------------


def test_garbage_file_raises_and_does_not_wipe(tmp_path):
    """A wholly-corrupt schedules.json must RAISE — never silently start empty.

    Starting empty would let the next mutation persist the empty view over real
    data (the store-wipe bug). So reads raise, a mutation aborts, and the corrupt
    bytes are left untouched on disk."""
    path = tmp_path / "schedules.json"
    garbage = "{not json"
    path.write_text(garbage, encoding="utf-8")
    store = ScheduleStore(tmp_path)
    with pytest.raises(ValueError):
        store.load()
    # A mutation reads first, so it aborts BEFORE writing — no wipe.
    with pytest.raises(ValueError):
        store.create(title="t", prompt="p", kind="interval", when="3600", now=T0)
    assert path.read_text(encoding="utf-8") == garbage


def test_transient_read_oserror_aborts_mutation_without_wiping(tmp_path, monkeypatch):
    """A transient read failure MUST abort the mutation and leave data intact.

    The store-wipe bug: on Windows a peer process mid-``os.replace`` briefly makes
    ``schedules.json`` unopenable, raising a sharing-violation ``PermissionError``.
    The old ``_read_rows`` caught that and "treated as empty", and ``_mutate`` then
    persisted the empty list over every real schedule. The fix re-raises, so the
    mutation aborts BEFORE writing and the existing schedules survive untouched."""
    store = ScheduleStore(tmp_path)
    a = store.create(title="a", prompt="p", kind="interval", when="3600", now=T0)
    b = store.create(title="b", prompt="p", kind="daily", when="09:00", now=T0)
    path = tmp_path / "schedules.json"
    before = path.read_text(encoding="utf-8")

    real_read_text = Path.read_text

    def boom(self, *args, **kwargs):
        if self.name == "schedules.json":
            raise PermissionError(13, "sharing violation (simulated)")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(PermissionError):
        store.cancel(a.id)  # read raises → mutation aborts
    monkeypatch.undo()

    # The file was NEVER overwritten with an empty store — no wipe.
    assert path.read_text(encoding="utf-8") == before
    assert {i.id for i in store.load()} == {a.id, b.id}  # both originals intact


def test_load_skips_one_malformed_row(tmp_path):
    store = ScheduleStore(tmp_path)
    good = store.create(title="t", prompt="p", kind="interval", when="3600", now=T0)
    # Inject a broken row alongside the good one.
    import json

    raw = json.loads((tmp_path / "schedules.json").read_text(encoding="utf-8"))
    raw["schedules"].append({"no_id": True, "kind": "interval"})
    (tmp_path / "schedules.json").write_text(json.dumps(raw), encoding="utf-8")
    items = store.load()
    assert [i.id for i in items] == [good.id]  # broken row skipped, good survives


# --- due-query --------------------------------------------------------------


def test_due_returns_only_past_enabled(tmp_path):
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="once",
                        when="2026-07-11T12:00", now=T0)
    assert store.due(T0) == []  # 12:00 not yet reached at 10:00
    due = store.due(datetime(2026, 7, 11, 12, 30, tzinfo=TR_TZ))
    assert [i.id for i in due] == [item.id]


def test_due_excludes_disabled(tmp_path):
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="once",
                        when="2026-07-11T09:00", now=T0)
    store.update(item.id, enabled=False, now=T0)
    assert store.due(T0) == []


# --- mark_ran: one-shot self-disable + roll-forward -------------------------


def test_once_disables_after_firing(tmp_path):
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="once",
                        when=to_iso(T0), now=T0)
    assert [i.id for i in store.due(T0)] == [item.id]
    store.mark_ran(item.id, status="ok", now=T0)
    fired = store.get(item.id)
    assert fired.enabled is False
    assert fired.last_run["status"] == "ok"
    assert store.due(T0 + timedelta(days=365)) == []  # never fires again


def test_interval_rolls_forward_once_from_now(tmp_path):
    """Server was off 10h; a 1h interval fires ONCE and rolls to now+1h (no backlog)."""
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="interval", when="3600", now=T0)
    late = T0 + timedelta(hours=10)  # long overdue
    store.mark_ran(item.id, status="ok", now=late)
    nxt = store.get(item.id)
    assert nxt.enabled is True
    assert nxt.next_run_at == to_iso(late + timedelta(seconds=3600))


def test_daily_rolls_forward_to_next_future_occurrence(tmp_path):
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="daily", when="09:00", now=T0)
    # Pretend it is 10 days later, 10:00 — the 09:00 slot has passed today.
    late = datetime(2026, 7, 21, 10, 0, tzinfo=TR_TZ)
    store.mark_ran(item.id, status="ok", now=late)
    nxt = store.get(item.id)
    assert nxt.next_run_at == to_iso(datetime(2026, 7, 22, 9, 0, tzinfo=TR_TZ))


def test_mark_ran_persists_conversation_id(tmp_path):
    store = ScheduleStore(tmp_path)
    item = store.create(title="t", prompt="p", kind="daily", when="09:00", now=T0)
    store.mark_ran(item.id, status="ok", conversation_id="conv-1", now=T0)
    got = store.get(item.id)
    assert got.delivery.conversation_id == "conv-1"
    assert got.last_run["conversation_id"] == "conv-1"
