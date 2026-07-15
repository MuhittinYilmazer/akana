"""OpenAI provider dispatch — wires OpenAIDriver to Akana wire events + llm_dispatch
routing (the twin of the ``ollama_provider`` test). Hermetic: no real OpenAI,
driven via ``httpx.MockTransport``. For ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` compatibility
use ``asyncio.run`` (so async tests still run when pytest-asyncio is absent).

Provider-specific distinctions (different from ollama, intentional): ``_driver(settings)`` takes
a SINGLE argument (the model is resolved separately via ``_resolve_openai_model`` → both are patched);
tool rounds do NOT emit a ``tool_call`` wire event (silent dispatch); ``usage.tool_calls``
is always ``[]``. The stream carries SSE, single-shot carries JSON (two separate handlers)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from akana.driver.openai import OpenAIDriver
from akana_server.orchestrator import llm_dispatch, openai_provider
from akana_server.orchestrator.chat_persona import CHAT_SYSTEM_PREFIX
from akana_server.orchestrator.llm_dispatch import LLMCallError


def _settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path)


def _sse(*objs) -> str:
    """OpenAI SSE body: each object as ``data: {json}\\n\\n``, then ``data: [DONE]``."""
    return "".join("data: " + json.dumps(o) + "\n\n" for o in objs) + "data: [DONE]\n\n"


def _delta(content=None, tool_calls=None, finish_reason=None, usage=None) -> dict:
    """Single SSE frame (choices[0].delta) — the twin of the helper in the driver test."""
    d: dict = {}
    if content is not None or tool_calls is not None:
        delta: dict = {}
        if content is not None:
            delta["content"] = content
        if tool_calls is not None:
            delta["tool_calls"] = tool_calls
        d["choices"] = [{"delta": delta, "finish_reason": finish_reason}]
    elif finish_reason is not None:
        d["choices"] = [{"delta": {}, "finish_reason": finish_reason}]
    if usage is not None:
        d["usage"] = usage
    return d


def _mock_driver(monkeypatch, handler, model: str = "gpt-5.4") -> None:
    """Pin ``_driver`` + ``_resolve_openai_model`` (skip key/persist resolution).

    ``stream_user_chat`` reads the model name SEPARATELY from ``_resolve_openai_model``
    (for the reasoning gate) → both must be patched; ``model`` controls the reasoning
    tier (gpt-5.4 → reasoning; gpt-4o → not)."""
    drv = OpenAIDriver(api_key="k", model=model, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(openai_provider, "_driver", lambda settings: drv)
    monkeypatch.setattr(openai_provider, "_resolve_openai_model", lambda settings: model)


def _stream_rounds_handler(rounds, captured):
    """Return the next SSE round on each POST (multi-round FC); accumulate bodies."""
    state = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        idx = min(state["i"], len(rounds) - 1)
        state["i"] += 1
        return httpx.Response(200, text=_sse(*rounds[idx]))

    return handler


def _complete_rounds_handler(rounds, captured):
    """Return the next JSON response on each POST (multi-round non-stream FC)."""
    state = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        idx = min(state["i"], len(rounds) - 1)
        state["i"] += 1
        return httpx.Response(200, json=rounds[idx])

    return handler


async def _drain(agen):
    async for _ in agen:
        pass


async def _collect(agen):
    return [ev async for ev in agen]


# -- basic stream / completion --------------------------------------------------


def test_stream_user_chat_yields_wire_events(tmp_path, monkeypatch) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                _delta(content="Mer"),
                _delta(content="haba"),
                _delta(finish_reason="stop", usage={"prompt_tokens": 3, "completion_tokens": 5}),
            ),
        )

    _mock_driver(monkeypatch, handler)
    events = asyncio.run(_collect(openai_provider.stream_user_chat(_settings(tmp_path), "selam")))
    assert "".join(e["delta"] for e in events if "delta" in e) == "Merhaba"
    done = [e for e in events if e.get("done")]
    assert done and done[-1]["usage"]["completion_tokens"] == 5
    assert done[-1]["usage"]["tool_calls"] == []  # openai: usage.tool_calls always empty


def test_complete_chat_returns_text_status_usage(tmp_path, monkeypatch) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "tam yanıt"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            },
        )

    _mock_driver(monkeypatch, handler)
    text, status, usage = asyncio.run(
        openai_provider.complete_chat(_settings(tmp_path), "selam")
    )
    assert text == "tam yanıt"
    assert status == "finished"
    assert usage["completion_tokens"] == 3
    assert usage["tool_calls"] == []


def test_system_prompt_flows_as_system_message(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, text=_sse(_delta(content="ok", finish_reason="stop")))

    _mock_driver(monkeypatch, handler)
    asyncio.run(
        _drain(
            openai_provider.stream_user_chat(
                _settings(tmp_path),
                "soru",
                system_prompt="Sen Akana'sın",
                history=[{"role": "assistant", "content": "önceki"}],
            )
        )
    )
    roles = [m["role"] for m in captured["body"]["messages"]]
    assert roles == ["system", "assistant", "user"]
    assert captured["body"]["messages"][0]["content"] == "Sen Akana'sın"


def test_default_persona_falls_back_to_chat_system_prefix(tmp_path, monkeypatch) -> None:
    """system_prompt=None (default persona) → the model STILL receives CHAT_SYSTEM_PREFIX.

    Regression: without the fallback openai got NO system prompt, so the tool-use
    directives never reached the model and FC tools (memory_search/save_memory/vault_*)
    silently never fired."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, text=_sse(_delta(content="ok", finish_reason="stop")))

    _mock_driver(monkeypatch, handler)
    asyncio.run(_drain(openai_provider.stream_user_chat(_settings(tmp_path), "soru")))
    msgs = captured["body"]["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].strip() == CHAT_SYSTEM_PREFIX.strip()


def test_driver_error_maps_to_cursor_call_error(tmp_path, monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("openai down", request=req)

    _mock_driver(monkeypatch, handler)
    with pytest.raises(LLMCallError) as ei:
        asyncio.run(_drain(openai_provider.stream_user_chat(_settings(tmp_path), "x")))
    assert ei.value.status_code == 503  # DriverUnavailable → 503


def test_cursor_client_routes_to_openai(tmp_path, monkeypatch) -> None:
    """provider=openai → llm_dispatch.stream_user_chat delegates to openai_provider."""
    monkeypatch.setattr(llm_dispatch, "_active_provider", lambda s: "openai")

    async def fake(settings, user_text, **_kw):
        yield {"delta": "ok", "done": False}
        yield {"done": True, "usage": {}}

    monkeypatch.setattr(openai_provider, "stream_user_chat", fake)
    events = asyncio.run(_collect(llm_dispatch.stream_user_chat(_settings(tmp_path), "hi")))
    assert any(e.get("delta") == "ok" for e in events)


# -- native function-calling + thinking parity ---------------------------------


def test_tools_always_sent_in_request_body(tmp_path, monkeypatch) -> None:
    """Native FC: every request carries OpenAI-style ``tools`` (memory_search + save_memory)."""
    bodies: list = []
    handler = _stream_rounds_handler([[_delta(content="selam", finish_reason="stop")]], bodies)
    _mock_driver(monkeypatch, handler)
    asyncio.run(_drain(openai_provider.stream_user_chat(_settings(tmp_path), "merhaba")))
    tools = bodies[0]["tools"]
    names = [t["function"]["name"] for t in tools]
    assert "memory_search" in names and "save_memory" in names
    assert all(t["type"] == "function" for t in tools)
    assert bodies[0]["tool_choice"] == "auto"


def test_reasoning_effort_gated_by_model_and_mode(tmp_path, monkeypatch) -> None:
    """reasoning model + thinking_mode → ``reasoning_effort``; classic model OR no-mode
    → the field is NEVER sent (gpt-4o rejects ``reasoning_effort`` with a 400)."""
    # reasoning model (gpt-5.4) + deep mode → high
    bodies: list = []
    handler = _stream_rounds_handler([[_delta(content="ok", finish_reason="stop")]], bodies)
    _mock_driver(monkeypatch, handler, model="gpt-5.4")
    asyncio.run(
        _drain(openai_provider.stream_user_chat(_settings(tmp_path), "x", thinking_mode="derin"))
    )
    assert bodies[0]["reasoning_effort"] == "high"

    # classic chat model (gpt-4o) → not sent even when mode is set
    bodies2: list = []
    handler2 = _stream_rounds_handler([[_delta(content="ok", finish_reason="stop")]], bodies2)
    _mock_driver(monkeypatch, handler2, model="gpt-4o")
    asyncio.run(
        _drain(openai_provider.stream_user_chat(_settings(tmp_path), "x", thinking_mode="derin"))
    )
    assert "reasoning_effort" not in bodies2[0]

    # reasoning model but no mode → not sent
    bodies3: list = []
    handler3 = _stream_rounds_handler([[_delta(content="ok", finish_reason="stop")]], bodies3)
    _mock_driver(monkeypatch, handler3, model="gpt-5.4")
    asyncio.run(_drain(openai_provider.stream_user_chat(_settings(tmp_path), "x")))
    assert "reasoning_effort" not in bodies3[0]


def test_native_effort_levels_pass_through_verbatim(tmp_path, monkeypatch) -> None:
    """The composer sends OpenAI's own level names (minimal…xhigh) when openai is active;
    they reach ``reasoning_effort`` VERBATIM (no Akana-tier mapping). ``minimal`` is a real
    GPT-5 level (not collapsed to low) and ``xhigh`` (extra-high) is reachable."""
    for level in ("minimal", "low", "medium", "high", "xhigh"):
        bodies: list = []
        handler = _stream_rounds_handler([[_delta(content="ok", finish_reason="stop")]], bodies)
        _mock_driver(monkeypatch, handler, model="gpt-5.4")
        asyncio.run(
            _drain(openai_provider.stream_user_chat(_settings(tmp_path), "x", thinking_mode=level))
        )
        assert bodies[0]["reasoning_effort"] == level


def test_thinking_deltas_surface_as_thinking_events(tmp_path, monkeypatch) -> None:
    """A ``reasoning_content`` delta becomes a separate ``thinking`` wire event; it does NOT
    mix into the answer (gemini/ollama shape). The wire contract requires a DICT
    ({"phase":"delta","text":..}) — a plain string would be dropped by the SSE consumer
    (chat_producer ``isinstance(thinking, dict)``). When thinking ends, ``{"phase":"completed"}``
    is emitted (identical to ollama)."""
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_sse(
                {"choices": [{"delta": {"reasoning_content": "düşün"}}]},
                {"choices": [{"delta": {"reasoning_content": "üyorum"}}]},
                _delta(content="Cevap", finish_reason="stop", usage={"completion_tokens": 2}),
            ),
        )

    _mock_driver(monkeypatch, handler)
    events = asyncio.run(_collect(openai_provider.stream_user_chat(_settings(tmp_path), "soru", thinking_mode="derin")))
    thinking = [e["thinking"] for e in events if "thinking" in e]
    # Every thinking event is a DICT (wire contract); deltas + a final completed.
    assert all(isinstance(t, dict) for t in thinking)
    deltas = [t for t in thinking if t.get("phase") == "delta"]
    assert "".join(t["text"] for t in deltas) == "düşünüyorum"
    assert thinking[-1] == {"phase": "completed"}
    # the thinking text does NOT mix into the answer → only the content delta shows
    assert "".join(e["delta"] for e in events if "delta" in e) == "Cevap"


def test_stream_function_call_loop_invokes_dispatch(tmp_path, monkeypatch) -> None:
    """stream: round 1 tool_calls (emits no text) → dispatch → round 2 streams text. Intermediate-round
    tool text does not mix into the final answer; request 2 carries the assistant tool_calls round + the tool result."""
    dispatched: list = []
    monkeypatch.setattr(
        openai_provider,
        "dispatch_llm_tool",
        lambda s, c, n, a: dispatched.append((n, a)) or "ARAÇ-SONUCU",
    )
    bodies: list = []
    handler = _stream_rounds_handler(
        [
            # round 1: model calls save_memory (no content)
            [
                _delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "save_memory", "arguments": '{"text":"kahve sever"}'},
                        }
                    ],
                    finish_reason="tool_calls",
                ),
                _delta(finish_reason="tool_calls", usage={"prompt_tokens": 1, "completion_tokens": 1}),
            ],
            # round 2: model returns the final text
            [
                _delta(content="Not"),
                _delta(content=" eklendi.", finish_reason="stop", usage={"prompt_tokens": 2, "completion_tokens": 3}),
            ],
        ],
        bodies,
    )
    _mock_driver(monkeypatch, handler)
    events = asyncio.run(
        _collect(openai_provider.stream_user_chat(_settings(tmp_path), "bunu hatırla", conversation_id="c1"))
    )
    assert dispatched == [("save_memory", {"text": "kahve sever"})]
    assert "".join(e["delta"] for e in events if "delta" in e) == "Not eklendi."
    assert len(bodies) == 2  # tool round + final round
    roles = [m["role"] for m in bodies[1]["messages"]]
    assert "assistant" in roles and "tool" in roles
    tool_msg = next(m for m in bodies[1]["messages"] if m["role"] == "tool")
    assert tool_msg["content"] == "ARAÇ-SONUCU"
    # assistant round OpenAI contract: the assistant message carrying tool_calls comes BEFORE the tool result
    asst = next(m for m in bodies[1]["messages"] if m["role"] == "assistant" and m.get("tool_calls"))
    assert asst["tool_calls"][0]["function"]["name"] == "save_memory"
    done = [e for e in events if e.get("done")]
    # usage is accumulated across ALL rounds: tool round (c1) + final round (c3) → 4.
    assert done and done[-1]["usage"]["completion_tokens"] == 4


def test_stream_emits_tool_call_start_end_events_and_usage(tmp_path, monkeypatch) -> None:
    """PARITY (Gap B): a tool round emits start+end ``tool_call`` wire events (ollama/claude
    shape) → UI card + audit ledger. start carries args/result empty; end carries result/args empty;
    both share the SAME id (persist dedup). done.usage.tool_calls carries these records (old behavior was always []).
    """
    monkeypatch.setattr(openai_provider, "dispatch_llm_tool", lambda s, c, n, a: "ARAÇ-SONUCU")
    bodies: list = []
    handler = _stream_rounds_handler(
        [
            [
                _delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "save_memory", "arguments": '{"text":"kahve sever"}'},
                        }
                    ],
                    finish_reason="tool_calls",
                ),
                _delta(finish_reason="tool_calls", usage={"prompt_tokens": 1, "completion_tokens": 1}),
            ],
            [_delta(content="Tamam.", finish_reason="stop", usage={"prompt_tokens": 2, "completion_tokens": 3})],
        ],
        bodies,
    )
    _mock_driver(monkeypatch, handler)
    events = asyncio.run(
        _collect(openai_provider.stream_user_chat(_settings(tmp_path), "bunu hatırla", conversation_id="c1"))
    )
    tcs = [e["tool_call"] for e in events if "tool_call" in e]
    assert len(tcs) == 2  # single tool call → start + end
    start, end = tcs
    assert start["phase"] == "start" and start["name"] == "save_memory"
    assert start["args"] == {"text": "kahve sever"} and start["result"] is None
    assert end["phase"] == "end" and end["result"] == "ARAÇ-SONUCU" and end["status"] == "ok"
    assert start["id"] == end["id"] == "call_1"  # same id → persist updates the start card in place
    # done.usage.tool_calls carries the start+end records (audit ledger)
    done = [e for e in events if e.get("done")][-1]
    assert done["usage"]["tool_calls"] == tcs


def test_stream_tool_call_synthesizes_id_when_missing(tmp_path, monkeypatch) -> None:
    """A tool_call without an id (rare) → start/end are MATCHED via an index-based synthetic id
    (``openai-tool-<idx>``), so persist can still merge the pair."""
    monkeypatch.setattr(openai_provider, "dispatch_llm_tool", lambda s, c, n, a: "R")
    bodies: list = []
    handler = _stream_rounds_handler(
        [
            [
                _delta(
                    tool_calls=[
                        {"index": 0, "type": "function", "function": {"name": "memory_search", "arguments": "{}"}}
                    ],
                    finish_reason="tool_calls",
                ),
                _delta(finish_reason="tool_calls"),
            ],
            [_delta(content="ok", finish_reason="stop")],
        ],
        bodies,
    )
    _mock_driver(monkeypatch, handler)
    events = asyncio.run(_collect(openai_provider.stream_user_chat(_settings(tmp_path), "x")))
    tcs = [e["tool_call"] for e in events if "tool_call" in e]
    assert tcs[0]["id"] == tcs[1]["id"] == "openai-tool-0"


def test_complete_chat_populates_usage_tool_calls(tmp_path, monkeypatch) -> None:
    """complete_chat also populates usage.tool_calls (parity with stream → voice/non-stream
    audit path). start+end records are produced for every tool called."""
    monkeypatch.setattr(openai_provider, "dispatch_llm_tool", lambda s, c, n, a: "HAFIZA")
    bodies: list = []
    handler = _complete_rounds_handler(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {"id": "call_9", "type": "function", "function": {"name": "memory_search", "arguments": '{"query":"kahve"}'}}
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            {"choices": [{"message": {"content": "son"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        ],
        bodies,
    )
    _mock_driver(monkeypatch, handler)
    _text, _status, usage = asyncio.run(openai_provider.complete_chat(_settings(tmp_path), "kahve?"))
    tcs = usage["tool_calls"]
    assert len(tcs) == 2  # start + end
    assert tcs[0]["phase"] == "start" and tcs[0]["args"] == {"query": "kahve"}
    assert tcs[1]["phase"] == "end" and tcs[1]["result"] == "HAFIZA"
    assert tcs[0]["id"] == tcs[1]["id"] == "call_9"


def test_complete_chat_function_call_loop(tmp_path, monkeypatch) -> None:
    """complete_chat: response 1 tool_calls (OpenAI NESTED shape) → dispatch → response 2 final
    text. REGRESSION: if ``complete_once`` does not flatten tool_calls to the FLAT shape, dispatch
    would be called with empty name/args (``[("", {})]``) — here the real name/args are verified."""
    dispatched: list = []
    monkeypatch.setattr(
        openai_provider,
        "dispatch_llm_tool",
        lambda s, c, n, a: dispatched.append((n, a)) or "HAFIZA-SONUCU",
    )
    bodies: list = []
    handler = _complete_rounds_handler(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_9",
                                    "type": "function",
                                    "function": {"name": "memory_search", "arguments": '{"query":"kahve"}'},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            {
                "choices": [{"message": {"content": "Kahveyi seversin."}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 6},
            },
        ],
        bodies,
    )
    _mock_driver(monkeypatch, handler)
    text, status, usage = asyncio.run(
        openai_provider.complete_chat(_settings(tmp_path), "kahveyi sever miyim?")
    )
    assert text == "Kahveyi seversin."
    assert status == "finished"
    # usage is accumulated across ALL rounds: tool round (c1) + final round (c6) → 7.
    assert usage["completion_tokens"] == 7
    assert dispatched == [("memory_search", {"query": "kahve"})]  # without flattening, ("", {})
    assert len(bodies) == 2
    roles = [m["role"] for m in bodies[1]["messages"]]
    assert "assistant" in roles and "tool" in roles
    tool_msg = next(m for m in bodies[1]["messages"] if m["role"] == "tool")
    assert tool_msg["content"] == "HAFIZA-SONUCU"
    assert tool_msg["tool_call_id"] == "call_9"  # OpenAI contract: result is matched by the call id


def test_tool_loop_caps_at_max_rounds(tmp_path, monkeypatch) -> None:
    """Even if the model calls a tool on every round, the loop stops at ``_MAX_TOOL_ROUNDS`` (infinite
    loop guard) — it returns ``finished`` at the round limit without blowing up."""
    monkeypatch.setattr(openai_provider, "dispatch_llm_tool", lambda s, c, n, a: "yine")
    bodies: list = []
    always_calls = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "memory_search", "arguments": '{"query":"q"}'},
                        }
                    ],
                }
            }
        ],
        "usage": {},
    }
    handler = _complete_rounds_handler([always_calls], bodies)
    _mock_driver(monkeypatch, handler)
    _text, status, _usage = asyncio.run(
        openai_provider.complete_chat(_settings(tmp_path), "loop")
    )
    # 5 tool rounds + a FINAL tool-less completion (to avoid falling back to an empty answer
    # when the model gets stuck, one more tool-less completion is made → _MAX_TOOL_ROUNDS + 1 calls total).
    assert len(bodies) == openai_provider._MAX_TOOL_ROUNDS + 1
    assert not bodies[-1].get("tools")  # the final completion is TOOL-LESS (model replies with text)
    assert status == "finished"  # does not hang / blow up


# --- Multimodal: NATIVE image input (file_ids → image_url data-URI) -------


class _FakeUploadRec:
    def __init__(
        self,
        *,
        kind="image",
        disabled=False,
        media="image/png",
        file_name="up.bin",
        original_name=None,
    ) -> None:
        self.kind = kind
        self.disabled = disabled
        self._media = media
        self.file_name = file_name
        self.original_name = original_name

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    @property
    def media_type(self) -> str:
        return self._media


def _fake_store(monkeypatch, tmp_path, records: dict):
    """Replace ``UploadStore`` with a fake: id→(rec, bytes). file_path is a real tmp path."""
    blobs = {}
    for fid, (rec, data) in records.items():
        p = tmp_path / f"{fid}.bin"
        p.write_bytes(data)
        blobs[fid] = p

    class _Store:
        @classmethod
        def for_settings(cls, settings):
            return cls()

        def get(self, fid):
            entry = records.get(fid)
            return entry[0] if entry else None

        def file_path(self, rec):
            for fid, (r, _data) in records.items():
                if r is rec:
                    return blobs[fid]
            raise FileNotFoundError

    import akana_server.multimodal.store as store_mod

    monkeypatch.setattr(store_mod, "UploadStore", _Store)


def test_stream_embeds_image_url_in_last_user_message(tmp_path, monkeypatch) -> None:
    """file_ids → the last user message's content becomes an ARRAY: a text part + an image_url
    data-URI part (OpenAI vision; the model actually sees the image)."""
    _fake_store(monkeypatch, tmp_path, {"f1": (_FakeUploadRec(), b"PNGDATA")})
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, text=_sse(_delta(content="gördüm", finish_reason="stop")))

    _mock_driver(monkeypatch, handler)
    asyncio.run(
        _drain(openai_provider.stream_user_chat(_settings(tmp_path), "bu ne?", file_ids=["f1"]))
    )
    last = captured["body"]["messages"][-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    text_parts = [p for p in last["content"] if p.get("type") == "text"]
    img_parts = [p for p in last["content"] if p.get("type") == "image_url"]
    assert text_parts[0]["text"] == "bu ne?"
    assert len(img_parts) == 1
    url = img_parts[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_no_file_ids_keeps_plain_text_content(tmp_path, monkeypatch) -> None:
    """Without file_ids the last user content is a plain text STRING (not an array) — old behavior."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, text=_sse(_delta(content="ok", finish_reason="stop")))

    _mock_driver(monkeypatch, handler)
    asyncio.run(_drain(openai_provider.stream_user_chat(_settings(tmp_path), "selam")))
    assert captured["body"]["messages"][-1]["content"] == "selam"


def test_image_parts_skips_unsupported_disabled_oversize(tmp_path, monkeypatch) -> None:
    """``_image_parts``: an unsupported-type (not image/PDF) / disabled / over-budget
    attachment is skipped; only a valid image becomes image_url. (PDF is no longer skipped — a
    separate test verifies the PDF's ``file`` part.)"""
    monkeypatch.setattr(openai_provider, "_MAX_INLINE_TOTAL_BYTES", 8)
    _fake_store(
        monkeypatch,
        tmp_path,
        {
            "img": (_FakeUploadRec(), b"PNG"),  # 3 bytes → fits
            # text/docx: neither image NOR PDF → skip
            "txt": (_FakeUploadRec(kind="text", media="text/plain"), b"merhaba"),
            "off": (_FakeUploadRec(disabled=True), b"X"),  # disabled → skip
            "big": (_FakeUploadRec(), b"X" * 20),  # over budget → skip
        },
    )
    parts = openai_provider._image_parts(_settings(tmp_path), ["img", "txt", "off", "big", "yok"])
    assert len(parts) == 1
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_stream_embeds_pdf_as_file_part(tmp_path, monkeypatch) -> None:
    """PDF file_id → an OpenAI ``file`` content part (filename +
    ``application/pdf`` data-URI) is added to the last user message — NOT image_url (OpenAI PDF contract)."""
    _fake_store(
        monkeypatch,
        tmp_path,
        {"d1": (_FakeUploadRec(kind="pdf", media="application/pdf", file_name="rapor.pdf"), b"%PDF-1.7")},
    )
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, text=_sse(_delta(content="okudum", finish_reason="stop")))

    _mock_driver(monkeypatch, handler)
    asyncio.run(
        _drain(openai_provider.stream_user_chat(_settings(tmp_path), "özetle", file_ids=["d1"]))
    )
    last = captured["body"]["messages"][-1]
    assert isinstance(last["content"], list)
    assert not [p for p in last["content"] if p.get("type") == "image_url"]  # PDF is not image_url
    file_parts = [p for p in last["content"] if p.get("type") == "file"]
    assert len(file_parts) == 1
    assert file_parts[0]["file"]["filename"] == "rapor.pdf"
    assert file_parts[0]["file"]["file_data"].startswith("data:application/pdf;base64,")


# --- FC-loop resilience: round-limit does not fall back to an empty answer + tokens ACCUMULATE -------


def _complete_branch_handler(tool_round, final_text, bodies):
    """Request carrying ``tools`` → a tool call; NO ``tools`` → plain text (the response of the
    tool-less FINAL completion after the cap). For driving the multi-round cap scenario."""

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        bodies.append(body)
        if "tools" in body:
            return httpx.Response(200, json=tool_round)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": final_text}}], "usage": {"prompt_tokens": 2, "completion_tokens": 4}},
        )

    return handler


def test_complete_chat_tool_cap_falls_back_to_text(tmp_path, monkeypatch) -> None:
    """If the model calls a tool on EVERY round and produces no text (the cap is exhausted): after
    the loop a tool-less FINAL completion is made → the answer is NOT empty, real text is returned
    (the old behavior ``("", ...)`` gave an empty bubble). The final request does NOT carry ``tools``."""
    monkeypatch.setattr(openai_provider, "dispatch_llm_tool", lambda s, c, n, a: "yine")
    always_calls = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"id": "c", "type": "function", "function": {"name": "memory_search", "arguments": "{}"}}
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    bodies: list = []
    handler = _complete_branch_handler(always_calls, "Son cevap.", bodies)
    _mock_driver(monkeypatch, handler)
    text, status, _usage = asyncio.run(openai_provider.complete_chat(_settings(tmp_path), "loop"))
    assert text == "Son cevap."  # NOT empty — tool-less completion after the cap
    assert status == "finished"
    # _MAX_TOOL_ROUNDS tool-carrying rounds + 1 tool-less final round
    assert len(bodies) == openai_provider._MAX_TOOL_ROUNDS + 1
    assert "tools" not in bodies[-1]  # the final completion DELIBERATELY skips tools


def test_complete_chat_usage_accumulates_across_rounds(tmp_path, monkeypatch) -> None:
    """Tokens are ACCUMULATED across ALL rounds (not overwritten): tool round (p1+c1)
    + final round (p4+c6) → prompt=5, completion=7. The old behavior kept only the last round."""
    monkeypatch.setattr(openai_provider, "dispatch_llm_tool", lambda s, c, n, a: "ok")
    bodies: list = []
    handler = _complete_rounds_handler(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {"id": "c1", "type": "function", "function": {"name": "memory_search", "arguments": "{}"}}
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            {
                "choices": [{"message": {"content": "bitti"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 6},
            },
        ],
        bodies,
    )
    _mock_driver(monkeypatch, handler)
    _text, _status, usage = asyncio.run(openai_provider.complete_chat(_settings(tmp_path), "x"))
    assert usage["prompt_tokens"] == 5  # 1 (tool round) + 4 (final) — accumulated
    assert usage["completion_tokens"] == 7  # 1 + 6 — accumulated


def test_stream_usage_accumulates_across_rounds(tmp_path, monkeypatch) -> None:
    """stream: the intermediate tool round's tokens (p1+c1) are ADDED to the final round's tokens
    (p2+c3) → done.usage prompt=3, completion=4 (the old behavior kept only the final round)."""
    monkeypatch.setattr(openai_provider, "dispatch_llm_tool", lambda s, c, n, a: "ok")
    bodies: list = []
    handler = _stream_rounds_handler(
        [
            [
                _delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "save_memory", "arguments": "{}"},
                        }
                    ],
                    finish_reason="tool_calls",
                ),
                _delta(finish_reason="tool_calls", usage={"prompt_tokens": 1, "completion_tokens": 1}),
            ],
            [_delta(content="bitti", finish_reason="stop", usage={"prompt_tokens": 2, "completion_tokens": 3})],
        ],
        bodies,
    )
    _mock_driver(monkeypatch, handler)
    events = asyncio.run(_collect(openai_provider.stream_user_chat(_settings(tmp_path), "x")))
    done = [e for e in events if e.get("done")]
    assert done[-1]["usage"]["prompt_tokens"] == 3  # 1 + 2 — accumulated
    assert done[-1]["usage"]["completion_tokens"] == 4  # 1 + 3 — accumulated


# --- MCP bridge: external tools join the native FC surface + dispatch is routed -----


class _FakeBridge:
    """``McpToolBridge`` duck-type: decls + handles + async dispatch + async ctx mgr.

    ``handles`` is True for names carrying the ``mcp__`` prefix (the real bridge's namespace);
    ``dispatch`` accumulates calls → the test verifies whether the call went to the bridge or to
    native. By patching ``external_mcp_bridge`` the bridged scenario is driven even in an
    unpatched (yaml-less) environment."""

    def __init__(self, decls, result="MCP-SONUCU") -> None:
        self.decls = decls
        self._result = result
        self.calls: list = []

    def handles(self, name) -> bool:
        return isinstance(name, str) and name.startswith("mcp__")

    async def dispatch(self, name, args) -> str:
        self.calls.append((name, args))
        return self._result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


_MCP_DECL = {
    "type": "function",
    "function": {
        "name": "mcp__fs__read_file",
        "description": "Dosya oku",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}


def test_bridge_decls_merge_into_request_tools(tmp_path, monkeypatch) -> None:
    """Bridge tools (``bridge.decls``) are ADDED to the native FC surface: the ``tools`` in the
    request body carry both the built-in ones (memory_search/save_memory) and the ``mcp__…`` decl
    (the OpenAI ``tools`` format is already shared → a flat merge)."""
    bridge = _FakeBridge([_MCP_DECL])
    monkeypatch.setattr(openai_provider, "external_mcp_bridge", lambda s: bridge)
    bodies: list = []
    handler = _stream_rounds_handler([[_delta(content="ok", finish_reason="stop")]], bodies)
    _mock_driver(monkeypatch, handler)
    asyncio.run(_drain(openai_provider.stream_user_chat(_settings(tmp_path), "x")))
    names = [t["function"]["name"] for t in bodies[0]["tools"]]
    assert "memory_search" in names and "save_memory" in names
    assert "mcp__fs__read_file" in names  # the bridge decl is on the surface too


def test_bridge_tool_call_routes_to_bridge_dispatch(tmp_path, monkeypatch) -> None:
    """A tool call named ``mcp__…`` is routed to bridge.dispatch (NOT native ``dispatch_llm_tool``);
    the bridge result flows both into the ``role=tool`` message and into the end event's result."""
    bridge = _FakeBridge([_MCP_DECL], result="MCP-OKUNDU")
    monkeypatch.setattr(openai_provider, "external_mcp_bridge", lambda s: bridge)
    native: list = []
    monkeypatch.setattr(
        openai_provider, "dispatch_llm_tool", lambda s, c, n, a: native.append((n, a)) or "NATIVE"
    )
    bodies: list = []
    handler = _stream_rounds_handler(
        [
            [
                _delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "mcp__fs__read_file", "arguments": '{"path":"/notlar"}'},
                        }
                    ],
                    finish_reason="tool_calls",
                ),
                _delta(finish_reason="tool_calls"),
            ],
            [_delta(content="Okudum.", finish_reason="stop")],
        ],
        bodies,
    )
    _mock_driver(monkeypatch, handler)
    events = asyncio.run(
        _collect(openai_provider.stream_user_chat(_settings(tmp_path), "dosyayı oku", conversation_id="c1"))
    )
    assert bridge.calls == [("mcp__fs__read_file", {"path": "/notlar"})]  # went to the bridge
    assert native == []  # native dispatch was NOT called
    tcs = [e["tool_call"] for e in events if "tool_call" in e]
    end = next(t for t in tcs if t["phase"] == "end")
    assert end["result"] == "MCP-OKUNDU"
    tool_msg = next(m for m in bodies[1]["messages"] if m["role"] == "tool")
    assert tool_msg["content"] == "MCP-OKUNDU"
    assert tool_msg["tool_call_id"] == "call_1"  # OpenAI contract: result is matched by the call id


def test_bridge_complete_chat_routes_mcp_tool(tmp_path, monkeypatch) -> None:
    """complete_chat (non-stream, voice/audit path) also routes an ``mcp__…`` call to the bridge;
    the result flows into the tool message + the usage.tool_calls end record (parity with stream)."""
    bridge = _FakeBridge([_MCP_DECL], result="MCP-VERI")
    monkeypatch.setattr(openai_provider, "external_mcp_bridge", lambda s: bridge)
    monkeypatch.setattr(openai_provider, "dispatch_llm_tool", lambda s, c, n, a: "NATIVE")
    bodies: list = []
    handler = _complete_rounds_handler(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {"id": "call_7", "type": "function", "function": {"name": "mcp__fs__read_file", "arguments": '{"path":"/x"}'}}
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            {"choices": [{"message": {"content": "bitti"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        ],
        bodies,
    )
    _mock_driver(monkeypatch, handler)
    _text, _status, usage = asyncio.run(openai_provider.complete_chat(_settings(tmp_path), "oku"))
    assert bridge.calls == [("mcp__fs__read_file", {"path": "/x"})]
    tool_msg = next(m for m in bodies[1]["messages"] if m["role"] == "tool")
    assert tool_msg["content"] == "MCP-VERI"
    end = next(t for t in usage["tool_calls"] if t["phase"] == "end")
    assert end["result"] == "MCP-VERI"


# -- generation-timeout knob (the ollama _driver timeout twin; openai's _driver
#    takes ONLY settings + requires a key, so the key/model resolvers are patched) --


def _timeout_settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, cursor_model="composer-2")


def _patch_key_and_model(monkeypatch) -> None:
    monkeypatch.setattr(openai_provider, "resolve_openai_key", lambda s: "sk-test")
    monkeypatch.setattr(openai_provider, "_resolve_openai_model", lambda s: "gpt-5.4")


def test_driver_timeout_defaults_to_historical_ceiling(tmp_path, monkeypatch) -> None:
    """Default (env unset, no store): the OpenAI driver keeps the historical 300 s
    generation ceiling (openai's default is NOT the 0-sentinel ollama uses)."""
    monkeypatch.delenv("AKANA_OPENAI_TIMEOUT", raising=False)
    _patch_key_and_model(monkeypatch)
    drv = openai_provider._driver(_timeout_settings(tmp_path))
    assert drv._timeout == 300.0


def test_driver_timeout_reads_runtime_openai_timeout(tmp_path, monkeypatch) -> None:
    """A configured AKANA_OPENAI_TIMEOUT flows into the driver (resolved live per call);
    0 disables the generation ceiling."""
    _patch_key_and_model(monkeypatch)
    monkeypatch.setenv("AKANA_OPENAI_TIMEOUT", "45")
    drv = openai_provider._driver(_timeout_settings(tmp_path))
    assert drv._timeout == 45.0
    monkeypatch.setenv("AKANA_OPENAI_TIMEOUT", "0")
    drv0 = openai_provider._driver(_timeout_settings(tmp_path))
    assert drv0._timeout == 0.0
