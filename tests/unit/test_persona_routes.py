"""Persona REST surface — list, user persona creation, binding, auth."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.persona.registry import reset_persona_registries

URL = "/api/v1/personas"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    reset_persona_registries()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_persona_registries()


def _create(client: TestClient, **overrides) -> dict:
    body = {"name": "Resmî Akana", "system_prompt": "Resmî ve kısa konuş.", "tone": "resmî"}
    body.update(overrides)
    r = client.post(URL, json=body)
    assert r.status_code == 201, r.text
    return r.json()["persona"]


# -- GET ------------------------------------------------------------------------ #


def test_liste_builtin_akana_icerir(client: TestClient) -> None:
    r = client.get(URL)
    assert r.status_code == 200
    body = r.json()
    by_id = {p["id"]: p for p in body["personas"]}
    assert by_id["akana"]["source"] == "builtin"
    # The title depends on the personal name ("Alice'in ...") — assert on the stable prefix.
    assert by_id["akana"]["system_prompt"].startswith("[Akana —")
    assert body["bindings"] == []


# -- POST ------------------------------------------------------------------------ #


def test_user_persona_olusturma_ve_listede_gorunme(client: TestClient) -> None:
    created = _create(client, id="resmi")
    assert created["id"] == "resmi" and created["source"] == "user"
    listed = {p["id"] for p in client.get(URL).json()["personas"]}
    assert {"akana", "resmi"} <= listed


def test_id_verilmezse_isimden_slug(client: TestClient) -> None:
    created = _create(client, name="Kuru Espri 2")
    assert created["id"] == "kuru-espri-2"


def test_cakisan_id_409(client: TestClient) -> None:
    _create(client, id="tek")
    r = client.post(URL, json={"id": "tek", "name": "X", "system_prompt": "y"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"]["code"] == "PERSONA_EXISTS"


def test_builtin_id_ezilemez(client: TestClient) -> None:
    r = client.post(URL, json={"id": "akana", "name": "Sahte", "system_prompt": "x"})
    assert r.status_code == 409


def test_gecersiz_id_400(client: TestClient) -> None:
    r = client.post(URL, json={"id": "Büyük İd!", "name": "X", "system_prompt": "y"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "PERSONA_INVALID"


def test_eksik_govde_422(client: TestClient) -> None:
    assert client.post(URL, json={"name": "X"}).status_code == 422


def test_system_prompt_sinir_degerleri(client: TestClient) -> None:
    # Exactly 20,000 characters (multi-byte) accepted; one more is a 422.
    ok = client.post(
        URL, json={"id": "sinirda", "name": "B", "system_prompt": "ş" * 20_000}
    )
    assert ok.status_code == 201, ok.text
    bad = client.post(
        URL, json={"id": "sinirustu", "name": "B", "system_prompt": "ş" * 20_001}
    )
    assert bad.status_code == 422


def test_bozuk_json_govde_422(client: TestClient) -> None:
    r = client.post(
        URL, content="{bozuk", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 422


# -- PUT /{id}/bind ---------------------------------------------------------------- #


def test_kanal_baglama(client: TestClient) -> None:
    _create(client, id="resmi")
    r = client.put(f"{URL}/resmi/bind", json={"channel": "Telegram"})
    assert r.status_code == 200
    body = r.json()
    assert body["bound"] == {"channel": "telegram"}
    assert body["bindings"] == [
        {
            "scope": "channel",
            "key": "telegram",
            "persona_id": "resmi",
            "updated_at": body["bindings"][0]["updated_at"],
        }
    ]


def test_konusma_baglama_ve_listede_gorunme(client: TestClient) -> None:
    _create(client, id="resmi")
    r = client.put(f"{URL}/resmi/bind", json={"conversation_id": "c42"})
    assert r.status_code == 200
    scopes = {(b["scope"], b["key"]) for b in client.get(URL).json()["bindings"]}
    assert ("conversation", "c42") in scopes


def test_bilinmeyen_persona_bind_404(client: TestClient) -> None:
    r = client.put(f"{URL}/yok/bind", json={"channel": "telegram"})
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "PERSONA_NOT_FOUND"


def test_hedefsiz_bind_400(client: TestClient) -> None:
    r = client.put(f"{URL}/akana/bind", json={})
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "PERSONA_BIND_INVALID"


# -- PUT /{id} (update) ----------------------------------------------------------- #


def test_user_persona_guncelle(client: TestClient) -> None:
    _create(client, id="resmi")
    r = client.put(
        f"{URL}/resmi",
        json={"name": "Resmî v2", "system_prompt": "Daha da kısa.", "tone": "net"},
    )
    assert r.status_code == 200, r.text
    p = r.json()["persona"]
    assert p["name"] == "Resmî v2" and p["system_prompt"] == "Daha da kısa."
    listed = {x["id"]: x for x in client.get(URL).json()["personas"]}
    assert listed["resmi"]["system_prompt"] == "Daha da kısa."  # the current state is in the list


def test_builtin_guncellenemez_403(client: TestClient) -> None:
    r = client.put(f"{URL}/akana", json={"name": "Sahte", "system_prompt": "x"})
    assert r.status_code == 403
    assert r.json()["detail"]["error"]["code"] == "PERSONA_READONLY"


def test_olmayan_guncelle_404(client: TestClient) -> None:
    r = client.put(f"{URL}/yok", json={"name": "X", "system_prompt": "y"})
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "PERSONA_NOT_FOUND"


def test_guncelle_bos_prompt_422(client: TestClient) -> None:
    _create(client, id="resmi")
    bad = client.put(f"{URL}/resmi", json={"name": "X", "system_prompt": ""})
    assert bad.status_code == 422


# -- DELETE /{id} ----------------------------------------------------------------- #


def test_user_persona_sil(client: TestClient) -> None:
    _create(client, id="resmi")
    r = client.delete(f"{URL}/resmi")
    assert r.status_code == 200 and r.json()["deleted"] == "resmi"
    assert "resmi" not in {p["id"] for p in client.get(URL).json()["personas"]}


def test_sil_baglamayi_da_temizler(client: TestClient) -> None:
    _create(client, id="resmi")
    assert client.put(f"{URL}/resmi/bind", json={"channel": "web"}).status_code == 200
    assert client.delete(f"{URL}/resmi").status_code == 200
    assert client.get(URL).json()["bindings"] == []  # no dangling binding


def test_builtin_silinemez_403(client: TestClient) -> None:
    r = client.delete(f"{URL}/akana")
    assert r.status_code == 403
    assert r.json()["detail"]["error"]["code"] == "PERSONA_READONLY"


def test_olmayan_sil_404(client: TestClient) -> None:
    r = client.delete(f"{URL}/yok")
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "PERSONA_NOT_FOUND"


# -- PUT/DELETE /personas/base (core prompt override) ----------------------------- #


def test_base_prompt_override_ve_reset(client: TestClient) -> None:
    body = client.get(URL).json()
    akana = next(p for p in body["personas"] if p["id"] == "akana")
    assert akana["system_prompt"].startswith("[Akana —")  # the code default
    assert body["base"]["is_override"] is False
    r = client.put(f"{URL}/base", json={"system_prompt": "Sen Test-Akana'sın."})
    assert r.status_code == 200 and r.json()["is_override"] is True
    after_put = client.get(URL).json()
    akana2 = next(p for p in after_put["personas"] if p["id"] == "akana")
    assert akana2["system_prompt"] == "Sen Test-Akana'sın."  # the override is reflected in akana
    assert after_put["base"]["is_override"] is True  # GET reports the override
    assert client.delete(f"{URL}/base").status_code == 200
    after_delete = client.get(URL).json()
    akana3 = next(p for p in after_delete["personas"] if p["id"] == "akana")
    assert akana3["system_prompt"].startswith("[Akana —")  # reverted to the code
    # After reset, GET must report is_override=False — the UI's "Reset to default"
    # button is drawn ONLY when an override exists; if this isn't False the button won't disappear.
    assert after_delete["base"]["is_override"] is False


def test_base_route_persona_id_ile_karismaz(client: TestClient) -> None:
    # PUT /personas/base must not fall through to update_persona (persona_id="base") → 200, not 404.
    assert client.put(f"{URL}/base", json={"system_prompt": "x"}).status_code == 200


def test_base_bos_422(client: TestClient) -> None:
    assert client.put(f"{URL}/base", json={"system_prompt": ""}).status_code == 422


# -- PUT/DELETE /personas/voice-directive (voice-mode directive override) -------- #


def test_voice_directive_override_ve_reset(client: TestClient) -> None:
    body = client.get(URL).json()
    vd = body["voice_directive"]
    assert vd["is_override"] is False
    assert "Voice mode is active" in vd["default"]  # English-first code default
    assert vd["value"] == vd["default"]  # no override → effective == default
    r = client.put(f"{URL}/voice-directive", json={"voice_directive": "Speak briefly."})
    assert r.status_code == 200 and r.json()["is_override"] is True
    assert client.get(URL).json()["voice_directive"]["value"] == "Speak briefly."
    assert client.delete(f"{URL}/voice-directive").status_code == 200
    assert client.get(URL).json()["voice_directive"]["is_override"] is False


def test_voice_directive_route_persona_id_ile_karismaz(client: TestClient) -> None:
    # /personas/voice-directive must NOT fall through to update_persona → 200, not 404.
    assert client.put(f"{URL}/voice-directive", json={"voice_directive": "x"}).status_code == 200


def test_voice_directive_bos_422(client: TestClient) -> None:
    assert client.put(f"{URL}/voice-directive", json={"voice_directive": ""}).status_code == 422


# -- PUT/DELETE /personas/catalog (capability SELECTION — id list) ---------------- #


def test_catalog_selection_ve_reset(client: TestClient) -> None:
    cat = client.get(URL).json()["catalog"]
    assert cat["selection"] is None  # default: all (auto)
    assert isinstance(cat["skills"], list)  # list of installed skills (for the UI selection)
    r = client.put(f"{URL}/catalog", json={"selection": ["a", "b"]})
    assert r.status_code == 200 and r.json()["selection"] == ["a", "b"]
    assert client.get(URL).json()["catalog"]["selection"] == ["a", "b"]
    assert client.delete(f"{URL}/catalog").status_code == 200
    assert client.get(URL).json()["catalog"]["selection"] is None


def test_catalog_bos_secim_resolve_bos(client: TestClient, tmp_path) -> None:
    import types

    from akana_server.skills.catalog import resolve_catalog

    client.put(f"{URL}/catalog", json={"selection": []})
    # empty selection = none included → catalog text is "" (even if a skill exists, it's filtered out to empty)
    assert resolve_catalog(types.SimpleNamespace(data_dir=tmp_path)) == ""


def test_get_personas_base_ve_catalog_meta(client: TestClient) -> None:
    body = client.get(URL).json()
    assert set(body["base"].keys()) == {"is_override", "default"}
    assert set(body["catalog"].keys()) == {"enabled", "selection", "skills"}
    assert body["base"]["default"].startswith("[Akana —")


# -- persistence + auth ------------------------------------------------------------- #


def test_persona_ve_baglama_kalicidir(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    reset_persona_registries()
    with TestClient(create_app()) as c1:
        _create(c1, id="resmi")
        assert c1.put(f"{URL}/resmi/bind", json={"channel": "telegram"}).status_code == 200
    reset_persona_registries()  # fresh-process simulation — must be read from the db
    with TestClient(create_app()) as c2:
        body = c2.get(URL).json()
        assert "resmi" in {p["id"] for p in body["personas"]}
        assert {(b["scope"], b["key"], b["persona_id"]) for b in body["bindings"]} == {
            ("channel", "telegram", "resmi")
        }
    reset_persona_registries()


def test_bearer_zorunlu(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "gizli-token")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    reset_persona_registries()
    # The token gate applies only to PROXIED requests; X-Forwarded-For makes the
    # request look proxied so the configured token is actually enforced.
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    with TestClient(create_app()) as c:
        assert c.get(URL, headers=proxied).status_code == 401
        assert (
            c.get(
                URL, headers={**proxied, "Authorization": "Bearer gizli-token"}
            ).status_code
            == 200
        )
    reset_persona_registries()
