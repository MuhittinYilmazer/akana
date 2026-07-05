"""LLM decides what (if anything) to stage in memory inbox after a chat turn."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from akana_server.config import Settings
from akana_server.orchestrator.llm_dispatch import (
    LLMCallError,
    complete_chat_with_usage,
)

if TYPE_CHECKING:
    from akana.memory import Memory

log = logging.getLogger(__name__)

_CONTEXT_TURNS = 8
_EXISTING_KEY_LIMIT = 80
_MAX_FACTS = 5

# The capture-decision prompt is read by the model, so it is BILINGUAL and follows
# the ``language`` runtime setting (en|tr). JSON keys stay English → the parser is
# language-agnostic; only the prose/labels switch.
_DIALOGUE_ROLES = {
    "en": {"user": "User", "assistant": "Assistant"},
    "tr": {"user": "Kullanıcı", "assistant": "Asistan"},
}
_NO_PRIOR_TURN = {"en": "(no prior turn)", "tr": "(önceki tur yok)"}
_NO_KEYS = {"en": "(no records yet)", "tr": "(henüz kayıt yok)"}
_NO_REPLY = {"en": "(no reply)", "tr": "(yanıt yok)"}

#: How many pending inbox rows to show the capture model (and how far each value is clipped)
#: so the "already staged, do not re-propose" list can't blow the prompt budget.
_PENDING_SNAPSHOT_LIMIT = 40
_PENDING_VALUE_CLIP = 120
#: The pending-inbox section label (only rendered when there IS something pending).
_PENDING_FIELD = {
    "en": "Awaiting in the inbox — do NOT re-propose these",
    "tr": "Inbox'ta bekleyen — bunları tekrar ÖNERME",
}

_CAPTURE_BODY = {
    "en": (
        "[Memory capture decision]\n"
        "Evaluate this chat turn for Akana's long-term memory.\n"
        "If there is info worth saving it goes to the Inbox; the user approves it from the Memory screen.\n\n"
        "Rules:\n"
        "- Your ONLY output must be valid JSON (no markdown, explanation, or code).\n"
        "- capture: false → facts: []\n"
        "- Personal, durable, verifiable info (first name, surname, preference, contact, important date).\n"
        "- Do not save chat summaries, speculation, transient state, code snippets, or greetings.\n"
        "- If the user says «save to memory», «remember:», «save this», look at the earlier turns.\n"
        "- Do not repeat info already under existing keys; if it is a correction, add it with the new value.\n"
        "- Do NOT re-propose anything still awaiting approval in the inbox — not even reworded "
        "or under a different key; it is already captured. Only a genuine CORRECTION (a new value) is allowed.\n"
        "- key: short snake_case (e.g. surname, email, birth_date).\n"
        "- ATOMIC records: one fact = one key/value. Do NOT MERGE several facts into one value "
        "(e.g. value='Ali, 30, Istanbul' is WRONG → split into separate facts: "
        "name=Ali, age=30, city=Istanbul). That way each fact can later be updated or "
        "deleted on its own (granular forgetting).\n\n"
    ),
    "tr": (
        "[Hafıza kayıt kararı]\n"
        "Akana'in uzun süreli hafızası için bu sohbet turunu değerlendir.\n"
        "Kaydedilecek bilgi varsa Inbox'a gidecek; kullanıcı Hafıza ekranından onaylayacak.\n\n"
        "Kurallar:\n"
        "- Yanıtın TEK çıktısı geçerli JSON olmalı (markdown, açıklama, kod yok).\n"
        "- capture: false → facts: []\n"
        "- Kişisel, kalıcı, doğrulanabilir bilgiler (ad, soyad, tercih, iletişim, önemli tarih).\n"
        "- Sohbet özeti, spekülasyon, geçici durum, kod parçası, selam kaydetme.\n"
        "- Kullanıcı «hafızaya kaydet», «hatırla:», «bunu kaydet» derse önceki turlara bak.\n"
        "- Mevcut anahtarlarla aynı bilgiyi tekrarlama; düzeltme ise yeni değerle ekle.\n"
        "- Onay için Inbox'ta bekleyen bir bilgiyi tekrar ÖNERME — farklı ifadeyle "
        "veya farklı anahtarla bile olsa; o bilgi zaten yakalandı. Yalnızca gerçek bir DÜZELTME "
        "(yeni değer) serbesttir.\n"
        "- key: kısa snake_case (ör. soyad, email, dogum_tarihi).\n"
        "- ATOMİK kayıt: tek bilgi = tek key/value. Birden çok bilgiyi TEK değerde "
        "BİRLEŞTİRME (ör. value='Ali, 30, İstanbul' YANLIŞ → ayrı fact'lere böl: "
        "ad=Ali, yas=30, sehir=İstanbul). Böylece her bilgi sonradan tek başına "
        "güncellenebilir veya silinebilir (parça-parça unutma).\n\n"
    ),
}
# (existing-keys label, recent-messages label, this-turn-user label, this-turn-assistant label)
_CAPTURE_FIELDS = {
    "en": ("Existing memory keys", "Recent messages", "This turn — User", "This turn — Assistant"),
    "tr": ("Mevcut hafıza anahtarları", "Son mesajlar", "Bu tur — Kullanıcı", "Bu tur — Asistan"),
}
_CAPTURE_EXAMPLE = {
    "en": (
        'JSON template: {"capture": true, "facts": [{"key": "surname", '
        '"value": "Smith", "reason": "user stated"}]}\n'
    ),
    "tr": (
        'JSON şablonu: {"capture": true, "facts": [{"key": "soyad", '
        '"value": "Yılmaz", "reason": "kullanıcı bildirdi"}]}\n'
    ),
}


@dataclass(frozen=True, slots=True)
class MemoryCaptureCandidate:
    key: str
    value: str
    reason: str = ""


def _env_capture_enabled() -> bool:
    import os

    raw = os.environ.get("AKANA_MEMORY_LLM_CAPTURE", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def capture_enabled(data_dir: Any = None) -> bool:
    """Whether background auto-capture runs — the Memory Studio 'automatic capture' toggle
    (``memory_settings.auto_capture``, next to 'remember without approval'; env
    ``AKANA_MEMORY_LLM_CAPTURE`` overrides it on load). Falls back to the env-only default when
    no data_dir is given or the settings can't be read."""
    if data_dir is not None:
        try:
            from akana.memory.settings import load_memory_settings

            return bool(load_memory_settings(data_dir).auto_capture)
        except Exception:  # a corrupt/unreadable settings file must never break the gate
            pass
    return _env_capture_enabled()


def parse_capture_response(raw: str) -> list[MemoryCaptureCandidate]:
    """Parse model JSON into staging candidates."""
    text = (raw or "").strip()
    if not text:
        return []
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        log.debug("memory capture JSON parse failed: %r", text[:200])
        return []
    if not isinstance(data, dict):
        return []
    if not data.get("capture"):
        return []
    facts = data.get("facts")
    if not isinstance(facts, list):
        return []
    out: list[MemoryCaptureCandidate] = []
    for item in facts[:_MAX_FACTS]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip().lower().replace(" ", "_")[:64]
        value = str(item.get("value") or "").strip()[:8000]
        reason = str(item.get("reason") or "").strip()[:300]
        if len(key) < 2 or len(value) < 2:
            continue
        out.append(MemoryCaptureCandidate(key=key, value=value, reason=reason))
    return out


def _existing_keys(memory: Memory) -> list[str]:
    facts = memory.list_facts(limit=_EXISTING_KEY_LIMIT)
    return sorted({f.key for f in facts if f.key})[:_EXISTING_KEY_LIMIT]


def _pending_snapshot(memory: Memory) -> list[tuple[str, str]]:
    """``(key, value)`` of the pending inbox candidates the capture model must NOT re-propose.

    Why: a fact the agent already saved via the memory tool sits in the inbox as PENDING (not
    yet durable), so ``_existing_keys`` (durable only) can't see it. On a LATER turn the capture
    model re-reads the recent dialogue and re-proposes the same fact under a different key → a
    duplicate inbox row (the reported bug). Feeding it the pending rows lets it dedup semantically.

    Session summaries (``extractor='session_closer'``) are excluded — they are long prose
    paragraphs, not atomic facts the capture would restate. Newest first, capped and value-clipped
    so this section can't blow the prompt budget."""
    try:
        # list_pending is ts ASC, so a small limit would return the OLDEST rows and
        # drop the newest candidates (the ones most likely to be re-proposed). The
        # inbox is capped at 500 pending rows, so fetch the whole window and then
        # reverse to get newest-first before clipping to _PENDING_SNAPSHOT_LIMIT.
        pending = memory.staging.list_pending(limit=500)
    except Exception:  # a staging read must never break capture
        log.debug("pending snapshot read failed", exc_info=True)
        return []
    out: list[tuple[str, str]] = []
    for s in reversed(pending):  # list_pending is ts ASC → iterate newest first
        if s.extractor == "session_closer":
            continue
        key = (s.key or "").strip()
        val = " ".join((s.value or "").split())[:_PENDING_VALUE_CLIP]
        if key and val:
            out.append((key, val))
        if len(out) >= _PENDING_SNAPSHOT_LIMIT:
            break
    return out


def _recent_dialogue(
    memory: Memory,
    conversation_id: str | None,
    *,
    exclude_user: str | None = None,
    language: str = "en",
) -> str:
    lang = language if language in ("en", "tr") else "en"
    roles = _DIALOGUE_ROLES[lang]
    if not conversation_id:
        return _NO_PRIOR_TURN[lang]
    # `recent_turns` → `list_conversation` (ts ASC LIMIT N) = the FIRST N turns of
    # the conversation; since `turns[-N:]` assumes the newest, in long
    # conversations the capture LLM only saw the BEGINNING. `list_conversation_recent`
    # returns the NEWEST N (in ASC order) — the correct source.
    turns = memory.episodic.list_conversation_recent(
        conversation_id, limit=_CONTEXT_TURNS + 4
    )
    if not turns:
        return _NO_PRIOR_TURN[lang]
    exclude = (exclude_user or "").strip()
    lines: list[str] = []
    for t in turns[-_CONTEXT_TURNS:]:
        if exclude and t.role == "user" and t.text.strip() == exclude:
            continue
        role = roles["user"] if t.role == "user" else roles["assistant"]
        text = (t.text or "")[:400].replace("\n", " ")
        lines.append(f"- {role}: {text}")
    return "\n".join(lines) if lines else _NO_PRIOR_TURN[lang]


def _capture_prompt(
    *,
    user_text: str,
    assistant_text: str | None,
    existing_keys: list[str],
    recent_dialogue: str,
    pending_items: list[tuple[str, str]] | None = None,
    language: str = "en",
) -> str:
    lang = language if language in ("en", "tr") else "en"
    keys_line = ", ".join(existing_keys) if existing_keys else _NO_KEYS[lang]
    asst = (assistant_text or "").strip()[:1200] or _NO_REPLY[lang]
    keys_label, recent_label, user_label, asst_label = _CAPTURE_FIELDS[lang]
    # Only render the pending-inbox block when there IS something pending — an empty
    # "(none)" section would just add noise to the prompt.
    pending_block = ""
    if pending_items:
        rows = "\n".join(f"- {k}: {v}" for k, v in pending_items)
        pending_block = f"{_PENDING_FIELD[lang]}:\n{rows}\n\n"
    return (
        _CAPTURE_BODY[lang]
        + f"{keys_label}: {keys_line}\n\n"
        + pending_block
        + f"{recent_label}:\n{recent_dialogue}\n\n"
        + f"{user_label}:\n{user_text.strip()[:1500]}\n\n"
        + f"{asst_label}:\n{asst}\n\n"
        + _CAPTURE_EXAMPLE[lang]
    )


async def propose_memory_captures(
    settings: Settings,
    memory: Memory,
    *,
    user_text: str,
    assistant_text: str | None = None,
    conversation_id: str | None = None,
    model: str | None = None,
) -> list[MemoryCaptureCandidate]:
    """Ask the LLM whether to stage facts for this turn (``Memory`` context)."""
    if not capture_enabled(getattr(settings, "data_dir", None)):
        return []
    if not (user_text or "").strip():
        return []

    from akana_server.runtime_settings import resolve_language

    language = resolve_language(settings)
    existing = _existing_keys(memory)
    pending = _pending_snapshot(memory)
    recent = _recent_dialogue(
        memory,
        conversation_id,
        exclude_user=user_text if len(user_text.strip()) < 120 else None,
        language=language,
    )
    prompt = _capture_prompt(
        user_text=user_text,
        assistant_text=assistant_text,
        existing_keys=existing,
        recent_dialogue=recent,
        pending_items=pending,
        language=language,
    )
    try:
        raw, _usage = await complete_chat_with_usage(
            settings,
            prompt,
            history=None,
            model=model,
            chat_mode=False,
        )
    except LLMCallError as e:
        log.warning("LLM memory capture failed: %s", e.message)
        return []
    except Exception:
        log.exception("LLM memory capture unexpected error")
        return []
    return parse_capture_response(raw or "")


__all__ = [
    "MemoryCaptureCandidate",
    "parse_capture_response",
    "propose_memory_captures",
]
