"""WI-1 — unit tests for per-turn skill injection (suggest → inject).

FULL AUTONOMY: no approval gate — every strongly matching skill (including those
marked ``requires_approval``) is injected directly.

Async paths are driven with ``asyncio.run`` (there is no pytest-asyncio under
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1). The registry is a fake; the LLM never runs.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from akana_server.skills.turn_injection import (
    SkillTurnPlan,
    plan_skill_turn,
)


class FakeRegistry:
    """Minimal registry that mimics the suggest_for_text + load_body contract."""

    def __init__(
        self,
        suggestions: list[dict[str, Any]] | None = None,
        bodies: dict[str, str] | None = None,
        *,
        raise_on_suggest: Exception | None = None,
        suggest_delay_s: float = 0.0,
    ) -> None:
        self.suggestions = suggestions or []
        self.bodies = bodies or {}
        self.raise_on_suggest = raise_on_suggest
        self.suggest_delay_s = suggest_delay_s
        self.calls: list[tuple[str, int]] = []

    def suggest_for_text(self, text: str, top_k: int = 3) -> list[dict[str, Any]]:
        self.calls.append((text, top_k))
        if self.suggest_delay_s:
            time.sleep(self.suggest_delay_s)
        if self.raise_on_suggest is not None:
            raise self.raise_on_suggest
        return list(self.suggestions)

    def load_body(self, skill_id: str) -> str:
        body = self.bodies.get(skill_id)
        if body is None:
            raise KeyError(skill_id)
        return body


def _suggestion(
    skill_id: str,
    *,
    score: float = 1.0,
    reason: str = "trigger_exact",
    requires_approval: bool = False,
    tools_allowed: list[str] | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": skill_id,
        "title": skill_id.replace("_", " ").title(),
        "score": score,
        "match_reason": reason,
        "requires_approval": requires_approval,
        "risk": "medium",
    }
    if tools_allowed:
        d["tools_allowed"] = tools_allowed
    return d


@pytest.fixture
def settings(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=tmp_path)


def _run(coro):
    return asyncio.run(coro)


# -- strong match → injection ---------------------------------------------------


def test_trigger_exact_injects_skill_body(settings) -> None:
    reg = FakeRegistry(
        [_suggestion("re_analyze")],
        {"re_analyze": "## Boru hattı\nre_load → re_map → rapor."},
    )
    plan = _run(plan_skill_turn(settings, "şu exe'yi analiz et", registry=reg))
    assert not plan.blocked
    assert [e["id"] for e in plan.injected] == ["re_analyze"]
    assert plan.injected[0]["status"] == "injected"
    assert "[Capability: re_analyze" in plan.prompt_block
    assert "re_load → re_map" in plan.prompt_block
    assert "[/Capability]" in plan.prompt_block


def test_weak_score_below_threshold_not_injected(settings) -> None:
    reg = FakeRegistry(
        [_suggestion("re_map", score=0.0164, reason="title")],
        {"re_map": "harita"},
    )
    plan = _run(plan_skill_turn(settings, "haritayı göster", registry=reg))
    assert plan.injected == [] and plan.prompt_block == ""
    assert not plan.has_signal


def test_score_above_threshold_injected(settings, monkeypatch) -> None:
    monkeypatch.setenv("AKANA_SKILL_INJECT_THRESHOLD", "0.03")
    reg = FakeRegistry(
        [_suggestion("re_map", score=0.05, reason="title+fts")],
        {"re_map": "harita gövdesi"},
    )
    plan = _run(plan_skill_turn(settings, "binary haritası çıkar", registry=reg))
    assert [e["id"] for e in plan.injected] == ["re_map"]


def test_max_n_config_injects_top_n(settings, monkeypatch) -> None:
    monkeypatch.setenv("AKANA_SKILL_INJECT_MAX", "2")
    reg = FakeRegistry(
        [
            _suggestion("re_analyze"),
            _suggestion("re_map"),
            _suggestion("re_strings"),
        ],
        {"re_analyze": "a", "re_map": "b", "re_strings": "c"},
    )
    plan = _run(plan_skill_turn(settings, "analiz et", registry=reg))
    assert [e["id"] for e in plan.injected] == ["re_analyze", "re_map"]
    assert plan.prompt_block.count("[Capability:") == 2


def test_inject_disabled_via_env(settings, monkeypatch) -> None:
    monkeypatch.setenv("AKANA_SKILL_INJECT", "0")
    reg = FakeRegistry([_suggestion("re_analyze")], {"re_analyze": "x"})
    plan = _run(plan_skill_turn(settings, "şu exe'yi analiz et", registry=reg))
    assert not plan.has_signal and reg.calls == []


# -- FULL AUTONOMY: requires_approval is now inert (no gate) --------------------------


def test_requires_approval_skill_injected_directly(settings) -> None:
    """The approval gate was removed: even a skill marked ``requires_approval`` is
    injected directly without approval. The flag is only carried as inert advisory metadata."""
    reg = FakeRegistry(
        [_suggestion("re_setup", requires_approval=True)], {"re_setup": "kurulum gövdesi"}
    )
    plan = _run(plan_skill_turn(settings, "ghidra kur", registry=reg))
    assert plan.blocked == []
    assert [e["id"] for e in plan.injected] == ["re_setup"]
    assert plan.injected[0]["status"] == "injected"
    assert plan.injected[0]["requires_approval"] is True  # carried but inert
    assert "kurulum gövdesi" in plan.prompt_block


# -- error resilience ----------------------------------------------------------


def test_suggest_error_never_breaks_turn(settings) -> None:
    reg = FakeRegistry(raise_on_suggest=RuntimeError("fts patladı"))
    plan = _run(plan_skill_turn(settings, "şu exe'yi analiz et", registry=reg))
    assert isinstance(plan, SkillTurnPlan)
    assert not plan.has_signal and plan.prompt_block == ""


def test_suggest_timeout_never_breaks_turn(settings, monkeypatch) -> None:
    # Use the schema minimum (0.1s): env values below it now fall back to the
    # default (bounds are enforced on the env layer too, mirroring the PUT path),
    # so pair it with a longer suggest delay — the timeout still fires well before
    # the search would finish.
    monkeypatch.setenv("AKANA_SKILL_SUGGEST_TIMEOUT_S", "0.1")
    reg = FakeRegistry(
        [_suggestion("re_analyze")], {"re_analyze": "x"}, suggest_delay_s=1.0
    )
    plan = _run(plan_skill_turn(settings, "şu exe'yi analiz et", registry=reg))
    assert not plan.has_signal


def test_body_load_error_skips_skill(settings) -> None:
    reg = FakeRegistry([_suggestion("re_analyze")], bodies={})  # no body
    plan = _run(plan_skill_turn(settings, "şu exe'yi analiz et", registry=reg))
    assert plan.injected == [] and plan.prompt_block == ""


def test_empty_text_returns_empty_plan(settings) -> None:
    reg = FakeRegistry([_suggestion("re_analyze")], {"re_analyze": "x"})
    plan = _run(plan_skill_turn(settings, "   ", registry=reg))
    assert not plan.has_signal and reg.calls == []


# -- missing-tool signal -----------------------------------------------------------


def test_missing_mcp_servers_flagged_in_block(settings) -> None:
    reg = FakeRegistry(
        [
            _suggestion(
                "re_decompile",
                tools_allowed=["ghidra.decompile_function", "memory_remember"],
            )
        ],
        {"re_decompile": "decompile playbook"},
    )
    plan = _run(plan_skill_turn(settings, "fonksiyonu decompile et", registry=reg))
    assert plan.injected and plan.injected[0]["missing_tools"] == ["ghidra"]
    assert "ghidra" in plan.prompt_block
    assert "Missing-tool signal" in plan.prompt_block


def test_used_payload_injects_requires_approval_too(settings, monkeypatch) -> None:
    """Mixed set (normal + requires_approval) → BOTH are injected in the payload."""
    monkeypatch.setenv("AKANA_SKILL_INJECT_MAX", "3")
    reg = FakeRegistry(
        [
            _suggestion("re_analyze"),
            _suggestion("re_setup", requires_approval=True),
        ],
        {"re_analyze": "a", "re_setup": "b"},
    )
    plan = _run(plan_skill_turn(settings, "analiz et ve kur", registry=reg))
    payload = plan.used_payload()
    statuses = {e["id"]: e["status"] for e in payload}
    assert statuses == {"re_analyze": "injected", "re_setup": "injected"}


def test_skill_block_is_bilingual() -> None:
    """The injected skill block the model reads follows the language (default EN)."""
    from akana_server.skills.turn_injection import _format_block

    entry = {"id": "wa", "title": "WhatsApp"}
    en = _format_block(entry, "body line", ["mcp_srv"], "en")
    assert en.startswith("[Capability: wa — WhatsApp]") and en.rstrip().endswith("[/Capability]")
    assert "Missing-tool signal" in en
    tr = _format_block(entry, "body line", ["mcp_srv"], "tr")
    assert tr.startswith("[Yetenek: wa — WhatsApp]") and tr.rstrip().endswith("[/Yetenek]")
    assert "Eksik araç sinyali" in tr


# -- catalog selection also gates WI-1 injection (master-gate + filter) ------------


def _set_catalog_selection(data_dir, ids: list[str]) -> None:
    """Write the catalog selection to the persona store — the shared source for WI-1 and WI-2."""
    from akana_server.persona.registry import get_persona_registry

    get_persona_registry(data_dir).set_catalog_selection(ids)


def test_catalog_disabled_blocks_all_injection(settings, monkeypatch) -> None:
    """Catalog fully off → no skill is injected (not even a search is performed)."""
    monkeypatch.setenv("AKANA_SKILL_CATALOG", "0")
    reg = FakeRegistry([_suggestion("re_analyze")], {"re_analyze": "x"})
    plan = _run(plan_skill_turn(settings, "şu exe'yi analiz et", registry=reg))
    assert not plan.has_signal and reg.calls == []


def test_selection_excludes_skill_not_injected(settings) -> None:
    """A skill outside the selection is not injected even if it matches an exact trigger."""
    _set_catalog_selection(settings.data_dir, ["other_skill"])
    reg = FakeRegistry([_suggestion("re_analyze")], {"re_analyze": "x"})
    plan = _run(plan_skill_turn(settings, "şu exe'yi analiz et", registry=reg))
    assert plan.injected == [] and plan.prompt_block == ""


def test_selection_includes_skill_injected(settings) -> None:
    """A skill that is in the selection is injected normally."""
    _set_catalog_selection(settings.data_dir, ["re_analyze"])
    reg = FakeRegistry([_suggestion("re_analyze")], {"re_analyze": "gövde"})
    plan = _run(plan_skill_turn(settings, "şu exe'yi analiz et", registry=reg))
    assert [e["id"] for e in plan.injected] == ["re_analyze"]


def test_empty_selection_blocks_injection(settings) -> None:
    """Empty selection ([]) → no skill is eligible; no search is performed."""
    _set_catalog_selection(settings.data_dir, [])
    reg = FakeRegistry([_suggestion("re_analyze")], {"re_analyze": "x"})
    plan = _run(plan_skill_turn(settings, "şu exe'yi analiz et", registry=reg))
    assert not plan.has_signal and reg.calls == []


def test_excluded_skill_does_not_consume_slot(settings, monkeypatch) -> None:
    """A filtered-out high-scoring skill does not consume the max_n slot; the selected skill comes in."""
    monkeypatch.setenv("AKANA_SKILL_INJECT_MAX", "1")
    _set_catalog_selection(settings.data_dir, ["re_map"])
    reg = FakeRegistry(
        [_suggestion("re_analyze"), _suggestion("re_map")],
        {"re_analyze": "a", "re_map": "b"},
    )
    plan = _run(plan_skill_turn(settings, "analiz et", registry=reg))
    assert [e["id"] for e in plan.injected] == ["re_map"]


def test_selection_change_auto_updates_between_turns(settings) -> None:
    """When the selection changes mid-conversation the next turn reflects it automatically (no cache)."""
    reg = FakeRegistry([_suggestion("re_analyze")], {"re_analyze": "gövde"})

    _set_catalog_selection(settings.data_dir, ["other"])  # turn 1: outside the selection
    p1 = _run(plan_skill_turn(settings, "analiz et", registry=reg))
    assert p1.injected == []

    _set_catalog_selection(settings.data_dir, ["re_analyze"])  # selection updated
    p2 = _run(plan_skill_turn(settings, "analiz et", registry=reg))
    assert [e["id"] for e in p2.injected] == ["re_analyze"]
