"""SkillEngine F2 — hybrid retrieval: FTS5 Turkish folding, RRF fusion,
suggest_for_text contract, route backwards-compatible schema."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.skills.registry import (
    SkillRegistry,
    akana_skills_dir,
    reload_skills,
)
from akana_server.skills.retrieval import (
    SkillFtsIndex,
    fold_text,
    rrf_fuse,
)

BODY = "# Demo\n\nAdımlar burada.\n"


def _write_skill(
    root: Path,
    skill_id: str,
    *,
    description: str = "Demo açıklama",
    triggers: list[str] | None = None,
    tags: list[str] | None = None,
    requires_approval: bool | None = None,
    body: str = BODY,
) -> Path:
    d = root / skill_id
    d.mkdir(parents=True)
    fm = f"---\nname: {skill_id}\ndescription: {description}\n"
    if triggers:
        fm += "triggers:\n" + "".join(f'  - "{t}"\n' for t in triggers)
    if tags:
        fm += "tags:\n" + "".join(f"  - {t}\n" for t in tags)
    if requires_approval is not None:
        fm += f"requires_approval: {str(requires_approval).lower()}\n"
    (d / "SKILL.md").write_text(fm + "---\n\n" + body, encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    reload_skills()
    monkeypatch.setattr(
        "akana_server.skills.registry.cursor_skill_roots", lambda: []
    )
    yield
    reload_skills()


# -- fold_text + FTS Turkish folding --------------------------------------------------


def test_fold_text_turkish_pairs() -> None:
    assert fold_text("İZMİR") == fold_text("izmir") == "izmir"
    assert fold_text("IŞIK") == fold_text("ışık") == "ışık"
    assert fold_text("") == ""


def test_fts_index_turkish_folding(tmp_path: Path) -> None:
    idx = SkillFtsIndex(tmp_path / "db" / "skills.db")
    assert idx.rebuild([("dep", "İZMİR sunucusuna DAĞITIM yapar"), ("oth", "lint koşar")])
    assert idx.available
    assert idx.search("izmir") == ["dep"]
    assert idx.search("İZMİR DAĞITIM") == ["dep"]
    assert idx.search("???") == []  # a query yielding no terms returns empty


def test_registry_fts_matches_body_summary(tmp_path: Path) -> None:
    root = akana_skills_dir(tmp_path)
    _write_skill(root, "canary_skill", body="# K\n\nGövdede kanaryalar ötüyor.\n")
    _write_skill(root, "other_skill")
    reg = SkillRegistry(tmp_path)
    results = reg.search("kanaryalar")
    assert [r.entry.id for r in results] == ["canary_skill"]
    assert results[0].match_reason == "fts"  # substring not in metadata, but in body
    # L2 contract: FTS index build does not cache the body
    assert not reg.body_loaded("canary_skill")


def test_registry_search_turkish_case_fold(tmp_path: Path) -> None:
    root = akana_skills_dir(tmp_path)
    _write_skill(root, "izmir_deploy", description="İzmir sunucusuna dağıtım")
    reg = SkillRegistry(tmp_path)
    results = reg.search("İZMİR")
    assert results and results[0].entry.id == "izmir_deploy"


# -- RRF fusion ----------------------------------------------------------------------


def test_rrf_fuse_scores_and_order() -> None:
    fused = rrf_fuse([["a", "b"], ["b", "a"], ["b"]])
    assert [i for i, _ in fused] == ["b", "a"]
    scores = dict(fused)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61 + 1 / 61)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    # empty ranking contributes nothing; on ties id is alphabetical (deterministic)
    assert rrf_fuse([["x", "y"], [], []])[0][0] == "x"
    assert [i for i, _ in rrf_fuse([["b"], ["a"]])] == ["a", "b"]


# -- suggest_for_text (WI-1 contract) ----------------------------------------------


def test_suggest_trigger_short_circuit(tmp_path: Path) -> None:
    root = akana_skills_dir(tmp_path)
    _write_skill(
        root,
        "project_checks",
        description="Test ve lint çalıştırır",
        triggers=["testleri çalıştır"],
        requires_approval=True,
    )
    _write_skill(root, "deploy_skill", description="Dağıtım yapar")
    reg = SkillRegistry(tmp_path)
    out = reg.suggest_for_text("Lütfen TESTLERİ ÇALIŞTIR ve sonucu özetle", top_k=3)
    assert out[0]["id"] == "project_checks"
    assert out[0]["score"] == 1.0
    assert out[0]["match_reason"] == "trigger_exact"
    assert out[0]["requires_approval"] is True


def test_suggest_longest_trigger_wins_and_fills_hybrid(tmp_path: Path) -> None:
    root = akana_skills_dir(tmp_path)
    _write_skill(root, "generic_test", triggers=["test"], description="Genel test")
    _write_skill(
        root, "full_checks", triggers=["testleri çalıştır"], description="Tam kontrol"
    )
    _write_skill(root, "deploy_skill", description="Dağıtım senaryosu")
    reg = SkillRegistry(tmp_path)
    out = reg.suggest_for_text("testleri çalıştır sonra dağıtım yap", top_k=3)
    # both triggers appear in the text: the longest (more specific) comes first
    assert [o["id"] for o in out[:2]] == ["full_checks", "generic_test"]
    assert all(o["match_reason"] == "trigger_exact" for o in out[:2])
    # the remaining slot fills from hybrid; requires_approval present in every suggestion and bool
    assert out[2]["id"] == "deploy_skill"
    assert out[2]["requires_approval"] is False
    assert all(isinstance(o["requires_approval"], bool) for o in out)


def test_suggest_top_k_and_empty_text(tmp_path: Path) -> None:
    root = akana_skills_dir(tmp_path)
    _write_skill(root, "a_skill", triggers=["ortak tetik"])
    _write_skill(root, "b_skill", triggers=["ortak tetik"])
    reg = SkillRegistry(tmp_path)
    assert reg.suggest_for_text("   ") == []
    out = reg.suggest_for_text("ortak tetik", top_k=1)
    assert len(out) == 1
    assert out[0]["id"] == "a_skill"  # on ties id is alphabetical


# -- route backwards-compatible schema ---------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    root = akana_skills_dir(tmp_path)
    _write_skill(
        root,
        "project_checks",
        description="Test ve lint çalıştırır",
        triggers=["testleri çalıştır"],
        body="# PC\n\nGövdede kanaryalar var.\n",
    )
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_route_schema_backwards_compatible(client: TestClient) -> None:
    # F0/F1 schema: count + skills[] + query; skill fields id/title/source/...
    r = client.get("/api/v1/skills?q=testleri çalıştır")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"count", "skills", "query"}
    top = body["skills"][0]
    assert top["id"] == "project_checks"
    assert top["score"] == 1.0
    assert top["match_reason"] == "trigger_exact"
    for key in ("id", "source", "title", "path", "type", "risk", "trust_tier"):
        assert key in top


def test_route_hybrid_layer_in_match_reason(client: TestClient) -> None:
    # A word that appears only in the L2 body comes from the FTS layer, schema stays the same
    r = client.get("/api/v1/skills?q=kanaryalar")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["skills"][0]["id"] == "project_checks"
    assert "fts" in body["skills"][0]["match_reason"]

    # The list without q returns the full list (empty query = returns all skills).
    # Packs are always ON: in create_app() lifespan, register_all copies the skills
    # of all discovered packs into this data_dir (app.py:210-218), so the list
    # contains the fixture's project_checks + the pack skills.
    # Invariant: items in the list without q have no match_reason field (it is present
    # only in search results).
    r2 = client.get("/api/v1/skills")
    assert r2.status_code == 200
    listed = r2.json()
    by_id = {s["id"]: s for s in listed["skills"]}
    assert "project_checks" in by_id, "fixture skill should be in the list"
    assert listed["count"] == len(listed["skills"]) >= 1
    assert all("match_reason" not in s for s in listed["skills"])
