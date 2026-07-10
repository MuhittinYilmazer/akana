"""Blitz-3 fe-memory-onboard regression tests.

Runs the node-vm behavior harness (findings 1-4, 6) and asserts the static-HTML
fix for finding 5 (the browser-tab <title> must be a data-i18n text node, not a
data-i18n-title tooltip attribute that never reaches document.title).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "web_ui" / "static"


def _run_node_harness(harness: Path) -> None:
    # Local copy of the pattern in test_web_ui_modules.py (kept private so parallel
    # agents editing that shared file don't conflict). Fail fast on a hung harness.
    try:
        proc = subprocess.run(
            ["node", str(harness)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise AssertionError(
            f"node harness did not finish within 60s: {harness.name}"
        ) from exc
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_blitz3_fe_memory_onboard_harness() -> None:
    """Findings 1-4, 6: dirty-editor forced reloads, wake-flag guard, listStaging
    limit, localized Inbox empty action, omit-empty settings."""
    _run_node_harness(REPO_ROOT / "tests/web/blitz3_fe-memory-onboard.harness.mjs")


def test_blitz3_memory_title_is_data_i18n_text_node() -> None:
    """Finding 5 (review): the browser-tab <title> must use a DEDICATED i18n key that
    keeps the app name ("Memory — Akana"), distinct from the in-page h1's
    memory.page_title ("Memory"). Sharing the h1 key made the tab read a bare "Memory"."""
    html = (REPO_ROOT / "web_ui/memory.html").read_text(encoding="utf-8")
    assert '<title data-i18n="memory.page_title_tab">' in html, (
        "the <title> must carry a dedicated data-i18n key so the tab keeps '— Akana'"
    )
    # The in-page h1 still uses the short heading key.
    assert '<h1 data-i18n="memory.page_title">' in html, (
        "the h1 heading must keep the short memory.page_title (no '— Akana')"
    )
    # data-i18n-title on <title> only sets a tooltip attribute; the tab never localizes.
    assert 'data-i18n-title="memory.page_title' not in html, (
        "data-i18n-title on <title> only sets a tooltip attribute; the tab never localizes"
    )
    strings = (STATIC / "akana-i18n-strings.js").read_text(encoding="utf-8")
    # Both keys must exist; the tab key must keep the app name for both languages.
    assert '"memory.page_title"' in strings, "memory.page_title i18n key must exist"
    assert '"memory.page_title_tab"' in strings, "memory.page_title_tab i18n key must exist"
    m = re.search(r'"memory\.page_title_tab":\s*\{([^}]*)\}', strings)
    assert m, "memory.page_title_tab entry not found"
    entry = m.group(1)
    assert "Akana" in entry, "the tab title must keep the app name '— Akana' (en + tr)"
