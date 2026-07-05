"""chat_titler.maybe_title_conversation — LLM-summarized chat titles (background).

Hermetic (tmp_path, real ``Memory``/``ConversationService``); the LLM call is stubbed via
``complete_chat_with_usage``. Covers: (a) a returned title is set + ``llm_titled`` stamped +
``conversation_updated`` broadcast; (b) SKIP over a manual title; (c) idempotent (second
call → no LLM call, no change); (d) blank/failed LLM result keeps the truncation title.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from akana.memory import Memory
from akana_server.conversation_service import ConversationService
from akana_server.orchestrator import chat_titler


class _StubHub:
    """Captures ``broadcast_json`` payloads (stands in for EventHub)."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def broadcast_json(self, data: dict) -> None:
        self.events.append(data)


def _settings(tmp_path: Path) -> SimpleNamespace:
    # Duck-typed settings: the titler only reads ``.data_dir``. ``get_runtime`` raises
    # KeyError for the unknown ``llm_chat_titles`` key (no schema spec yet) → the titler's
    # defensive read treats that as the default (on). complete_chat_with_usage is stubbed.
    return SimpleNamespace(data_dir=tmp_path)


def _wire_llm(monkeypatch: pytest.MonkeyPatch, reply: str) -> list[str]:
    """Stub the one-shot LLM call; return the list of prompts it received."""
    prompts: list[str] = []

    async def fake_complete(settings, prompt, **kwargs):
        prompts.append(prompt)
        # chat_mode=False / reuse_agent=False is the stateless one-shot path.
        assert kwargs.get("chat_mode") is False
        assert kwargs.get("reuse_agent") is False
        return reply, {"prompt_tokens": 1}

    monkeypatch.setattr(chat_titler.llm_dispatch, "complete_chat_with_usage", fake_complete)
    return prompts


def _seed_untitled_with_truncation(mem: Memory, cid: str, first_text: str) -> None:
    """A fresh conversation with the existing TRUNCATION auto-title (untitled → auto)."""
    mem.conversations_meta.on_user_message(cid, first_text)


def test_sets_llm_title_and_broadcasts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mem = Memory.for_data_dir(tmp_path)
    cid = "conv-title-1"
    _seed_untitled_with_truncation(mem, cid, "espresso makinesi bugün bozuldu ne yapmalıyım")
    # Pre-condition: truncation title present, source auto, not yet llm-titled.
    meta0 = mem.conversations_meta.get(cid)
    assert meta0 is not None and meta0.title  # truncation title exists
    assert (meta0.json_metadata or {}).get("llm_titled") is None

    prompts = _wire_llm(monkeypatch, '"Espresso Makinesi Arızası"\n')
    hub = _StubHub()

    async def run() -> None:
        await chat_titler.maybe_title_conversation(
            settings=_settings(tmp_path),
            hub=hub,
            conversation_id=cid,
            first_user_text="espresso makinesi bugün bozuldu ne yapmalıyım",
            lang="tr",
        )

    asyncio.run(run())

    svc = ConversationService.for_data_dir(tmp_path)
    meta = svc.get_json_metadata(cid)
    row = mem.conversations_meta.get(cid)
    assert row is not None
    # Title cleaned: surrounding quotes + trailing whitespace/newline stripped.
    assert row.title == "Espresso Makinesi Arızası"
    assert meta.get("llm_titled") is True
    assert meta.get("title_source") == "auto"
    # Broadcast happened with the cleaned title.
    assert hub.events == [
        {"type": "conversation_updated", "conversation_id": cid, "title": "Espresso Makinesi Arızası"}
    ]
    # The prompt included (a clip of) the first user message.
    assert "espresso makinesi" in prompts[0]


def test_skips_when_title_source_manual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mem = Memory.for_data_dir(tmp_path)
    cid = "conv-title-manual"
    mem.conversations_meta.ensure(cid)
    mem.conversations_meta.patch(cid, title="Benim başlığım")
    mem.conversations_meta.merge_json_metadata(cid, {"title_source": "manual"})

    prompts = _wire_llm(monkeypatch, "Some LLM Title")
    hub = _StubHub()

    async def run() -> None:
        await chat_titler.maybe_title_conversation(
            settings=_settings(tmp_path),
            hub=hub,
            conversation_id=cid,
            first_user_text="ilk mesaj burada",
            lang="tr",
        )

    asyncio.run(run())

    row = mem.conversations_meta.get(cid)
    assert row is not None
    assert row.title == "Benim başlığım"  # manual title untouched
    assert prompts == []  # LLM never called (Gate 2 short-circuits before the call)
    assert hub.events == []  # no broadcast


def test_idempotent_second_call_no_llm_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mem = Memory.for_data_dir(tmp_path)
    cid = "conv-title-idem"
    _seed_untitled_with_truncation(mem, cid, "vergi beyannamesi nasıl doldurulur")

    prompts = _wire_llm(monkeypatch, "Vergi Beyannamesi Yardımı")
    hub = _StubHub()

    async def run() -> None:
        # First call titles it.
        await chat_titler.maybe_title_conversation(
            settings=_settings(tmp_path),
            hub=hub,
            conversation_id=cid,
            first_user_text="vergi beyannamesi nasıl doldurulur",
            lang="tr",
        )
        # Second call must be a no-op: llm_titled is set → no LLM call, no broadcast.
        await chat_titler.maybe_title_conversation(
            settings=_settings(tmp_path),
            hub=hub,
            conversation_id=cid,
            first_user_text="vergi beyannamesi nasıl doldurulur",
            lang="tr",
        )

    asyncio.run(run())

    row = mem.conversations_meta.get(cid)
    assert row is not None and row.title == "Vergi Beyannamesi Yardımı"
    assert len(prompts) == 1  # LLM called exactly once
    assert len(hub.events) == 1  # broadcast exactly once


def test_blank_llm_result_keeps_truncation_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mem = Memory.for_data_dir(tmp_path)
    cid = "conv-title-blank"
    _seed_untitled_with_truncation(mem, cid, "merhaba dünya bu bir test mesajıdır")
    truncation_title = mem.conversations_meta.get(cid).title
    assert truncation_title  # sanity

    _wire_llm(monkeypatch, "   \n  ")  # blank/garbage LLM output
    hub = _StubHub()

    async def run() -> None:
        await chat_titler.maybe_title_conversation(
            settings=_settings(tmp_path),
            hub=hub,
            conversation_id=cid,
            first_user_text="merhaba dünya bu bir test mesajıdır",
            lang="tr",
        )

    asyncio.run(run())

    row = mem.conversations_meta.get(cid)
    assert row is not None
    assert row.title == truncation_title  # unchanged — existing truncation title kept
    # Blank result → no write, so it is NOT marked llm_titled (a real title can still land later).
    assert (row.json_metadata or {}).get("llm_titled") is None
    assert hub.events == []  # nothing broadcast


def test_disabled_toggle_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the ``llm_chat_titles`` flag resolves falsy, the titler returns immediately."""
    mem = Memory.for_data_dir(tmp_path)
    cid = "conv-title-off"
    _seed_untitled_with_truncation(mem, cid, "kapalıyken başlık üretme")
    truncation_title = mem.conversations_meta.get(cid).title

    monkeypatch.setattr(chat_titler, "_titles_enabled", lambda _s: False)
    prompts = _wire_llm(monkeypatch, "Should Not Be Used")
    hub = _StubHub()

    async def run() -> None:
        await chat_titler.maybe_title_conversation(
            settings=_settings(tmp_path),
            hub=hub,
            conversation_id=cid,
            first_user_text="kapalıyken başlık üretme",
            lang="tr",
        )

    asyncio.run(run())

    row = mem.conversations_meta.get(cid)
    assert row is not None and row.title == truncation_title
    assert prompts == []
    assert hub.events == []


@pytest.mark.parametrize(
    "runtime_lang,turn_lang,expect_marker",
    [
        ("tr", "en", "Türkçe"),   # runtime 'tr' wins even when the turn lang is 'en'
        ("en", "tr", "English"),  # runtime 'en' wins even when the turn lang is 'tr'
    ],
)
def test_title_language_follows_runtime_language_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_lang: str,
    turn_lang: str,
    expect_marker: str,
) -> None:
    """The title language follows the runtime ``language`` setting (the UI/reply mode),
    NOT ``body.lang`` — which is null on text turns and made every title English."""
    mem = Memory.for_data_dir(tmp_path)
    cid = f"conv-lang-{runtime_lang}"
    _seed_untitled_with_truncation(mem, cid, "test message for the language check")

    def fake_get_runtime(key, _settings):  # noqa: ANN001, ANN202
        if key == "language":
            return runtime_lang
        raise KeyError(key)  # llm_chat_titles unknown → titler defaults to on

    import akana_server.runtime_settings as rs

    monkeypatch.setattr(rs, "get_runtime", fake_get_runtime)
    prompts = _wire_llm(monkeypatch, "A Title")
    hub = _StubHub()

    asyncio.run(
        chat_titler.maybe_title_conversation(
            settings=_settings(tmp_path),
            hub=hub,
            conversation_id=cid,
            first_user_text="test message for the language check",
            lang=turn_lang,  # opposite of runtime_lang → proves runtime setting wins
        )
    )

    assert prompts, "the LLM titler should have been called"
    assert expect_marker in prompts[0], (
        f"title prompt must be in the runtime language {runtime_lang!r} "
        f"(marker {expect_marker!r}); got: {prompts[0][:120]}"
    )


def test_clean_title_strips_meta_markdown_and_label_preambles() -> None:
    """The model sometimes narrates ("başlık üretmek için…") or emits a markdown/label
    preamble instead of a bare title; _clean_title drops those and keeps only a real
    title (or "" so the caller falls back to the truncation title)."""
    clean = chat_titler._clean_title
    assert clean("başlık üretmek için sohbeti anlıyorum") == ""  # pure meta/reasoning
    assert clean("İşte günün özeti: ## Dün — 30 Haziran") == ""  # "İşte…" preamble (Turkish İ)
    assert clean("## Mercimek çorbası tarifi") == "Mercimek çorbası tarifi"  # md heading stripped
    assert clean("Başlık: React login hatası") == "React login hatası"  # label peeled
    assert clean("Here is the title:\nFixing the React login bug") == "Fixing the React login bug"
    assert clean("React login hatası düzeltme") == "React login hatası düzeltme"  # clean title kept
    # The claude CLI narrating its own approach as the "answer" (an observed leak) is
    # dropped, so the truncation title is kept instead of a garbage title.
    assert clean("Reasoning effort minimal for this task; just pro") == ""
    # A legitimate title merely STARTING with "reasoning" (letter boundary) survives.
    assert clean("Reasoningbot architecture") == "Reasoningbot architecture"


def test_title_prompt_carries_no_fewshot_examples() -> None:
    """Regression: the few-shot examples poisoned output (opus copied "React login
    hatası düzeltme" verbatim for unrelated messages), so the prompt must NOT ship
    any canned example title — only rules + the delimited user message."""
    for lang in ("tr", "en"):
        p = chat_titler._title_prompt("kullanıcının gerçek mesajı", lang)
        low = p.lower()
        assert "react login" not in low
        assert "fixing the react" not in low
        assert "mercimek çorbası tarifi" not in low
        assert "lentil soup" not in low
        # The user's message IS delimited in the prompt.
        assert "kullanıcının gerçek mesajı" in p
    # The language rule stays explicit in each language.
    assert "Türkçe" in chat_titler._title_prompt("x", "tr")
    assert "English" in chat_titler._title_prompt("x", "en")


def test_title_system_prompt_pins_language_and_forbids_reasoning() -> None:
    """The one-shot title call must ship a tight system prompt that forces the UI
    language and forbids reasoning/tools — the leak fixed here."""
    tr = chat_titler._title_system_prompt("tr")
    assert "Türkçe" in tr and "tool" in tr.lower()
    en = chat_titler._title_system_prompt("en")
    assert "English" in en and "tool" in en.lower()


def test_titler_forwards_language_matched_system_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """maybe_title_conversation passes a system_prompt in the runtime language to the
    LLM call (so the agentic CLI is constrained to a bare Turkish title)."""
    mem = Memory.for_data_dir(tmp_path)
    cid = "conv-sysprompt"
    _seed_untitled_with_truncation(mem, cid, "espresso makinesi bozuldu ne yapmalıyım")

    captured: dict[str, object] = {}

    async def fake_complete(settings, prompt, **kwargs):  # noqa: ANN001, ANN003
        captured["prompt"] = prompt
        captured["system_prompt"] = kwargs.get("system_prompt")
        return "Espresso Arızası", {"prompt_tokens": 1}

    def fake_get_runtime(key, _settings):  # noqa: ANN001, ANN202
        if key == "language":
            return "tr"
        raise KeyError(key)

    import akana_server.runtime_settings as rs

    monkeypatch.setattr(rs, "get_runtime", fake_get_runtime)
    monkeypatch.setattr(chat_titler.llm_dispatch, "complete_chat_with_usage", fake_complete)

    asyncio.run(
        chat_titler.maybe_title_conversation(
            settings=_settings(tmp_path),
            hub=_StubHub(),
            conversation_id=cid,
            first_user_text="espresso makinesi bozuldu ne yapmalıyım",
            lang=None,  # text turn: body.lang null → runtime 'tr' drives the language
        )
    )
    sysp = captured.get("system_prompt")
    assert isinstance(sysp, str) and sysp, "a system prompt must be forwarded"
    assert "Türkçe" in sysp  # runtime language 'tr' → Turkish system prompt


def test_llm_failure_never_raises_and_keeps_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider error inside the titler must be swallowed (never affects the turn)."""
    mem = Memory.for_data_dir(tmp_path)
    cid = "conv-title-boom"
    _seed_untitled_with_truncation(mem, cid, "sağlayıcı patlarsa ne olur")
    truncation_title = mem.conversations_meta.get(cid).title

    async def boom(settings, prompt, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(chat_titler.llm_dispatch, "complete_chat_with_usage", boom)
    hub = _StubHub()

    async def run() -> None:
        # Must not raise.
        await chat_titler.maybe_title_conversation(
            settings=_settings(tmp_path),
            hub=hub,
            conversation_id=cid,
            first_user_text="sağlayıcı patlarsa ne olur",
            lang="tr",
        )

    asyncio.run(run())

    row = mem.conversations_meta.get(cid)
    assert row is not None and row.title == truncation_title
    assert hub.events == []
