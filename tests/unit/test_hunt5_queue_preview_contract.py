"""Hunt-5 (reload-restore-2) — queue-preview contract between server and frontend.

A message queued behind a running turn (202) is exposed to the client ONLY as
``text_preview`` (``chat_turn_queue._preview_text``). The chat store rescues such a
message from being dropped as a stale ghost after F5 by comparing its local pending text
in that same preview shape (``previewOfText`` in akana-chat-store.js). If either side's
truncation rule drifts, long queued messages silently start vanishing again — this test
pins the two implementations to the same output.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from akana_server.api.chat_turn_queue import _preview_text

REPO_ROOT = Path(__file__).resolve().parents[2]
STORE_JS = REPO_ROOT / "web_ui/static/akana-chat-store.js"

CASES = [
    "short one",
    "  padded  ",
    "line one\nline two",
    "x" * 80,
    "x" * 81,
    "y" * 400,
    "",
    # ASTRAL characters: Python measures/slices CODE POINTS, JS strings are UTF-16 code
    # UNITS — an emoji counts as 1 on the server and 2 in the browser. Without matching
    # rules the two previews diverge and the F5 queue-rescue silently fails for exactly
    # the messages that contain one (and a naive slice can even cut a surrogate pair in
    # half, producing a preview that is not even valid text).
    "👍" + "x" * 100,  # the boundary shifts by one unit per emoji
    "👍" * 40 + "x" * 39,  # 79 code points / 119 code units: truncate or not?
    "x" * 78 + "👍👍",  # 80 code points, and the cut lands INSIDE a surrogate pair
    "aile 👨‍👩‍👧‍👦 fotoğrafı",  # ZWJ sequence, comfortably under the limit
]


def _js_previews(texts: list[str]) -> list[str]:
    """Run the frontend's previewOfText over `texts` (extracted from the real module)."""
    src = STORE_JS.read_text(encoding="utf-8")
    m = re.search(r"\n(    function previewOfText\(text\) \{.*?\n    \})\n", src, re.S)
    assert m, "previewOfText not found in akana-chat-store.js (frontend mirror removed?)"
    script = f"{m.group(1)}\nconsole.log(JSON.stringify({json.dumps(texts)}.map(previewOfText)));"
    # encoding is explicit: the ellipsis in the preview is non-ASCII and the Windows
    # console default (cp1252) would mangle it into a false mismatch.
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the JS mirror")
def test_frontend_preview_matches_server_preview() -> None:
    assert _js_previews(CASES) == [_preview_text(t) for t in CASES]


def test_long_queued_text_is_truncated_with_an_ellipsis() -> None:
    # The rescue only works because BOTH sides truncate identically; a preview that is not
    # a prefix of the real text would never match the local pending row.
    long_text = "follow-up " + "z" * 200
    preview = _preview_text(long_text)
    assert preview.endswith("…")
    assert len(preview) == 80
    assert long_text.startswith(preview[:-1])
