"""QUALITY TOUR — area boundary-case tests (skills/packs/persona/
context).

Evidence-based: each test either LOCKS a boundary behavior (regression shield)
or BREAKS a discovered bug (red before the fix). Bugs are marked in the test title
with ``regresyon:``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akana_server.persona.models import (
    MAX_PROMPT,
    PersonaError,
    validate_persona_fields,
)
from akana_server.persona.registry import (
    PersonaRegistry,
    channel_env_var,
    reset_persona_registries,
)
from akana_server.skills.parser import SkillParseError, parse_skill_md
from akana_server.skills.registry import SkillRegistry, reload_skills
from akana_server.skills.retrieval import SkillFtsIndex


# --------------------------------------------------------------------------- #
# SKILLS — parser / retrieval / suggest boundaries                            #
# --------------------------------------------------------------------------- #


def _write_skill(root: Path, sid: str, *, triggers: list[str], desc: str = "demo") -> None:
    d = root / "skills" / sid
    d.mkdir(parents=True)
    trig_yaml = "".join(f'  - "{t}"\n' for t in triggers)
    (d / "manifest.yaml").write_text(
        f"name: {sid}\ndescription: {desc}\ntriggers:\n{trig_yaml}", encoding="utf-8"
    )
    (d / "SKILL.md").write_text(f"# {sid}\n\ngövde\n", encoding="utf-8")


def test_skill_frontmatter_acilmis_kapanmamis_hata() -> None:
    with pytest.raises(SkillParseError, match="no closing '---' line was found"):
        parse_skill_md("---\nname: x\nbody without close", require_frontmatter=True)


def test_skill_frontmatter_mapping_degil_hata() -> None:
    with pytest.raises(SkillParseError, match="mapping"):
        parse_skill_md("---\n- a\n- b\n---\nbody", require_frontmatter=True)


def test_skill_cok_buyuk_govde_parse_olur() -> None:
    # A very large body must not break the parse; the body is carried over verbatim.
    big = "x" * 200_000
    parsed = parse_skill_md(f"---\nname: a\ndescription: b\n---\n{big}")
    assert parsed.body.strip() == big


@pytest.fixture(autouse=True)
def _clear_caches():
    reload_skills()
    reset_persona_registries()
    yield
    reload_skills()
    reset_persona_registries()


def test_suggest_bos_metin_bos_liste(tmp_path: Path) -> None:
    _write_skill(tmp_path, "deploy", triggers=["dağıt"])
    reg = SkillRegistry(tmp_path, include_cursor=False)
    reg.reload()
    assert reg.suggest_for_text("   ") == []
    assert reg.suggest_for_text("") == []


def test_suggest_dev_metin_patlamaz(tmp_path: Path) -> None:
    _write_skill(tmp_path, "deploy", triggers=["dağıt"])
    reg = SkillRegistry(tmp_path, include_cursor=False)
    reg.reload()
    # 50K characters of meaningless text must not leak an exception.
    res = reg.suggest_for_text("z" * 50_000)
    assert isinstance(res, list)


def test_regresyon_tek_harf_trigger_gurultude_exact_uretmez(tmp_path: Path) -> None:
    """BUG: a single-letter trigger ('e') appearing as a substring in ordinary text
    produced ``trigger_exact`` (score 1.0), pinning an irrelevant skill to the top.
    Fix: a short trigger only matches on exact equality."""
    _write_skill(tmp_path, "noise", triggers=["e"])
    _write_skill(tmp_path, "deploy", triggers=["dağıtım yap"])
    reg = SkillRegistry(tmp_path, include_cursor=False)
    reg.reload()
    res = reg.suggest_for_text("merhaba dünya nasılsın")
    # the 'noise' skill must no longer come back via trigger_exact.
    reasons = {r["id"]: r["match_reason"] for r in res}
    assert reasons.get("noise") != "trigger_exact"
    assert all(r["score"] < 1.0 or r["match_reason"] != "trigger_exact" for r in res
               if r["id"] == "noise")


def test_tek_harf_trigger_tam_metinde_hala_eslesir(tmp_path: Path) -> None:
    # If the user actually types 'e' the match is preserved (the inverse of the regression).
    _write_skill(tmp_path, "noise", triggers=["e"])
    reg = SkillRegistry(tmp_path, include_cursor=False)
    reg.reload()
    res = reg.suggest_for_text("e")
    assert any(r["id"] == "noise" and r["match_reason"] == "trigger_exact" for r in res)


def test_regresyon_iki_harf_trigger_gurultude_exact_uretmez(tmp_path: Path) -> None:
    """BUG: a 2-letter trigger ('as') appearing as a substring in ordinary text such
    as 'nasılsın' produced ``trigger_exact`` (score 1.0). Fix: 1–2 letters only on exact equality."""
    _write_skill(tmp_path, "noise", triggers=["as"])
    reg = SkillRegistry(tmp_path, include_cursor=False)
    reg.reload()
    res = reg.suggest_for_text("merhaba dünya nasılsın")
    reasons = {r["id"]: r["match_reason"] for r in res}
    assert reasons.get("noise") != "trigger_exact"


def test_iki_harf_trigger_tam_metinde_hala_eslesir(tmp_path: Path) -> None:
    _write_skill(tmp_path, "noise", triggers=["as"])
    reg = SkillRegistry(tmp_path, include_cursor=False)
    reg.reload()
    res = reg.suggest_for_text("as")
    assert any(r["id"] == "noise" and r["match_reason"] == "trigger_exact" for r in res)


def test_fts_ozel_karakter_sorgusu_patlamaz(tmp_path: Path) -> None:
    # FTS5 special-syntax characters (* : " ( OR) must not break the query.
    idx = SkillFtsIndex(tmp_path / "db" / "skills.db")
    idx.rebuild([("a", "dağıtım yap"), ("b", "test çalıştır")])
    assert idx.available
    for q in ['dağıt* OR (x', 'NEAR("a"', 'foo: bar', '""', '* * *']:
        assert isinstance(idx.search(q), list)  # no exception


def test_fts_yalniz_kisa_terim_bos_doner(tmp_path: Path) -> None:
    idx = SkillFtsIndex(tmp_path / "db" / "skills.db")
    idx.rebuild([("a", "dağıtım")])
    # single-letter terms are below the min length → no match → empty
    assert idx.search("a") == []


def test_es_zamanli_reload_tutarli(tmp_path: Path) -> None:
    import threading

    _write_skill(tmp_path, "deploy", triggers=["dağıt"])
    reg = SkillRegistry(tmp_path, include_cursor=False)
    reg.reload()
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(20):
                reg.reload()
                reg.suggest_for_text("dağıt şunu")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert reg.get("deploy") is not None


# --------------------------------------------------------------------------- #
# PERSONA — validation boundaries / channel / env                             #
# --------------------------------------------------------------------------- #


def test_persona_system_prompt_tam_sinir_kabul() -> None:
    # Exactly MAX_PROMPT length is ACCEPTED; one more is REJECTED.
    validate_persona_fields("p", "Ad", "x" * MAX_PROMPT, "")
    with pytest.raises(PersonaError, match="system_prompt"):
        validate_persona_fields("p", "Ad", "x" * (MAX_PROMPT + 1), "")


def test_persona_id_buyuk_harf_ret() -> None:
    with pytest.raises(PersonaError, match="id"):
        validate_persona_fields("Bad-ID", "Ad", "prompt", "")


def test_channel_env_var_ozel_karakter_temizlenir() -> None:
    assert channel_env_var("tele gram!") == "AKANA_PERSONA_TELE_GRAM_"
    assert channel_env_var("") == "AKANA_PERSONA_"


def test_persona_bilinmeyen_kanal_default_akana(tmp_path: Path) -> None:
    reg = PersonaRegistry(tmp_path)
    p = reg.resolve(channel="bilinmeyen-kanal-xyz")
    assert p.id == "akana"  # no binding → default


def test_persona_builtin_id_kullanilamaz(tmp_path: Path) -> None:
    reg = PersonaRegistry(tmp_path)
    with pytest.raises(PersonaError, match="in use"):
        reg.create_user_persona(
            persona_id="akana", name="Sahte", system_prompt="x"
        )


def test_persona_baglama_konusma_yok_hata(tmp_path: Path) -> None:
    reg = PersonaRegistry(tmp_path)
    reg.create_user_persona(persona_id="p1", name="P1", system_prompt="x")
    with pytest.raises(PersonaError, match="channel or conversation_id is required"):
        reg.bind("p1")  # neither channel nor conversation_id
