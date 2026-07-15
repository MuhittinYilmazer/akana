"""Static web UI modules — files exist and export key globals."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "web_ui" / "static"

REQUIRED_MODULES = (
    "akana-core.js",
    "akana-bus.js",
    "akana-markdown.js",
    "akana-chat-render.js",
    "akana-chat-store.js",
    "akana-chat-archive.js",
    "akana-chat-threads.js",
    "akana-chat-transport.js",
    "akana-shell.js",
    "akana-voice-fsm.js",
    "akana-voice-capture.js",
    "akana-voice-pipeline.js",
    "akana-voice-settings.js",
    "akana-voice.js",
    "akana-voice-live.js",
    "akana-chat.js",
    "akana-settings.js",
    "akana-memory-api.js",
    "akana-memory-render.js",
    "akana-memory-studio.js",
    "akana-personas.js",
    "app.js",
)

# Studio-only advanced modules: loaded on /memory, not the main chat page.
# (graph + insight removed → currently empty; memory.html only loads core+studio.)
MEMORY_ONLY_MODULES = ()

# i18n engine + string tables load right after akana-core.js and BEFORE every
# consumer (bus/render/chat/settings/…), so `AkanaI18n.t()` exists when those
# modules render user-facing text. The matcher (`_akana_script_names`) picks up
# `akana-i18n*.js` too, so the expected order must list them in this position.
I18N_SCRIPTS = (
    "akana-i18n-strings.js",
    "akana-i18n-strings-chat.js",
    "akana-i18n-strings-chat2.js",
    "akana-i18n-strings-voice.js",
    "akana-i18n-strings-settings.js",
    "akana-i18n-strings-settings2.js",
    "akana-i18n-strings-runtime.js",
    "akana-i18n-strings-memory.js",
    "akana-i18n-strings-misc.js",
    "akana-i18n-strings-index.js",
    "akana-i18n.js",
)

EXPECTED_AKANA_ORDER = (
    "akana-core.js",
    *I18N_SCRIPTS,
    "akana-bus.js",
    "akana-markdown.js",
    "akana-chat-render.js",
    "akana-chat-store.js",
    "akana-chat-archive.js",
    "akana-chat-threads.js",
    "akana-chat-transport.js",
    "akana-shell.js",
    "akana-voice-fsm.js",
    "akana-voice-capture.js",
    "akana-voice-pipeline.js",
    "akana-voice-settings.js",
    "akana-voice.js",
    "akana-chat.js",
    "akana-settings.js",
    "akana-memory-api.js",
    "akana-memory-render.js",
    "akana-memory-studio.js",
)

# The cache-bust version is NOT UNIFORM: project practice is a per-file meaningful
# version — every UI change bumps only the ``?v=…`` tag of the CHANGED file (turnfix3,
# voice3, voicereadfix, pwa1…). That's why the tests don't look for a single fixed
# version; they verify that each script carries ONE cache-bust (complete ritual).

# index.html loads the main chat shell + PWA/aurora add-ons (turn-status,
# mobile-nav, pair, vault, artifacts). memory.html (Studio) does not load advanced
# viz modules (graph + insight removed).
EXPECTED_BY_PAGE = {
    "index.html": (
        "akana-core.js",
        *I18N_SCRIPTS,
        "akana-bus.js",
        "akana-markdown.js",
        "akana-chat-render.js",
        "akana-chat-store.js",
        "akana-chat-archive.js",
        "akana-chat-threads.js",
        "akana-turn-status.js",
        "akana-chat-transport.js",
        "akana-chat-panes.js",
        "akana-shell.js",
        "akana-voice-fsm.js",
        "akana-voice-capture.js",
        "akana-voice-pipeline.js",
        "akana-voice-settings.js",
        "akana-voice.js",
        "akana-voice-live.js",
        "akana-chat.js",
        "akana-settings.js",
        "akana-mobile-nav.js",
        "akana-pair.js",
        "akana-vault.js",
        "akana-personas.js",
        "akana-packs.js",
        "akana-observability.js",
        "akana-memory-api.js",
        "akana-memory-render.js",
        "akana-memory-studio.js",
        "akana-artifacts.js",
    ),
    "memory.html": EXPECTED_AKANA_ORDER + MEMORY_ONLY_MODULES,
}

GLOBAL_EXPORTS = {
    "akana-core.js": "window.AkanaCore",
    "akana-bus.js": "window.AkanaBus",
    "akana-markdown.js": "window.AkanaMarkdown",
    "akana-chat-render.js": "window.AkanaChatRender",
    "akana-chat-store.js": "window.AkanaChatStore",
    "akana-chat-archive.js": "window.AkanaChatArchive",
    "akana-chat-threads.js": "window.AkanaChatThreads",
    "akana-chat-transport.js": "window.AkanaChatTransport",
    "akana-shell.js": "window.AkanaShell",
    "akana-memory-api.js": "window.AkanaMemoryApi",
    "akana-memory-render.js": "window.AkanaMemoryRender",
    "akana-voice-fsm.js": "window.AkanaVoiceFsm",
    "akana-voice-capture.js": "window.AkanaVoiceCapture",
    "akana-voice-pipeline.js": "window.AkanaVoicePipeline",
    "akana-voice-settings.js": "window.AkanaVoiceSettings",
    "akana-voice.js": "handoffToTextChat",
    "akana-voice-live.js": "window.AkanaVoiceLive",
    "akana-chat.js": "window.AkanaChat",
    "akana-settings.js": "window.AkanaSettings",
    "akana-memory-studio.js": "window.AkanaMemoryStudio",
    "akana-personas.js": "window.AkanaPersonas",
}


@pytest.mark.parametrize("filename", REQUIRED_MODULES)
def test_static_module_file_exists(filename: str) -> None:
    path = STATIC / filename
    assert path.is_file(), f"missing {path}"


@pytest.mark.parametrize("filename,export_marker", GLOBAL_EXPORTS.items())
def test_static_module_exports_global(filename: str, export_marker: str) -> None:
    text = (STATIC / filename).read_text(encoding="utf-8")
    assert export_marker in text, f"{filename} should assign {export_marker}"


def _akana_script_names(html: str) -> list[str]:
    return re.findall(r"/static/(akana-[^\"?]+\.js)", html)


def _akana_script_tags(html: str) -> list[str]:
    return re.findall(r'<script src="/static/(akana-[^"]+\.js\?v=[^"]+)"', html)


def test_stream_chat_handoffs_wake_capture_for_text() -> None:
    """Typed send during Hey Akana listen must not call full cancelVoiceActivity."""
    text = (STATIC / "akana-chat-transport.js").read_text(encoding="utf-8")
    assert "handoffToTextChat" in text


def test_akana_chat_has_no_top_level_form_listener() -> None:
    """Module load must not reference DOM hooks before init (regression)."""
    text = (STATIC / "akana-chat.js").read_text(encoding="utf-8")
    assert "if (form) form.addEventListener" not in text


def test_packs_refresh_and_rescan_merged_into_one_button() -> None:
    """Packs toolbar exposes ONE refresh control, wired to rescan.

    rescan is a strict superset of the old GET-only refresh (returns the same
    {count,packs} payload PLUS reconciles with packs/ on disk and reports the
    added/removed delta), so the two toolbar buttons were merged into a single
    "Refresh" (pack.refresh_btn) button that triggers POST /rescan. Guards
    against the second button or the GET-only branch/keys creeping back.
    """
    packs = (STATIC / "akana-packs.js").read_text(encoding="utf-8")
    # Exactly one toolbar action button, and it is the rescan-backed refresh.
    assert packs.count('data-action="rescan"') == 1
    assert 'data-action="refresh"' not in packs
    assert 'data-action="rescan">${t("pack.refresh_btn")}' in packs
    # The old GET-only refresh branch (and its status string) are gone.
    assert 'action === "refresh"' not in packs
    assert "pack.status.refreshed" not in packs
    # Dead i18n keys pruned from the settings string table; label key survives.
    i18n = (STATIC / "akana-i18n-strings-settings.js").read_text(encoding="utf-8")
    assert '"pack.rescan_btn"' not in i18n
    assert '"pack.status.refreshed"' not in i18n
    assert '"pack.refresh_btn"' in i18n


@pytest.mark.parametrize("page", list(EXPECTED_BY_PAGE))
def test_html_akana_load_order_and_cache_bust(page: str) -> None:
    expected = EXPECTED_BY_PAGE[page]
    html = (REPO_ROOT / f"web_ui/{page}").read_text(encoding="utf-8")
    scripts = _akana_script_names(html)
    assert scripts == list(expected)
    # Cache-bust: each akana-*.js must carry ONE ``?v=…`` (so the browser re-fetches
    # the changed one) — but not a UNIFORM version (bumped per-file). Since
    # ``_akana_script_tags`` only catches scripts carrying ``?v=``, the count matching
    # ``expected`` proves every expected script is cache-busted.
    tags = _akana_script_tags(html)
    assert len(tags) == len(expected)
    assert re.search(r'src="/static/app\.js\?v=[^"]+"', html), "app.js must be cache-busted"


def _run_node_harness(harness: Path) -> None:
    # timeout: don't let a hung harness (dangling timer etc.) keep the whole suite
    # waiting forever — fail fast. Harnesses exit with process.exit(0) on success;
    # still, a defensive layer.
    try:
        proc = subprocess.run(
            ["node", str(harness)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"node harness did not finish within 60s (likely a dangling timer): {harness.name}"
        ) from exc
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_memory_studio_contract_harness() -> None:
    """Node contract harness (same path as the akana_markdown wrapper pattern)."""
    _run_node_harness(REPO_ROOT / "tests/web/memory_studio_contract.harness.mjs")


def test_memory_facts_editor_guard_harness() -> None:
    """Node-vm + fake-DOM regression: loadFacts() must NOT wipe an OPEN fact editor with
    unsaved textarea content on a background/automatic reload (debounced search, bg
    approve/reject, paging). Drives the real render setListState wipe + real studio
    loadFacts/openFactEditor/hasDirtyFactEditor via the _test seam; a forced reload
    (explicit save/refresh) still replaces the list."""
    _run_node_harness(REPO_ROOT / "tests/web/memory_facts_editor_guard.harness.mjs")


def test_voice_live_contract_harness() -> None:
    """Node-vm contract (Phase 2): Gemini Live frame codec (0x01 + LE PCM16),
    float→PCM16 clamp/scale, orb/barge-in state machine (pure helpers)."""
    _run_node_harness(REPO_ROOT / "tests/web/voice_live_contract.harness.mjs")


def test_pcm_playback_worklet_registers_processor() -> None:
    """Live playback worklet (loaded via addModule, NOT a script-tag): 24k PCM16
    queue + barge-in flush + processor registration. Since it's outside the scope of
    GLOBAL_EXPORTS, a separate text contract."""
    text = (STATIC / "pcm-playback-worklet.js").read_text(encoding="utf-8")
    assert 'registerProcessor("akana-pcm-playback"' in text
    assert "flush" in text  # barge-in queue clearing


def test_voice_live_loads_after_capture_dependency() -> None:
    """akana-voice-live.js depends on akana-voice-capture.js for downsampling →
    it must load AFTER it in index.html (correct order even if there is a fallback)."""
    html = (REPO_ROOT / "web_ui/index.html").read_text(encoding="utf-8")
    cap = html.find("akana-voice-capture.js")
    live = html.find("akana-voice-live.js")
    assert cap >= 0 and live >= 0 and cap < live


def test_settings_ws_contract_harness() -> None:
    """Node-vm contract: ws.onmessage is bound, events land on bus+toast."""
    _run_node_harness(REPO_ROOT / "tests/web/settings_ws_contract.harness.mjs")


def test_runtime_settings_contract_harness() -> None:
    """Node-vm contract: runtime setting REST paths + pure schema→form model."""
    _run_node_harness(REPO_ROOT / "tests/web/runtime_settings_contract.harness.mjs")


def test_i18n_language_writethrough_harness() -> None:
    """Node-vm contract (U5): the UI language picker writes through to the server
    `language` runtime setting, follows the backend on boot, and a failed write rejects
    (no silent UI/backend divergence)."""
    _run_node_harness(REPO_ROOT / "tests/web/i18n_language_writethrough.harness.mjs")


def test_chat_stream_resilience_harness() -> None:
    """Node-vm contract: message-storm dedupe/abort shield, chat/active resume
    contract, RADICALLY COMPACT tool card (human-readable action sentence + duration
    badge + <details> collapsed + start→end status update), STOP server cancel
    endpoint + auto-cancel on send, auto-scroll when a tool card is added, LIVE
    throttled markdown (not per-frame), send↔stop button state machine + ui16."""
    _run_node_harness(REPO_ROOT / "tests/web/chat_stream_resilience.harness.mjs")


def test_chat_conversation_switch_harness() -> None:
    """Node-vm contract: conversation-switch isolation. Root bug of "send message →
    open new chat → send message": fire-and-forget store sync (end of
    streamChat/resume) blindly overwrote the active thread, writing A's turns into B
    + B.conversationId=A → the message went to the wrong conversation. Covers:
    activateThreadForConversation cannibalization shield, syncConversationLogFromServer
    convId-targeted write, reloadConversationLogFromServer stale-reload shield,
    switch gen guard."""
    _run_node_harness(REPO_ROOT / "tests/web/chat_conversation_switch.harness.mjs")


def test_chat_stream_isolation_harness() -> None:
    """Node-vm contract: live tool-card queue isolation when TWO conversations stream
    at the same time. Previously _toolUiPending/_toolUiRaf/_toolKeyState were at
    MODULE level → one stream's flush dumped all pending (including the other
    conversation's) into its own body. Now the state lives on streamCtx
    (ensureToolScratch); the test verifies via interleaved queue→flush that each
    stream gets ONLY its own cards + that anon keys don't collide."""
    _run_node_harness(REPO_ROOT / "tests/web/chat_stream_isolation.harness.mjs")


def test_ui_edge_cases_harness() -> None:
    """Node-vm edge-case contract: markdown XSS shield (script/img/javascript:),
    SSE corrupt/out-of-order/half frame, tool card 0-arg/huge-arg/unicode/storm-dedupe,
    cost empty data, voice FSM unknown event, bus silent dispatch,
    archive search/empty-list."""
    _run_node_harness(REPO_ROOT / "tests/web/ui_edge_cases.harness.mjs")


def test_ask_user_card_harness() -> None:
    """Node-vm contract (Part A): AskUserQuestion interactive card —
    renderAskUserCard structure, single-select radio / multi-select toggle, Submit
    disabled gate, submit→onSubmit "header: labels" format + answered lock,
    free-text contribution, null on invalid input; transport ask_user SSE branch +
    empty-response error exemption + answer→resume send source."""
    _run_node_harness(REPO_ROOT / "tests/web/ask_user_card.harness.mjs")


def test_plan_card_harness() -> None:
    """Node-vm contract (plan-mode front-end): claude plan-mode / ExitPlanMode plan
    card — renderPlanCard structure (body markdown + Apply/Revise), Apply→
    onApprove + applied lock, two-stage Revise (open → text/Enter → onRevise) +
    revised lock, empty revision no-op, null on invalid plan; transport plan_review
    SSE branch + done.plan_review + empty-response exemption + plan→resume; chat plan
    toggle (plan_mode payload) + submitPlanText; CSS/HTML toggle source."""
    _run_node_harness(REPO_ROOT / "tests/web/plan_card.harness.mjs")


def test_todo_card_harness() -> None:
    """Node-vm contract (task list live checklist): instead of a TodoWrite raw JSON
    tool card, an ALWAYS-OPEN prominent checklist — renderTodoCard structure (head +
    ul.ac-todos + counter), extractTodoItems (items[]/activeForm/status normalize),
    isTodoCall routing gate, family-dedup (consecutive different-ID TodoWrites update
    the SAME card in place — they don't pile up), upsertToolCardIntoTimeline live
    routing, argless end-phase preserves the list; TODO_TOOL_RE single-source
    ('todowrite' norm) + the upsert paths' todo-family routing source + CSS."""
    _run_node_harness(REPO_ROOT / "tests/web/todo_card.harness.mjs")


def test_agent_activity_harness() -> None:
    """Node-vm contract (agent activity, Batch 1): Claude subagent (Task) timeline
    group + TODO progress wiring — renderSubagentGroup structure
    (icon/title 'Subagent · {name}'/status/body + description line), upsertSubagentGroup
    singleton-create/idempotent/end→done|error, tool card with matching parent_id
    nesting NESTED into the group body + end patching the same node, graceful degrade
    on non-matching parent_id (root level); export + CSS; transport todo/subagent
    dispatch branches + handleTodoEvent/handleSubagentEvent + pill (done/total, tasks_n,
    data-complete); i18n keys (subagent_title/fallback, tasks_n). Batch 2
    (live tool input): upsertToolInputStream streams the partial input into the card's
    subtitle (data-streaming), the real tool_call patches the same card and clears the
    flag (no dup); no-op if there's no id; transport tool_call_delta branch + handler."""
    _run_node_harness(REPO_ROOT / "tests/web/agent_activity.harness.mjs")


def test_greeting_lang_gate_harness() -> None:
    """Node-vm contract: the opening greeting's memory query is gated by language —
    EN → only q=name, TR → only q=adı (not both at once); EN if getLanguage is absent."""
    _run_node_harness(REPO_ROOT / "tests/web/greeting_lang_gate.harness.mjs")


def test_onboard_i18n_contract_harness() -> None:
    """Node-vm contract: all onboard.* i18n keys used in aurora-onboard.js exist in
    EN+TR (recheck feedback + expanded feature tour); {provider} placeholders are
    preserved in the translation."""
    _run_node_harness(REPO_ROOT / "tests/web/onboard_i18n_contract.harness.mjs")


def test_onboard_connect_state_harness() -> None:
    """Node-vm contract: the onboarding connection banner has an HONEST 3 states —
    _deriveConnectState (pure) turns only a LIVE-verified provider green ('ok');
    key registered but unreachable (invalid Cursor key / missing Gemini SDK /
    offline) yellow ('warn') + concrete reason; no key/no session 'cta'.
    Regression: an invalid key was read as 'Bağlandı' and gave a 401 in chat."""
    _run_node_harness(REPO_ROOT / "tests/web/onboard_connect_state.harness.mjs")


def test_bridge_daemon_abort_harness() -> None:
    """Node harness: the REAL cursor_bridge/bridge_daemon.mjs over stdio with a fake
    @cursor/sdk. BUG 3 — a STOP that lands while turn A's Agent.create is still in
    flight must cancel A's run (honored mid-setup intent) and an immediately-resent
    turn B must reuse A's cached agent (no second, leaked agent) instead of skipping
    serialization and deleting A's intent. BUG 8 — stdin EOF exits the daemon (a hard,
    non-aclose parent death must not orphan the daemon)."""
    _run_node_harness(REPO_ROOT / "tests/cursor_bridge/bridge_daemon_abort.harness.mjs")


def test_bridge_lib_activity_lang_harness() -> None:
    """Node harness: makeOnDelta activity fallbacks follow the active language.
    BUG 4 — summary-started (empty text) and step-started (no label) used hardcoded
    Turkish defaults regardless of the setting; defaults must be English (mandate),
    Turkish only when language:'tr' is explicitly set."""
    _run_node_harness(REPO_ROOT / "tests/cursor_bridge/lib_activity_lang.harness.mjs")


def test_voice_mute_earcon_harness() -> None:
    """Node-vm contract (voice mode): voice:mic:mute subscriber + micMuted re-arm
    gating + enter/exit reset; earcon volume is read from akana.voiceEarconVol and
    scales the gain (higher than the default 0.05); recognizer start is deferred with
    mic-settle (not synchronous in the drain callstack) + teardown latch."""
    _run_node_harness(REPO_ROOT / "tests/web/voice_mute_earcon.harness.mjs")


def test_voice_frontend_bugfixes_harness() -> None:
    """Node-vm contract: ten voice front-end fixes — whisper mic-deny latch (no microtask
    freeze loop), ensureAudio supersession token (mic-leak), enter-during-wake-POST re-arm,
    whisper SR-free entry, Aurora Stop aborting an in-flight /voice/transcribe, post-final
    grace surviving an SR restart, wake-fallback onend detach, TTS/SR 'auto' language
    resolution (English default not Turkish), and resumeAfterVisible not wedging on a finished
    TTS chunk."""
    _run_node_harness(REPO_ROOT / "tests/web/voice_frontend_bugfixes.harness.mjs")


def test_code_tools_scroll_extent_harness() -> None:
    """Node-vm contract: the code-copy capsule (.akana-code-tools) must be fully
    dismissed on conversation switch ([hidden] + cleared top) and born [hidden].
    User report: after a long chat, a new/other chat stayed scrollable for the OLD
    chat's full extent — the capsule, absolutely positioned in #log-scroll, was
    stranded at the old chat's deep `top` (opacity-0 boxes still count toward
    scrollable overflow) and its Copy button floated over the new chat."""
    _run_node_harness(REPO_ROOT / "tests/web/code_tools_scroll_extent.harness.mjs")


@pytest.mark.parametrize("page", list(EXPECTED_BY_PAGE))
def test_html_theme_preload_prevents_fouc(page: str) -> None:
    """The saved theme (akana.theme) must be applied via an inline script before the first paint.

    html data-theme="dark" is hardcoded; so that a user on the light theme doesn't see
    a dark flash on every load, an inline theme script sits BEFORE the css links in the
    head (the key is the same as akana-settings.js applyThemePreference).
    """
    html = (REPO_ROOT / f"web_ui/{page}").read_text(encoding="utf-8")
    marker = 'localStorage.getItem("akana.theme")'
    assert marker in html, f"{page}: FOUC-preventing inline theme script is missing"
    assert html.index(marker) < html.index("/static/tokens.css"), (
        f"{page}: theme script must come before the css links"
    )


def test_chat_threads_actions_have_visible_counterpart() -> None:
    """Server NL-command actions (task_route/teach) must not be silently swallowed —
    applyChatServerAction drops a toast for known actions. (With FULL AUTONOMY the
    plan/skill approval actions were removed.)"""
    text = (STATIC / "akana-chat-threads.js").read_text(encoding="utf-8")
    assert "CHAT_ACTION_NOTICES" in text
    for action in (
        "task_route",
        "teach_draft",
        "teach_failed",
    ):
        assert action in text, f"applyChatServerAction '{action}' counterpart is missing"


def test_transport_renders_skill_used_and_handles_disconnect() -> None:
    """skill_used card in the done payload + no pending bubble remains on SSE disconnect.

    - skill_used: the WI-2 injection is now visible (renderSkillUse, .skill-use).
    - A read error (network disconnect) is turned into the serverError path; AbortError
      propagates upward as-is (no double error line on a user abort).
    """
    transport = (STATIC / "akana-chat-transport.js").read_text(encoding="utf-8")
    render = (STATIC / "akana-chat-render.js").read_text(encoding="utf-8")
    assert "renderSkillUse" in transport
    assert "skill_used" in transport
    # AbortError (user abort) is thrown upward; other disconnects fall to CONN.
    assert 'e.name === "AbortError"' in transport
    assert "throw e" in transport
    assert 'code: "CONN"' in transport
    assert "function renderSkillUse" in render
    assert "skill-use" in render


def test_chat_header_is_single_compact_row() -> None:
    """A SINGLE compact row above the chat: the old two layers (toolbar + thread bar)
    were merged — the «CHAT» label repeats are gone; thread actions (rename /
    download / pin / delete) live as icon-buttons on a single row."""
    html = (REPO_ROOT / "web_ui/index.html").read_text(encoding="utf-8")
    # Merged container: toolbar class + thread-bar id on the same div.
    assert 'class="chat-panel-toolbar" id="chat-thread-bar"' in html
    # The old label layers must not come back:
    assert "chat-panel-toolbar-label" not in html
    assert "chat-thread-label" not in html
    assert "chat-thread-bar-main" not in html
    # Thread actions live as icon-buttons on the compact row:
    assert 'class="chat-thread-actions"' in html
    for _bid in ("btn-thread-rename", "btn-thread-export", "btn-thread-pin", "btn-thread-delete"):
        assert f'id="{_bid}"' in html, f"thread action missing: {_bid}"


def test_composer_has_attachments_not_capture_button() -> None:
    """Composer: attach button + hidden file input + chip strip; the secondary memory
    button next to send was removed from the composer."""
    html = (REPO_ROOT / "web_ui/index.html").read_text(encoding="utf-8")
    composer = html.split('id="chat-form"')[1].split("</form>")[0]
    assert 'id="btn-attach"' in composer
    assert 'id="attach-input"' in composer
    assert 'id="composer-attachments"' in composer
    assert "btn-capture-memory" not in composer
    # Only image extensions are accepted:
    assert 'accept=".png,.jpg,.jpeg,.webp,.gif' in composer


def test_chat_sends_file_ids_from_uploads() -> None:
    """Attachment flow contract (PHASE2): the file goes to POST /api/v1/uploads; on
    send, `file_ids` (a list of strings, ANY type) is added to the ChatRequest body —
    if empty the field is not sent at all (transport chatPayload). Type-icon chip +
    cursor warning."""
    chat = (STATIC / "akana-chat.js").read_text(encoding="utf-8")
    transport = (STATIC / "akana-chat-transport.js").read_text(encoding="utf-8")
    assert "/api/v1/uploads" in chat
    assert "consumePendingFileIds" in chat
    assert "composer-attachments" in chat
    # 413 → size error (i18n key; text depends on EN/TR `language`):
    assert "chat.upload_too_large" in chat
    # Type icon (📄/🖼/📦) + provider-capability/limit warning (backend provider_native):
    assert "attachmentIcon" in chat
    assert "maybeWarnAttachments" in chat
    assert "file_ids" in transport
    assert "if (fileIds.length) payload.file_ids = fileIds;" in transport


def test_settings_model_pill_is_provider_aware() -> None:
    """Model pill contract: when provider=claude, 'Cursor · composer-2' MUST NOT be shown.

    akana-settings.js must read the single correct field from the status payload
    (model.active_tag, or on old servers claude_tag/cursor_tag depending on the
    provider); the pill text must be built with the provider label.
    """
    text = (STATIC / "akana-settings.js").read_text(encoding="utf-8")
    assert "activeModelInfo" in text
    assert "active_tag" in text
    assert "claude_tag" in text
    # The old provider-blind patterns must not come back:
    assert "`Cursor · ${model}`" not in text
    assert "Aktif Cursor model" not in text
    # The hero/profile label is provider-aware too:
    assert "Claude CLI" in text and "Cursor SDK" in text


def test_settings_surfaces_openai_provider_like_gemini() -> None:
    """The OpenAI provider must have EXACTLY the same surface as gemini in the UI:
    model switcher branch (openai_model field), credentials key (openai_api_key),
    hero/auth/pill labels. The backend payload (openai_models/active_openai_model_tag)
    is already GREEN; this contract verifies the front-end binds to it (gemini analog).
    """
    text = (STATIC / "akana-settings.js").read_text(encoding="utf-8")
    # Model switcher: the openai branch fetches models LIVE (/system/openai/models),
    # just like gemini (NOT a static payload field — gemini also reads from the live
    # endpoint; if unreachable the endpoint itself carries the fallback list).
    # active_openai_model_tag holds the active selection, persisted to the openai_model
    # field. Without this branch openai would fall to "unknown→Claude list" and PUT
    # claude_model (bug).
    assert 'provider === "openai"' in text
    assert "system/openai/models" in text  # live endpoint (twin of gemini's system/gemini/models)
    assert "active_openai_model_tag" in text
    assert '"openai_model"' in text
    # Credentials key: POSTed as openai_api_key (gemini twin).
    assert "openai_api_key" in text
    assert "cred-openai-key" in text
    assert "btn-toggle-cred-openai" in text
    # The pill / hero / auth-status labels recognize openai (don't fall to cursor).
    assert "OpenAI" in text
    assert "openai_tag" in text
    # Per-conversation LLM persist/restore carries openai_model too (like gemini).
    assert "patch.openai_model" in text
