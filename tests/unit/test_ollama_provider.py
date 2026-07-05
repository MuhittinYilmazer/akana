"""Ollama provider dispatch — wires OllamaDriver to Akana wire events + llm_dispatch
routing. Hermetic: no real Ollama, driven via ``httpx.MockTransport``
(``asyncio.run`` for ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` compatibility)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from akana.driver.ollama import OllamaDriver
from akana_server.orchestrator import llm_dispatch, ollama_provider
from akana_server.orchestrator.chat_persona import CHAT_SYSTEM_PREFIX
from akana_server.orchestrator.llm_dispatch import LLMCallError


def _settings(tmp_path):
    return SimpleNamespace(
        data_dir=tmp_path, ollama_url="http://ollama.test", ollama_model="llama3.1"
    )


def _ndjson(*objs) -> str:
    return "".join(json.dumps(o) + "\n" for o in objs)


def _mock_driver(monkeypatch, handler) -> None:
    drv = OllamaDriver(
        url="http://ollama.test", model="llama3.1", transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(ollama_provider, "_driver", lambda settings, model: drv)


def test_stream_user_chat_yields_wire_events(tmp_path, monkeypatch) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_ndjson(
                {"message": {"role": "assistant", "content": "Mer"}, "done": False},
                {"message": {"role": "assistant", "content": "haba"}, "done": False},
                {"done": True, "prompt_eval_count": 3, "eval_count": 5},
            ),
        )

    _mock_driver(monkeypatch, handler)

    async def run():
        return [
            ev async for ev in ollama_provider.stream_user_chat(_settings(tmp_path), "selam")
        ]

    events = asyncio.run(run())
    assert "".join(e["delta"] for e in events if "delta" in e) == "Merhaba"
    done = [e for e in events if e.get("done")]
    assert done and done[-1]["usage"]["completion_tokens"] == 5
    assert done[-1]["usage"]["tool_calls"] == []  # no tools in ollama


def test_complete_chat_returns_text_status_usage(tmp_path, monkeypatch) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_ndjson(
                {"message": {"content": "tam yanıt"}, "done": False},
                {"done": True, "prompt_eval_count": 2, "eval_count": 3},
            ),
        )

    _mock_driver(monkeypatch, handler)
    text, status, usage = asyncio.run(
        ollama_provider.complete_chat(_settings(tmp_path), "selam")
    )
    assert text == "tam yanıt"
    assert status == "finished"
    assert usage["tool_calls"] == []


def test_system_prompt_flows_as_system_message(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, text=_ndjson({"done": True}))

    _mock_driver(monkeypatch, handler)
    asyncio.run(
        _drain(ollama_provider.stream_user_chat(
            _settings(tmp_path), "soru", system_prompt="Sen Akana'sın", history=[{"role": "assistant", "content": "önceki"}]
        ))
    )
    roles = [m["role"] for m in captured["body"]["messages"]]
    assert roles == ["system", "assistant", "user"]
    assert captured["body"]["messages"][0]["content"] == "Sen Akana'sın"


def test_default_persona_falls_back_to_chat_system_prefix(tmp_path, monkeypatch) -> None:
    """system_prompt=None (default persona) → the model STILL receives CHAT_SYSTEM_PREFIX.

    Regression: without the fallback ollama got NO system prompt, so the tool-use
    directives never reached the model and FC tools (memory_search/save_memory/vault_*)
    silently never fired — the reported "ollama can't reach memory/vault" symptom."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, text=_ndjson({"done": True}))

    _mock_driver(monkeypatch, handler)
    asyncio.run(_drain(ollama_provider.stream_user_chat(_settings(tmp_path), "soru")))
    msgs = captured["body"]["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].strip() == CHAT_SYSTEM_PREFIX.strip()


def test_driver_error_maps_to_cursor_call_error(tmp_path, monkeypatch) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("ollama down")

    _mock_driver(monkeypatch, handler)
    with pytest.raises(LLMCallError) as ei:
        asyncio.run(_drain(ollama_provider.stream_user_chat(_settings(tmp_path), "x")))
    assert ei.value.status_code == 503  # DriverUnavailable → 503


def test_cursor_client_routes_to_ollama(tmp_path, monkeypatch) -> None:
    """provider=ollama → llm_dispatch.stream_user_chat delegates to ollama_provider."""
    monkeypatch.setattr(llm_dispatch, "_active_provider", lambda s: "ollama")

    async def fake(settings, user_text, **_kw):
        yield {"delta": "ok", "done": False}
        yield {"done": True, "usage": {}}

    monkeypatch.setattr(ollama_provider, "stream_user_chat", fake)

    async def run():
        return [
            ev async for ev in llm_dispatch.stream_user_chat(_settings(tmp_path), "hi")
        ]

    events = asyncio.run(run())
    assert any(e.get("delta") == "ok" for e in events)


async def _drain(agen):
    async for _ in agen:
        pass


def test_resolve_ollama_model_ignores_cursor_tag(tmp_path) -> None:
    """chat_producer passes the cursor tag (_active_cursor_model) to ALL providers →
    'default'/'composer-2' MUST NOT LEAK to ollama (root of the 404 'model default not found');
    it must fall back to the persisted ollama_model. An explicit ollama tag ('name:tag') is honored."""
    s = SimpleNamespace(
        data_dir=tmp_path,
        ollama_url="http://ollama.test",
        ollama_model="llama3.1",
        cursor_model="composer-2",
    )
    # Foreign/placeholder tag → fall back to persist/env (llama3.1), does NOT LEAK to ollama
    assert ollama_provider._resolve_ollama_model(s, "default") == "llama3.1"
    assert ollama_provider._resolve_ollama_model(s, "composer-2") == "llama3.1"
    assert ollama_provider._resolve_ollama_model(s, "claude-sonnet-4-6") == "llama3.1"
    assert ollama_provider._resolve_ollama_model(s, "") == "llama3.1"
    # Explicit ollama tag ('name:tag') is used as-is
    assert ollama_provider._resolve_ollama_model(s, "qwen2.5:7b-instruct") == "qwen2.5:7b-instruct"


def test_default_tag_does_not_reach_ollama_request_body(tmp_path, monkeypatch) -> None:
    """End-to-end regression: stream_user_chat(model='default') → in the ollama HTTP body
    the model MUST NOT be 'default', it must be the resolved one (llama3.1). Bug: a RAW
    model override was passed to driver.stream_chat → it negated the resolve and produced a
    'model default not found' 404. (The isolated _resolve test missed it; end-to-end caught it.)"""
    captured = {}

    def handler(req):
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, text=_ndjson({"message": {"content": "ok"}}, {"done": True}))

    real_driver = ollama_provider._driver

    def driver_with_transport(settings, model):
        d = real_driver(settings, model)  # REAL resolve (self._model = resolved)
        d._transport = httpx.MockTransport(handler)
        return d

    monkeypatch.setattr(ollama_provider, "_driver", driver_with_transport)
    s = SimpleNamespace(
        data_dir=tmp_path,
        ollama_url="http://ollama.test",
        ollama_model="llama3.1",
        cursor_model="composer-2",
    )

    async def run():
        async for _ in ollama_provider.stream_user_chat(s, "selam", model="default"):
            pass

    asyncio.run(run())
    assert captured["body"]["model"] == "llama3.1"  # NOT 'default' → override bug regression


def _timeout_settings(tmp_path):
    return SimpleNamespace(
        data_dir=tmp_path,
        ollama_url="http://ollama.test",
        ollama_model="llama3.1",
        cursor_model="composer-2",
    )


def test_driver_timeout_defaults_to_no_ceiling(tmp_path, monkeypatch) -> None:
    """Default (env unset, no store): the driver is built with the '0 = no timeout'
    sentinel so a slow/cold Ollama model is never cut off."""
    monkeypatch.delenv("AKANA_OLLAMA_TIMEOUT", raising=False)
    drv = ollama_provider._driver(_timeout_settings(tmp_path), None)
    assert drv._timeout == 0.0


def test_driver_timeout_reads_runtime_ollama_timeout(tmp_path, monkeypatch) -> None:
    """A configured AKANA_OLLAMA_TIMEOUT flows into the driver (resolved live per call)."""
    monkeypatch.setenv("AKANA_OLLAMA_TIMEOUT", "45")
    drv = ollama_provider._driver(_timeout_settings(tmp_path), None)
    assert drv._timeout == 45.0


# -- Native function-calling + thinking parity (gemini/openai symmetry) ---------


def _rounds_handler(rounds, captured_bodies):
    """Handler that returns the next NDJSON response on each POST call (multi-round FC).

    ``rounds`` is a list of obj-lists per round; each ``/api/chat`` request consumes the
    next one (stays on the last round once exhausted). ``captured_bodies`` accumulates the
    body of every request (for tools/think/messages verification)."""
    state = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(req.content))
        idx = min(state["i"], len(rounds) - 1)
        state["i"] += 1
        return httpx.Response(200, text=_ndjson(*rounds[idx]))

    return handler


def test_tools_always_sent_in_request_body(tmp_path, monkeypatch) -> None:
    """Native FC: every ``/api/chat`` request carries OpenAI-style ``tools`` — the FULL
    native surface (memory_search + save_memory + memory_forget + the 7 vault tools),
    at parity with the claude/cursor MCP surface, matching gemini's 'tools always present'."""
    bodies: list = []
    handler = _rounds_handler(
        [[{"message": {"content": "selam"}}, {"done": True}]], bodies
    )
    _mock_driver(monkeypatch, handler)
    asyncio.run(_drain(ollama_provider.stream_user_chat(_settings(tmp_path), "merhaba")))
    tools = bodies[0]["tools"]
    names = [t["function"]["name"] for t in tools]
    assert "memory_search" in names
    assert "save_memory" in names
    assert "memory_forget" in names
    for vt in (
        "vault_list",
        "vault_get",
        "vault_get_credential",
        "vault_set",
        "vault_set_credential",
        "vault_delete",
        "vault_delete_credential",
    ):
        assert vt in names, f"{vt} missing from the request body tools"
    assert all(t["type"] == "function" for t in tools)


def test_think_sent_when_thinking_mode_set(tmp_path, monkeypatch) -> None:
    """thinking_mode set → ``think: true`` in the body; empty/None → ``think`` NOT present at all.
    Ollama ``think`` is BOOLEAN (unlike gemini's graded thinking_level)."""
    bodies: list = []
    handler = _rounds_handler([[{"message": {"content": "ok"}}, {"done": True}]], bodies)
    _mock_driver(monkeypatch, handler)
    asyncio.run(
        _drain(ollama_provider.stream_user_chat(_settings(tmp_path), "x", thinking_mode="normal"))
    )
    assert bodies[0]["think"] is True

    # no thinking_mode → think is not sent
    bodies2: list = []
    handler2 = _rounds_handler([[{"message": {"content": "ok"}}, {"done": True}]], bodies2)
    _mock_driver(monkeypatch, handler2)
    asyncio.run(_drain(ollama_provider.stream_user_chat(_settings(tmp_path), "x")))
    assert "think" not in bodies2[0]

    # whitespace-only thinking_mode → think is not sent (safe)
    bodies3: list = []
    handler3 = _rounds_handler([[{"message": {"content": "ok"}}, {"done": True}]], bodies3)
    _mock_driver(monkeypatch, handler3)
    asyncio.run(_drain(ollama_provider.stream_user_chat(_settings(tmp_path), "x", thinking_mode="  ")))
    assert "think" not in bodies3[0]


def test_thinking_deltas_surface_as_thinking_events(tmp_path, monkeypatch) -> None:
    """``message.thinking`` (separate from content) → separate ``thinking`` wire events
    (gemini/claude shape: {phase:delta,text} + {phase:completed}); does NOT MIX into the answer."""
    handler = _rounds_handler(
        [
            [
                {"message": {"thinking": "düşün", "content": ""}, "done": False},
                {"message": {"thinking": "üyorum", "content": ""}, "done": False},
                {"message": {"content": "Cevap"}, "done": False},
                {"done": True, "prompt_eval_count": 1, "eval_count": 2},
            ]
        ],
        [],
    )
    _mock_driver(monkeypatch, handler)

    async def run():
        return [
            ev
            async for ev in ollama_provider.stream_user_chat(
                _settings(tmp_path), "soru", thinking_mode="derin"
            )
        ]

    events = asyncio.run(run())
    thinking = [e["thinking"] for e in events if "thinking" in e]
    deltas = [t["text"] for t in thinking if t.get("phase") == "delta"]
    assert "".join(deltas) == "düşünüyorum"
    assert any(t.get("phase") == "completed" for t in thinking)
    # thinking text does NOT MIX into the answer → only the content delta appears
    assert "".join(e["delta"] for e in events if "delta" in e) == "Cevap"


def test_stream_function_call_loop_invokes_dispatch(tmp_path, monkeypatch) -> None:
    """stream: round 1 tool_calls (emits no text) → dispatch_llm_tool → round 2 streams
    text. Intermediate-round tool text does not mix into the final answer; only final
    deltas are emitted. tool_call start/end events are emitted; done.usage.tool_calls is populated."""
    dispatched: list = []
    monkeypatch.setattr(
        ollama_provider,
        "dispatch_llm_tool",
        lambda s, c, n, a: dispatched.append((n, a)) or "ARAÇ-SONUCU",
    )
    bodies: list = []
    handler = _rounds_handler(
        [
            # round 1: model calls save_memory (no content)
            [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "save_memory", "arguments": {"text": "kahve sever"}}}
                        ],
                    },
                    "done": False,
                },
                {"done": True, "prompt_eval_count": 1, "eval_count": 1},
            ],
            # round 2: model returns the final text
            [
                {"message": {"content": "Not"}, "done": False},
                {"message": {"content": " eklendi."}, "done": False},
                {"done": True, "prompt_eval_count": 2, "eval_count": 3},
            ],
        ],
        bodies,
    )
    _mock_driver(monkeypatch, handler)

    async def run():
        return [
            ev
            async for ev in ollama_provider.stream_user_chat(
                _settings(tmp_path), "bunu hatırla", conversation_id="c1"
            )
        ]

    events = asyncio.run(run())
    assert dispatched == [("save_memory", {"text": "kahve sever"})]
    assert "".join(e["delta"] for e in events if "delta" in e) == "Not eklendi."
    assert len(bodies) == 2  # tool round + final round
    # the 2nd request's messages must carry the assistant tool_calls round + the tool result
    roles = [m["role"] for m in bodies[1]["messages"]]
    assert "assistant" in roles and "tool" in roles
    tool_msg = next(m for m in bodies[1]["messages"] if m["role"] == "tool")
    assert tool_msg["content"] == "ARAÇ-SONUCU"
    # tool_call start/end events + done.usage.tool_calls
    tc = [e["tool_call"] for e in events if "tool_call" in e]
    phases = [c["phase"] for c in tc]
    assert "start" in phases and "end" in phases
    done = [e for e in events if e.get("done")]
    assert done and len(done[-1]["usage"]["tool_calls"]) == 2  # start + end record
    # BUGFIX: usage tokens are SUMMED across ALL rounds (not overwritten) →
    # the intermediate tool round's tokens are also counted. round1 eval_count=1 + round2 eval_count=3 = 4
    # (old behavior counted only the final round = 3, dropping the intermediate round).
    assert done[-1]["usage"]["completion_tokens"] == 4  # 1 (tool round) + 3 (final round)
    assert done[-1]["usage"]["prompt_tokens"] == 3  # 1 (tool round) + 2 (final round)


def test_complete_chat_function_call_loop(tmp_path, monkeypatch) -> None:
    """complete_chat: 1st response tool_calls → dispatch → append to messages → 2nd response
    returns the final text. Dispatch must be called; final text + usage must be correct."""
    dispatched: list = []
    monkeypatch.setattr(
        ollama_provider,
        "dispatch_llm_tool",
        lambda s, c, n, a: dispatched.append((n, a)) or "HAFIZA-SONUCU",
    )
    bodies: list = []
    handler = _rounds_handler(
        [
            [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "memory_search", "arguments": {"query": "kahve"}}}
                        ],
                    },
                    "done": False,
                },
                {"done": True},
            ],
            [
                {"message": {"content": "Kahveyi seversin."}, "done": False},
                {"done": True, "prompt_eval_count": 4, "eval_count": 6},
            ],
        ],
        bodies,
    )
    _mock_driver(monkeypatch, handler)
    text, status, usage = asyncio.run(
        ollama_provider.complete_chat(_settings(tmp_path), "kahveyi sever miyim?")
    )
    assert text == "Kahveyi seversin."
    assert status == "finished"
    # BUGFIX: tokens are SUMMED across rounds. round1's done has no counter (0) +
    # round2 eval_count=6 = 6; prompt_eval_count 0 + 4 = 4. (Old behavior kept only
    # the final round; here the intermediate round is 0 so the total is still 6, but
    # it is verified via the summing logic.)
    assert usage["completion_tokens"] == 6  # 0 (tool round) + 6 (final round)
    assert usage["prompt_tokens"] == 4  # 0 (tool round) + 4 (final round)
    assert dispatched == [("memory_search", {"query": "kahve"})]
    assert len(usage["tool_calls"]) == 2  # start + end
    assert len(bodies) == 2


def test_tool_call_arguments_json_string_parsed(tmp_path, monkeypatch) -> None:
    """DEFENSIVE: if ``arguments`` arrives as STR JSON (OpenAI classic) it is parsed;
    malformed JSON → empty args (the tool returns a safe result, the round is not broken)."""
    dispatched: list = []
    monkeypatch.setattr(
        ollama_provider,
        "dispatch_llm_tool",
        lambda s, c, n, a: dispatched.append((n, a)) or "OK",
    )
    handler = _rounds_handler(
        [
            [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "memory_search", "arguments": '{"query": "x"}'}}
                        ],
                    },
                    "done": False,
                },
                {"done": True},
            ],
            [{"message": {"content": "bitti"}, "done": False}, {"done": True}],
        ],
        [],
    )
    _mock_driver(monkeypatch, handler)
    text, _, _ = asyncio.run(ollama_provider.complete_chat(_settings(tmp_path), "ara"))
    assert text == "bitti"
    assert dispatched == [("memory_search", {"query": "x"})]


def test_tool_loop_caps_at_max_rounds(tmp_path, monkeypatch) -> None:
    """Even if the model keeps calling tools every round, the loop stops at
    ``_MAX_TOOL_ROUNDS`` (infinite-loop guard). UNIFIED round-limit behavior
    (providers:smell:3): after the cap one FINAL tool-less round is made (its request
    carries no ``tools``) so the model answers with real text instead of a truncation
    notice → _MAX_TOOL_ROUNDS + 1 requests total."""
    monkeypatch.setattr(ollama_provider, "dispatch_llm_tool", lambda s, c, n, a: "yine")
    bodies: list = []
    # EVERY tool-carrying round returns tool_calls; the final tool-less request (no
    # ``tools`` in the body) returns plain text.
    always_calls = [
        {
            "message": {
                "content": "",
                "tool_calls": [{"function": {"name": "memory_search", "arguments": {"query": "q"}}}],
            },
            "done": False,
        },
        {"done": True},
    ]
    final_text = [{"message": {"content": "son cevap"}, "done": False}, {"done": True}]

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        bodies.append(body)
        rounds = always_calls if "tools" in body else final_text
        return httpx.Response(200, text=_ndjson(*rounds))

    _mock_driver(monkeypatch, handler)
    events = asyncio.run(
        _collect(ollama_provider.stream_user_chat(_settings(tmp_path), "loop"))
    )
    # _MAX_TOOL_ROUNDS tool rounds + 1 tool-less final round.
    assert len(bodies) == ollama_provider._MAX_TOOL_ROUNDS + 1
    assert not bodies[-1].get("tools")  # the final round is TOOL-LESS
    assert any(e.get("done") for e in events)
    assert "".join(e["delta"] for e in events if "delta" in e) == "son cevap"


def test_unsupported_tools_retries_without_them(tmp_path, monkeypatch) -> None:
    """Model that does NOT SUPPORT tools: the first request (with tools) returns 400 'does not support tools' →
    the provider RETRIES the same round WITHOUT tools → it finishes with text without breaking the round (graceful degradation).
    On a supporting model there is NO extra request (retry only on 400)."""
    bodies: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        bodies.append(body)
        if "tools" in body:
            return httpx.Response(400, json={"error": "llama2 does not support tools"})
        return httpx.Response(
            200, text=_ndjson({"message": {"content": "merhaba"}}, {"done": True})
        )

    _mock_driver(monkeypatch, handler)

    async def run():
        return [
            ev async for ev in ollama_provider.stream_user_chat(_settings(tmp_path), "selam")
        ]

    events = asyncio.run(run())
    assert "".join(e["delta"] for e in events if "delta" in e) == "merhaba"
    assert len(bodies) == 2  # 1) with tools (400) → 2) without tools (200)
    assert "tools" in bodies[0] and "tools" not in bodies[1]
    assert any(e.get("done") for e in events)


def test_unsupported_thinking_retries_without_think(tmp_path, monkeypatch) -> None:
    """Model that does NOT SUPPORT thinking + thinking_mode set: the first request (with think) returns 400 'does not
    support thinking' → it is retried WITHOUT think; tools are preserved (only think is dropped)."""
    bodies: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        bodies.append(body)
        if body.get("think"):
            return httpx.Response(400, json={"error": "gemma does not support thinking"})
        return httpx.Response(200, text=_ndjson({"message": {"content": "yanıt"}}, {"done": True}))

    _mock_driver(monkeypatch, handler)
    events = asyncio.run(_collect(
        ollama_provider.stream_user_chat(_settings(tmp_path), "x", thinking_mode="derin")
    ))
    assert "".join(e["delta"] for e in events if "delta" in e) == "yanıt"
    assert len(bodies) == 2
    assert bodies[0].get("think") is True and "think" not in bodies[1]
    assert "tools" in bodies[1]  # think was dropped but tools were PRESERVED


def test_other_400_is_not_silently_retried(tmp_path, monkeypatch) -> None:
    """DEFENSIVE: a 400 that does not contain 'support' (e.g. a real model error) is NOT retried →
    it surfaces as an LLMCallError (dropping tools is only for 'does not support …')."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "model 'nope' not found"})

    _mock_driver(monkeypatch, handler)
    with pytest.raises(LLMCallError):
        asyncio.run(_collect(ollama_provider.stream_user_chat(_settings(tmp_path), "x")))


async def _collect(agen):
    return [ev async for ev in agen]
