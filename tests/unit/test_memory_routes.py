"""Memory v2 management API — /api/v1/memory/* over the clean src/akana core."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana.memory import FactCandidate, StagingStore

from akana_server.api.app import create_app

BASE = "/api/v1/memory"


def _make_client(monkeypatch: pytest.MonkeyPatch, tmp_path, *, vector: str | None):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    # allow_direct env override would defeat the YAML roundtrip assertions.
    monkeypatch.delenv("AKANA_MEMORY_ALLOW_DIRECT", raising=False)
    if vector is None:
        # Shipped default ("auto") — only the settings-roundtrip test needs this
        # to prove the default round-trips through YAML.
        monkeypatch.delenv("AKANA_MEMORY_VECTOR", raising=False)
    else:
        # Determinism: pin vector mode so a *locally running* Ollama can't engage
        # hybrid recall (strategy "rrf") or write embeddings. These tests assert
        # keyword-only behaviour ("fts_first", vector_embeddings == 0); without
        # this pin they pass in CI (no Ollama) but fail on dev machines where
        # "auto" probes a reachable Ollama. The suite must not depend on an
        # external service being absent.
        monkeypatch.setenv("AKANA_MEMORY_VECTOR", vector)
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    yield from _make_client(monkeypatch, tmp_path, vector="off")


@pytest.fixture
def client_auto_vector(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Ships-default ('auto') memory — used where a test asserts the default
    itself (settings roundtrip). Ollama-agnostic: makes no recall/embedding
    assertions, so it passes whether or not Ollama is reachable."""
    yield from _make_client(monkeypatch, tmp_path, vector=None)


def _stage(
    tmp_path,
    *,
    key: str = "şehir",
    value: str = "İzmir",
    trust: str = "user_statement",
) -> str:
    """Seed one pending candidate straight into the shared memory.db inbox."""
    staging = StagingStore.for_data_dir(Path(tmp_path))
    staged = staging.stage(
        FactCandidate(key=key, value=value, reason="unit-test", trust=trust),
        conversation_id="conv-1",
    )
    return staged.id


def _post_fact(client: TestClient, **overrides) -> dict:
    body = {"value": "Kahveyi sade içerim", "kind": "preference", **overrides}
    r = client.post(f"{BASE}/facts", json=body)
    assert r.status_code == 200
    return r.json()


# -- staging ----------------------------------------------------------------------


def test_staging_starts_empty(client: TestClient) -> None:
    r = client.get(f"{BASE}/staging")
    assert r.status_code == 200
    assert r.json() == {"items": [], "count": 0, "pending_count": 0}


def test_staging_invalid_status_422(client: TestClient) -> None:
    r = client.get(f"{BASE}/staging", params={"status": "bogus"})
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "INVALID_STATUS"


def test_approve_flow_staging_to_facts_to_recall(client: TestClient, tmp_path) -> None:
    staged_id = _stage(tmp_path)

    # Pending item is listed with the full review shape.
    item = client.get(f"{BASE}/staging").json()["items"][0]
    assert item["id"] == staged_id
    assert item["status"] == "pending"
    for field in ("key", "value", "reason", "trust", "ts", "conversation_id"):
        assert field in item
    assert item["key"] == "şehir"
    assert item["conversation_id"] == "conv-1"

    # Approve promotes it into a durable fact.
    r = client.post(f"{BASE}/staging/{staged_id}/approve")
    assert r.status_code == 200
    body = r.json()
    fact_id = body["fact_id"]
    assert body["status"] == "promoted"
    assert fact_id

    # Visible in the facts list (kind derived from the key).
    facts = client.get(f"{BASE}/facts").json()["items"]
    assert [f["id"] for f in facts] == [fact_id]
    assert facts[0]["value"] == "İzmir"
    assert facts[0]["kind"] == "fact"
    assert facts[0]["trust"] == "user_statement"

    # Searchable (Turkish-fold) and recallable through the orchestrator.
    hits = client.get(f"{BASE}/facts", params={"q": "izmir"}).json()["items"]
    assert fact_id in [f["id"] for f in hits]

    recall = client.get(f"{BASE}/recall", params={"q": "şehir"}).json()
    assert fact_id in [it["id"] for it in recall["items"]]
    assert recall["explain_id"]
    assert recall["trace"]["strategy"] == "fts_first"

    # Inbox bookkeeping: promoted, pointing at the new fact; not re-approvable.
    promoted = client.get(f"{BASE}/staging", params={"status": "promoted"}).json()
    assert promoted["items"][0]["promoted_fact_id"] == fact_id
    assert client.post(f"{BASE}/staging/{staged_id}/approve").status_code == 409


def test_reject_marks_candidate_rejected(client: TestClient, tmp_path) -> None:
    staged_id = _stage(tmp_path)
    r = client.post(f"{BASE}/staging/{staged_id}/reject")
    assert r.status_code == 200
    assert r.json() == {"status": "rejected", "staged_id": staged_id}

    rejected = client.get(f"{BASE}/staging", params={"status": "rejected"}).json()
    assert [s["id"] for s in rejected["items"]] == [staged_id]
    assert client.get(f"{BASE}/facts").json()["items"] == []
    # A decided candidate can be neither approved nor re-rejected.
    assert client.post(f"{BASE}/staging/{staged_id}/approve").status_code == 409
    assert client.post(f"{BASE}/staging/{staged_id}/reject").status_code == 409


def test_staging_unknown_id_404(client: TestClient) -> None:
    assert client.post(f"{BASE}/staging/nope/approve").status_code == 404
    assert client.post(f"{BASE}/staging/nope/reject").status_code == 404


# -- facts --------------------------------------------------------------------------


def test_create_fact_derives_kind_prefixed_key(client: TestClient) -> None:
    fact = _post_fact(client)
    assert fact["kind"] == "preference"
    assert fact["key"].startswith("preference:")
    assert fact["trust"] == "user_statement"
    assert fact["is_valid"] is True

    explicit = _post_fact(client, value="ssh portu 2222", key="ssh port", kind="rule")
    assert explicit["key"] == "rule:ssh port"
    assert explicit["kind"] == "rule"


def test_list_facts_q_limit_promise_above_50(client: TestClient) -> None:
    """Route promises a limit up to 500; on a q= search the store used to clamp to 50."""
    for i in range(60):
        _post_fact(client, value=f"limit deneme kaydı {i}", key=f"deneme {i}", kind="fact")
    body = client.get(f"{BASE}/facts", params={"q": "limit deneme", "limit": 500}).json()
    assert body["count"] == 60


def test_list_facts_pagination_offset_total(client: TestClient) -> None:
    """Browse paging: offset walks disjoint pages; total is the full match count."""
    n = 7
    for i in range(n):
        _post_fact(client, value=f"sayfalama kaydı {i}", key=f"sayfa {i}", kind="fact")

    seen: list[str] = []
    for off in (0, 3, 6):
        body = client.get(f"{BASE}/facts", params={"limit": 3, "offset": off}).json()
        assert body["total"] == n  # full count, independent of the page
        assert body["offset"] == off
        assert body["limit"] == 3
        seen.extend(f["id"] for f in body["items"])

    # 3 + 3 + 1 ids, all distinct — pages are disjoint and cover the whole set.
    assert len(seen) == n
    assert len(set(seen)) == n


def test_list_facts_search_hides_pager_total_equals_slice(client: TestClient) -> None:
    """On a q= search, total mirrors the returned slice so the pager stays hidden."""
    for i in range(5):
        _post_fact(client, value=f"arama kaydı {i}", key=f"arama {i}", kind="fact")
    body = client.get(f"{BASE}/facts", params={"q": "arama", "limit": 50}).json()
    assert body["total"] == body["count"] == len(body["items"])


def test_create_fact_requires_value(client: TestClient) -> None:
    assert client.post(f"{BASE}/facts", json={}).status_code == 422
    assert client.post(f"{BASE}/facts", json={"value": ""}).status_code == 422


def test_patch_supersede_preserves_history(client: TestClient) -> None:
    fact = _post_fact(client, value="Ankara'da yaşıyorum", kind="fact")
    r = client.patch(
        f"{BASE}/facts/{fact['id']}",
        json={"new_value": "İstanbul'da yaşıyorum", "mode": "supersede"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "superseded"
    assert body["old_id"] == fact["id"]
    new_id = body["fact"]["id"]
    assert new_id != fact["id"]

    valid = client.get(f"{BASE}/facts").json()["items"]
    assert [f["id"] for f in valid] == [new_id]
    everything = client.get(
        f"{BASE}/facts", params={"include_invalidated": "true"}
    ).json()["items"]
    by_id = {f["id"]: f for f in everything}
    assert by_id[fact["id"]]["invalidated_at"] is not None
    assert by_id[new_id]["value"] == "İstanbul'da yaşıyorum"

    # The closed row cannot be superseded again.
    again = client.patch(
        f"{BASE}/facts/{fact['id']}", json={"new_value": "x", "mode": "supersede"}
    )
    assert again.status_code == 409


def test_patch_correct_fixes_value_in_place(client: TestClient) -> None:
    fact = _post_fact(client, value="Kaheyi sade içerim")
    r = client.patch(
        f"{BASE}/facts/{fact['id']}",
        json={"new_value": "Kahveyi sade içerim", "mode": "correct"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "corrected"
    assert body["fact"]["id"] == fact["id"]  # same row, no new id
    assert body["fact"]["value"] == "Kahveyi sade içerim"
    assert len(client.get(f"{BASE}/facts").json()["items"]) == 1


def test_patch_validation_and_unknown(client: TestClient) -> None:
    assert (
        client.patch(f"{BASE}/facts/nope", json={"new_value": "x"}).status_code == 404
    )
    fact = _post_fact(client)
    bad = client.patch(
        f"{BASE}/facts/{fact['id']}", json={"new_value": "x", "mode": "explode"}
    )
    assert bad.status_code == 422


def test_delete_soft_then_hard(client: TestClient) -> None:
    soft = _post_fact(client, value="soft target", kind="fact")
    r = client.delete(f"{BASE}/facts/{soft['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "invalidated"
    assert client.get(f"{BASE}/facts").json()["items"] == []
    kept = client.get(f"{BASE}/facts", params={"include_invalidated": "true"}).json()
    assert soft["id"] in [f["id"] for f in kept["items"]]
    # Soft-deleting again is a no-op, not an error.
    assert client.delete(f"{BASE}/facts/{soft['id']}").json()["status"] == "already_inactive"

    hard = _post_fact(client, value="hard target", kind="fact")
    r = client.delete(f"{BASE}/facts/{hard['id']}", params={"hard": "true"})
    assert r.json()["status"] == "deleted"
    gone = client.get(f"{BASE}/facts", params={"include_invalidated": "true"}).json()
    assert hard["id"] not in [f["id"] for f in gone["items"]]

    assert client.delete(f"{BASE}/facts/nope").status_code == 404


# -- recall ---------------------------------------------------------------------------


def test_recall_requires_query(client: TestClient) -> None:
    assert client.get(f"{BASE}/recall").status_code == 422


def test_recall_empty_memory_returns_no_items(client: TestClient) -> None:
    body = client.get(f"{BASE}/recall", params={"q": "hiçbirşey", "k": 5}).json()
    assert body["items"] == []
    assert "trace" in body and "explain_id" in body


# -- settings ---------------------------------------------------------------------------


def test_settings_roundtrip_and_orchestrator_rebuild(
    client_auto_vector: TestClient, tmp_path
) -> None:
    client = client_auto_vector  # asserts the shipped default ("auto") round-trips
    before = client.get(f"{BASE}/settings").json()
    assert before["allow_direct"] is False
    assert before["vector"] == "auto"
    assert before["embed_backend"] == "local"  # fastembed default (NOT Ollama)
    assert before["settings_path"].endswith("memory_settings.yaml")

    # Instantiate the lazy orchestrator so PUT demonstrably replaces it.
    client.get(f"{BASE}/recall", params={"q": "warmup"})
    orch_before = client.app.state.memory_orchestrator

    r = client.put(
        f"{BASE}/settings",
        json={"allow_direct": True, "vector": "off", "embed_backend": "ollama"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["allow_direct"] is True
    assert body["vector"] == "off"
    assert body["embed_backend"] == "ollama"  # backend choice round-trips
    assert (tmp_path / "memory_settings.yaml").is_file()

    # Rebuilt with the new clamp, effective immediately.
    orch_after = client.app.state.memory_orchestrator
    assert orch_after is not orch_before
    assert orch_after._settings.allow_direct is True

    # Persisted: a fresh GET (and a fresh load under the hood) agrees.
    again = client.get(f"{BASE}/settings").json()
    assert again["allow_direct"] is True
    assert again["vector"] == "off"
    assert again["embed_backend"] == "ollama"  # persistent

    # With approvals relaxed the owner API still writes facts directly.
    fact = _post_fact(client, value="direct yazım", kind="fact")
    assert fact["is_valid"] is True


def test_settings_put_rejects_bad_vector_mode(client: TestClient) -> None:
    assert client.put(f"{BASE}/settings", json={"vector": "sometimes"}).status_code == 422


def test_settings_session_summary_roundtrip(client: TestClient) -> None:
    """The Memory Studio 'session summarization' toggle round-trips (default ON)."""
    before = client.get(f"{BASE}/settings").json()
    assert before["session_summary"] is True  # shipped default
    r = client.put(f"{BASE}/settings", json={"session_summary": False})
    assert r.status_code == 200
    assert r.json()["session_summary"] is False
    again = client.get(f"{BASE}/settings").json()
    assert again["session_summary"] is False  # persisted


# -- stats ------------------------------------------------------------------------------


def test_stats_shape(client: TestClient, tmp_path) -> None:
    _post_fact(client, value="istatistik fakti", kind="fact")
    _stage(tmp_path, key="bekleyen", value="aday")

    body = client.get(f"{BASE}/stats").json()
    assert body["facts"] == 1
    assert body["valid_facts"] == 1
    assert body["staging_pending"] == 1
    assert body["turns"] == 0
    assert body["conversations"] == 0
    assert body["vector_embeddings"] == 0
    assert body["ledger_path"].endswith("event_log.jsonl")
    # Vector health summary (observability): 'is it working' at a glance.
    v = body["vector"]
    assert {"active", "mode", "backend", "available", "embeddings", "models"} <= set(v)
    assert v["embeddings"] == 0


# -- timeline ---------------------------------------------------------------------------


def test_timeline_empty(client: TestClient) -> None:
    body = client.get(f"{BASE}/timeline").json()
    assert body == {"items": [], "count": 0}


def test_timeline_newest_first_with_human_titles(client: TestClient) -> None:
    _post_fact(client, value="bir", key="k1", kind="fact")
    _post_fact(client, value="iki", key="k2", kind="fact")

    body = client.get(f"{BASE}/timeline").json()
    assert body["count"] >= 2
    items = body["items"]
    for it in items:
        assert set(it) == {"ts", "kind", "title", "detail", "ref_id"}
    # Newest first: ts in descending order (ms-Z ISO → lexicographic = chronological).
    timestamps = [it["ts"] for it in items]
    assert timestamps == sorted(timestamps, reverse=True)
    # Most recently written fact first; carries a human-readable title + ref_id.
    newest = items[0]
    assert newest["kind"] == "fact"
    assert newest["title"] == "New fact"
    assert newest["detail"] == "k2: iki"
    assert newest["ref_id"]


def test_timeline_limit_and_kind_filter(client: TestClient) -> None:
    for i in range(3):
        _post_fact(client, value=f"v{i}", key=f"k{i}", kind="fact")
    # limit returns the last N events.
    limited = client.get(f"{BASE}/timeline", params={"limit": 1}).json()
    assert limited["count"] == 1
    # The kind filter returns only that raw event.kind.
    facts_only = client.get(f"{BASE}/timeline", params={"kind": "fact"}).json()
    assert facts_only["count"] >= 3
    assert all(it["kind"] == "fact" for it in facts_only["items"])
    # A non-existent kind → empty.
    assert client.get(f"{BASE}/timeline", params={"kind": "nope"}).json()["count"] == 0


# -- recall as_of (D: time travel) ------------------------------------------------------


def test_recall_as_of_returns_historical_value(client: TestClient) -> None:
    # write v1 → take an as_of stamp → supersede → as_of must return the old value.
    v1 = _post_fact(client, value="Ankara", key="şehir", kind="fact")

    # A moment when v1 was valid: its own valid_from (<= itself is always true).
    everything = client.get(
        f"{BASE}/facts", params={"include_invalidated": "true"}
    ).json()["items"]
    v1_row = next(f for f in everything if f["id"] == v1["id"])
    as_of_stamp = v1_row["valid_from"] or v1_row["ts_first"]

    # Let the supersede happen a bit later so v1's invalidated_at is greater than as_of.
    time.sleep(0.01)
    r = client.patch(
        f"{BASE}/facts/{v1['id']}",
        json={"new_value": "İstanbul", "mode": "supersede"},
    )
    assert r.status_code == 200

    # Live recall sees the most recent value (İstanbul).
    live = client.get(f"{BASE}/recall", params={"q": "şehir"}).json()
    live_summaries = " ".join(it["summary"] for it in live["items"])
    assert "İstanbul" in live_summaries

    # as_of looks at the past: returns Ankara, which was valid at that moment.
    past = client.get(
        f"{BASE}/recall", params={"q": "şehir", "as_of": as_of_stamp}
    ).json()
    assert past["trace"]["strategy"] == "as_of"
    past_summaries = " ".join(it["summary"] for it in past["items"])
    assert "Ankara" in past_summaries
    assert "İstanbul" not in past_summaries


def test_recall_as_of_invalid_format_400(client: TestClient) -> None:
    r = client.get(f"{BASE}/recall", params={"q": "x", "as_of": "not-a-time"})
    assert r.status_code == 400


# -- recall observed_from/observed_to (bi-temporal observation filter) -------------------


def _seed_observed_fact(tmp_path, *, fact_id: str, key: str, value: str, observed_at: str) -> None:
    """Write a fact with a specific observation time directly into the shared memory.db."""
    from akana.memory.semantic import SemanticStore

    SemanticStore.for_data_dir(Path(tmp_path)).upsert_fact(
        fact_id=fact_id, key=key, value=value, trust="user_statement",
        observed_at=observed_at,
    )


def test_recall_observed_range_filters_old_vs_new(client: TestClient, tmp_path) -> None:
    # Old observation (March) written directly to the DB; new record via the API (observed=now).
    _seed_observed_fact(
        tmp_path, fact_id="kahve-eski", key="kahve notu",
        value="filtre kahve sever", observed_at="2026-03-10T09:00:00.000Z",
    )
    yeni = _post_fact(client, value="kahve makinesi espresso yapıyor", key="kahve makinesi")

    both = client.get(f"{BASE}/recall", params={"q": "kahve"}).json()
    assert {"kahve-eski", yeni["id"]} <= {i["id"] for i in both["items"]}

    # The March window returns only the old observation ('to' date-only: the day is covered).
    march = client.get(
        f"{BASE}/recall",
        params={"q": "kahve", "observed_from": "2026-03-01", "observed_to": "2026-03-31"},
    ).json()
    assert [i["id"] for i in march["items"]] == ["kahve-eski"]

    # Turkish natural phrase: things observed today → only the new record.
    today = client.get(
        f"{BASE}/recall", params={"q": "kahve", "observed_from": "bugün"}
    ).json()
    assert [i["id"] for i in today["items"]] == [yeni["id"]]

    # Trace reports from a single place.
    stage = next(s for s in march["trace"]["stages"] if s["stage"] == "observed_filter")
    assert stage["dropped"] >= 1


def test_recall_observed_invalid_format_400(client: TestClient) -> None:
    r = client.get(f"{BASE}/recall", params={"q": "x", "observed_from": "not-a-time"})
    assert r.status_code == 400
    r2 = client.get(f"{BASE}/recall", params={"q": "x", "observed_to": "not-a-time"})
    assert r2.status_code == 400


def test_recall_as_of_accepts_turkish_phrase(client: TestClient) -> None:
    """as_of='bugün' → state as of end of day (the most recent value)."""
    _post_fact(client, value="Ankara", key="şehir", kind="fact")
    out = client.get(f"{BASE}/recall", params={"q": "şehir", "as_of": "bugün"}).json()
    assert out["trace"]["strategy"] == "as_of"
    assert any("Ankara" in i["summary"] for i in out["items"])


# -- provenance (citation-native source) ------------------------------------------------


def test_fact_payload_carries_source(client: TestClient) -> None:
    """Every fact response carries its {origin, detail, observed_at} source."""
    fact = _post_fact(client, value="koyu tema", key="tema", kind="fact")
    src = fact["source"]
    assert src["origin"] == "user_statement"  # owner write → derives from trust
    assert src["detail"] == "api.memory"  # detail falls back to extractor
    assert src["observed_at"]

    # If source_detail is provided it is carried verbatim (tool name / URL / conversation id).
    cited = _post_fact(
        client, value="ssh portu 2222", key="ssh", kind="rule",
        source_detail="https://wiki.local/ssh",
    )
    assert cited["source"]["detail"] == "https://wiki.local/ssh"

    listed = client.get(f"{BASE}/facts").json()["items"]
    assert all("source" in f and f["source"]["origin"] for f in listed)


def test_recall_items_carry_source(client: TestClient, tmp_path) -> None:
    staged_id = _stage(tmp_path)  # key=şehir, value=İzmir, trust=user_statement
    client.post(f"{BASE}/staging/{staged_id}/approve")

    recall = client.get(f"{BASE}/recall", params={"q": "şehir"}).json()
    semantic = [it for it in recall["items"] if it["type"] != "Episode"]
    assert semantic
    for it in semantic:
        assert it["source"]["origin"] == "user_statement"
        assert it["source"]["observed_at"]


def test_supersede_keeps_provenance_alive(client: TestClient) -> None:
    fact = _post_fact(client, value="Ankara", key="şehir", kind="fact")
    r = client.patch(
        f"{BASE}/facts/{fact['id']}",
        json={"new_value": "İstanbul", "mode": "supersede"},
    )
    new = r.json()["fact"]
    assert new["source"]["origin"] == "user_statement"  # carried over from the old trust
    assert new["source"]["detail"] == "api.memory"


# -- facts salience fields --------------------------------------------------------------


def test_facts_carry_salience_fields(client: TestClient) -> None:
    _post_fact(client, value="koyu", key="tema", kind="fact")
    fact = client.get(f"{BASE}/facts").json()["items"][0]
    # §13 salience fields must be in the response shape (new fact → 0 / None).
    assert "hit_count" in fact and fact["hit_count"] == 0
    assert "last_hit_at" in fact and fact["last_hit_at"] is None
    assert "importance" in fact and isinstance(fact["importance"], (int, float))


# -- auth -------------------------------------------------------------------------------


def test_bearer_required_when_token_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "sekret-token")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    # The token gate applies only to PROXIED requests; X-Forwarded-For makes the
    # request look proxied so the configured token is actually enforced.
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    with TestClient(create_app()) as c:
        assert c.get(f"{BASE}/staging", headers=proxied).status_code == 401
        assert c.get(f"{BASE}/stats", headers=proxied).status_code == 401
        assert c.get(f"{BASE}/timeline", headers=proxied).status_code == 401
        assert (
            c.put(
                f"{BASE}/settings", json={"allow_direct": True}, headers=proxied
            ).status_code
            == 401
        )
        ok = c.get(
            f"{BASE}/stats", headers={**proxied, "Authorization": "Bearer sekret-token"}
        )
        assert ok.status_code == 200


# -- audit fixes (Group A) --------------------------------------------------------------


def test_patch_correct_on_invalidated_fact_is_409_not_404(client: TestClient) -> None:
    """audit C30: correct_fact returns None for both missing AND already-invalidated
    facts; the route must distinguish them — an invalidated fact is 409, not 404
    (404 wrongly implies the fact does not exist though it was just found)."""
    fact = _post_fact(client, value="düzeltilecek", kind="fact")
    assert client.delete(f"{BASE}/facts/{fact['id']}").status_code == 200  # soft-invalidate
    r = client.patch(
        f"{BASE}/facts/{fact['id']}", json={"new_value": "yeni", "mode": "correct"}
    )
    assert r.status_code == 409  # was a misleading 404 before the fix
    # A genuinely missing fact is still 404.
    assert (
        client.patch(
            f"{BASE}/facts/does-not-exist", json={"new_value": "x", "mode": "correct"}
        ).status_code
        == 404
    )


def test_stats_counts_total_vs_valid_facts(client: TestClient) -> None:
    """audit C28: stats now derives counts from COUNT(*) (count_facts) rather than
    hydrating rows; total counts invalidated, valid_facts does not."""
    keep = _post_fact(client, value="geçerli fakt", key="k_keep", kind="fact")
    drop = _post_fact(client, value="silinen fakt", key="k_drop", kind="fact")
    assert keep["id"] and drop["id"]
    client.delete(f"{BASE}/facts/{drop['id']}")  # invalidate one
    body = client.get(f"{BASE}/stats").json()
    assert body["facts"] == 2  # include_invalidated total
    assert body["valid_facts"] == 1  # only the still-valid one


@pytest.mark.parametrize(
    ("code", "status"),
    [("rate_limited", 429), ("internal_error", 500), ("invalid_request", 400)],
)
def test_recall_maps_orchestrator_error_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, code: str, status: int
) -> None:
    """audit C29: /recall must map the orchestrator error code through the shared
    status map (rate_limited→429, internal_error→500), not collapse everything to 400."""
    from akana.memory import MemoryOrchestrator

    monkeypatch.setattr(
        MemoryOrchestrator,
        "handle_tool_call",
        lambda self, name, args=None, **kw: {"error": {"code": code, "message": "x"}},
    )
    r = client.get(f"{BASE}/recall", params={"q": "herhangi"})
    assert r.status_code == status
