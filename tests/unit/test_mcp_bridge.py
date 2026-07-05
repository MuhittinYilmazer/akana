"""In-process MCP bridge (mcp_bridge) — converts external MCP server tools into
OpenAI decls for native function-calling providers (ollama/gemini/openai)
and dispatches calls back. Hermetic: no real MCP subprocess — ``_open_session``
is monkeypatched and driven with a fake session (``asyncio.run`` for
``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` compatibility)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


from akana_server.orchestrator.mcp_bridge import (
    McpToolBridge,
    _stringify_result,
    _to_openai_decl,
    external_mcp_bridge,
)


# -- fake MCP primitives (same duck-type as the real ``mcp`` SDK types) --------


class _FakeTool:
    def __init__(self, name, description="", input_schema=None):
        self.name = name
        self.description = description
        self.inputSchema = input_schema


class _FakeSession:
    """``ClientSession`` duck-type: initialize/list_tools/call_tool (all async)."""

    def __init__(self, *, tools=None, result=None, raise_on_call=False):
        self._tools = tools or []
        self._result = result
        self._raise = raise_on_call
        self.calls: list = []

    async def initialize(self):
        return None

    async def list_tools(self):
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if self._raise:
            raise RuntimeError("oturum koptu")
        return self._result


def _result(*texts) -> SimpleNamespace:
    """``CallToolResult`` duck-type: a ``content`` list carrying text content blocks."""
    return SimpleNamespace(content=[SimpleNamespace(text=t) for t in texts])


def _patch_session(monkeypatch, by_cfg) -> None:
    """Replace ``_open_session`` with a function that returns a fake session per cfg.

    ``by_cfg(cfg)`` either returns a ``_FakeSession`` or raises (the server-fails-to-start
    scenario)."""

    async def fake_open(self, cfg):  # noqa: ANN001 — duck-typed test double
        return by_cfg(cfg)

    monkeypatch.setattr(McpToolBridge, "_open_session", fake_open)


# -- _to_openai_decl ----------------------------------------------------------------


def test_to_openai_decl_carries_schema() -> None:
    """MCP ``Tool`` → OpenAI tools entry: name/description/schema (inputSchema) carried as-is."""
    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    decl = _to_openai_decl("mcp__fs__read_file", _FakeTool("read_file", "Dosya oku", schema))
    assert decl == {
        "type": "function",
        "function": {
            "name": "mcp__fs__read_file",
            "description": "Dosya oku",
            "parameters": schema,
        },
    }


def test_to_openai_decl_defaults_when_schema_missing() -> None:
    """Missing/invalid ``inputSchema`` → falls back to an empty object schema (decl is always valid);
    if ``description`` is None it's normalized to an empty string."""
    decl = _to_openai_decl("mcp__x__t", _FakeTool("t", None, None))
    assert decl["function"]["parameters"] == {"type": "object", "properties": {}}
    assert decl["function"]["description"] == ""

    decl2 = _to_openai_decl("mcp__x__t", _FakeTool("t", "d", input_schema=["liste-değil-dict"]))
    assert decl2["function"]["parameters"] == {"type": "object", "properties": {}}


# -- _stringify_result --------------------------------------------------------------


def test_stringify_joins_text_blocks() -> None:
    assert _stringify_result(_result("satır1", "satır2")) == "satır1\nsatır2"


def test_stringify_ignores_non_text_blocks() -> None:
    """Non-text blocks (image/resource) are skipped; only text surfaces."""
    res = SimpleNamespace(content=[SimpleNamespace(data=b"\x00"), SimpleNamespace(text="metin")])
    assert _stringify_result(res) == "metin"


def test_stringify_empty_content_returns_placeholder() -> None:
    assert _stringify_result(SimpleNamespace(content=[])) == "(tool returned no text content)"
    assert _stringify_result(SimpleNamespace(content=None)) == "(tool returned no text content)"


def test_stringify_malformed_result_is_safe() -> None:
    """DEFENSIVE: if iterating over the content blows up (a malformed result), clean text is
    returned and the turn isn't broken."""
    assert _stringify_result(SimpleNamespace(content=5)) == "(could not read tool result)"


# -- no-op bridge (no yaml) ---------------------------------------------------------


def test_empty_bridge_is_noop() -> None:
    """Empty ``servers`` → zero-cost no-op: no decls, no tool is handled,
    an unknown dispatch returns clean text, AsyncExitStack is never set up."""

    async def run():
        async with McpToolBridge({}) as bridge:
            assert bridge.decls == []
            assert bridge.handles("mcp__x__y") is False
            assert bridge._stack is None  # no subprocess/stack was set up
            return await bridge.dispatch("mcp__x__y", {"a": 1})

    assert asyncio.run(run()) == "Unknown tool: mcp__x__y"


def test_none_servers_is_noop() -> None:
    """``servers=None`` is also a no-op (the bridge factory doesn't turn an empty loader
    result into None, but None is tolerated defensively too)."""

    async def run():
        async with McpToolBridge(None) as bridge:
            return bridge.decls

    assert asyncio.run(run()) == []


# -- tool registration + namespace --------------------------------------------------


def test_bridge_registers_namespaced_tools(monkeypatch) -> None:
    """A server's tools are registered into decls and routes with the ``mcp__<server>__<tool>``
    namespace; ``handles`` returns True only for a registered name."""
    session = _FakeSession(
        tools=[
            _FakeTool("read_file", "oku", {"type": "object", "properties": {}}),
            _FakeTool("write_file", "yaz", {"type": "object", "properties": {}}),
        ]
    )
    _patch_session(monkeypatch, lambda cfg: session)

    async def run():
        async with McpToolBridge({"fs": {"type": "stdio", "command": "x"}}) as bridge:
            return [d["function"]["name"] for d in bridge.decls], bridge.handles(
                "mcp__fs__read_file"
            ), bridge.handles("mcp__fs__missing")

    names, handled, unknown = asyncio.run(run())
    assert names == ["mcp__fs__read_file", "mcp__fs__write_file"]
    assert handled is True and unknown is False


def test_bridge_skips_unnamed_tools(monkeypatch) -> None:
    """An unnamed (empty ``name``) tool is skipped — a malformed server entry doesn't break the turn."""
    session = _FakeSession(tools=[_FakeTool("", "adsız"), _FakeTool("ok", "iyi", {})])
    _patch_session(monkeypatch, lambda cfg: session)

    async def run():
        async with McpToolBridge({"srv": {"type": "stdio", "command": "x"}}) as bridge:
            return [d["function"]["name"] for d in bridge.decls]

    assert asyncio.run(run()) == ["mcp__srv__ok"]


def test_failed_server_does_not_sink_others(monkeypatch) -> None:
    """If a server fails to start (``_open_session`` raises) it's skipped; healthy servers are
    still registered (defensive: a single broken server doesn't sink the turn/bridge)."""
    good = _FakeSession(tools=[_FakeTool("ping", "p", {})])

    def by_cfg(cfg):
        if cfg.get("command") == "bad":
            raise RuntimeError("başlatılamadı")
        return good

    _patch_session(monkeypatch, by_cfg)

    async def run():
        servers = {
            "bozuk": {"type": "stdio", "command": "bad"},
            "saglam": {"type": "stdio", "command": "good"},
        }
        async with McpToolBridge(servers) as bridge:
            return [d["function"]["name"] for d in bridge.decls], bridge.handles(
                "mcp__bozuk__ping"
            )

    names, bozuk_handled = asyncio.run(run())
    assert names == ["mcp__saglam__ping"]
    assert bozuk_handled is False


# -- dispatch -----------------------------------------------------------------------


def test_dispatch_routes_to_session(monkeypatch) -> None:
    """A bridged call goes to the correct session (the tool name is stripped of the namespace)
    and the result is converted to text."""
    session = _FakeSession(tools=[_FakeTool("read_file", "oku", {})], result=_result("DOSYA-İÇERİĞİ"))
    _patch_session(monkeypatch, lambda cfg: session)

    async def run():
        async with McpToolBridge({"fs": {"type": "stdio", "command": "x"}}) as bridge:
            return await bridge.dispatch("mcp__fs__read_file", {"path": "/notlar"})

    out = asyncio.run(run())
    assert out == "DOSYA-İÇERİĞİ"
    assert session.calls == [("read_file", {"path": "/notlar"})]  # namespace stripped


def test_dispatch_unknown_tool_is_clean(monkeypatch) -> None:
    session = _FakeSession(tools=[_FakeTool("ok", "i", {})])
    _patch_session(monkeypatch, lambda cfg: session)

    async def run():
        async with McpToolBridge({"s": {"type": "stdio", "command": "x"}}) as bridge:
            return await bridge.dispatch("mcp__s__yok", {})

    assert asyncio.run(run()) == "Unknown tool: mcp__s__yok"


def test_dispatch_error_is_defensive(monkeypatch) -> None:
    """DEFENSIVE: if the session call blows up (network/protocol/timeout) dispatch returns a clean
    message — the turn isn't broken, no exception leaks."""
    session = _FakeSession(tools=[_FakeTool("read_file", "oku", {})], raise_on_call=True)
    _patch_session(monkeypatch, lambda cfg: session)

    async def run():
        async with McpToolBridge({"fs": {"type": "stdio", "command": "x"}}) as bridge:
            return await bridge.dispatch("mcp__fs__read_file", {"path": "/x"})

    assert asyncio.run(run()) == "The tool is unavailable right now."


def test_dispatch_none_args_becomes_empty(monkeypatch) -> None:
    """``args=None`` → an empty dict is passed to the session (the model sometimes omits arguments)."""
    session = _FakeSession(tools=[_FakeTool("now", "saat", {})], result=_result("12:00"))
    _patch_session(monkeypatch, lambda cfg: session)

    async def run():
        async with McpToolBridge({"clock": {"type": "stdio", "command": "x"}}) as bridge:
            return await bridge.dispatch("mcp__clock__now", None)

    asyncio.run(run())
    assert session.calls == [("now", {})]


# -- external_mcp_bridge factory ----------------------------------------------------


def test_external_mcp_bridge_no_yaml_is_noop(tmp_path) -> None:
    """If ``mcp_servers.yaml`` is absent the factory yields a no-op bridge (no decls, no stack)."""
    bridge = external_mcp_bridge(SimpleNamespace(data_dir=tmp_path))

    async def run():
        async with bridge as b:
            return b.decls, b._stack

    decls, stack = asyncio.run(run())
    assert decls == [] and stack is None
