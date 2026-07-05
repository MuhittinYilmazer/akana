"""Gemini shared tool module (``gemini_tools``) — declaration schema + dispatch.

Hermetic: ``get_memory_core`` is patched with a fake ``Memory`` (the real memory.db is
not touched). ``memory_search`` → recall; ``save_memory`` → the orchestrator's
``memory.remember`` (policy=stage) path; unknown/empty arg/error → clean English text.
The suite runs with ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` → pure synchronous, no asyncio."""

from __future__ import annotations

from types import SimpleNamespace

from akana.memory.tools import tool_schemas
from akana_server.orchestrator import gemini_tools as gt
from akana_server.orchestrator.vault_tools import VAULT_TOOL_DECLS
from akana_server.vault_mcp.tools import vault_schemas


def _settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path)


# --- Fake memory + orchestrator --------------------------------------------


class _FakeBlock:
    def __init__(self, text: str, score: float = 1.0) -> None:
        self.text = text
        self.score = score


class _FakeRecall:
    def __init__(self, blocks) -> None:
        self.blocks = blocks


class _FakeOrch:
    """make_orchestrator twin — captures the handle_tool_call arguments."""

    def __init__(self, *, result=None, calls=None) -> None:
        self._result = result if result is not None else {"status": "staged", "staged_id": "s1"}
        self.calls = calls if calls is not None else []

    def handle_tool_call(self, name, args, *, conversation_id=None):
        self.calls.append((name, args, conversation_id))
        return self._result


class _FakeMemory:
    def __init__(self, *, blocks=None, orch=None) -> None:
        self._blocks = blocks or []
        self._orch = orch
        self.recall_calls: list = []

    def recall(self, query, *, conversation_id=None, limit=6, budget_tokens=1000):
        self.recall_calls.append((query, conversation_id, limit, budget_tokens))
        return _FakeRecall(self._blocks)

    def make_orchestrator(self, *, settings=None):
        # verify via settings that it was built with allow_direct=False (K30).
        self.orch_settings = settings
        return self._orch


# --- Declaration schema -----------------------------------------------------


def test_tool_decls_include_memory_search_and_save_memory() -> None:
    names = [d["name"] for d in gt.GEMINI_TOOL_DECLS]
    assert "memory_search" in names
    assert "save_memory" in names

    ms = next(d for d in gt.GEMINI_TOOL_DECLS if d["name"] == "memory_search")
    assert "query" in ms["parameters"]["properties"]
    assert ms["parameters"]["required"] == ["query"]

    sm = next(d for d in gt.GEMINI_TOOL_DECLS if d["name"] == "save_memory")
    assert "text" in sm["parameters"]["properties"]
    assert sm["parameters"]["required"] == ["text"]
    # description must convey the 'remember' intent
    assert "remember" in sm["description"].lower()

    # memory_forget: parity with the memory.forget MCP tool; the schema is derived
    # single-source from the MCP schema (target_id required + mode/new_value).
    assert "memory_forget" in names
    mf = next(d for d in gt.GEMINI_TOOL_DECLS if d["name"] == "memory_forget")
    assert mf["parameters"]["required"] == ["target_id"]
    assert set(mf["parameters"]["properties"]) >= {"target_id", "mode", "new_value", "reason"}


def test_native_memory_and_vault_names_match_mcp() -> None:
    """DRIFT GUARD: the native function-calling surface must stay at PARITY with the MCP
    (claude/cursor) surface. If a new memory or vault tool is added to the MCP registry
    but not mirrored on the native side (or the name mapping drifts), this test breaks."""
    native_names = {d["name"] for d in gt.GEMINI_TOOL_DECLS}

    # memory: native names (via the underscore mapping) == MCP memory tool names.
    mcp_memory = {s["name"] for s in tool_schemas()}
    assert set(gt.MEMORY_TOOL_NAME_MAP.values()) == mcp_memory, (
        "native memory surface diverged from the memory.* MCP tools"
    )
    assert set(gt.MEMORY_TOOL_NAME_MAP) <= native_names, (
        "a mapped memory tool is missing its native declaration"
    )

    # vault: native vault names == vault_schemas() names (already single-source-derived).
    mcp_vault = {s["name"] for s in vault_schemas()}
    native_vault = {d["name"] for d in VAULT_TOOL_DECLS}
    assert native_vault == mcp_vault, "native vault surface diverged from the vault MCP tools"
    assert native_vault <= native_names


# --- memory_search dispatch -------------------------------------------------


def test_dispatch_memory_search_routes_to_recall(tmp_path, monkeypatch) -> None:
    fake = _FakeMemory(blocks=[_FakeBlock("[user] kahve sever"), _FakeBlock("[assistant] not")])
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: fake)
    out = gt.dispatch_gemini_tool(_settings(tmp_path), "c", "memory_search", {"query": "kahve"})
    assert "kahve sever" in out
    assert fake.recall_calls and fake.recall_calls[0][0] == "kahve"


def test_dispatch_memory_search_empty_query(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: _FakeMemory())
    out = gt.dispatch_gemini_tool(_settings(tmp_path), "c", "memory_search", {"query": ""})
    assert "empty" in out.lower()


def test_dispatch_memory_search_empty_recall(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: _FakeMemory())
    out = gt.dispatch_gemini_tool(_settings(tmp_path), "c", "memory_search", {"query": "x"})
    assert "no matching records" in out.lower()


# --- save_memory dispatch (K30 staging) -------------------------------------


def test_dispatch_save_memory_stages_via_orchestrator(tmp_path, monkeypatch) -> None:
    """save_memory → calls the orchestrator's memory.remember (policy=stage, allow_direct=False)
    path → returns an English confirmation; does NOT do a direct durable write."""
    orch = _FakeOrch(result={"status": "staged", "staged_id": "s1"})
    fake = _FakeMemory(orch=orch)
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: fake)
    out = gt.dispatch_gemini_tool(
        _settings(tmp_path), "conv-7", "save_memory", {"text": "kullanıcı kahve sever"}
    )
    assert "inbox" in out.lower()
    # handle_tool_call was called with the correct tool + policy=stage + conv
    assert orch.calls, "handle_tool_call must be called"
    name, args, conv = orch.calls[0]
    assert name == "memory.remember"
    assert args["content"] == "kullanıcı kahve sever"
    assert args["policy"] == "stage"
    assert conv == "conv-7"
    # K30: the orchestrator was built with allow_direct=False
    assert fake.orch_settings is not None and fake.orch_settings.allow_direct is False


def test_dispatch_save_memory_empty_text(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: _FakeMemory())
    out = gt.dispatch_gemini_tool(_settings(tmp_path), "c", "save_memory", {"text": "   "})
    assert "empty" in out.lower()


def test_dispatch_save_memory_error_envelope_is_clean(tmp_path, monkeypatch) -> None:
    """If the orchestrator returns {'error':...} → clean English text (no raise, no raw envelope)."""
    orch = _FakeOrch(result={"error": {"code": "invalid_request", "message": "bad"}})
    fake = _FakeMemory(orch=orch)
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: fake)
    out = gt.dispatch_gemini_tool(_settings(tmp_path), "c", "save_memory", {"text": "x"})
    assert "couldn't save" in out.lower()
    assert "error" not in out  # the raw envelope does not leak


# --- memory_forget dispatch (C9: soft/reversible, not allow_direct-gated) ----


def test_dispatch_memory_forget_routes_through_handle_tool_call(tmp_path, monkeypatch) -> None:
    """memory_forget → orchestrator's memory.forget path (C9: NOT a new direct-delete path).
    The orchestrator is still built with allow_direct=False; forget works anyway (soft/ledger)."""
    orch = _FakeOrch(result={"status": "forgotten", "mode": "retract", "fact_id": "f1"})
    fake = _FakeMemory(orch=orch)
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: fake)
    out = gt.dispatch_gemini_tool(
        _settings(tmp_path), "conv-9", "memory_forget", {"target_id": "f1"}
    )
    assert "forgotten" in out.lower()
    assert orch.calls, "handle_tool_call must be called"
    name, args, conv = orch.calls[0]
    assert name == "memory.forget"
    assert args["target_id"] == "f1"
    assert conv == "conv-9"
    # C9: the orchestrator is still allow_direct=False — forget is not gated on it.
    assert fake.orch_settings is not None and fake.orch_settings.allow_direct is False


def test_dispatch_memory_forget_passes_supersede_fields(tmp_path, monkeypatch) -> None:
    """Partial-forget: mode/new_value/reason are forwarded to the memory.forget request."""
    orch = _FakeOrch(result={"status": "superseded", "old_id": "f1", "new_id": "f2"})
    fake = _FakeMemory(orch=orch)
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: fake)
    gt.dispatch_gemini_tool(
        _settings(tmp_path),
        "c",
        "memory_forget",
        {"target_id": "f1", "mode": "supersede", "new_value": "Ali, 30", "reason": "moved"},
    )
    _, args, _ = orch.calls[0]
    assert args["mode"] == "supersede"
    assert args["new_value"] == "Ali, 30"
    assert args["reason"] == "moved"


def test_dispatch_memory_forget_missing_target_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: _FakeMemory())
    out = gt.dispatch_gemini_tool(_settings(tmp_path), "c", "memory_forget", {"target_id": "  "})
    assert "target_id" in out.lower()


def test_dispatch_memory_forget_error_envelope_is_clean(tmp_path, monkeypatch) -> None:
    orch = _FakeOrch(result={"error": {"code": "not_found", "message": "no memory"}})
    fake = _FakeMemory(orch=orch)
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: fake)
    out = gt.dispatch_gemini_tool(_settings(tmp_path), "c", "memory_forget", {"target_id": "x"})
    assert "couldn't forget" in out.lower()
    assert "error" not in out


# --- defensive paths --------------------------------------------------------


def test_dispatch_unknown_tool(tmp_path) -> None:
    out = gt.dispatch_gemini_tool(_settings(tmp_path), "c", "nope", {})
    assert "Unknown" in out


def test_dispatch_defensive_on_exception(tmp_path, monkeypatch) -> None:
    def boom(dd):
        raise RuntimeError("db down")

    monkeypatch.setattr("akana_server.memory_core.get_memory_core", boom)
    # memory_search and save_memory: both convert the error to clean text
    out_s = gt.dispatch_gemini_tool(_settings(tmp_path), "c", "memory_search", {"query": "x"})
    out_w = gt.dispatch_gemini_tool(_settings(tmp_path), "c", "save_memory", {"text": "x"})
    assert "unavailable" in out_s
    assert "unavailable" in out_w


def test_dispatch_none_args_safe(tmp_path) -> None:
    # args=None → behaves like an empty dict (empty query/note message), does not blow up
    assert "empty" in gt.dispatch_gemini_tool(_settings(tmp_path), "c", "memory_search", None).lower()
    assert "empty" in gt.dispatch_gemini_tool(_settings(tmp_path), "c", "save_memory", None).lower()


# --- provider-neutral entry point (openai/ollama) reaches the same tools ----


def test_llm_dispatch_reaches_memory_forget_not_unknown(tmp_path, monkeypatch) -> None:
    """dispatch_llm_tool (the openai/ollama entry point) routes memory_forget to the real
    forget path — NOT 'Unknown tool'. Parity: the same change the gemini surface received."""
    from akana_server.orchestrator.llm_tools import dispatch_llm_tool

    orch = _FakeOrch(result={"status": "forgotten", "mode": "retract", "fact_id": "f1"})
    fake = _FakeMemory(orch=orch)
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: fake)
    out = dispatch_llm_tool(_settings(tmp_path), "c", "memory_forget", {"target_id": "f1"})
    assert "unknown" not in out.lower()
    assert "forgotten" in out.lower()
    assert orch.calls and orch.calls[0][0] == "memory.forget"


def test_function_response_shape(tmp_path) -> None:
    """_function_response: types.FunctionResponse if the SDK is present, else dict — read from both."""
    fc = SimpleNamespace(name="memory_search", args={"query": "x"}, id="t1")
    fr = gt._function_response(fc, "RESULT")
    name = fr["name"] if isinstance(fr, dict) else getattr(fr, "name")
    resp = fr["response"] if isinstance(fr, dict) else getattr(fr, "response")
    assert name == "memory_search"
    assert resp["result"] == "RESULT"
