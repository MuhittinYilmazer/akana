"""ContextEngine F0 — assemble combination matrix, budget-trim order, behavior-neutrality.

Assembler contract:

* persona×skill×plan matrix: in each combination the system prompt / user text /
  injected_blocks are built correctly, and the trace carries the "why" answer.
* budget trim: history first (from the oldest), the skill block next, system and
  user text never.
* behavior-neutrality: default persona + the historical skill-prepend formula —
  byte-for-byte equal to the prompts chat.py produced before the assembler.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from akana.memory.summary_types import SummaryView
from akana_server.config import load_settings
from akana_server.context import (
    DEFAULT_MAX_CONTEXT_CHARS,
    ContextAssembler,
    ContextRequest,
    context_budget_chars,
)
from akana_server.context import assembler as assembler_mod
from akana_server.conversation_service import ConversationService
from akana_server.persona.builtin import CHAT_SYSTEM_PREFIX
from akana_server.persona.registry import (
    get_persona_registry,
    reset_persona_registries,
)

CONV = "conv-ctx-assemble"
SKILL_BLOCK = "[Yetenek: smoke — Smoke Raporu]\ngövde satırı\n[/Yetenek]"
SKILL_ENTRIES = [{"id": "smoke", "status": "injected"}]


@pytest.fixture
def req(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A SimpleNamespace-based fake Request — the app.state seams are real."""
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AKANA_CONTEXT_MAX_CHARS", raising=False)
    reset_persona_registries()
    settings = load_settings()
    svc = ConversationService.for_data_dir(tmp_path)
    svc.ensure(CONV)
    app = SimpleNamespace(
        state=SimpleNamespace(settings=settings, conversation_service=svc)
    )
    yield SimpleNamespace(app=app)
    reset_persona_registries()


def _assemble(req, **kwargs):
    kwargs.setdefault("conversation_id", CONV)
    return asyncio.run(ContextAssembler(req).assemble(ContextRequest(**kwargs)))


def _bind_persona(req, *, conversation_id=None, channel=None, prompt="Resmî konuş."):
    reg = get_persona_registry(req.app.state.settings.data_dir)
    if reg.get("resmi") is None:
        reg.create_user_persona(
            persona_id="resmi", name="Resmî Akana", system_prompt=prompt
        )
    reg.bind("resmi", channel=channel, conversation_id=conversation_id)


# -- combination matrix (persona × skill) --------------------------------------- #


def test_default_tur_davranis_notr(req) -> None:
    """No binding + no skill → exactly today's behavior."""
    out = _assemble(req, text="merhaba")
    assert out.system_prompt == CHAT_SYSTEM_PREFIX
    assert out.system_prompt_override is None  # the client places its own prefix
    assert out.system_prompt_is_default is True
    assert out.user_text == "merhaba"
    assert out.history == []
    assert out.injected_blocks == []
    assert out.trace["persona"]["id"] == "akana"


def test_skill_blogu_tarihi_formulle_eklenir(req) -> None:
    """Behavior-neutrality snapshot: f"{block}\\n\\n{text}" — chat.py's old formula."""
    out = _assemble(
        req, text="rapor çıkar", skill_block=SKILL_BLOCK, skill_entries=SKILL_ENTRIES
    )
    assert out.user_text == f"{SKILL_BLOCK}\n\nrapor çıkar"  # the exact old combination
    kinds = [b["kind"] for b in out.injected_blocks]
    assert kinds == ["skill"]
    assert out.injected_blocks[0]["entries"] == [{"id": "smoke", "status": "injected"}]
    assert out.system_prompt_override is None


def test_gorsel_blogu_kullanici_metninin_sonuna_eklenir(req) -> None:
    """MultimodalEngine F1: [Görsel: <path>] lines go at the END of the text;
    the skill-block formula is unchanged (skill at the start, image at the end)."""
    image_block = "[Görsel: /tmp/uploads/a.png]\n[Görsel: /tmp/uploads/b.png]"
    out = _assemble(req, text="bu görsellerde ne var?", image_block=image_block)
    assert out.user_text == f"bu görsellerde ne var?\n\n{image_block}"
    blocks = [b for b in out.injected_blocks if b["kind"] == "image"]
    assert len(blocks) == 1 and blocks[0]["count"] == 2

    both = _assemble(
        req,
        text="bu görsellerde ne var?",
        image_block=image_block,
        skill_block=SKILL_BLOCK,
        skill_entries=SKILL_ENTRIES,
    )
    assert both.user_text == (
        f"{SKILL_BLOCK}\n\nbu görsellerde ne var?\n\n{image_block}"
    )


def test_konusma_personasi_system_prompta_girer(req) -> None:
    _bind_persona(req, conversation_id=CONV)
    out = _assemble(req, text="merhaba")
    assert out.persona_id == "resmi"
    assert out.system_prompt == "Resmî konuş."
    assert out.system_prompt_override == "Resmî konuş."  # now an override is sent
    assert out.system_prompt_is_default is False
    assert out.trace["persona"]["default"] is False


def test_kanal_personasi_system_prompta_girer(req) -> None:
    _bind_persona(req, channel="web")
    out = _assemble(req, text="merhaba", conversation_id="conv-baska")
    assert out.persona_id == "resmi"
    assert out.system_prompt_override == "Resmî konuş."


def test_tam_matris_persona_skill_birlikte(req) -> None:
    _bind_persona(req, conversation_id=CONV)
    out = _assemble(
        req,
        text="rapor çıkar",
        skill_block=SKILL_BLOCK,
        skill_entries=SKILL_ENTRIES,
    )
    assert out.system_prompt == "Resmî konuş."
    assert out.user_text == f"{SKILL_BLOCK}\n\nrapor çıkar"
    assert {b["kind"] for b in out.injected_blocks} == {"skill"}


def test_persona_arizasi_turu_kirmaz_builtin_doner(
    req, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_data_dir):
        raise RuntimeError("persona.db locked")

    monkeypatch.setattr(assembler_mod, "get_persona_registry", boom)
    out = _assemble(req, text="merhaba")
    assert out.system_prompt == CHAT_SYSTEM_PREFIX
    assert out.system_prompt_override is None
    assert out.persona_id == "akana"


# -- WI-2: the installed capability catalog enters the system prompt ------------- #


def _install_akana_skill(data_dir, skill_id: str, title: str, triggers: list[str]) -> None:
    d = data_dir / "skills" / skill_id
    d.mkdir(parents=True, exist_ok=True)
    trig_yaml = "".join(f"  - {t}\n" for t in triggers)
    (d / "manifest.yaml").write_text(
        f"id: {skill_id}\nversion: 1\ntitle: {title}\nrisk: low\ntriggers:\n{trig_yaml}",
        encoding="utf-8",
    )
    (d / "SKILL.md").write_text(f"# {title}\n\nGÖVDE ADIMLARI.\n", encoding="utf-8")


def test_yetenek_katalogu_default_personaya_eklenir(req) -> None:
    """If an installed akana skill exists: the catalog is appended to CHAT_SYSTEM_PREFIX, an override is sent."""
    from akana_server.skills.registry import reload_skills

    _install_akana_skill(
        req.app.state.settings.data_dir, "whatsapp", "WhatsApp", ["whatsapp", "mesaj gönder"]
    )
    reload_skills()
    out = _assemble(req, text="merhaba")
    assert out.system_prompt.startswith(CHAT_SYSTEM_PREFIX)  # the persona base is preserved
    assert "[INSTALLED CAPABILITIES]" in out.system_prompt
    assert "WhatsApp" in out.system_prompt and "mesaj gönder" in out.system_prompt
    assert "GÖVDE ADIMLARI" not in out.system_prompt  # the body does NOT leak
    assert out.system_prompt_is_default is False  # an override is now sent
    assert out.system_prompt_override is not None
    cat = out.trace["capability_catalog"]
    assert cat["applied"] is True and cat["chars"] > 0
    reload_skills()


def test_yetenek_katalogu_kapaliyken_davranis_notr(req, monkeypatch) -> None:
    """AKANA_SKILL_CATALOG=0 → the system prompt is unchanged even if an installed skill exists."""
    from akana_server.skills.registry import reload_skills

    monkeypatch.setenv("AKANA_SKILL_CATALOG", "0")
    _install_akana_skill(req.app.state.settings.data_dir, "whatsapp", "WhatsApp", ["whatsapp"])
    reload_skills()
    out = _assemble(req, text="merhaba")
    assert out.system_prompt == CHAT_SYSTEM_PREFIX  # byte-for-byte identical
    assert out.system_prompt_override is None
    assert out.trace["capability_catalog"]["applied"] is False
    reload_skills()


# -- budget: trimming from a single place ----------------------------------------- #


@pytest.fixture
def fixed_history(monkeypatch: pytest.MonkeyPatch):
    """Deterministic history: 2 messages × 50 chars (oldest are 'a's)."""

    async def fake_history(request, conversation_id):
        return (
            [
                {"role": "user", "content": "a" * 50},
                {"role": "assistant", "content": "b" * 50},
            ],
            3,
            False,
        )

    monkeypatch.setattr(assembler_mod, "async_llm_history_for_assemble", fake_history)


def _small_persona(req) -> None:
    """system=10 chars — keep the budget numbers hand-computable."""
    _bind_persona(req, conversation_id=CONV, prompt="S" * 10)


def test_butce_once_en_eski_history_duser(
    req, fixed_history, monkeypatch: pytest.MonkeyPatch
) -> None:
    # system 10 + history 100 + (skill 30 + "\n\n" 2 + text 20) = 162
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "120")
    _small_persona(req)
    out = _assemble(req, text="u" * 20, skill_block="k" * 30)
    assert [m["content"] for m in out.history] == ["b" * 50]  # the oldest was dropped
    assert "k" * 30 in out.user_text  # the skill block was preserved
    trimmed = out.trace["budget"]["trimmed"]
    assert [t["kind"] for t in trimmed] == ["history"]
    assert out.trace["budget"]["total_chars_after"] <= 120


def test_butce_skill_blogu_historyden_sonra_duser(
    req, fixed_history, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "40")
    _small_persona(req)
    out = _assemble(req, text="u" * 20, skill_block="k" * 30)
    assert out.history == []  # all history first
    assert out.user_text == "u" * 20  # then the skill block in full
    kinds = [t["kind"] for t in out.trace["budget"]["trimmed"]]
    assert kinds == ["history", "history", "skill"]
    assert out.system_prompt == "S" * 10  # system is NEVER trimmed


def test_butce_system_ve_kullanici_metni_asla_kirpilmaz(
    req, fixed_history, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "5")  # impossible budget
    _small_persona(req)
    out = _assemble(req, text="u" * 20)
    assert out.system_prompt == "S" * 10
    assert out.user_text == "u" * 20
    assert out.trace["budget"]["total_chars_after"] == 30  # stays above the limit


def test_butce_sifir_sinirsiz(req, fixed_history, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "0")
    out = _assemble(req, text="u" * 20, skill_block="k" * 30)
    assert len(out.history) == 2
    assert out.trace["budget"]["trimmed"] == []
    assert out.dropped_turns == 3  # the service counter passes through unchanged


def test_butce_config_tek_kaynak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AKANA_CONTEXT_MAX_CHARS", raising=False)
    assert context_budget_chars() == DEFAULT_MAX_CONTEXT_CHARS
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "bozuk")
    assert context_budget_chars() == DEFAULT_MAX_CONTEXT_CHARS
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "5000")
    assert context_budget_chars() == 5000
    # Out-of-range env values now clamp through the runtime-settings schema (min=0),
    # matching GET /settings/runtime: a negative is invalid -> default (0 = unlimited
    # is still honored, since 0 is in-range and passes validation).
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "0")
    assert context_budget_chars() == 0
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "-3")
    assert context_budget_chars() == DEFAULT_MAX_CONTEXT_CHARS
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "3000000")
    assert context_budget_chars() == DEFAULT_MAX_CONTEXT_CHARS


# == (B) prior-session summary injection ======================================= #
#
# The summary seam is injected (constructor) with a FAKE here; the lead wires the
# real get_session_summary at integration. The hard contract is behavior-
# neutrality: with the seam None, every test above stays green (they build
# ContextAssembler(req) with no seam).


def _assemble_with(req, *, summary_provider=None, **kwargs):
    """Assemble through the seam-aware constructor (fakes supplied by the test)."""
    kwargs.setdefault("conversation_id", CONV)
    asm = ContextAssembler(
        req,
        summary_provider=summary_provider,
    )
    return asyncio.run(asm.assemble(ContextRequest(**kwargs)))


#: The summary paragraph the fake provider returns (distinctive enough to assert on).
_FAKE_SUMMARY = (
    "Akana OSS i18n çalışması sürüyor: model-facing prompt'ları çevirmek ve "
    "testleri yeşile çekmek kaldı."
)


def _fake_view(conversation_id: str) -> SummaryView:
    return SummaryView(conversation_id=conversation_id, summary=_FAKE_SUMMARY)


# -- (B) injection ------------------------------------------------------------- #


def test_prior_context_injected_when_provider_returns_view(req) -> None:
    """A non-empty SummaryView → a [Prior context] block prepended to the user text."""
    out = _assemble_with(req, text="devam edelim", summary_provider=_fake_view)
    assert "[Prior context]" in out.user_text
    assert out.user_text.endswith("devam edelim")  # raw text stays at the tail
    assert _FAKE_SUMMARY in out.user_text  # the summary paragraph rendered verbatim
    kinds = [b["kind"] for b in out.injected_blocks]
    assert "prior_context" in kinds
    block = next(b for b in out.injected_blocks if b["kind"] == "prior_context")
    assert block["conversation_id"] == CONV
    assert block["chars"] > 0
    assert out.trace["prior_context"]["applied"] is True


def test_prior_context_default_no_provider_is_neutral(req) -> None:
    """No provider (default) → byte-for-byte the old user text, no block, no trace flip."""
    out = _assemble(req, text="merhaba")  # the seam-less helper == default None
    assert out.user_text == "merhaba"
    assert out.injected_blocks == []
    assert out.trace["prior_context"]["applied"] is False


def test_prior_context_tiny_budget_drops_block_no_half_marker() -> None:
    """CTX-6: a max_chars of 1..14 must NOT emit a broken '[Prior c' header
    fragment — a budget too small for the header + a minimal payload drops the
    block whole (marker integrity), like an empty view."""
    view = SummaryView(conversation_id=CONV, summary=_FAKE_SUMMARY)
    header = assembler_mod._PRIOR_CONTEXT_LABEL["en"]
    floor = len(header) + assembler_mod._MIN_PRIOR_CONTEXT_PAYLOAD
    # Below the floor: nothing rendered (no half header).
    for mc in (1, 8, len(header), floor - 1):
        assert assembler_mod._render_prior_context(view, "en", max_chars=mc) == ""
    # At/above the floor: the header survives intact and the summary is clipped.
    out = assembler_mod._render_prior_context(view, "en", max_chars=floor)
    assert out.startswith(header)
    assert len(out) <= floor
    # Turkish header (also 15 chars) behaves identically — no mid-marker cut.
    tr_header = assembler_mod._PRIOR_CONTEXT_LABEL["tr"]
    assert assembler_mod._render_prior_context(view, "tr", max_chars=5) == ""
    assert assembler_mod._render_prior_context(view, "tr", max_chars=500).startswith(tr_header)


def test_prior_context_empty_view_is_neutral(req) -> None:
    """A provider that returns an EMPTY view → nothing injected (a hollow marker misleads)."""
    out = _assemble_with(
        req,
        text="merhaba",
        summary_provider=lambda _cid: SummaryView(conversation_id=_cid),
    )
    assert out.user_text == "merhaba"
    assert [b["kind"] for b in out.injected_blocks] == []
    assert out.trace["prior_context"]["applied"] is False


def test_prior_context_provider_none_view_is_neutral(req) -> None:
    """Provider returns None (no stored summary) → neutral."""
    out = _assemble_with(req, text="merhaba", summary_provider=lambda _cid: None)
    assert out.user_text == "merhaba"
    assert out.trace["prior_context"]["applied"] is False


def test_prior_context_provider_failure_is_neutral(req) -> None:
    """A summary lookup that raises must not break the turn → neutral fallback."""
    def boom(_cid):
        raise RuntimeError("summary store locked")

    out = _assemble_with(req, text="merhaba", summary_provider=boom)
    assert out.user_text == "merhaba"
    assert out.trace["prior_context"]["applied"] is False


def test_prior_context_label_follows_language_tr(req, monkeypatch: pytest.MonkeyPatch) -> None:
    """language=tr → the Turkish marker [Önceki bağlam] is used."""
    monkeypatch.setattr(
        assembler_mod.ContextAssembler, "_active_language", lambda self: "tr"
    )
    out = _assemble_with(req, text="devam", summary_provider=_fake_view)
    assert "[Önceki bağlam]" in out.user_text
    assert "[Prior context]" not in out.user_text
    assert _FAKE_SUMMARY in out.user_text  # the summary paragraph rendered verbatim


def test_prior_context_coexists_with_skill_block(req) -> None:
    """[Yetenek] stays at the very front; [Prior context] sits between it and the raw text."""
    out = _assemble_with(
        req,
        text="rapor çıkar",
        skill_block=SKILL_BLOCK,
        skill_entries=SKILL_ENTRIES,
        summary_provider=_fake_view,
    )
    assert out.user_text.startswith(SKILL_BLOCK)  # skill block still first
    assert "[Prior context]" in out.user_text
    assert out.user_text.index(SKILL_BLOCK) < out.user_text.index("[Prior context]")
    assert {"skill", "prior_context"} <= {b["kind"] for b in out.injected_blocks}


# -- (C) drop-on-overflow (no summarizer) -------------------------------------- #


def test_overflow_drops_oldest_turn(
    req, fixed_history, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On budget overflow the oldest turn is dropped, no story-so-far block."""
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "120")
    _small_persona(req)
    out = _assemble(req, text="u" * 20, skill_block="k" * 30)  # seam-less == None
    assert [m["content"] for m in out.history] == ["b" * 50]  # oldest dropped
    trimmed = out.trace["budget"]["trimmed"]
    assert trimmed and trimmed[0]["kind"] == "history"
    assert "dropped" in trimmed[0]["reason"]
