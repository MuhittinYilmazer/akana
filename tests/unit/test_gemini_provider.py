"""Gemini provider dispatch — wires the google-genai stream to Akana wire events +
llm_dispatch routing. Hermetic: NO real network/Live — whether or not the SDK is
installed, ``gemini_provider.make_client`` is patched with a fake client.
``asyncio.run`` is used for ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` compatibility
(so async tests still run without pytest-asyncio)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from akana_server.orchestrator import llm_dispatch, gemini_provider
from akana_server.orchestrator.chat_persona import CHAT_SYSTEM_PREFIX
from akana_server.orchestrator.llm_dispatch import LLMCallError


def _settings(tmp_path):
    # defaults_from_env only reads settings.cursor_model; with data_dir,
    # load_llm_settings falls to defaults in the empty tmp_path (no file) →
    # resolve_gemini_model_tag returns "gemini-2.5-flash" (hermetic).
    return SimpleNamespace(
        data_dir=tmp_path, cursor_model="composer-2", gemini_model=""
    )


# --- Fake google-genai client ---------------------------------------------


class _FakeUsage:
    def __init__(self, prompt: int, candidates: int) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates


class _FakeFC:
    """Fake function-call (SDK ``types.FunctionCall`` duck-type: name/args/id)."""

    def __init__(self, name, args=None, fid=None) -> None:
        self.name = name
        self.args = args or {}
        self.id = fid


class _FakePart:
    """Fake SDK ``types.Part`` duck-type: carries function_call + thought_signature.

    In the Gemini 3 "thinking" stream the model's function_call part carries a
    ``thought_signature`` (bytes); it must be preserved on re-call (or 400 INVALID_ARGUMENT)."""

    def __init__(self, function_call=None, thought_signature=None) -> None:
        self.function_call = function_call
        self.thought_signature = thought_signature


class _FakeChunk:
    def __init__(self, text=None, usage_metadata=None, function_calls=None) -> None:
        self.text = text
        self.usage_metadata = usage_metadata
        # The SDK stream chunk also has a ``function_calls`` property; if we leave it
        # None on the fake side the provider falls to candidates (both yield []).
        self.function_calls = function_calls


class _FakeResp:
    def __init__(self, text="", usage_metadata=None, function_calls=None) -> None:
        self.text = text
        self.usage_metadata = usage_metadata
        self.function_calls = function_calls


class _FakeModels:
    """``client.aio.models`` twin — captures the arguments, drives the error/stream/response.

    For the function-calling loop, ``resps``/``chunk_rounds`` can be given as a SEQUENCE:
    each ``generate_content``/``generate_content_stream`` call consumes the next item
    (stays on the last item once exhausted). ``capture`` holds the LAST call's args,
    ``contents_history`` holds a contents copy of each call (FC-append verification)."""

    def __init__(
        self, *, chunks=None, resp=None, resps=None, chunk_rounds=None, error=None, capture=None
    ) -> None:
        self._chunk_rounds = chunk_rounds if chunk_rounds is not None else None
        self._chunks = chunks or []
        self._resps = list(resps) if resps is not None else None
        self._resp = resp
        self._error = error
        self.capture = capture if capture is not None else {}
        self.calls = 0
        self.contents_history: list = []

    def _record(self, model, contents, config):
        self.capture.update(model=model, contents=contents, config=config)
        # copy contents because it is mutated each turn (to see the FC append).
        self.contents_history.append([dict(c) for c in contents])
        self.calls += 1

    async def generate_content_stream(self, *, model, contents, config):
        self._record(model, contents, config)
        if self._error is not None:
            raise self._error
        if self._chunk_rounds is not None:
            idx = min(self.calls - 1, len(self._chunk_rounds) - 1)
            chunks = self._chunk_rounds[idx]
        else:
            chunks = self._chunks

        async def _gen():
            for c in chunks:
                yield c

        return _gen()

    async def generate_content(self, *, model, contents, config):
        self._record(model, contents, config)
        if self._error is not None:
            raise self._error
        if self._resps is not None:
            idx = min(self.calls - 1, len(self._resps) - 1)
            return self._resps[idx]
        return self._resp


class _FakeClient:
    def __init__(self, models: _FakeModels) -> None:
        self.aio = SimpleNamespace(models=models)


def _fake_client(
    monkeypatch, *, chunks=None, resp=None, resps=None, chunk_rounds=None, error=None
) -> _FakeModels:
    models = _FakeModels(
        chunks=chunks, resp=resp, resps=resps, chunk_rounds=chunk_rounds, error=error
    )
    monkeypatch.setattr(
        gemini_provider, "make_client", lambda settings, **_kw: _FakeClient(models)
    )
    return models


async def _drain(agen):
    async for _ in agen:
        pass


# --- Tests -----------------------------------------------------------------


def test_stream_user_chat_yields_wire_events(tmp_path, monkeypatch) -> None:
    _fake_client(
        monkeypatch,
        chunks=[
            _FakeChunk("Mer"),
            _FakeChunk("haba"),
            _FakeChunk(None, usage_metadata=_FakeUsage(3, 5)),
        ],
    )

    async def run():
        return [
            ev
            async for ev in gemini_provider.stream_user_chat(_settings(tmp_path), "selam")
        ]

    events = asyncio.run(run())
    assert "".join(e["delta"] for e in events if "delta" in e) == "Merhaba"
    done = [e for e in events if e.get("done")]
    assert done and done[-1]["usage"]["completion_tokens"] == 5
    assert done[-1]["usage"]["prompt_tokens"] == 3
    assert done[-1]["usage"]["tool_calls"] == []  # gemini Phase 1: no tools
    assert "cost_usd" not in done[-1]["usage"]  # Anthropic-only pricing → gemini omits


def test_complete_chat_returns_text_status_usage(tmp_path, monkeypatch) -> None:
    _fake_client(
        monkeypatch,
        resp=_FakeResp("tam yanıt", usage_metadata=_FakeUsage(2, 3)),
    )
    text, status, usage = asyncio.run(
        gemini_provider.complete_chat(_settings(tmp_path), "selam")
    )
    assert text == "tam yanıt"
    assert status == "finished"
    assert usage["completion_tokens"] == 3
    assert usage["tool_calls"] == []


def test_system_prompt_and_history_shape_contents(tmp_path, monkeypatch) -> None:
    """system_prompt → config.system_instruction (NOT in contents); history roles
    map to user/model (assistant→model), the last turn user. The config now ALWAYS
    carries native function-calling ``tools`` (memory_search + save_memory)."""
    models = _fake_client(monkeypatch, chunks=[_FakeChunk("ok")])
    asyncio.run(
        _drain(
            gemini_provider.stream_user_chat(
                _settings(tmp_path),
                "soru",
                system_prompt="Sen Akana'sın",
                history=[{"role": "assistant", "content": "önceki"}],
            )
        )
    )
    cfg = models.capture["config"]
    assert cfg["system_instruction"] == "Sen Akana'sın"
    # tools are always present; no thinking_mode was given so NO thinking_config.
    tool_names = [d["name"] for d in cfg["tools"][0]["function_declarations"]]
    assert "memory_search" in tool_names and "save_memory" in tool_names
    assert "thinking_config" not in cfg
    roles = [c["role"] for c in models.capture["contents"]]
    assert roles == ["model", "user"]  # assistant→model, then the last user turn
    assert models.capture["contents"][0]["parts"][0]["text"] == "önceki"
    assert models.capture["contents"][-1]["parts"][0]["text"] == "soru"


def test_default_persona_falls_back_to_chat_system_prefix(tmp_path, monkeypatch) -> None:
    """system_prompt=None (default persona) → config.system_instruction is CHAT_SYSTEM_PREFIX.

    Regression: without the fallback gemini got NO system_instruction, so the tool-use
    directives never reached the model and FC tools (memory_search/save_memory/vault_*)
    silently never fired."""
    models = _fake_client(monkeypatch, chunks=[_FakeChunk("ok")])
    asyncio.run(_drain(gemini_provider.stream_user_chat(_settings(tmp_path), "soru")))
    assert models.capture["config"]["system_instruction"].strip() == CHAT_SYSTEM_PREFIX.strip()


def test_thinking_config_present_when_mode_set(tmp_path, monkeypatch) -> None:
    """Gemini 3+ + a non-empty thinking_mode → config.thinking_config (thinking_level +
    include_thoughts). (2.5 and earlier do NOT accept thinking_level → separate test.)"""
    monkeypatch.setattr(gemini_provider, "_resolve_gemini_model", lambda s: "gemini-3-flash")
    models = _fake_client(monkeypatch, chunks=[_FakeChunk("ok")])
    asyncio.run(
        _drain(
            gemini_provider.stream_user_chat(
                _settings(tmp_path), "soru", thinking_mode="high"
            )
        )
    )
    tc = models.capture["config"]["thinking_config"]
    assert tc["thinking_level"] == "HIGH"
    assert tc["include_thoughts"] is True

    # unknown value → safe middle level (MEDIUM)
    m2 = _fake_client(monkeypatch, chunks=[_FakeChunk("ok")])
    asyncio.run(
        _drain(gemini_provider.stream_user_chat(_settings(tmp_path), "x", thinking_mode="weird"))
    )
    assert m2.capture["config"]["thinking_config"]["thinking_level"] == "MEDIUM"


def test_thinking_level_omitted_for_pre_gemini_3(tmp_path, monkeypatch) -> None:
    """REGRESSION: Gemini 2.5 and earlier REJECT ``thinking_level`` (400 'Thinking
    level is not supported for this model'). EVEN IF thinking_mode is set, on 2.5/2.0
    thinking_config must NOT be sent. This is the root-cause regression test for the
    'Gemini bridge errored: {…}' 400 the user saw on gemini-2.5-flash."""
    for model in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"):
        monkeypatch.setattr(gemini_provider, "_resolve_gemini_model", lambda s, m=model: m)
        models = _fake_client(monkeypatch, chunks=[_FakeChunk("ok")])
        asyncio.run(
            _drain(gemini_provider.stream_user_chat(_settings(tmp_path), "naber", thinking_mode="normal"))
        )
        assert "thinking_config" not in models.capture["config"], model


def test_supports_thinking_level_by_model_family() -> None:
    """Only Gemini 3+ supports ``thinking_level``; 2.5/2.0/versionless → False."""
    for m in ("gemini-3-flash-preview", "gemini-3.5-flash", "models/gemini-3-pro", "gemini-4-flash"):
        assert gemini_provider._supports_thinking_level(m) is True, m
    for m in ("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash", "gemini-flash-latest", "gemini-1.5-pro"):
        assert gemini_provider._supports_thinking_level(m) is False, m


def test_thinking_config_absent_when_mode_empty(tmp_path, monkeypatch) -> None:
    """thinking_mode None/empty → no thinking_config is added (existing behavior preserved)."""
    m_none = _fake_client(monkeypatch, chunks=[_FakeChunk("ok")])
    asyncio.run(_drain(gemini_provider.stream_user_chat(_settings(tmp_path), "x")))
    assert "thinking_config" not in m_none.capture["config"]

    m_blank = _fake_client(monkeypatch, chunks=[_FakeChunk("ok")])
    asyncio.run(
        _drain(gemini_provider.stream_user_chat(_settings(tmp_path), "x", thinking_mode="  "))
    )
    assert "thinking_config" not in m_blank.capture["config"]


def test_complete_chat_function_call_loop(tmp_path, monkeypatch) -> None:
    """complete_chat: the 1st response emits a function_call → the provider dispatches,
    appends the call+result to contents, the 2nd response returns the final text. Dispatch must be called."""
    dispatched: list = []
    monkeypatch.setattr(
        gemini_provider,
        "dispatch_gemini_tool",
        lambda s, c, n, a: dispatched.append((n, a)) or "ARAÇ-SONUCU",
    )
    models = _fake_client(
        monkeypatch,
        resps=[
            _FakeResp(text="", function_calls=[_FakeFC("memory_search", {"query": "kahve"}, "t1")]),
            _FakeResp(text="Kahveyi seversin.", usage_metadata=_FakeUsage(4, 6)),
        ],
    )
    text, status, usage = asyncio.run(
        gemini_provider.complete_chat(_settings(tmp_path), "kahveyi sever miyim?")
    )
    assert text == "Kahveyi seversin."
    assert status == "finished"
    assert usage["completion_tokens"] == 6
    assert dispatched == [("memory_search", {"query": "kahve"})]
    assert models.calls == 2  # tool turn + final turn
    # the 2nd call's contents must carry the function_call + function_response parts
    last_contents = models.contents_history[-1]
    kinds = [list(c["parts"][0].keys())[0] for c in last_contents]
    assert "function_call" in kinds and "function_response" in kinds


def test_stream_user_chat_function_call_loop(tmp_path, monkeypatch) -> None:
    """stream: the 1st turn function_call (emits no text) → dispatch → the 2nd turn streams text.
    The intermediate tool text does not mix into the final reply; only the final turn's deltas are emitted."""
    dispatched: list = []
    monkeypatch.setattr(
        gemini_provider,
        "dispatch_gemini_tool",
        lambda s, c, n, a: dispatched.append((n, a)) or "ARAÇ-SONUCU",
    )
    models = _fake_client(
        monkeypatch,
        chunk_rounds=[
            [_FakeChunk(function_calls=[_FakeFC("save_memory", {"text": "kahve sever"}, "t1")])],
            [_FakeChunk("Not"), _FakeChunk(" eklendi.", usage_metadata=_FakeUsage(2, 3))],
        ],
    )

    async def run():
        return [
            ev
            async for ev in gemini_provider.stream_user_chat(
                _settings(tmp_path), "bunu hatırla"
            )
        ]

    events = asyncio.run(run())
    assert "".join(e["delta"] for e in events if "delta" in e) == "Not eklendi."
    assert dispatched == [("save_memory", {"text": "kahve sever"})]
    assert models.calls == 2
    done = [e for e in events if e.get("done")]
    assert done and done[-1]["usage"]["completion_tokens"] == 3


def test_stream_emits_tool_call_start_end_events_and_usage(tmp_path, monkeypatch) -> None:
    """PARITY (Missing B): the tool turn emits start+end ``tool_call`` wire events (ollama/claude
    shape) → UI card + audit ledger. start carries args/result empty; end carries result/args empty;
    both share the SAME id (persist dedup). done.usage.tool_calls carries these records (old state was always []).
    """
    monkeypatch.setattr(
        gemini_provider, "dispatch_gemini_tool", lambda s, c, n, a: "ARAÇ-SONUCU"
    )
    models = _fake_client(
        monkeypatch,
        chunk_rounds=[
            [_FakeChunk(function_calls=[_FakeFC("save_memory", {"text": "kahve sever"}, "t1")])],
            [_FakeChunk("Tamam.", usage_metadata=_FakeUsage(2, 3))],
        ],
    )

    async def run():
        return [
            ev async for ev in gemini_provider.stream_user_chat(_settings(tmp_path), "bunu hatırla")
        ]

    events = asyncio.run(run())
    tcs = [e["tool_call"] for e in events if "tool_call" in e]
    assert len(tcs) == 2  # single tool call → start + end
    start, end = tcs
    assert start["phase"] == "start" and start["name"] == "save_memory"
    assert start["args"] == {"text": "kahve sever"} and start["result"] is None
    assert end["phase"] == "end" and end["result"] == "ARAÇ-SONUCU" and end["status"] == "ok"
    assert start["id"] == end["id"] == "t1"  # FunctionCall.id → same id (persist dedup)
    done = [e for e in events if e.get("done")][-1]
    assert done["usage"]["tool_calls"] == tcs


def test_stream_tool_call_synthesizes_id_when_missing(tmp_path, monkeypatch) -> None:
    """A FunctionCall WITHOUT an id → start/end are MATCHED via an index-based synthetic id
    (``gemini-tool-<idx>``), so the persist pair can still be merged."""
    monkeypatch.setattr(gemini_provider, "dispatch_gemini_tool", lambda s, c, n, a: "R")
    models = _fake_client(
        monkeypatch,
        chunk_rounds=[
            [_FakeChunk(function_calls=[_FakeFC("memory_search", {"q": "x"})])],  # fid=None
            [_FakeChunk("ok")],
        ],
    )

    async def run():
        return [ev async for ev in gemini_provider.stream_user_chat(_settings(tmp_path), "x")]

    events = asyncio.run(run())
    tcs = [e["tool_call"] for e in events if "tool_call" in e]
    assert tcs[0]["id"] == tcs[1]["id"] == "gemini-tool-0"


def test_complete_chat_populates_usage_tool_calls(tmp_path, monkeypatch) -> None:
    """complete_chat populates usage.tool_calls too (parity with stream → voice/non-stream
    audit path). start+end records are produced for each tool called."""
    monkeypatch.setattr(gemini_provider, "dispatch_gemini_tool", lambda s, c, n, a: "HAFIZA")
    models = _fake_client(
        monkeypatch,
        resps=[
            _FakeResp(text="", function_calls=[_FakeFC("memory_search", {"query": "kahve"}, "t9")]),
            _FakeResp(text="son", usage_metadata=_FakeUsage(1, 1)),
        ],
    )
    _text, _status, usage = asyncio.run(
        gemini_provider.complete_chat(_settings(tmp_path), "kahve?")
    )
    tcs = usage["tool_calls"]
    assert len(tcs) == 2  # start + end
    assert tcs[0]["phase"] == "start" and tcs[0]["args"] == {"query": "kahve"}
    assert tcs[1]["phase"] == "end" and tcs[1]["result"] == "HAFIZA"
    assert tcs[0]["id"] == tcs[1]["id"] == "t9"


def test_complete_chat_tool_cap_falls_back_to_no_tools_final(tmp_path, monkeypatch) -> None:
    """REGRESSION (a): if the model calls a tool on EVERY turn (``_MAX_TOOL_ROUNDS`` exhausts)
    the old behavior returned EMPTY text (``""``) → the user saw an empty bubble. Now after the
    loop a FINAL toolless (``tools``-free config) ``generate_content`` is made → the model must
    answer with text. Call count: _MAX_TOOL_ROUNDS + 1 (final)."""
    monkeypatch.setattr(gemini_provider, "dispatch_gemini_tool", lambda s, c, n, a: "OK")
    rounds = gemini_provider._MAX_TOOL_ROUNDS
    # The first _MAX_TOOL_ROUNDS responses ALWAYS call a tool (no text); for the final
    # (toolless) call the next item = plain text. Since _FakeModels stays on the last item we
    # provide exactly rounds+1 items (the final call consumes the last text item).
    tool_resps = [
        _FakeResp(text="", function_calls=[_FakeFC("memory_search", {"q": str(i)}, f"t{i}")])
        for i in range(rounds)
    ]
    final_text = _FakeResp(text="Sonunda metinle yanıt.", usage_metadata=_FakeUsage(1, 2))
    models = _fake_client(monkeypatch, resps=[*tool_resps, final_text])
    text, status, usage = asyncio.run(
        gemini_provider.complete_chat(_settings(tmp_path), "döngüye sok")
    )
    assert text == "Sonunda metinle yanıt."  # NOT empty — toolless final completion
    assert status == "finished"
    assert models.calls == rounds + 1  # cap turns + toolless final turn
    # The final call's config must not carry ``tools`` (the model must not be able to call one).
    assert "tools" not in models.capture["config"]
    assert usage["completion_tokens"] == 2  # the final turn's tokens are counted too


def test_complete_chat_accumulates_usage_across_rounds(tmp_path, monkeypatch) -> None:
    """REGRESSION (b): because ``usage_metadata`` was OVERWRITTEN each turn, the tokens of
    the intermediate tool turns were lost. Now prompt+completion are SUMMED across all turns
    (each API call is billed separately → summing is correct). 1st turn (tool) 10/20, 2nd turn
    (final text) 4/6 → total 14/26."""
    monkeypatch.setattr(gemini_provider, "dispatch_gemini_tool", lambda s, c, n, a: "OK")
    models = _fake_client(
        monkeypatch,
        resps=[
            _FakeResp(
                text="",
                function_calls=[_FakeFC("memory_search", {"query": "x"}, "t1")],
                usage_metadata=_FakeUsage(10, 20),  # the intermediate tool turn's tokens
            ),
            _FakeResp(text="bitti", usage_metadata=_FakeUsage(4, 6)),
        ],
    )
    _text, _status, usage = asyncio.run(
        gemini_provider.complete_chat(_settings(tmp_path), "kahve?")
    )
    assert models.calls == 2
    assert usage["prompt_tokens"] == 14  # 10 + 4 (the intermediate turn is NOT dropped)
    assert usage["completion_tokens"] == 26  # 20 + 6


def test_stream_user_chat_tool_cap_falls_back_to_no_tools_final(tmp_path, monkeypatch) -> None:
    """REGRESSION (a) STREAMING: if the model calls a tool on EVERY turn the accumulated text
    deltas are empty → the old behavior emitted no text (empty bubble). Now a FINAL toolless
    (``tools``-free) stream is made → text deltas are emitted. Streaming tokens are summed too."""
    monkeypatch.setattr(gemini_provider, "dispatch_gemini_tool", lambda s, c, n, a: "OK")
    rounds = gemini_provider._MAX_TOOL_ROUNDS
    tool_rounds = [
        [_FakeChunk(function_calls=[_FakeFC("save_memory", {"i": str(i)}, f"t{i}")])]
        for i in range(rounds)
    ]
    final_round = [_FakeChunk("Araçsız "), _FakeChunk("son metin.", usage_metadata=_FakeUsage(1, 5))]

    async def run():
        return [
            ev
            async for ev in gemini_provider.stream_user_chat(
                _settings(tmp_path), "döngüye sok"
            )
        ]

    models = _fake_client(monkeypatch, chunk_rounds=[*tool_rounds, final_round])
    events = asyncio.run(run())
    assert "".join(e["delta"] for e in events if "delta" in e) == "Araçsız son metin."
    assert models.calls == rounds + 1  # cap turns + toolless final stream
    assert "tools" not in models.capture["config"]  # the final stream is toolless
    done = [e for e in events if e.get("done")]
    assert done and done[-1]["usage"]["completion_tokens"] == 5


def test_stream_user_chat_accumulates_usage_across_rounds(tmp_path, monkeypatch) -> None:
    """REGRESSION (b) STREAMING: usage was overwritten each turn → the intermediate tool turn's
    tokens were lost. Now all turns are SUMMED. 1st turn (tool) 7/11, 2nd turn (text)
    2/3 → total 9/14."""
    monkeypatch.setattr(gemini_provider, "dispatch_gemini_tool", lambda s, c, n, a: "OK")
    models = _fake_client(
        monkeypatch,
        chunk_rounds=[
            [_FakeChunk(function_calls=[_FakeFC("save_memory", {"t": "x"}, "t1")],
                        usage_metadata=_FakeUsage(7, 11))],
            [_FakeChunk("tamam", usage_metadata=_FakeUsage(2, 3))],
        ],
    )

    async def run():
        return [
            ev
            async for ev in gemini_provider.stream_user_chat(_settings(tmp_path), "x")
        ]

    events = asyncio.run(run())
    assert models.calls == 2
    done = [e for e in events if e.get("done")]
    assert done and done[-1]["usage"]["prompt_tokens"] == 9  # 7 + 2
    assert done[-1]["usage"]["completion_tokens"] == 14  # 11 + 3


def test_fc_call_content_wraps_real_parts_preserving_signature() -> None:
    """Real SDK parts → wrapped AS-IS with ``types.Content``; the model's
    ``thought_signature`` is preserved exactly (Google's recommended pattern). If the
    signature is not preserved the Gemini 3 re-call gives 400 INVALID_ARGUMENT."""
    types = pytest.importorskip("google.genai.types")
    part = types.Part(
        function_call=types.FunctionCall(name="memory_search", args={"query": "x"}),
        thought_signature=b"SIGNATURE-BYTES",
    )
    turn = gemini_provider._fc_call_content([part.function_call], [part])
    assert isinstance(turn, types.Content)
    assert turn.role == "model"
    assert turn.parts[0].function_call.name == "memory_search"
    assert turn.parts[0].thought_signature == b"SIGNATURE-BYTES"


def test_fc_call_content_dict_fallback_keeps_signature(monkeypatch) -> None:
    """If SDK ``types.Content`` cannot be built (no SDK / coerce error) it falls to a plain
    dict BUT ``thought_signature`` is still copied → the signature is lost on no path."""
    gtypes = pytest.importorskip("google.genai.types")

    def _boom(*a, **k):
        raise RuntimeError("simulated SDK-absent / coerce failure")

    monkeypatch.setattr(gtypes, "Content", _boom)
    fc = _FakeFC("memory_search", {"query": "kahve"}, "t1")
    part = _FakePart(function_call=fc, thought_signature=b"SIGX")
    turn = gemini_provider._fc_call_content([fc], [part])
    assert isinstance(turn, dict) and turn["role"] == "model"
    p0 = turn["parts"][0]
    assert p0["function_call"]["name"] == "memory_search"
    assert p0["thought_signature"] == b"SIGX"


def test_fc_call_content_no_parts_reconstructs_from_calls() -> None:
    """If there are no raw parts at all (a client that only gives the ``function_calls``
    property) it is built from fcs; no signature field is ADDED (there is no signature on that path)."""
    fc = _FakeFC("save_memory", {"text": "x"}, "t1")
    turn = gemini_provider._fc_call_content([fc], [])
    assert turn["role"] == "model"
    assert turn["parts"][0]["function_call"]["name"] == "save_memory"
    assert "thought_signature" not in turn["parts"][0]


def test_fc_call_content_reconciles_property_only_leftover_calls(monkeypatch) -> None:
    """RECONCILIATION: if ``fcs`` is LONGER than ``parts`` (some call came only from the
    ``function_calls`` property → no raw part) the extra calls must also be added to the model
    turn; otherwise the call/response count mismatches → the next request 400s. ``types.Content``
    is patched to drive the dict-fallback path (deterministic with/without the SDK)."""
    gtypes = pytest.importorskip("google.genai.types")
    monkeypatch.setattr(gtypes, "Content", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("force dict")))
    fc0 = _FakeFC("memory_search", {"query": "x"}, "t0")  # from a raw part (signed)
    fc1 = _FakeFC("save_memory", {"text": "y"}, "t1")  # ONLY from the property (no part)
    part0 = _FakePart(function_call=fc0, thought_signature=b"SIG0")
    turn = gemini_provider._fc_call_content([fc0, fc1], [part0])
    names = [p["function_call"]["name"] for p in turn["parts"]]
    assert names == ["memory_search", "save_memory"]  # the leftover was added too
    assert turn["parts"][0]["thought_signature"] == b"SIG0"
    assert "thought_signature" not in turn["parts"][1]  # property-only → unsigned


def test_fc_loop_threads_thought_signature_into_recall(tmp_path, monkeypatch) -> None:
    """complete_chat FC loop: the model's SIGNED function_call part (via candidates) must be
    re-added to the re-call contents with its signature. This is the root-cause regression test
    for the 'Gemini bridge errored: {…}' 400 the user saw."""
    types = pytest.importorskip("google.genai.types")
    monkeypatch.setattr(gemini_provider, "dispatch_gemini_tool", lambda s, c, n, a: "OK")
    part = types.Part(
        function_call=types.FunctionCall(name="memory_search", args={"query": "kahve"}),
        thought_signature=b"SIGX",
    )
    r1 = _FakeResp(text="", function_calls=None)
    r1.candidates = [SimpleNamespace(content=SimpleNamespace(parts=[part]))]
    r2 = _FakeResp(text="son cevap", usage_metadata=_FakeUsage(1, 1))
    models = _fake_client(monkeypatch, resps=[r1, r2])
    text, status, _ = asyncio.run(
        gemini_provider.complete_chat(_settings(tmp_path), "kahve sever miyim?")
    )
    assert text == "son cevap" and status == "finished"
    assert models.calls == 2
    # The RAW contents going to the re-call (capture) must carry a signed model turn (Content or
    # dict — either, depending on SDK version; accept both).
    sigs: list = []
    for c in models.capture["contents"]:
        parts = c.parts if hasattr(c, "parts") else (c.get("parts") if isinstance(c, dict) else [])
        for p in parts or []:
            sig = p.get("thought_signature") if isinstance(p, dict) else getattr(p, "thought_signature", None)
            if sig is not None:
                sigs.append(sig)
    assert b"SIGX" in sigs


def test_thinking_mode_turkish_levels_mapped(tmp_path, monkeypatch) -> None:
    """chat_producer sends Turkish level names (hizli/normal/derin/yogun/azami/ultra);
    previously they all fell to MEDIUM (no match) → the user's choice was ignored.
    Now they map directly to thinking_level ("ultra" is claude-only; on gemini it
    just tops out at the existing HIGH tier)."""
    monkeypatch.setattr(gemini_provider, "_resolve_gemini_model", lambda s: "gemini-3-flash")
    cases = [
        ("hizli", "LOW"),
        ("normal", "MEDIUM"),
        ("derin", "HIGH"),
        ("yogun", "HIGH"),
        ("azami", "HIGH"),
        ("ultra", "HIGH"),
    ]
    for mode, level in cases:
        m = _fake_client(monkeypatch, chunks=[_FakeChunk("ok")])
        asyncio.run(
            _drain(gemini_provider.stream_user_chat(_settings(tmp_path), "x", thinking_mode=mode))
        )
        assert m.capture["config"]["thinking_config"]["thinking_level"] == level, mode


def test_provider_error_maps_to_cursor_call_error(tmp_path, monkeypatch) -> None:
    """google-genai APIError duck-type (code+message) → LLMCallError (status carried)."""

    class _FakeAPIError(Exception):
        def __init__(self, code, message) -> None:
            super().__init__(message)
            self.code = code
            self.message = message

    _fake_client(monkeypatch, error=_FakeAPIError(429, "rate limited"))
    with pytest.raises(LLMCallError) as ei:
        asyncio.run(_drain(gemini_provider.stream_user_chat(_settings(tmp_path), "x")))
    assert ei.value.status_code == 429  # 400–599 → HTTP status is carried


def test_resolve_gemini_model_ignores_cursor_tag(tmp_path, monkeypatch) -> None:
    """chat_producer passes the cursor tag to ALL providers; gemini IGNORES it →
    the model sent to Google is the native gemini_model (gemini-2.5-flash), NOT the cursor tag.
    (A stricter version of ollama's 'name:tag' guard; since gemini names collide
    syntactically with cursor aliases, the inbound model is NEVER honored.)"""
    models = _fake_client(monkeypatch, chunks=[_FakeChunk("ok")])
    asyncio.run(
        _drain(
            gemini_provider.stream_user_chat(
                _settings(tmp_path), "selam", model="composer-2"
            )
        )
    )
    assert models.capture["model"] == "gemini-2.5-flash"  # the cursor tag does not leak


def test_client_unavailable_raises_clear_error(tmp_path, monkeypatch) -> None:
    """make_client None → a CLEAR LLMCallError by cause (no raw ImportError/None):
    install hint if no SDK, Settings hint if no key. Both are 503."""
    monkeypatch.setattr(gemini_provider, "make_client", lambda settings, **_kw: None)

    # 1) SDK not installed
    monkeypatch.setattr(gemini_provider, "genai_installed", lambda: False)
    with pytest.raises(LLMCallError) as ei_sdk:
        asyncio.run(_drain(gemini_provider.stream_user_chat(_settings(tmp_path), "x")))
    assert ei_sdk.value.status_code == 503
    assert "google-genai" in str(ei_sdk.value)

    # 2) SDK present but no key
    monkeypatch.setattr(gemini_provider, "genai_installed", lambda: True)
    with pytest.raises(LLMCallError) as ei_key:
        asyncio.run(_drain(gemini_provider.stream_user_chat(_settings(tmp_path), "x")))
    assert ei_key.value.status_code == 503
    assert "api key" in str(ei_key.value).lower()


def test_cursor_client_routes_stream_to_gemini(tmp_path, monkeypatch) -> None:
    """provider=gemini → llm_dispatch.stream_user_chat delegates to gemini_provider."""
    monkeypatch.setattr(llm_dispatch, "_active_provider", lambda s: "gemini")

    async def fake(settings, user_text, **_kw):
        yield {"delta": "ok", "done": False}
        yield {"done": True, "usage": {}}

    monkeypatch.setattr(gemini_provider, "stream_user_chat", fake)

    async def run():
        return [
            ev async for ev in llm_dispatch.stream_user_chat(_settings(tmp_path), "hi")
        ]

    events = asyncio.run(run())
    assert any(e.get("delta") == "ok" for e in events)


def test_cursor_client_routes_complete_to_gemini(tmp_path, monkeypatch) -> None:
    """provider=gemini → llm_dispatch.complete_chat delegates to gemini_provider."""
    monkeypatch.setattr(llm_dispatch, "_active_provider", lambda s: "gemini")

    async def fake_complete(settings, user_text, **_kw):
        return "gemini yanıtı", "finished", {"tool_calls": []}

    monkeypatch.setattr(gemini_provider, "complete_chat", fake_complete)
    result = asyncio.run(llm_dispatch.complete_chat(_settings(tmp_path), "hi"))
    assert result.text == "gemini yanıtı"
    assert result.status == "finished"


# --- Multimodal: NATIVE image input (file_ids → inline_data) ----------------


class _FakeUploadRec:
    def __init__(self, *, kind="image", disabled=False, media="image/png") -> None:
        self.kind = kind
        self.disabled = disabled
        self._media = media

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    @property
    def media_type(self) -> str:
        return self._media


def _fake_store(monkeypatch, tmp_path, records: dict):
    """Replace ``UploadStore`` with a fake: id→(rec, bytes). file_path is a real tmp."""
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


def test_stream_attaches_image_inline_data(tmp_path, monkeypatch) -> None:
    """file_ids → an ``inline_data`` part on the last user turn (the model actually sees the image)."""
    _fake_store(monkeypatch, tmp_path, {"f1": (_FakeUploadRec(), b"PNGDATA")})
    models = _fake_client(monkeypatch, chunks=[_FakeChunk("gördüm")])
    asyncio.run(
        _drain(
            gemini_provider.stream_user_chat(
                _settings(tmp_path), "bu ne?", file_ids=["f1"]
            )
        )
    )
    parts = models.capture["contents"][-1]["parts"]
    assert parts[0]["text"] == "bu ne?"  # the text is preserved
    inline = [p for p in parts if "inline_data" in p]
    assert len(inline) == 1
    assert inline[0]["inline_data"]["mime_type"] == "image/png"
    assert inline[0]["inline_data"]["data"] == b"PNGDATA"


def test_image_loading_skips_non_image_disabled_missing(tmp_path, monkeypatch) -> None:
    """A non-image / disabled / missing attachment is skipped; the turn is not broken."""
    _fake_store(
        monkeypatch,
        tmp_path,
        {
            "img": (_FakeUploadRec(), b"OK"),
            "pdf": (_FakeUploadRec(kind="pdf"), b"PDF"),  # not an image → skip
            "off": (_FakeUploadRec(disabled=True), b"X"),  # disabled → skip
        },
    )
    models = _fake_client(monkeypatch, chunks=[_FakeChunk("ok")])
    asyncio.run(
        _drain(
            gemini_provider.stream_user_chat(
                _settings(tmp_path), "?", file_ids=["img", "pdf", "off", "yok"]
            )
        )
    )
    inline = [p for p in models.capture["contents"][-1]["parts"] if "inline_data" in p]
    assert len(inline) == 1  # only the valid image
    assert inline[0]["inline_data"]["data"] == b"OK"


def test_no_file_ids_is_text_only(tmp_path, monkeypatch) -> None:
    """With no file_ids, contents are byte-for-byte the old behavior (only a text part)."""
    models = _fake_client(monkeypatch, chunks=[_FakeChunk("ok")])
    asyncio.run(_drain(gemini_provider.stream_user_chat(_settings(tmp_path), "selam")))
    parts = models.capture["contents"][-1]["parts"]
    assert parts == [{"text": "selam"}]  # NO inline_data


def test_stream_attaches_pdf_inline_data(tmp_path, monkeypatch) -> None:
    """CONTRACT: Gemini natively supports PDF too (like images). A PDF file_id → an
    ``inline_data`` part on the last user turn (mime_type "application/pdf"); even if is_image
    is False it is embedded because media_type is "application/pdf" (UploadRecord has no is_pdf,
    the distinction is via media_type)."""
    _fake_store(
        monkeypatch,
        tmp_path,
        {"p1": (_FakeUploadRec(kind="pdf", media="application/pdf"), b"%PDF-1.7\n...")},
    )
    models = _fake_client(monkeypatch, chunks=[_FakeChunk("okudum")])
    asyncio.run(
        _drain(
            gemini_provider.stream_user_chat(
                _settings(tmp_path), "bu belge ne?", file_ids=["p1"]
            )
        )
    )
    parts = models.capture["contents"][-1]["parts"]
    assert parts[0]["text"] == "bu belge ne?"  # the text is preserved
    inline = [p for p in parts if "inline_data" in p]
    assert len(inline) == 1
    assert inline[0]["inline_data"]["mime_type"] == "application/pdf"
    assert inline[0]["inline_data"]["data"] == b"%PDF-1.7\n..."


def test_inline_data_skips_files_over_size_budget(tmp_path, monkeypatch) -> None:
    """An attachment EXCEEDING the cumulative ``inline_data`` size budget is skipped — better UX
    to not send that attachment than to break the turn with Google's 400 'request too large'.
    We shrink the budget to verify the behavior (no need to write a real 18MB blob)."""
    monkeypatch.setattr(gemini_provider, "_MAX_INLINE_TOTAL_BYTES", 8)
    _fake_store(
        monkeypatch,
        tmp_path,
        {
            "small": (_FakeUploadRec(), b"PNG"),  # 3 bytes → fits the budget
            "big": (_FakeUploadRec(), b"X" * 20),  # 3+20 > 8 → skipped
        },
    )
    models = _fake_client(monkeypatch, chunks=[_FakeChunk("ok")])
    asyncio.run(
        _drain(
            gemini_provider.stream_user_chat(
                _settings(tmp_path), "?", file_ids=["small", "big"]
            )
        )
    )
    inline = [p for p in models.capture["contents"][-1]["parts"] if "inline_data" in p]
    assert len(inline) == 1  # only the small attachment; the big one was skipped due to budget
    assert inline[0]["inline_data"]["data"] == b"PNG"


# --- MCP bridge: external tools join Gemini function_declarations + dispatch is routed --


class _FakeBridge:
    """``McpToolBridge`` duck-type: decls + handles + async dispatch + async ctx mgr.

    ``handles`` is True for names carrying the ``mcp__`` prefix (the real bridge's namespace);
    ``dispatch`` accumulates calls → the test verifies whether the call went to the bridge or
    to native. By patching ``external_mcp_bridge`` the bridged scenario is driven even in an
    unpatched (yaml-free) environment."""

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


def test_bridge_decls_convert_into_function_declarations(tmp_path, monkeypatch) -> None:
    """Bridge tools (``bridge.decls``, OpenAI shape) are CONVERTED into Gemini
    ``function_declarations``: the outer wrapper (``type``/``function``) is stripped, the inner
    body (name/description/parameters) is joined flat; it surfaces alongside the built-in
    memory_search/save_memory."""
    bridge = _FakeBridge([_MCP_DECL])
    monkeypatch.setattr(gemini_provider, "external_mcp_bridge", lambda s: bridge)
    models = _fake_client(monkeypatch, chunks=[_FakeChunk("ok")])
    asyncio.run(_drain(gemini_provider.stream_user_chat(_settings(tmp_path), "x")))
    decls = models.capture["config"]["tools"][0]["function_declarations"]
    names = [d["name"] for d in decls]
    assert "memory_search" in names and "save_memory" in names
    assert "mcp__fs__read_file" in names  # the bridge decl is on the surface too
    mcp = next(d for d in decls if d["name"] == "mcp__fs__read_file")
    assert "function" not in mcp and "type" not in mcp  # the OpenAI wrapper was stripped
    assert mcp["parameters"]["properties"]["path"]["type"] == "string"  # the schema was preserved


def test_bridge_tool_call_routes_to_bridge_dispatch(tmp_path, monkeypatch) -> None:
    """A FunctionCall named ``mcp__…`` is routed to bridge.dispatch (NOT native
    ``dispatch_gemini_tool``); the bridge result flows into the end ``tool_call`` event's result."""
    bridge = _FakeBridge([_MCP_DECL], result="MCP-OKUNDU")
    monkeypatch.setattr(gemini_provider, "external_mcp_bridge", lambda s: bridge)
    native: list = []
    monkeypatch.setattr(
        gemini_provider,
        "dispatch_gemini_tool",
        lambda s, c, n, a: native.append((n, a)) or "NATIVE",
    )
    _fake_client(
        monkeypatch,
        chunk_rounds=[
            [_FakeChunk(function_calls=[_FakeFC("mcp__fs__read_file", {"path": "/notlar"}, "t1")])],
            [_FakeChunk("Okudum.", usage_metadata=_FakeUsage(2, 3))],
        ],
    )

    async def run():
        return [
            ev
            async for ev in gemini_provider.stream_user_chat(_settings(tmp_path), "dosyayı oku")
        ]

    events = asyncio.run(run())
    assert bridge.calls == [("mcp__fs__read_file", {"path": "/notlar"})]  # went to the bridge
    assert native == []  # native dispatch was NOT called
    tcs = [e["tool_call"] for e in events if "tool_call" in e]
    end = next(t for t in tcs if t["phase"] == "end")
    assert end["result"] == "MCP-OKUNDU"
    assert "".join(e["delta"] for e in events if "delta" in e) == "Okudum."


def test_complete_chat_routes_mcp_tool_to_bridge(tmp_path, monkeypatch) -> None:
    """complete_chat (non-stream, voice/audit path) also routes the ``mcp__…`` call to the bridge;
    the result flows into the usage.tool_calls end record (parity with stream)."""
    bridge = _FakeBridge([_MCP_DECL], result="MCP-VERI")
    monkeypatch.setattr(gemini_provider, "external_mcp_bridge", lambda s: bridge)
    native: list = []
    monkeypatch.setattr(
        gemini_provider,
        "dispatch_gemini_tool",
        lambda s, c, n, a: native.append((n, a)) or "NATIVE",
    )
    _fake_client(
        monkeypatch,
        resps=[
            _FakeResp(text="", function_calls=[_FakeFC("mcp__fs__read_file", {"path": "/x"}, "t9")]),
            _FakeResp(text="bitti", usage_metadata=_FakeUsage(2, 3)),
        ],
    )
    _text, _status, usage = asyncio.run(
        gemini_provider.complete_chat(_settings(tmp_path), "oku")
    )
    assert bridge.calls == [("mcp__fs__read_file", {"path": "/x"})]  # went to the bridge
    assert native == []  # native dispatch was NOT called
    tcs = usage["tool_calls"]
    end = next(t for t in tcs if t["phase"] == "end")
    assert end["name"] == "mcp__fs__read_file" and end["result"] == "MCP-VERI"


def test_gemini_decls_from_bridge_skips_malformed() -> None:
    """DEFENSIVE: ``_gemini_decls_from_bridge`` converts only a valid entry (dict + a named
    ``function``); malformed entries (not-a-dict / no-function / unnamed) are skipped →
    a single malformed bridged server does not break the config. A None input falls to an empty list."""
    good = {"type": "function", "function": {"name": "mcp__x__t", "parameters": {}}}
    out = gemini_provider._gemini_decls_from_bridge(
        [good, "not-a-dict", {"no_function": 1}, {"function": {"name": ""}}, {"function": "x"}]
    )
    assert out == [{"name": "mcp__x__t", "parameters": {}}]
    assert gemini_provider._gemini_decls_from_bridge(None) == []


# --- "Thinking" (include_thoughts): thought parts SEPARATE wire stream, NOT in the answer ---


class _FakePartText:
    """Fake SDK ``types.Part`` carrying TEXT (+ optional ``thought`` flag).

    With include_thoughts the model's thinking summary arrives as parts whose ``thought``
    is True; the answer is in the non-thought parts. ``function_call`` is None so the
    function-call scan (``_fc_signature_parts``) skips these (they are text parts)."""

    def __init__(self, text, thought=False) -> None:
        self.text = text
        self.thought = thought
        self.function_call = None


def _parts_obj(*parts, usage_metadata=None, text="MERGED-SENTINEL", function_calls=None):
    """A chunk/response twin exposing ``candidates[0].content.parts``.

    ``text`` is a SENTINEL (the ``.text`` shortcut would MERGE thought+answer): a correct
    implementation must read the parts and ignore this, so the sentinel never leaks."""
    content = SimpleNamespace(parts=list(parts))
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=content)],
        text=text,
        usage_metadata=usage_metadata,
        function_calls=function_calls,
    )


def test_stream_separates_thought_parts_from_answer(tmp_path, monkeypatch) -> None:
    """PARITY (Missing C): parts carrying ``part.thought`` become a SEPARATE ``thinking`` wire
    stream (delta + completed) and do NOT leak into the ANSWER. ``chunk.text`` merges the two →
    the thought sentinel must NOT APPEAR in the answer; thinking 'completed' must come BEFORE the answer delta."""
    _fake_client(
        monkeypatch,
        chunks=[
            _parts_obj(_FakePartText("düşünüyorum...", thought=True)),
            _parts_obj(_FakePartText("Cevap bu.")),
            _FakeChunk(None, usage_metadata=_FakeUsage(3, 5)),
        ],
    )

    async def run():
        return [
            ev
            async for ev in gemini_provider.stream_user_chat(
                _settings(tmp_path), "selam", thinking_mode="medium"
            )
        ]

    events = asyncio.run(run())
    thinking = [e["thinking"] for e in events if "thinking" in e]
    deltas = [t for t in thinking if t["phase"] == "delta"]
    assert "".join(t["text"] for t in deltas) == "düşünüyorum..."
    assert any(t["phase"] == "completed" for t in thinking)
    answer = "".join(e["delta"] for e in events if "delta" in e)
    assert answer == "Cevap bu."  # thought EXCLUDED
    assert "düşünüyorum" not in answer and "MERGED-SENTINEL" not in answer
    # ordering: thinking 'completed' comes before the answer delta
    completed_idx = next(i for i, e in enumerate(events) if e.get("thinking", {}).get("phase") == "completed")
    first_delta_idx = next(i for i, e in enumerate(events) if "delta" in e)
    assert completed_idx < first_delta_idx


def test_stream_emits_thinking_in_intermediate_tool_round(tmp_path, monkeypatch) -> None:
    """Thinking surfaces on EVERY turn — including the intermediate-turn reasoning that comes
    BEFORE the tool call (openai parity). Turn 1: thought + tool call (no answer text); turn 2: answer."""
    monkeypatch.setattr(gemini_provider, "dispatch_gemini_tool", lambda s, c, n, a: "OK")
    _fake_client(
        monkeypatch,
        chunk_rounds=[
            [
                _parts_obj(_FakePartText("önce arama yapmalıyım", thought=True)),
                _FakeChunk(function_calls=[_FakeFC("memory_search", {"q": "x"}, "t1")]),
            ],
            [_parts_obj(_FakePartText("Buldum."), usage_metadata=_FakeUsage(2, 3))],
        ],
    )

    async def run():
        return [
            ev async for ev in gemini_provider.stream_user_chat(_settings(tmp_path), "ara")
        ]

    events = asyncio.run(run())
    thinking = [e["thinking"] for e in events if "thinking" in e]
    assert any(t["phase"] == "delta" and t["text"] == "önce arama yapmalıyım" for t in thinking)
    assert any(t["phase"] == "completed" for t in thinking)
    answer = "".join(e["delta"] for e in events if "delta" in e)
    assert answer == "Buldum." and "önce arama" not in answer  # the intermediate-turn thought does not leak into the answer


def test_complete_chat_excludes_thought_from_answer(tmp_path, monkeypatch) -> None:
    """complete_chat (non-stream): there is NO separate thinking channel → the thought part is
    EXCLUDED from the answer (otherwise the reasoning would spill into the answer). Only non-thought parts are the answer."""
    _fake_client(
        monkeypatch,
        resp=_parts_obj(
            _FakePartText("iç muhakeme", thought=True),
            _FakePartText("Nihai cevap."),
            usage_metadata=_FakeUsage(2, 3),
        ),
    )
    text, status, _usage = asyncio.run(
        gemini_provider.complete_chat(_settings(tmp_path), "selam", thinking_mode="medium")
    )
    assert text == "Nihai cevap."  # thought EXCLUDED
    assert "iç muhakeme" not in text and "MERGED-SENTINEL" not in text
    assert status == "finished"


def test_chunk_answer_thought_falls_back_to_text_when_no_parts() -> None:
    """DEFENSIVE: a chunk with no parts surface (old shape / plain fake) falls to the ``.text``
    shortcut (answer-only, no thought) → Phase-1 behavior is preserved. If parts are present,
    splitting is done and the thought is removed from the answer."""
    no_parts = SimpleNamespace(text="merhaba")  # no candidates
    assert gemini_provider._chunk_answer_thought(no_parts) == ("merhaba", "")
    with_parts = _parts_obj(
        _FakePartText("düşünce", thought=True), _FakePartText("cevap"), text="düşüncecevap"
    )
    assert gemini_provider._chunk_answer_thought(with_parts) == ("cevap", "düşünce")
    # _response_text also excludes the thought (non-stream path).
    assert gemini_provider._response_text(with_parts) == "cevap"
