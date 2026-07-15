"""Bug-blitz 4 — infra-files-packs regression tests.

Covers five verified bugs:
  1. PersonasAdapter._load path-traversal / absolute-path file read (adapters.py)
  2. screenshot() monitor origin not added back to click coords (computer_mcp)
  3. open_application routes through ``cmd /c start`` → metacharacter injection
  4. GET /skills applies type/source filters AFTER the top_k cap (skills route)
  5. computer-control screenshots accumulate forever with no retention (computer_mcp)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

import akana_server.computer_mcp.__main__ as cm
from akana_server.api.app import create_app
from akana_server.computer_mcp import build_server
from akana_server.packs.adapters import PersonasAdapter
from akana_server.skills.registry import akana_skills_dir, reload_skills


# --------------------------------------------------------------------------- #
# Finding 1 — persona id path-traversal guard                                 #
# --------------------------------------------------------------------------- #


def test_persona_load_rejects_relative_traversal(tmp_path: Path) -> None:
    # An out-of-pack secret the manifest must NOT be able to reach.
    secret = tmp_path / "secret.yaml"
    secret.write_text("system_prompt: LEAKED\n", encoding="utf-8")
    root = tmp_path / "pack"
    (root / "personas").mkdir(parents=True)
    # root/personas/../../secret.yaml == tmp_path/secret.yaml — must be refused.
    assert PersonasAdapter._load(root, "../../secret") is None


def test_persona_load_rejects_absolute_id(tmp_path: Path) -> None:
    secret = tmp_path / "secret.yaml"
    secret.write_text("system_prompt: LEAKED\n", encoding="utf-8")
    root = tmp_path / "pack"
    (root / "personas").mkdir(parents=True)
    # An absolute id (ext appended → the secret file) replaces the base on join.
    abs_id = str(secret.with_suffix(""))
    assert PersonasAdapter._load(root, abs_id) is None


def test_persona_load_accepts_plain_id(tmp_path: Path) -> None:
    root = tmp_path / "pack"
    (root / "personas").mkdir(parents=True)
    (root / "personas" / "friendly.yaml").write_text("system_prompt: hi\n", encoding="utf-8")
    assert PersonasAdapter._load(root, "friendly") == {"system_prompt": "hi"}


# --------------------------------------------------------------------------- #
# Finding 2 — screenshot monitor origin added back before clicking            #
# --------------------------------------------------------------------------- #


class _FakePG:
    """A stand-in pyautogui recording the absolute coordinates it is handed."""

    class FailSafeException(Exception):
        pass

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def click(self, **kw):
        self.calls.append(("click", kw))

    def moveTo(self, *a):
        self.calls.append(("moveTo", a))

    def dragTo(self, *a, **kw):
        self.calls.append(("dragTo", a, kw))


def _call(server, name, args):
    return asyncio.run(server.call_tool(name, args))


def test_click_adds_monitor_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePG()
    monkeypatch.setattr(cm, "_pyautogui", lambda: fake)
    server = build_server()  # build resets _LAST_ORIGIN to [0, 0]
    # Emulate a screenshot of a secondary monitor at virtual origin (1920, 0).
    cm._LAST_ORIGIN[:] = [1920, 0]
    _call(server, "left_click", {"x": 500, "y": 300})
    assert fake.calls == [("click", {"x": 2420, "y": 300, "button": "left"})]


def test_drag_adds_monitor_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePG()
    monkeypatch.setattr(cm, "_pyautogui", lambda: fake)
    server = build_server()
    cm._LAST_ORIGIN[:] = [1920, 0]
    _call(server, "drag", {"x1": 10, "y1": 20, "x2": 30, "y2": 40})
    assert ("moveTo", (1930, 20)) in fake.calls
    assert any(c[0] == "dragTo" and c[1] == (1950, 40) for c in fake.calls)


def test_click_origin_zero_is_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # Primary monitor at the virtual origin: coords pass through unchanged.
    fake = _FakePG()
    monkeypatch.setattr(cm, "_pyautogui", lambda: fake)
    server = build_server()
    _call(server, "left_click", {"x": 640, "y": 480})
    assert fake.calls == [("click", {"x": 640, "y": 480, "button": "left"})]


# --------------------------------------------------------------------------- #
# Finding 3 — open_application must not route through cmd.exe                  #
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launch path only")
def test_open_application_does_not_use_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    startfile_args: list[str] = []
    popen_calls: list[list] = []
    monkeypatch.setattr(cm.os, "startfile", lambda p: startfile_args.append(p), raising=False)
    monkeypatch.setattr(
        cm.subprocess, "Popen", lambda *a, **k: popen_calls.append(list(a[0]))
    )
    server = build_server()
    # A name with '&' and no spaces: cmd would split this into two commands.
    _call(server, "open_application", {"name": "notes&calc"})
    # The whole name goes to the launcher intact, with NO cmd.exe re-parsing.
    assert startfile_args == ["notes&calc"]
    assert popen_calls == []


# --------------------------------------------------------------------------- #
# Finding 5 — screenshot retention cap                                        #
# --------------------------------------------------------------------------- #


def test_prune_shots_keeps_newest_n(tmp_path: Path) -> None:
    d = tmp_path / "run" / "computer"
    d.mkdir(parents=True)
    # 60 ULID-like sortable basenames; the highest names are the "newest".
    names = [f"{i:04d}" for i in range(60)]
    for n in names:
        (d / f"{n}.png").write_bytes(b"x")
    (d / "notes.txt").write_bytes(b"keep")  # non-png must be untouched

    cm._prune_shots(d, keep=40)

    remaining = sorted(p.stem for p in d.glob("*.png"))
    assert len(remaining) == 40
    assert remaining == names[-40:]  # the newest 40 survive
    assert (d / "notes.txt").is_file()


def test_prune_shots_noop_under_cap(tmp_path: Path) -> None:
    d = tmp_path / "run" / "computer"
    d.mkdir(parents=True)
    for i in range(5):
        (d / f"{i:04d}.png").write_bytes(b"x")
    cm._prune_shots(d, keep=40)
    assert len(list(d.glob("*.png"))) == 5


# --------------------------------------------------------------------------- #
# Finding 4 — GET /skills filter must apply BEFORE the top_k cap              #
# --------------------------------------------------------------------------- #


def _write_deploy_skill(root: Path, skill_id: str, *, strong: bool, type_: str = "skill") -> None:
    d = root / skill_id
    d.mkdir(parents=True)
    if strong:
        # Match "deploy" in title + description + body → high fused rank.
        fm = (
            f"---\nname: {skill_id}\ntitle: Deploy {skill_id}\n"
            f"type: {type_}\ndescription: deploy the service to production\n---\n\n"
            "# Deploy\n\nSteps to deploy the app.\n"
        )
    else:
        # Match "deploy" only once in the body → weak rank, easily capped out.
        fm = (
            f"---\nname: {skill_id}\ntitle: {skill_id}\n"
            f"type: {type_}\ndescription: an unrelated helper\n---\n\n"
            "# Helper\n\nMentions deploy exactly once here.\n"
        )
    (d / "SKILL.md").write_text(fm, encoding="utf-8")


@pytest.fixture
def skills_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setenv("AKANA_LLM_CHAT_TITLES", "0")
    monkeypatch.setattr("akana_server.skills.registry.cursor_skill_roots", lambda: [])
    reload_skills()
    root = akana_skills_dir(tmp_path)
    # Many strongly-matching type=skill entries fill the top of the ranking...
    for i in range(12):
        _write_deploy_skill(root, f"deployer_{i:02d}", strong=True)
    # ...and one weakly-matching type=rule entry that ranks below top_k.
    _write_deploy_skill(root, "rare_rule", strong=False, type_="rule")
    app = create_app()
    with TestClient(app) as c:
        yield c
    reload_skills()


def test_filtered_search_reaches_low_ranked_match(skills_client: TestClient) -> None:
    # Control: with a large cap the rule skill IS a valid match (proves it matches
    # "deploy" and is only excluded by the cap, not by relevance).
    big = skills_client.get("/api/v1/skills?q=deploy&type=rule&top_k=50")
    assert big.status_code == 200
    assert big.json()["count"] == 1

    # The bug: type filter applied AFTER the top_k=5 cap, so the rule skill —
    # ranked below the 5 strong skill-type matches — is unreachable and count==0.
    small = skills_client.get("/api/v1/skills?q=deploy&type=rule&top_k=5")
    assert small.status_code == 200
    body = small.json()
    assert body["count"] == 1, body
    assert body["skills"][0]["id"] == "rare_rule"


def test_filtered_search_respects_top_k(skills_client: TestClient) -> None:
    # Filtering before the cap must still honour top_k as an upper bound.
    r = skills_client.get("/api/v1/skills?q=deploy&type=skill&top_k=3")
    assert r.status_code == 200
    assert r.json()["count"] == 3
