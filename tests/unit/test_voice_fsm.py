"""Voice FSM transition table — guard regressions in akana-voice-fsm.js."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FSM = REPO_ROOT / "web_ui" / "static" / "akana-voice-fsm.js"


def test_voice_fsm_defines_phases_and_session_factory() -> None:
    text = FSM.read_text(encoding="utf-8")
    assert "createVoiceSession" in text
    assert "CAPTURE_MIC" in text
    assert "CAPTURE_WAKE" in text
    assert "window.AkanaVoiceFsm" in text


def test_voice_fsm_allows_mic_from_wake_capture() -> None:
    text = FSM.read_text(encoding="utf-8")
    m = re.search(r"capture_wake:\s*\[([^\]]+)\]", text)
    assert m is not None
    assert "capture_mic" in m.group(1)


def test_index_loads_fsm_before_voice() -> None:
    html = (REPO_ROOT / "web_ui/index.html").read_text(encoding="utf-8")
    fsm = html.find("akana-voice-fsm.js")
    voice = html.find("akana-voice.js")
    assert fsm >= 0 and voice >= 0 and fsm < voice


def test_voice_fsm_contract_harness() -> None:
    """Node-vm contract: FSM transitions (including unknown-phase rejection), capture
    pure functions + microphone-permission denial path, pipeline hallucination
    filter, handoffToTextChat + TTS half-queue cleanup."""
    proc = subprocess.run(
        ["node", str(REPO_ROOT / "tests/web/voice_fsm_contract.harness.mjs")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
