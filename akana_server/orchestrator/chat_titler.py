"""Background LLM chat titler — upgrade the truncation auto-title to a short summary.

Akana already auto-titles a fresh conversation by TRUNCATING the first user message
(``ConversationStore.on_user_message`` → first line clipped to ~60 chars). This module
upgrades that to a Cursor/Claude-style LLM summary: on the FIRST user message (before the
reply), the chat producer fires :func:`maybe_title_conversation` fire-and-forget; it asks
the active provider for a tight 3–6 word title and writes it via a meta-store method that
never clobbers a manual (user) title.

Design contract (mirrors ``session_closer_service`` for the LLM call):

* **On by default, toggleable** — gated by the runtime flag ``llm_chat_titles`` (default
  True). The schema spec lives in a SIBLING-WIP file, so the flag is read DEFENSIVELY:
  an unknown key (``get_runtime`` raises ``KeyError``) or any resolution failure is
  treated as the default (on). A spec can be added later (see the note in
  :func:`_titles_enabled`).
* **Once per conversation** — the store stamps ``json_metadata.llm_titled`` on write;
  Gate 2 skips a conversation that is already ``llm_titled`` or ``title_source="manual"``,
  so spawning per turn (the producer self-gates cheaply on the first turn too) is safe.
* **Never affects the turn** — EVERYTHING is wrapped in try/except → ``log.debug`` and
  return. A titling failure is invisible; the existing truncation title stays.
* **Active provider** — uses ``complete_chat_with_usage(chat_mode=False, reuse_agent=False)``
  (the stateless one-shot path, same as the session-closer summary).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from akana_server.config import Settings
from akana_server.orchestrator import llm_dispatch

if TYPE_CHECKING:  # type-only — EventHub is only used for the optional broadcast
    from akana_server.events import EventHub

log = logging.getLogger(__name__)

#: Clip the LLM title so a runaway response can't bloat the conversation-list payload.
_TITLE_CLIP = 48
#: Clip the first user message fed into the prompt (a long paste doesn't need to be whole).
_PROMPT_TEXT_CLIP = 500

#: In-flight guard — conversation ids currently being titled by a live task. The producer
#: spawns this fire-and-forget on every turn (Gate 2 self-gates on ``llm_titled``, which is
#: only stamped AFTER the slow LLM call); without this, two quickly-sent messages both pass
#: Gate 2 and race two provider calls with last-writer-wins. This process-local set makes the
#: gate check-and-act atomic within the loop (the tasks share one event loop, so no lock).
_inflight: set[str] = set()


def _titles_enabled(settings: Settings) -> bool:
    """Resolve the ``llm_chat_titles`` runtime flag; default True (on) on any miss.

    The user-facing on/off is the ``llm_chat_titles`` runtime setting (schema.py spec,
    category ``genel``; env ``AKANA_LLM_CHAT_TITLES``; ``Settings.llm_chat_titles``
    default True). ``get_runtime`` resolves the stored override → settings default. The
    read stays defensive: an unknown key (spec not loaded) or an unreadable store is
    treated as the DEFAULT (on), so titling never breaks on a config hiccup.
    """
    try:
        from akana_server.runtime_settings import get_runtime

        val = get_runtime("llm_chat_titles", settings)
    except Exception:
        # Unknown key (no spec yet) or unreadable store → default ON.
        return True
    if val is None:
        return True
    return bool(val)


def _title_prompt(first_user_text: str, lang: str | None) -> str:
    """Bilingual prompt asking for a SHORT chat title from the first message.

    Deliberately carries NO few-shot examples. Examples poisoned the output: opus
    copied an example's words verbatim (a plain greeting was titled "React login
    hatası düzeltme" — the old React example) or bled them into unrelated titles
    ("React …" prepended everywhere). Instead the user message is DELIMITED in
    triple quotes so the model can't mistake instruction for content, and the
    language rule is explicit. The companion :func:`_title_system_prompt` forbids
    preamble/reasoning/tools and pins the output language; ``_clean_title`` strips
    any residue defensively.
    """
    snippet = " ".join((first_user_text or "").split())[:_PROMPT_TEXT_CLIP]
    is_tr = str(lang or "").strip().lower() == "tr"
    if is_tr:
        return (
            "Aşağıda üç tırnak içindeki kullanıcı mesajını 3-6 kelimelik KISA bir "
            "başlıkla özetle. Başlık Türkçe olmalı ve yalnızca bu mesajın konusunu "
            "anlatmalı; mesajda geçmeyen hiçbir şey ekleme veya uydurma.\n\n"
            f'"""\n{snippet}\n"""\n\n'
            "Yalnızca başlığı yaz:"
        )
    return (
        "Summarize the user message in triple quotes below as a SHORT 3-6 word "
        "title. The title must be in English and describe only this message's "
        "topic; do not add or invent anything not in the message.\n\n"
        f'"""\n{snippet}\n"""\n\n'
        "Write only the title:"
    )


def _title_system_prompt(lang: str | None) -> str:
    """Tight system prompt for the one-shot title call — reins in the agentic CLI.

    The claude one-shot spawns the full Claude Code CLI with NO system prompt, so
    the model narrates its own reasoning as the "answer" ("Reasoning effort minimal
    for this task; just pro…", "Wait — let me reconsider") and that leaks straight
    into the title. This pins the reply to a bare one-line title in the UI language
    and forbids tools/preamble/reasoning. Providers that ignore a system prompt
    still get the language rule + delimited message from :func:`_title_prompt`, so
    the guard degrades gracefully.
    """
    is_tr = str(lang or "").strip().lower() == "tr"
    if is_tr:
        return (
            "Sen bir sohbet-başlık üreticisisin. Yanıt olarak SADECE tek satırlık "
            "kısa bir başlık döndür. Muhakemeni/düşünceni yazma, açıklama yapma, "
            "araç (tool) kullanma, dosya okuma. Çıktıda tırnak, markdown, '#', "
            "giriş cümlesi ('İşte', 'Başlık:') veya sonda noktalama olmasın. Başlık "
            "HER ZAMAN Türkçe olacak; kullanıcının mesajı başka dilde olsa bile."
        )
    return (
        "You are a chat-title generator. Reply with ONLY a single short one-line "
        "title. Do not write your reasoning, do not explain, do not use tools, do "
        "not read files. No quotes, no markdown, no '#', no preamble, no trailing "
        "punctuation. The title must ALWAYS be in English, even if the user's "
        "message is in another language."
    )


#: Multi-word / labelled preamble markers. These are unambiguous narration or labels ("here
#: is …", "the title …", "let me …", "chat title", "Title:"), so they match when followed by
#: end-of-line OR a boundary char (whitespace/punctuation). A bare ``startswith`` was too
#: greedy — it is now boundary-checked in :func:`_starts_with_meta`.
_TITLE_META_PHRASES = (
    "title:", "başlık:", "baslik:", "chat title", "sohbet başlığı", "sohbet basligi",
    "here is", "here's", "the title", "of course", "i'll", "i will", "let me", "işte",
)

#: Deliberate STEMS that must match a following word ("başlık üret" → "üretmek"/"üretiyorum").
#: Kept as bare ``startswith`` — they are model-reasoning fragments, never a real title opener.
#: ``reasoning effort`` is the claude CLI narrating its own approach ("Reasoning effort minimal
#: for this task; just pro…") — a leaked meta line the tight system prompt now prevents, kept
#: here as a defensive backstop for any provider that still emits it.
_TITLE_META_STEMS = ("başlık üret", "baslik uret", "reasoning effort")

#: Interjection/ambiguous stems that ALSO begin legitimate topic titles ("Sure-fire investing",
#: "Okay sign meaning", and the ASCII-folded "iste" which also opens Turkish "İstek …"/"İstemci
#: …"). Note "işte" (with ş) is the real Turkish "here is" preamble and lives in the phrases
#: above; only bare-ASCII "iste" is ambiguous. As preamble these run into PUNCTUATION ("Sure,
#: …", "Okay:", "iste:") — never straight into a topic word — so they match only when the
#: marker is the whole line or is followed by punctuation, NOT a letter/digit/whitespace.
_TITLE_META_INTERJECTIONS = ("sure", "okay", "iste")

#: Boundary after a PHRASE marker (whitespace or the punctuation a preamble runs into).
_META_PHRASE_BOUNDARY = " \t,:.!?;-–—…\"'“”‘’"
#: Boundary after an INTERJECTION stem — punctuation only (whitespace would eat "Okay sign …").
_META_INTERJECTION_BOUNDARY = ",:.!?;–—…\"'“”‘’"


def _starts_with_meta(low: str) -> bool:
    """True if ``low`` (case-folded line) begins with a preamble marker at a word boundary."""
    if low.startswith(_TITLE_META_STEMS):
        return True
    for p in _TITLE_META_PHRASES:
        if low.startswith(p):
            rest = low[len(p):]
            if not rest or rest[0] in _META_PHRASE_BOUNDARY:
                return True
    for p in _TITLE_META_INTERJECTIONS:
        if low.startswith(p):
            rest = low[len(p):]
            if not rest or rest[0] in _META_INTERJECTION_BOUNDARY:
                return True
    return False


def _clean_title(raw: str) -> str:
    """First REAL title line — preamble/markdown/label/quotes/punctuation stripped, clipped.

    Weaker providers may prepend a meta line ("İşte günün özeti: …", "Title: …",
    "başlık üretmek için sohbeti anlıyorum") or a markdown heading before the title.
    Walk the lines, drop those, and return the first genuine title; if nothing
    survives, return "" so the caller keeps the truncation title (not the garbage).
    """
    for candidate in (raw or "").splitlines():
        line = candidate.strip()
        if not line:
            continue
        # Drop leading markdown heading / list / quote markers ("## ", "- ", "> ").
        line = line.lstrip("#>*-•\t ").strip()
        # Peel a leading label like "Title:" / "Başlık:" (keep what follows).
        low = line.replace("İ", "i").lower()  # Turkish İ.lower() would add a combining dot
        for lbl in ("title:", "başlık:", "baslik:"):
            if low.startswith(lbl):
                line = line[len(lbl):].strip()
                low = line.replace("İ", "i").lower()  # Turkish İ.lower() would add a combining dot
                break
        if not line:
            continue
        # A meta/preamble line is not a title — skip it and try the next line.
        if _starts_with_meta(low):
            continue
        # Strip matched/leading/trailing quotes (straight + curly) + trailing punctuation AND
        # trailing markdown emphasis ("**Lentil soup recipe**" → the leading ** was already
        # lstripped above but the trailing ** survived and showed verbatim in the sidebar).
        line = line.strip("\"'“”‘’").strip().rstrip(" .!?:;،。*_").strip()
        if line:
            return line[:_TITLE_CLIP].strip()
    return ""


def _title_language(settings: Settings, fallback: str | None) -> str:
    """Effective title language (``tr``/``en``).

    The title must be in the UI/reply language, i.e. the runtime ``language`` setting —
    the SAME source that drives every other prompt/reply (and the session-closer
    summary). ``body.lang`` is the per-turn TTS/voice language and is usually ``None``
    on text turns, so it must NOT be the primary source (that made every title English).
    Order: runtime ``language`` → the turn ``fallback`` → ``en``.

    Cannot delegate to :func:`resolve_language` outright: that helper's own
    try/except collapses an unset/invalid runtime value straight to ``en``,
    which would skip this function's ``fallback`` tier. Reads the runtime value
    the same way resolve_language does (``get_runtime("language", settings)``)
    and applies the identical ``{"tr", "en"}`` validation.
    """
    try:
        from akana_server.runtime_settings import get_runtime

        val = str(get_runtime("language", settings) or "").strip().lower()
        if val in ("tr", "en"):
            return val
    except Exception:  # unknown key / unreadable store → fall back
        pass
    fb = str(fallback or "").strip().lower()
    return fb if fb in ("tr", "en") else "en"


async def maybe_title_conversation(
    *,
    settings: Settings,
    hub: "EventHub | None",
    conversation_id: str,
    first_user_text: str,
    lang: str | None,
) -> None:
    """Summarize the conversation title via the LLM, once, in the background.

    Fire-and-forget from the chat producer at turn START (before the reply). Self-gating
    and fully guarded: it NEVER raises and NEVER touches the turn. On success it writes the
    title via ``ConversationService.set_llm_title`` (which refuses to clobber a manual
    title) and broadcasts ``conversation_updated`` so the sidebar/thread bar update live.
    """
    cid = (conversation_id or "").strip()
    reserved = False
    try:
        if not cid:
            return
        # Gate 1 — the on/off toggle (default on).
        if not _titles_enabled(settings):
            return
        # Access layer consistent with the chat routes.
        from akana_server.conversation_service import ConversationService

        svc = ConversationService.for_data_dir(settings.data_dir)
        # Gate 2 — idempotent + never over a manual title. The producer spawns this on EVERY
        # non-voice turn, so guard against (a) retitling a pre-existing conversation from a
        # later message and (b) two quick messages racing two provider calls. Off-load the
        # sqlite reads/writes to a worker thread — this task runs on the event loop and a
        # locked memory.db write here would freeze every SSE/WS/HTTP endpoint (b26 pattern).
        # Read the RAW json_metadata: only an explicit user rename stamps
        # ``title_source="manual"`` there (the derived _Meta.title_source would synthesize
        # "manual" for any truncation-titled row, which must still be upgradable).
        meta = await asyncio.to_thread(svc.get_json_metadata, cid)
        if meta.get("title_source") == "manual" or meta.get("llm_titled"):
            return
        info = await asyncio.to_thread(svc.get, cid)
        if info is None:
            return  # deleted / unknown
        # Only title from the conversation's FIRST message. On a non-first turn ``first_user_text``
        # is the CURRENT message, not the conversation's opener, so titling from it would rename
        # the chat off a mid-conversation aside (e.g. an old 50-turn chat retitled from "any cat
        # food tips?"). At spawn the opening user turn is already persisted (count == 1).
        if info.message_count > 2:
            return
        text = (first_user_text or "").strip()
        if not text:
            return  # nothing to summarize → keep the (empty/truncation) title

        # In-flight guard — make the check-then-act atomic within the loop so a second turn's
        # titler (spawned before this one's ``llm_titled`` stamp lands) does not also spend a
        # provider call and race the write. The set is touched only from the event loop.
        if cid in _inflight:
            return
        _inflight.add(cid)
        reserved = True

        resolved_lang = _title_language(settings, lang)
        prompt = _title_prompt(text, resolved_lang)
        result, _usage = await llm_dispatch.complete_chat_with_usage(
            settings,
            prompt,
            chat_mode=False,
            reuse_agent=False,
            # A tight system prompt pins the output to a bare one-line title in the UI
            # language and stops the agentic claude CLI from leaking its reasoning into
            # the title (see :func:`_title_system_prompt`).
            system_prompt=_title_system_prompt(resolved_lang),
        )
        title = _clean_title(result)
        if not title:
            return  # empty/garbage LLM output → keep the existing truncation title

        await asyncio.to_thread(svc.set_llm_title, cid, title)
        # ``set_llm_title`` returns nothing and silently refuses over a manual title (a rename
        # can race in during the LLM call). Re-read the stored title: broadcast ONLY when the
        # store actually applied our title, else the FE would visibly clobber the user's manual
        # name with the auto title until the next reload (contract mismatch — the FE updates
        # whenever the incoming title differs from the shown one).
        applied = await asyncio.to_thread(svc.get, cid)
        if applied is None or applied.title != title:
            return
        if hub is not None:
            # Best-effort — a broadcast failure must not surface.
            try:
                await hub.broadcast_json(
                    {
                        "type": "conversation_updated",
                        "conversation_id": cid,
                        "title": title,
                    }
                )
            except Exception:
                log.debug("conversation_updated broadcast failed (conv=%s)", cid, exc_info=True)
    except Exception:
        # A titling failure must NEVER affect the turn — swallow everything.
        log.debug(
            "chat titler failed (conv=%s) — keeping the truncation title",
            conversation_id,
            exc_info=True,
        )
    finally:
        if reserved:
            _inflight.discard(cid)


__all__ = ["maybe_title_conversation"]
