"""PersonaEngine F0 — registry source merging, resolve priority matrix,
user persona persistence, and proof that the chat system-prompt bridge is not broken."""

from __future__ import annotations

import sqlite3

import pytest

from akana_server.persona import (
    CHAT_SYSTEM_PREFIX,
    DEFAULT_PERSONA_ID,
    Persona,
    PersonaError,
    PersonaRegistry,
    builtin_personas,
)
from akana_server.persona.registry import channel_env_var, get_persona_registry
from akana_server.persona.store import PersonaStore


@pytest.fixture
def registry(tmp_path) -> PersonaRegistry:
    return PersonaRegistry(tmp_path)


class _StubPackSource:
    """Duck-typed pack source — the minimum of the PersonasAdapter contract."""

    def __init__(self, items):
        self._items = items

    def get_active_personas(self):
        return self._items


class _BrokenSource:
    def get_active_personas(self):
        raise RuntimeError("patladı")


# -- chat system-prompt bridge (existing path not broken) ---------------------- #


def test_chat_persona_koprusu_tek_kaynak() -> None:
    """orchestrator.chat_persona re-exports the same constant from the persona module."""
    from akana_server.orchestrator import chat_persona

    assert chat_persona.CHAT_SYSTEM_PREFIX is CHAT_SYSTEM_PREFIX
    # The title depends on the personal name ("Alice's ...") — anchor to the stable prefix.
    assert chat_persona.CHAT_SYSTEM_PREFIX.startswith("[Akana —")
    assert "memory_search" in chat_persona.CHAT_SYSTEM_PREFIX


def test_wrap_chat_user_message_davranisi_ayni() -> None:
    from akana_server.orchestrator.chat_persona import wrap_chat_user_message

    wrapped = wrap_chat_user_message("merhaba")
    assert wrapped.startswith(CHAT_SYSTEM_PREFIX)
    assert wrapped.endswith("merhaba")
    assert wrap_chat_user_message("  ") == CHAT_SYSTEM_PREFIX


def test_builtin_akana_chat_prefixten_turetilir() -> None:
    (akana,) = builtin_personas()
    assert akana.id == DEFAULT_PERSONA_ID
    assert akana.source == "builtin"
    assert akana.system_prompt is CHAT_SYSTEM_PREFIX  # not a copy, the same object
    assert akana.tone  # K-α tone notes are not empty


# -- source merging ------------------------------------------------------------- #


def test_list_builtin_pack_user_birlesimi(registry: PersonaRegistry) -> None:
    registry.attach_pack_source(
        _StubPackSource(
            [
                {
                    "id": "re_analyst",
                    "name": "RE Analyst",
                    "system_prompt": "Sen bir tersine mühendislik analistisin.",
                    "_pack_id": "user/re-pack",
                }
            ]
        )
    )
    registry.create_user_persona(
        persona_id="resmi", name="Resmî Akana", system_prompt="Resmî konuş."
    )

    by_id = {p.id: p for p in registry.list()}
    assert by_id[DEFAULT_PERSONA_ID].source == "builtin"
    assert by_id["re_analyst"].source == "pack:user/re-pack"
    assert by_id["resmi"].source == "user"


def test_bozuk_pack_kaynagi_yuzeyi_kiramaz(registry: PersonaRegistry) -> None:
    registry.attach_pack_source(_BrokenSource())
    registry.attach_pack_source(_StubPackSource([{"id": "x"}, "çöp", None]))  # missing prompt
    ids = {p.id for p in registry.list()}
    assert ids == {DEFAULT_PERSONA_ID}  # broken source/record is silently skipped


def test_attach_pack_source_idempotent_ve_ducktyped(registry: PersonaRegistry) -> None:
    src = _StubPackSource([{"id": "p1", "system_prompt": "x"}])
    registry.attach_pack_source(src)
    registry.attach_pack_source(src)  # the second add is ignored
    registry.attach_pack_source(object())  # no get_active_personas → ignored
    assert [p.id for p in registry.list() if p.id == "p1"] == ["p1"]


def test_id_cakismasinda_builtin_kazanir(registry: PersonaRegistry) -> None:
    registry.attach_pack_source(
        _StubPackSource(
            [{"id": DEFAULT_PERSONA_ID, "system_prompt": "sahte akana", "_pack_id": "evil"}]
        )
    )
    assert registry.get(DEFAULT_PERSONA_ID).source == "builtin"


# -- user persona CRUD + persistence --------------------------------------------- #


def test_user_persona_kalicidir(tmp_path) -> None:
    reg1 = PersonaRegistry(tmp_path)
    reg1.create_user_persona(
        persona_id="kuru", name="Kuru", system_prompt="Kuru espri yap.", tone="ironik"
    )
    # A new registry on the same data_dir → reads from db/persona.db.
    reg2 = PersonaRegistry(tmp_path)
    p = reg2.get("kuru")
    assert p is not None and p.source == "user" and p.tone == "ironik"
    assert (tmp_path / "db" / "persona.db").is_file()


def test_user_persona_dogrulama_ve_cakisma(registry: PersonaRegistry) -> None:
    with pytest.raises(PersonaError):
        registry.create_user_persona(persona_id="Büyük İd", name="x", system_prompt="y")
    with pytest.raises(PersonaError):
        registry.create_user_persona(persona_id="bos", name=" ", system_prompt="y")
    with pytest.raises(PersonaError):  # no collision onto a builtin id
        registry.create_user_persona(
            persona_id=DEFAULT_PERSONA_ID, name="x", system_prompt="y"
        )
    registry.create_user_persona(persona_id="tek", name="Tek", system_prompt="z")
    with pytest.raises(PersonaError):
        registry.create_user_persona(persona_id="tek", name="Tek2", system_prompt="w")


def test_store_append_only_event_log(tmp_path) -> None:
    store = PersonaStore(tmp_path / "db" / "persona.db")
    store.create(Persona(id="a", name="A", system_prompt="p", source="user"))
    store.set_binding("channel", "telegram", "a")
    store.set_binding("channel", "telegram", "a")  # even an upsert writes a new event
    rows = sqlite3.connect(tmp_path / "db" / "persona.db").execute(
        "SELECT action FROM persona_events ORDER BY seq"
    ).fetchall()
    assert len(rows) == 3  # append-only: even an upsert writes a new event
    assert [r[0] for r in rows] == ["persona_created", "binding_set", "binding_set"]


# -- resolve priority matrix ----------------------------------------------------- #


def _make(tmp_path, skill_prompt=None) -> PersonaRegistry:
    reg = PersonaRegistry(
        tmp_path, skill_persona_resolver=lambda _sid: skill_prompt
    )
    reg.create_user_persona(persona_id="kanal-p", name="Kanal", system_prompt="kanal promptu")
    reg.create_user_persona(persona_id="konusma-p", name="Konuşma", system_prompt="konuşma promptu")
    return reg


def test_resolve_default_akana(tmp_path) -> None:
    reg = _make(tmp_path)
    assert reg.resolve().id == DEFAULT_PERSONA_ID
    assert reg.resolve(channel="telegram", conversation_id="c1").id == DEFAULT_PERSONA_ID


def test_resolve_bilinmeyen_kanal_default_akana(tmp_path, monkeypatch) -> None:
    """A channel with no binding/env (and an odd name) falls back to akana without failing."""
    monkeypatch.delenv("AKANA_PERSONA_TUHAF_KANAL_X", raising=False)
    reg = _make(tmp_path)
    assert reg.resolve(channel="tuhaf kanal-X").id == DEFAULT_PERSONA_ID
    assert reg.resolve(channel="").id == DEFAULT_PERSONA_ID


def test_system_prompt_boyut_siniri_karakter_sayar() -> None:
    """The MAX_PROMPT limit is in characters (not bytes, tested with the multi-byte 'ş')."""
    from akana_server.persona.models import MAX_PROMPT, validate_persona_fields

    validate_persona_fields("sinirda", "X", "ş" * MAX_PROMPT, "")  # exactly at the limit: passes
    with pytest.raises(PersonaError):
        validate_persona_fields("sinirustu", "X", "ş" * (MAX_PROMPT + 1), "")


def test_resolve_kanal_baglamasi(tmp_path) -> None:
    reg = _make(tmp_path)
    reg.bind("kanal-p", channel="Telegram")  # normalized: lowercase
    assert reg.resolve(channel="telegram").id == "kanal-p"
    assert reg.resolve(channel="web").id == DEFAULT_PERSONA_ID


def test_resolve_kanal_env_config(tmp_path, monkeypatch) -> None:
    reg = _make(tmp_path)
    assert channel_env_var("telegram") == "AKANA_PERSONA_TELEGRAM"
    monkeypatch.setenv("AKANA_PERSONA_TELEGRAM", "kanal-p")
    assert reg.resolve(channel="telegram").id == "kanal-p"
    # The persistent binding in the store takes priority over env.
    reg.bind("konusma-p", channel="telegram")
    assert reg.resolve(channel="telegram").id == "konusma-p"
    # If env points to an unknown persona, it falls back to the default.
    monkeypatch.setenv("AKANA_PERSONA_SLACK", "yok-boyle-biri")
    assert reg.resolve(channel="slack").id == DEFAULT_PERSONA_ID


def test_resolve_konusma_kanali_ezer(tmp_path) -> None:
    reg = _make(tmp_path)
    reg.bind("kanal-p", channel="telegram")
    reg.bind("konusma-p", conversation_id="c42")
    assert reg.resolve(channel="telegram", conversation_id="c42").id == "konusma-p"
    assert reg.resolve(channel="telegram", conversation_id="başka").id == "kanal-p"


def test_resolve_skill_hepsini_ezer(tmp_path) -> None:
    reg = _make(tmp_path, skill_prompt="Sen RE analistisin.")
    reg.bind("kanal-p", channel="telegram")
    reg.bind("konusma-p", conversation_id="c42")
    p = reg.resolve(channel="telegram", conversation_id="c42", skill="re_triage")
    assert p.id == "skill:re_triage"
    assert p.source == "pack:skill"
    assert p.system_prompt == "Sen RE analistisin."


def test_resolve_skill_hatasi_zinciri_kirmaz(tmp_path) -> None:
    def boom(_sid: str) -> str:
        raise RuntimeError("pack taraması çöktü")

    reg = PersonaRegistry(tmp_path, skill_persona_resolver=boom)
    assert reg.resolve(skill="re_triage").id == DEFAULT_PERSONA_ID


def test_resolve_kayip_baglama_dusumu(tmp_path) -> None:
    """If a binding exists but the persona can no longer be resolved, it falls to the lower tier."""
    reg = _make(tmp_path)
    reg.store.set_binding("conversation", "c1", "silinmis-persona")
    reg.bind("kanal-p", channel="telegram")
    assert reg.resolve(channel="telegram", conversation_id="c1").id == "kanal-p"


def test_bind_dogrulama(registry: PersonaRegistry) -> None:
    with pytest.raises(KeyError):
        registry.bind("yok", channel="telegram")
    with pytest.raises(PersonaError):
        registry.bind(DEFAULT_PERSONA_ID)  # no target


def test_get_persona_registry_cache(tmp_path) -> None:
    from akana_server.persona.registry import reset_persona_registries

    reset_persona_registries()
    try:
        a = get_persona_registry(tmp_path)
        b = get_persona_registry(tmp_path)
        assert a is b
    finally:
        reset_persona_registries()
