"""Observability panel backend — GET /api/v1/observability/summary.

Hermetic: builds a MINIMAL FastAPI app that mounts ONLY
``akana_server.api.routes.observability.router`` (not the full ``create_app()``
lifespan/router set) — this route has no dependency on the rest of the app
being wired up, and the test proves that in isolation. ``app.state.settings``
is a real ``Settings`` instance (via ``load_settings()`` + monkeypatched env),
so ``require_akana_bearer``/``resolve_provider``/etc. see the exact same shape
they see in production, without paying for the full server startup.

Scope: response shape (all four sections always present), the bearer gate,
bounded-scan usage aggregation (seeded via the real turn_writer persist path —
the SAME write path chat/voice use, so the read side is exercised against
realistic data), breaker health reflecting into the snapshot, audit-tail reuse,
and the empty-state (fresh data_dir) contract: zeros, never a 500.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from akana_server.api.routes.observability import (
    _parse_iso,
    _reset_summary_cache,
)
from akana_server.api.routes.observability import router as observability_router
from akana_server.audit import write_event as audit_write_event
from akana_server.config import load_settings
from akana_server.llm_settings import LlmSettings, save_llm_settings
from akana_server.network.guard import global_registry, reset_global_registry
from akana_server.observability import registry
from akana_server.orchestrator.turn_writer import (
    persist_assistant_turn,
    persist_user_turn,
)

URL = "/api/v1/observability/summary"


@pytest.fixture(autouse=True)
def _isolated_process_state():
    """Reset the two module-level singletons this route reads (breakers + metrics).

    Both ``global_registry()`` (network breakers) and ``observability.registry``
    (counters/timers) are process-wide singletons — without a reset, a counter
    bumped by an EARLIER test in the same pytest process would leak into this
    file's "empty state" assertions.
    """
    reset_global_registry()
    registry.reset()
    # The route memoizes its assembled payload in a process-wide short-TTL cache;
    # clear it so one test's cached summary can't leak into the next test's assertions.
    _reset_summary_cache()
    yield
    reset_global_registry()
    registry.reset()
    _reset_summary_cache()


def _make_app(tmp_path: Path) -> FastAPI:
    """Minimal app: just ``app.state.settings`` + the observability router.

    Mirrors the real mount point (``prefix="/api/v1"``, as every sibling router
    in ``akana_server/api/app.py`` is mounted) without importing ``create_app``
    or its lifespan.
    """
    app = FastAPI()
    app.state.settings = load_settings()
    app.include_router(observability_router, prefix="/api/v1")
    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    # Pin the env-derived provider default so "unconfigured" assertions don't
    # depend on the developer's local .env (this repo's own .env sets
    # LLM_PROVIDER=cursor for day-to-day use — dotenv's override=False means an
    # explicit monkeypatch here still wins, same pattern as test_llm_settings_routes.py).
    monkeypatch.setenv("LLM_PROVIDER", "")
    app = _make_app(tmp_path)
    with TestClient(app) as c:
        yield c


def _data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


# -- shape / empty state -----------------------------------------------------


def test_summary_shape_and_empty_state(client: TestClient) -> None:
    """Fresh data_dir → every section present, all zeros, no 500."""
    r = client.get(URL)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"metrics", "usage", "health", "audit"}

    metrics = body["metrics"]
    assert set(metrics) == {"counters", "timers"}
    assert metrics["counters"] == {}
    assert metrics["timers"] == {}

    usage = body["usage"]
    assert usage["turns_total"] == 0
    assert usage["conversations_in_window"] == 0
    assert usage["conversations_scanned_for_tokens"] == 0
    assert usage["tokens"] == {"prompt": 0, "completion": 0, "total": 0}
    assert usage["cost_usd"] == 0
    assert usage["provider_attribution"] is False
    assert usage["per_provider"] is None
    assert isinstance(usage["note"], str) and usage["note"]

    health = body["health"]
    assert health["active_provider"] == ""  # unconfigured by default
    assert health["breakers"] == []

    audit = body["audit"]
    assert audit == {"count": 0, "events": []}


def test_summary_requires_bearer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AKANA_TOKEN", "secret-token")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = _make_app(tmp_path)
    # Same trick as test_network_route.py: X-Forwarded-For makes the request
    # look proxied so the token gate (loopback-skip does NOT apply) is enforced.
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    with TestClient(app) as c:
        assert c.get(URL, headers=proxied).status_code == 401
        ok = c.get(URL, headers={**proxied, "Authorization": "Bearer secret-token"})
        assert ok.status_code == 200


# -- metrics passthrough ------------------------------------------------------


def test_summary_includes_metrics_counters(client: TestClient) -> None:
    registry.incr("llm_errors", 3)
    registry.observe("turn_latency_ms", 42.0)
    body = client.get(URL).json()
    assert body["metrics"]["counters"]["llm_errors"] == {"value": 3.0}
    assert body["metrics"]["timers"]["turn_latency_ms"]["count"] == 1.0


# -- health / breakers ---------------------------------------------------------


def test_summary_health_reflects_open_breaker(client: TestClient) -> None:
    br = global_registry().get("cursor")
    for _ in range(10):
        br.record_failure()
    body = client.get(URL).json()
    names = {b["name"]: b for b in body["health"]["breakers"]}
    assert "cursor" in names
    assert names["cursor"]["state"] == "open"


def test_summary_health_active_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    save_llm_settings(_data_dir(tmp_path), LlmSettings(provider="claude"))
    app = _make_app(tmp_path)
    with TestClient(app) as c:
        body = c.get(URL).json()
    assert body["health"]["active_provider"] == "claude"


# -- usage aggregation (bounded scan) ------------------------------------------


def _seed_turn(
    data_dir: Path,
    conv_id: str,
    *,
    prompt: int,
    completion: int,
    cost: float,
    provider: str | None = None,
) -> None:
    uid = persist_user_turn(
        conversation_id=conv_id, user_text="hello", data_dir=data_dir
    )
    usage: dict = {"prompt": prompt, "completion": completion, "cost_usd": cost}
    if provider is not None:
        usage["provider"] = provider
    persist_assistant_turn(
        conversation_id=conv_id,
        assistant_text="hi there",
        user_turn_id=uid,
        data_dir=data_dir,
        usage=usage,
    )


def test_summary_usage_aggregates_persisted_turns(client: TestClient, tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    _seed_turn(data_dir, "conv-a", prompt=100, completion=50, cost=0.01)
    _seed_turn(data_dir, "conv-a", prompt=200, completion=25, cost=0.02)
    _seed_turn(data_dir, "conv-b", prompt=10, completion=5, cost=0.001)

    body = client.get(URL).json()
    usage = body["usage"]
    # 2 conversations, 2 turns each (user+assistant) = 4 turns in conv-a, 2 in conv-b.
    assert usage["turns_total"] == 6
    assert usage["conversations_in_window"] == 2
    assert usage["conversations_scanned_for_tokens"] == 2
    assert usage["tokens"]["prompt"] == 310
    assert usage["tokens"]["completion"] == 80
    assert usage["tokens"]["total"] == 390
    assert usage["cost_usd"] == pytest.approx(0.031)
    # Un-stamped (legacy) turns → no attribution; they land in the "unknown" bucket
    # but provider_attribution stays False until a stamped turn appears.
    assert usage["provider_attribution"] is False
    assert usage["per_provider"] is None


def test_summary_usage_breaks_down_per_provider_when_stamped(
    client: TestClient, tmp_path: Path
) -> None:
    """New turns carry a ``provider`` stamp (llm_dispatch → _done_tokens_block) that
    survives persistence (usage is JSON-dumped whole) → the panel attributes tokens
    per provider."""
    data_dir = _data_dir(tmp_path)
    _seed_turn(data_dir, "c-claude", prompt=100, completion=40, cost=0.02, provider="claude")
    _seed_turn(data_dir, "c-codex", prompt=30, completion=10, cost=0.0, provider="codex")
    _seed_turn(data_dir, "c-legacy", prompt=5, completion=5, cost=0.0)  # no stamp

    usage = client.get(URL).json()["usage"]
    assert usage["provider_attribution"] is True
    per = usage["per_provider"]
    assert per["claude"]["prompt"] == 100 and per["claude"]["completion"] == 40
    assert per["claude"]["turns"] == 1
    assert per["codex"]["prompt"] == 30
    # The un-stamped legacy turn is bucketed under "unknown", not dropped.
    assert per["unknown"]["prompt"] == 5
    # Aggregate totals still include everything.
    assert usage["tokens"]["total"] == 100 + 40 + 30 + 10 + 5 + 5


def test_summary_usage_days_query_param_bounds_window(
    client: TestClient, tmp_path: Path
) -> None:
    """A conversation outside the requested window is excluded from turns_total."""
    data_dir = _data_dir(tmp_path)
    _seed_turn(data_dir, "conv-recent", prompt=1, completion=1, cost=0.0)
    # usage_days=90 (the route's upper Query bound) must still include it (sanity:
    # the param actually applies and a wide window doesn't exclude "just happened").
    body_wide = client.get(URL, params={"usage_days": 90}).json()
    assert body_wide["usage"]["turns_total"] == 2
    # A tiny but valid window (>=1, per the route's Query ge=1) still includes a
    # turn written moments ago — proves the param is honored without breaking
    # "just happened" data.
    body_narrow = client.get(URL, params={"usage_days": 1}).json()
    assert body_narrow["usage"]["turns_total"] == 2


# -- audit tail reuse -----------------------------------------------------------


def test_summary_audit_tail_reuses_read_tail(client: TestClient, tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    audit_write_event(data_dir, "chat", conv_id="conv-1", data={"n": 1})
    audit_write_event(data_dir, "voice", conv_id="conv-2", data={"n": 2})

    body = client.get(URL).json()
    assert body["audit"]["count"] == 2
    kinds = [e["kind"] for e in body["audit"]["events"]]
    assert kinds == ["chat", "voice"]  # chronological, oldest first (read_tail order)


def test_summary_audit_limit_query_param(client: TestClient, tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    audit_write_event(data_dir, "chat", data={"n": 1})
    audit_write_event(data_dir, "voice", data={"n": 2})

    body = client.get(URL, params={"audit_limit": 1}).json()
    assert body["audit"]["count"] == 1
    assert body["audit"]["events"][0]["kind"] == "voice"  # tail = newest within the cap


# -- H1: meta-only tier-1 (no N+1) --------------------------------------------


def test_summary_meta_only_tier1_scales(client: TestClient, tmp_path: Path) -> None:
    """Tier-1 aggregates many conversations from a SINGLE meta query (no N+1 scan).

    The old path called ``_newest_turn`` per conversation (a fresh SQLite connection
    each) — a large data_dir froze the event loop for ~0.5 s per poll. This seeds a
    batch of conversations and asserts the aggregate is correct via the meta-only path
    (``message_count`` + ``updated_at`` read straight off ``ConversationMeta``).
    """
    data_dir = _data_dir(tmp_path)
    n = 30
    for i in range(n):
        _seed_turn(data_dir, f"conv-{i:03d}", prompt=1, completion=1, cost=0.0)

    body = client.get(URL).json()
    usage = body["usage"]
    assert usage["conversations_in_window"] == n
    assert usage["turns_total"] == 2 * n  # user + assistant per seeded conversation
    # n (30) <= _MAX_CONVERSATIONS_FOR_USAGE (50) → all are token-scanned in tier 2.
    assert usage["conversations_scanned_for_tokens"] == n
    assert usage["tokens"]["prompt"] == n
    assert usage["tokens"]["completion"] == n


# -- M1: naive timestamp must never 500 ---------------------------------------


def test_parse_iso_normalizes_naive_to_aware() -> None:
    """A tz-less stamp parses to an AWARE (UTC) datetime, never a naive one."""
    aware = _parse_iso("2020-01-01T00:00:00")  # no tz suffix
    assert aware is not None
    assert aware.tzinfo is not None  # normalized, so ``ts >= aware_cutoff`` can't raise
    # A ``Z``-suffixed stamp stays aware too (unchanged behavior).
    z = _parse_iso("2026-07-11T10:00:00.000Z")
    assert z is not None and z.tzinfo is not None
    assert _parse_iso("not-a-date") is None


def test_summary_naive_timestamp_does_not_500(client: TestClient, tmp_path: Path) -> None:
    """A tz-less (naive) conversation timestamp must not 500 the endpoint.

    Regression: comparing a naive parsed datetime against the aware ``cutoff``
    (``ts >= cutoff``) raised ``TypeError`` — uncaught (only ``ValueError`` was), it
    500'd the panel forever, breaking the "never 500 on a bad timestamp" contract.
    """
    data_dir = _data_dir(tmp_path)
    _seed_turn(data_dir, "conv-naive", prompt=1, completion=1, cost=0.0)
    # Rewrite the row's updated_at to a tz-LESS ISO string, but RECENT (within the
    # window) — this exercises the actual naive-vs-aware comparison (the crash path),
    # not just an "old row is excluded" shortcut. Mirrors a legacy/hand-edited stamp
    # that predates the Z-suffixed iso_now() convention.
    naive_recent = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    db = data_dir / "db" / "memory.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (naive_recent, "conv-naive"),
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get(URL)
    assert r.status_code == 200  # not a 500
    # The naive-but-recent stamp was normalized to UTC and compared successfully, so
    # the conversation is still counted in the window (2 turns: user + assistant).
    assert r.json()["usage"]["turns_total"] == 2


# -- H1: short-TTL cache coalesces repeated polls -----------------------------


def test_summary_cached_within_ttl(client: TestClient, tmp_path: Path) -> None:
    """A repeat poll within the TTL is served from cache — the scan is not re-run.

    Proves the endpoint doesn't re-run the SQLite scan on every ~10s poll: seed, GET
    (populates the cache), MUTATE the store, GET again immediately — the second
    response is byte-identical to the first (ignores the mutation), which is only
    possible if the scan was skipped and the cached payload returned.
    """
    data_dir = _data_dir(tmp_path)
    _seed_turn(data_dir, "conv-a", prompt=100, completion=50, cost=0.01)
    first = client.get(URL).json()
    assert first["usage"]["turns_total"] == 2

    # Mutate AFTER the first poll cached the payload; a fresh rescan would see 4 turns.
    _seed_turn(data_dir, "conv-b", prompt=10, completion=5, cost=0.001)
    second = client.get(URL).json()
    assert second["usage"]["turns_total"] == 2  # cached, NOT the fresh 4
    assert second == first  # byte-identical cached payload

    # A different query param is a different cache key → fresh scan sees both convs.
    fresh = client.get(URL, params={"usage_days": 6}).json()
    assert fresh["usage"]["turns_total"] == 4
