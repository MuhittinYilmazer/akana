"""ConversationService.list_conversations — the Archived view must be archived-only.

The route maps ``GET /conversations?archived=true`` to ``archived_only=True``; the
service must push that filter down to the store BEFORE the 200-row ceiling, so an
archived conversation older than the newest 200 active ones is still returned (it is
otherwise unsearchable — search hard-codes ``WHERE archived=0`` — hence un-unarchivable).
"""

from __future__ import annotations

from pathlib import Path

from akana_server.conversation_service import ConversationService


def test_archived_only_view_returns_old_archived_beyond_active_ceiling(
    tmp_path: Path,
) -> None:
    svc = ConversationService(tmp_path)
    store = svc._meta_store
    store.ensure("arch")
    store.patch("arch", archived=True)  # oldest row, archived
    for i in range(201):  # more than the 200 ceiling, all newer + active
        store.ensure(f"active-{i:03d}")

    archived_view = svc.list_conversations(limit=50, archived_only=True)
    ids = [m.id for m in archived_view]
    assert ids == ["arch"]  # the archived conversation is reachable
    assert archived_view[0].archived_at is not None  # frontend filters on this

    # Default (active) view never shows the archived row.
    active_view = svc.list_conversations(limit=50)
    assert "arch" not in {m.id for m in active_view}
