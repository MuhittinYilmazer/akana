"""Settings → MCP wiring: does memory_settings.yaml actually drive the server?

Via ``build_orchestrator`` (the setup seam of mcp.main): the K30
``allow_direct`` clamp, vector modes (off/auto/on) and the Ollama probe.
No test connects to a real Ollama — the probe is either monkeypatched or
the embedder is injected via a parameter.
"""

from __future__ import annotations

import http.server
import json
import logging
import threading
from types import SimpleNamespace

import pytest

import akana.memory.mcp as mcp_mod
from akana.memory import HashingEmbedder, Memory
from akana.memory.embed import is_available
from akana.memory.mcp import McpServer, build_orchestrator
from akana.memory.settings import MemorySettings, save_memory_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Keep overrides from the developer environment from leaking into tests."""
    monkeypatch.delenv("AKANA_MEMORY_ALLOW_DIRECT", raising=False)
    monkeypatch.delenv("AKANA_MEMORY_VECTOR", raising=False)
    monkeypatch.delenv("AKANA_MEMORY_EMBED_BACKEND", raising=False)


def _forbid_probe(monkeypatch):
    """Fail the test if is_available is called (no network, no probe expected)."""
    monkeypatch.setattr(
        mcp_mod, "is_available", lambda *a, **k: pytest.fail("is_available should not have been called")
    )


def _remember_direct(orchestrator):
    return orchestrator.handle_tool_call(
        "memory.remember",
        {"content": "Pamuk", "kind": "fact", "key": "kedi adı", "policy": "direct"},
    )


# -- allow_direct clamp wiring ---------------------------------------------------


def test_allow_direct_yaml_reaches_mcp_remember(tmp_path, monkeypatch):
    """allow_direct=true in yaml → direct remember from MCP is not staged to inbox, it is written."""
    _forbid_probe(monkeypatch)  # vector=off: the probe should never run
    save_memory_settings(tmp_path, MemorySettings(allow_direct=True, vector="off"))
    memory, orchestrator, indexer = build_orchestrator(tmp_path)
    assert indexer is None

    server = McpServer(orchestrator)
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "memory_remember",
                "arguments": {
                    "content": "Pamuk",
                    "kind": "fact",
                    "key": "kedi adı",
                    "policy": "direct",
                },
            },
        }
    )
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["status"] == "stored"
    fact = memory.get_fact(payload["fact_id"])
    assert fact is not None and fact.value == "Pamuk"


def test_default_settings_keep_k30_clamp(tmp_path, monkeypatch):
    """With no yaml the default holds: a direct request is staged (K30)."""
    monkeypatch.setattr(mcp_mod, "is_available", lambda *a, **k: False)
    _, orchestrator, _ = build_orchestrator(tmp_path)
    out = _remember_direct(orchestrator)
    assert out["status"] == "staged"
    assert out["requested_policy"] == "direct"


def test_allow_direct_toggle_is_live_without_rebuild(tmp_path, monkeypatch):
    """Toggle bug fix: on a RUNNING orchestrator, when the allow_direct yaml changes the remember
    behavior changes IMMEDIATELY — no need to restart the MCP child (the bug the user described
    as 'the inbox on/off setting is broken'). _live_settings refreshes on every write."""
    monkeypatch.setattr(mcp_mod, "is_available", lambda *a, **k: False)
    save_memory_settings(tmp_path, MemorySettings(allow_direct=False, vector="off"))
    _, orchestrator, _ = build_orchestrator(tmp_path)

    assert _remember_direct(orchestrator)["status"] == "staged"  # clamp on → inbox

    # The user turned the toggle ON (yaml changed) — SAME orchestrator instance
    save_memory_settings(tmp_path, MemorySettings(allow_direct=True, vector="off"))
    assert _remember_direct(orchestrator)["status"] == "stored"  # live: written directly

    # Turned it back OFF → clamp again (live in both directions)
    save_memory_settings(tmp_path, MemorySettings(allow_direct=False, vector="off"))
    assert _remember_direct(orchestrator)["status"] == "staged"


# -- vector modes -------------------------------------------------------------------


def test_vector_off_keeps_fts_fallback(tmp_path, monkeypatch):
    """vector=off: no probe, no strategy record — explore still falls back to fts (with a warning)."""
    _forbid_probe(monkeypatch)
    save_memory_settings(tmp_path, MemorySettings(vector="off"))
    memory, orchestrator, indexer = build_orchestrator(tmp_path)
    assert indexer is None

    memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    out = orchestrator.handle_tool_call("memory.search", {"query": "kedi", "intent": "explore"})
    assert out["trace"]["strategy"] == "fts_first"
    assert any("fell back" in w for w in out["warnings"])


def test_auto_unreachable_degrades_to_keyword(tmp_path, monkeypatch):
    """ollama backend + auto + Ollama down: no indexer, works with keyword."""
    monkeypatch.setenv("AKANA_MEMORY_EMBED_BACKEND", "ollama")
    monkeypatch.setattr(mcp_mod, "is_available", lambda *a, **k: False)
    memory, orchestrator, indexer = build_orchestrator(tmp_path)
    assert indexer is None
    memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    out = orchestrator.handle_tool_call("memory.search", {"query": "kedi", "intent": "explore"})
    assert "error" not in out
    assert out["trace"]["strategy"] == "fts_first"


def test_local_backend_degrades_when_fastembed_missing(tmp_path, monkeypatch):
    """Default backend=local but fastembed missing: no indexer, keyword continues,
    Ollama is NEVER contacted (the local path must not call the probe)."""
    _forbid_probe(monkeypatch)
    monkeypatch.setattr(mcp_mod, "_fastembed_available", lambda: False)
    memory, orchestrator, indexer = build_orchestrator(tmp_path)
    assert indexer is None
    memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    out = orchestrator.handle_tool_call("memory.search", {"query": "kedi", "intent": "explore"})
    assert "error" not in out
    assert out["trace"]["strategy"] == "fts_first"


def test_on_unreachable_logs_error_and_survives(tmp_path, monkeypatch, caplog):
    """ollama backend + vector=on + Ollama down: the process does not crash — log.error + keyword."""
    monkeypatch.setenv("AKANA_MEMORY_EMBED_BACKEND", "ollama")
    monkeypatch.setattr(mcp_mod, "is_available", lambda *a, **k: False)
    save_memory_settings(tmp_path, MemorySettings(vector="on"))
    with caplog.at_level(logging.ERROR, logger="akana.memory.mcp"):
        memory, orchestrator, indexer = build_orchestrator(tmp_path)
    assert indexer is None
    assert any(
        r.levelno == logging.ERROR and "vector=on" in r.getMessage() for r in caplog.records
    )
    memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    out = orchestrator.handle_tool_call("memory.search", {"query": "kedi"})
    assert "error" not in out and out["items"]


# -- embedder injection (without touching settings) ---------------------------------------


def test_injected_embedder_enables_vector_first(tmp_path, monkeypatch):
    """An embedder passed via parameter skips the probe; explore → vector_first."""
    _forbid_probe(monkeypatch)  # with injection present the probe must not run
    memory, orchestrator, indexer = build_orchestrator(tmp_path, embedder=HashingEmbedder())
    assert indexer is not None

    _closed, fact = memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    out = orchestrator.handle_tool_call(
        "memory.search", {"query": "kedi adı", "intent": "explore"}
    )
    assert out["trace"]["strategy"] == "vector_first"
    assert not any("fell back" in w for w in out["warnings"])
    assert [i["id"] for i in out["items"]] == [fact.id]


def test_vector_off_wins_over_injected_embedder(tmp_path, monkeypatch):
    """off is the user's decision: not even an injected embedder can override it."""
    _forbid_probe(monkeypatch)
    save_memory_settings(tmp_path, MemorySettings(vector="off"))
    _, _, indexer = build_orchestrator(tmp_path, embedder=HashingEmbedder())
    assert indexer is None


def test_backfill_indexes_preexisting_facts(tmp_path, monkeypatch):
    """Facts written before setup are backfilled into the empty index."""
    _forbid_probe(monkeypatch)
    pre = Memory.for_data_dir(tmp_path)
    _closed, fact = pre.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")

    _, orchestrator, indexer = build_orchestrator(tmp_path, embedder=HashingEmbedder())
    assert indexer is not None
    out = orchestrator.handle_tool_call(
        "memory.search", {"query": "kedi adı", "intent": "explore"}
    )
    assert out["trace"]["strategy"] == "vector_first"
    assert fact.id in [i["id"] for i in out["items"]]


def test_model_change_clears_and_reindexes(tmp_path, monkeypatch):
    """When the embedder MODEL changes the index is cleared and rebuilt. After a backend switch
    (ollama:bge-m3 → fastembed) old-dimension vectors must not linger and silently kill
    semantic recall — the bug the user hit."""
    from akana.memory.vector import VectorStore

    _forbid_probe(monkeypatch)
    pre = Memory.for_data_dir(tmp_path)
    pre.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")

    # 1) Index with the old model (dim=256)
    build_orchestrator(tmp_path, embedder=HashingEmbedder(dim=256))
    store = VectorStore.for_data_dir(tmp_path)
    assert store.distinct_models() == ["hashing:3gram-256"]
    assert store.count() == 1

    # 2) Rebuild with a DIFFERENT model (dim=128) → old one is cleared + reindex
    _, _, indexer = build_orchestrator(tmp_path, embedder=HashingEmbedder(dim=128))
    assert indexer is not None
    assert store.distinct_models() == ["hashing:3gram-128"]  # old model gone
    assert store.count() == 1  # fact re-embedded with the new model

    # 3) Again with the SAME model → no reindex needed (nothing stale)
    build_orchestrator(tmp_path, embedder=HashingEmbedder(dim=128))
    assert store.distinct_models() == ["hashing:3gram-128"]
    assert store.count() == 1


def test_boot_prunes_orphans_and_unmasks_backfill(tmp_path, monkeypatch):
    """U6: on boot, _wire_vector prunes orphan embeddings (deleted/invalidated facts) BEFORE
    counting, so an orphan can no longer inflate store.count() and make `indexed >= expected`
    skip the resume-backfill of a genuinely missing embedding."""
    from akana.memory.vector import VectorStore

    _forbid_probe(monkeypatch)
    pre = Memory.for_data_dir(tmp_path)
    _closed, valid = pre.assert_fact_direct(key="dil", value="Python", trust="user_statement")

    # Seed exactly one ORPHAN embedding for a fact that does not exist, and leave the ONE
    # valid fact WITHOUT an embedding. Old behavior: count()==1 >= expected==1 → backfill
    # skipped, so the orphan survives forever AND the valid fact is never embedded.
    store = VectorStore.for_data_dir(tmp_path)
    store.index_fact("ghost-fact-id", "ghost: text", HashingEmbedder())
    assert store.count() == 1

    _, orchestrator, indexer = build_orchestrator(tmp_path, embedder=HashingEmbedder())
    assert indexer is not None

    # Orphan is gone; the valid fact's embedding was backfilled (count-masking regression).
    assert store.distinct_models() == ["hashing:3gram-256"]
    hits = store.search(HashingEmbedder().embed(["dil"])[0], limit=5, model="hashing:3gram-256")
    hit_ids = {h[0] for h in hits}
    assert valid.id in hit_ids
    assert "ghost-fact-id" not in hit_ids
    assert store.count() == 1  # only the valid fact remains


# -- is_available ----------------------------------------------------------------------


def test_is_available_false_on_dead_port():
    assert is_available("http://127.0.0.1:1", timeout=0.5) is False


def test_is_available_true_against_local_http_server():
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib API name)
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # do not pollute the test output
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # the trailing / is stripped: <url>/api/tags is built uniformly
        assert is_available(f"http://127.0.0.1:{server.server_port}/", timeout=2.0) is True
    finally:
        server.shutdown()
        server.server_close()


# -- server glue: env passthrough -------------------------------------------------------


def test_memory_mcp_servers_passes_overrides_when_set(monkeypatch, tmp_path):
    from akana_server.orchestrator.memory_tools import memory_mcp_servers

    monkeypatch.delenv("AKANA_MEMORY_TOOLS", raising=False)
    monkeypatch.setenv("AKANA_MEMORY_ALLOW_DIRECT", "1")
    monkeypatch.setenv("AKANA_MEMORY_VECTOR", "off")
    env = memory_mcp_servers(SimpleNamespace(data_dir=tmp_path))["akana_memory"]["env"]
    assert env["AKANA_MEMORY_ALLOW_DIRECT"] == "1"
    assert env["AKANA_MEMORY_VECTOR"] == "off"


def test_memory_mcp_servers_omits_overrides_when_unset(monkeypatch, tmp_path):
    from akana_server.orchestrator.memory_tools import memory_mcp_servers

    monkeypatch.delenv("AKANA_MEMORY_TOOLS", raising=False)
    monkeypatch.setenv("AKANA_MEMORY_VECTOR", "   ")  # empty/whitespace = unset
    env = memory_mcp_servers(SimpleNamespace(data_dir=tmp_path))["akana_memory"]["env"]
    assert "AKANA_MEMORY_ALLOW_DIRECT" not in env
    assert "AKANA_MEMORY_VECTOR" not in env
