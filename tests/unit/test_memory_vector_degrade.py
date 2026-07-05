"""Vector degrade contract — an embed failure never crashes recall under any condition.

Scenarios: timeout, connection refused, model 404 (permanent disable +
``ollama pull`` suggestion), broken embed response, dimension mismatch, indexer
failure not breaking writes, cooldown (no retry storm), batched +
interruptible backfill, and auto-detect also checking model presence.
Their common assertion: the lexical path always answers.
"""

from __future__ import annotations

import http.server
import json
import logging
import threading

import pytest

import akana.memory.mcp as mcp_mod
from akana.memory import HashingEmbedder, Memory, VectorIndexer, VectorStore, enable_vector_recall
from akana.memory.embed import (
    EmbeddingError,
    ModelNotFoundError,
    OllamaEmbedder,
    has_model,
)
from akana.memory.mcp import build_orchestrator
from akana.memory.vector_recall import VectorHealth, make_rrf_strategy, make_vector_strategy


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AKANA_MEMORY_ALLOW_DIRECT", raising=False)
    monkeypatch.delenv("AKANA_MEMORY_VECTOR", raising=False)


class _FailingEmbedder:
    """Raises the given exception on every embed call; counts the calls."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake:fail"

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        raise self._exc


class _EmptyEmbedder:
    """Mimics a broken backend: returns no vectors at all (downstream IndexError)."""

    @property
    def name(self) -> str:
        return "fake:empty"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return []


class _DimEmbedder:
    """Fixed model name, selectable dimension — for the dim-mismatch scenario."""

    def __init__(self, dim: int) -> None:
        self._base = HashingEmbedder(dim=dim)

    @property
    def name(self) -> str:
        return "fake:dim"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._base.embed(texts)


class _CountingEmbedder(HashingEmbedder):
    """Records batch sizes (proof that backfill really is batched)."""

    def __init__(self) -> None:
        super().__init__()
        self.batch_sizes: list[int] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        return super().embed(texts)


class _FailOnCallEmbedder(HashingEmbedder):
    """Blows up on the Nth embed call — partial backfill scenario."""

    def __init__(self, fail_on_call: int) -> None:
        super().__init__()
        self._fail_on = fail_on_call
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls >= self._fail_on:
            raise EmbeddingError("backend mid-flight düştü")
        return super().embed(texts)


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _memory_with_fact(tmp_path):
    memory = Memory.for_data_dir(tmp_path)
    _closed, fact = memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    return memory, fact


def _search(orch, query="kedi adı", intent="explore", **kw):
    return orch.handle_tool_call("memory.search", {"query": query, "intent": intent}, **kw)


# -- strategy degrade: failure → lexical answer -----------------------------------------


def test_embed_timeout_degrades_to_lexical(tmp_path):
    """embed failure inside vector_first: lexical result, not an error envelope."""
    memory, fact = _memory_with_fact(tmp_path)
    orch = memory.make_orchestrator()
    embedder = _FailingEmbedder(EmbeddingError("timed out"))
    enable_vector_recall(memory, orch, embedder)

    out = _search(orch)
    assert "error" not in out
    assert [i["id"] for i in out["items"]] == [fact.id]  # the lexical path answered
    assert embedder.calls == 1


def test_connection_refused_real_embedder_degrades(tmp_path):
    """Real OllamaEmbedder + dead port: connection refused also degrades to lexical."""
    memory, fact = _memory_with_fact(tmp_path)
    orch = memory.make_orchestrator()
    embedder = OllamaEmbedder(url="http://127.0.0.1:1/api/embed", timeout_s=0.3)
    enable_vector_recall(memory, orch, embedder)

    out = _search(orch)
    assert "error" not in out
    assert fact.id in [i["id"] for i in out["items"]]


def test_broken_embed_payload_degrades(tmp_path):
    """An empty/incomplete embed response (IndexError family) also doesn't break the contract."""
    memory, fact = _memory_with_fact(tmp_path)
    orch = memory.make_orchestrator()
    enable_vector_recall(memory, orch, _EmptyEmbedder())

    out = _search(orch)
    assert "error" not in out
    assert fact.id in [i["id"] for i in out["items"]]


def test_rrf_embed_failure_returns_keyword_results(tmp_path):
    """If embed fails on the rrf path, fusion returns the keyword ranking as-is."""
    memory, fact = _memory_with_fact(tmp_path)
    memory.remember_turn(role="user", conversation_id="c1", text="kedi maması almam lazım")
    orch = memory.make_orchestrator()
    enable_vector_recall(memory, orch, _FailingEmbedder(EmbeddingError("boom")))

    out = orch.handle_tool_call(
        "memory.search", {"query": "kedi", "intent": "timeline"}, conversation_id="c1"
    )
    assert "error" not in out
    assert out["trace"]["strategy"] == "rrf"
    assert fact.id in [i["id"] for i in out["items"]]


def test_dim_mismatch_rows_skipped_no_crash(tmp_path):
    """Rows indexed at the old dimension: no crash; rrf answers with keyword."""
    memory, fact = _memory_with_fact(tmp_path)
    store = VectorStore.for_data_dir(tmp_path)
    store.index_fact(fact.id, "kedi adı: Pamuk", _DimEmbedder(dim=64))  # old dimension

    vector_first = make_vector_strategy(memory, store, _DimEmbedder(dim=256))
    assert vector_first(query="kedi adı").blocks == []  # row skipped, no exception

    rrf = make_rrf_strategy(memory, store, _DimEmbedder(dim=256))
    assert fact.id in {b.source_ids[0] for b in rrf(query="kedi adı").blocks}


# -- model 404: permanent disable + single-line suggestion ----------------------------


def test_model_404_permanent_disable_no_retry(tmp_path, caplog):
    clock = _FakeClock()
    health = VectorHealth(cooldown_s=60.0, clock=clock)
    memory, fact = _memory_with_fact(tmp_path)
    orch = memory.make_orchestrator()
    embedder = _FailingEmbedder(
        ModelNotFoundError("Ollama embed model 'bge-m3' not installed (404); run: ollama pull bge-m3")
    )
    enable_vector_recall(memory, orch, embedder, health=health)

    with caplog.at_level(logging.WARNING, logger="akana.memory.vector_recall"):
        out1 = _search(orch)
        clock.t += 10_000.0  # cooldown long since passed — the permanent flag must still hold
        out2 = _search(orch)

    assert embedder.calls == 1  # single attempt: no retry storm
    # cooldown has long since passed (line above), so a still-inactive breaker can
    # only be the permanent flag holding.
    assert not health.active()
    assert sum("ollama pull bge-m3" in r.getMessage() for r in caplog.records) == 1
    for out in (out1, out2):
        assert "error" not in out
        assert fact.id in [i["id"] for i in out["items"]]


# -- cooldown: retry after a reasonable interval ----------------------------------------


def test_transient_failure_cooldown_then_retry(tmp_path):
    clock = _FakeClock()
    health = VectorHealth(cooldown_s=120.0, clock=clock)
    memory, fact = _memory_with_fact(tmp_path)
    orch = memory.make_orchestrator()
    embedder = _FailingEmbedder(EmbeddingError("geçici kesinti"))
    enable_vector_recall(memory, orch, embedder, health=health)

    out1 = _search(orch)
    assert embedder.calls == 1
    out2 = _search(orch)  # within cooldown: embed is not attempted at all
    assert embedder.calls == 1
    clock.t += 121.0
    out3 = _search(orch)  # window passed: attempted once more
    assert embedder.calls == 2
    for out in (out1, out2, out3):
        assert "error" not in out
        assert fact.id in [i["id"] for i in out["items"]]


# -- indexer: an embed failure never breaks writes --------------------------------------


def test_indexer_failure_never_breaks_writes(tmp_path):
    memory = Memory.for_data_dir(tmp_path)
    orch = memory.make_orchestrator()
    store = VectorStore.for_data_dir(tmp_path)
    embedder = _FailingEmbedder(EmbeddingError("embed yok"))
    enable_vector_recall(memory, orch, embedder, store=store)

    _closed, f1 = memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    _closed, f2 = memory.assert_fact_direct(key="favori dil", value="Python", trust="user_statement")
    assert memory.get_fact(f1.id) is not None and memory.get_fact(f2.id) is not None
    assert store.count() == 0  # no vectors but the facts are perfectly intact
    assert embedder.calls == 1  # cooldown on the second write: embed wasn't even attempted

    out = _search(orch, query="favori dil")
    assert "error" not in out
    assert f2.id in [i["id"] for i in out["items"]]


# -- backfill: batched, interruptible, partial on failure -------------------------------


def test_reindex_runs_in_batches(tmp_path):
    memory = Memory.for_data_dir(tmp_path)
    for i in range(5):
        memory.assert_fact_direct(key=f"k{i}", value=f"v{i}", trust="user_statement")
    store = VectorStore.for_data_dir(tmp_path)
    embedder = _CountingEmbedder()
    indexer = VectorIndexer(store, embedder)

    assert indexer.reindex(memory, batch_size=2) == 5
    assert embedder.batch_sizes == [2, 2, 1]  # one call per batch, not per fact
    assert store.count() == 5


def test_reindex_interruptible_via_should_continue(tmp_path):
    memory = Memory.for_data_dir(tmp_path)
    for i in range(5):
        memory.assert_fact_direct(key=f"k{i}", value=f"v{i}", trust="user_statement")
    store = VectorStore(tmp_path / "interrupt.db")
    indexer = VectorIndexer(store, HashingEmbedder())

    flags = iter([True, False])  # stop after the first batch
    n = indexer.reindex(memory, batch_size=2, should_continue=lambda: next(flags))
    assert n == 2
    assert store.count() == 2  # the indexed part remains, the rest is left without raising


def test_reindex_embed_failure_keeps_partial_and_does_not_raise(tmp_path):
    memory = Memory.for_data_dir(tmp_path)
    for i in range(5):
        memory.assert_fact_direct(key=f"k{i}", value=f"v{i}", trust="user_statement")
    store = VectorStore(tmp_path / "partial.db")
    indexer = VectorIndexer(store, _FailOnCallEmbedder(fail_on_call=2))

    n = indexer.reindex(memory, batch_size=2)  # the 2nd batch blows up
    assert n == 2
    assert store.count() == 2


# -- auto-detect: daemon is up but the model isn't pulled -------------------------------


def test_build_orchestrator_skips_vector_when_model_missing(tmp_path, monkeypatch, caplog):
    """ollama backend + probe green + no bge-m3: no indexer, 'ollama pull' suggestion."""
    monkeypatch.setenv("AKANA_MEMORY_EMBED_BACKEND", "ollama")
    monkeypatch.setattr(mcp_mod, "is_available", lambda *a, **k: True)
    monkeypatch.setattr(mcp_mod, "has_model", lambda *a, **k: False)
    with caplog.at_level(logging.WARNING, logger="akana.memory.mcp"):
        memory, orchestrator, indexer = build_orchestrator(tmp_path)
    assert indexer is None
    assert any("ollama pull" in r.getMessage() for r in caplog.records)

    memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    out = orchestrator.handle_tool_call("memory.search", {"query": "kedi", "intent": "explore"})
    assert "error" not in out
    assert out["trace"]["strategy"] == "fts_first"


def test_model_missing_logs_error_when_vector_on(tmp_path, monkeypatch, caplog):
    from akana.memory.settings import MemorySettings, save_memory_settings

    monkeypatch.setenv("AKANA_MEMORY_EMBED_BACKEND", "ollama")
    monkeypatch.setattr(mcp_mod, "is_available", lambda *a, **k: True)
    monkeypatch.setattr(mcp_mod, "has_model", lambda *a, **k: False)
    save_memory_settings(tmp_path, MemorySettings(vector="on"))
    with caplog.at_level(logging.ERROR, logger="akana.memory.mcp"):
        _, _, indexer = build_orchestrator(tmp_path)
    assert indexer is None
    assert any(
        r.levelno == logging.ERROR and "ollama pull" in r.getMessage() for r in caplog.records
    )


# -- has_model + OllamaEmbedder 404 (local fake daemon) ---------------------------------


def _serve(handler_cls):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_has_model_matches_tags(tmp_path):
    class _Tags(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib API name)
            body = json.dumps(
                {"models": [{"name": "bge-m3:latest"}, {"name": "qwen2.5:7b"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = _serve(_Tags)
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        assert has_model(url, "bge-m3", timeout=2.0) is True  # a tagless name matches any tag
        assert has_model(url, "bge-m3:latest", timeout=2.0) is True
        assert has_model(url, "bge-m3:567m", timeout=2.0) is False  # a tagged name matches exactly
        assert has_model(url, "nomic-embed", timeout=2.0) is False
    finally:
        server.shutdown()
        server.server_close()


def test_has_model_false_on_dead_port():
    assert has_model("http://127.0.0.1:1", "bge-m3", timeout=0.5) is False


def test_ollama_embedder_404_raises_model_not_found():
    class _NotFound(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (stdlib API name)
            body = b'{"error":"model \'bge-m3\' not found"}'
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = _serve(_NotFound)
    try:
        embedder = OllamaEmbedder(
            url=f"http://127.0.0.1:{server.server_port}/api/embed", timeout_s=2.0
        )
        with pytest.raises(ModelNotFoundError, match="ollama pull bge-m3"):
            embedder.embed(["kedi"])
    finally:
        server.shutdown()
        server.server_close()
