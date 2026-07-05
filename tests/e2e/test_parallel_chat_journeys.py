"""Parallel chat — cross-conversation proof in a REAL browser (Playwright).

Owner's #1 concern: "it breaks when messages are sent to different chats AT THE SAME TIME;
the bug usually happens when switching between chats (while the LLM is still thinking and
writing)." This module runs that scenario end-to-end in ONE real Chrome and produces PROOF:

* Slow fake LLM that EMBEDS the conv-id in every delta (cross-talk detector):
  if B's id appears in A's bubble → cross-talk bug.
* Choreography: write to A → while A streams open new chat B + write → switch to B/A
  while A streams. Both turns must finish CLEAN and COMPLETE in THEIR OWN conversation.

Nothing is faked except the fake LLM; the real lifespan app starts with REAL
uvicorn (daemon thread) and the real static UI is served.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

pytestmark = [pytest.mark.e2e, pytest.mark.browser]

CHROME = "/usr/bin/google-chrome"
#: First delta arrives after SLOW_DELAY (first-marker is fast). Long TAIL (TAIL_DELTAS)
#: makes each turn last ~SLOW_DELAY*(TAIL+2) ≈ 6 s → A is GUARANTEED to still stream
#: when B is sent (overlap is deterministic; harness jitter cannot collapse the window).
SLOW_DELAY = 0.4
TAIL_DELTAS = 12
_MARKER = re.compile(r"\[([0-9A-Za-z]{20,})\]B")  # conv id from the first BEGIN marker

#: DETERMINISM GATE — the parallel-overlap test HOLDS `done` (clear()): once A & B
#: have streamed all their deltas and reach `done` they stay SUSPENDED in the registry →
#: the "both alive" window is measured INDEPENDENT of harness/cold-start jitter
#: (zero-flake proof: A can no longer finish naturally before the poll starts). Default SET →
#: single-chat test and other flows finish fast as normal; test calls set() in finally.
_RELEASE_DONE = threading.Event()
_RELEASE_DONE.set()
#: Safety cap: if the gate is never opened (test forgets/crashes) the stream releases
#: itself after this interval — prevents the server hanging indefinitely
#: (overlap poll is 6 s; this is larger).
_DONE_HOLD_CAP_S = 15.0


async def slow_echo_stream(settings, user_for_llm, *args, **kwargs):
    """Slow stream that EMBEDS the conv_id in EVERY delta — cross-talk detector.

    Signature matches ``chat_producer._chatpkg.stream_user_chat(...)``:
    settings + user_for_llm positional, rest keyword. The produced event dict
    shape is the same as existing ``fake_stream`` (delta… then done+text+usage).
    """
    conv_id = str(kwargs.get("conversation_id") or "?")
    deltas = [f"[{conv_id}]B "] + [f"[{conv_id}]{i} " for i in range(TAIL_DELTAS)] + [f"[{conv_id}]E"]
    for piece in deltas:
        await asyncio.sleep(SLOW_DELAY)
        yield {"delta": piece, "done": False}
    # DETERMINISM: hold `done` until the gate opens → stream stays SUSPENDED in registry
    # (poll without blocking event loop). Gate is SET by default → never waits.
    held = 0.0
    while not _RELEASE_DONE.is_set() and held < _DONE_HOLD_CAP_S:
        await asyncio.sleep(0.05)
        held += 0.05
    yield {
        "done": True,
        "text": "".join(deltas),
        "usage": {"prompt_tokens": 1, "completion_tokens": len(deltas), "tool_calls": []},
        "status": "finished",
        "tool_calls": [],
    }


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[str]:
    """Isolated data_dir + REAL uvicorn (daemon thread) with a slow fake LLM.

    The patch (``stream_user_chat``) is written into the package namespace BEFORE
    the server starts; ``chat_producer`` reads it via ``_chatpkg`` at call time →
    the server running in the same process sees the patch.
    """
    env = {
        "AKANA_DATA_DIR": str(tmp_path),
        "AKANA_TOKEN": "",
        "CURSOR_API_KEY": "",
        "AKANA_MEMORY_LLM_CAPTURE": "0",
        "AKANA_SESSION_CLOSER_ENABLED": "0",
        "AKANA_TELEGRAM_ENABLED": "0",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from akana_server.api.app import create_app
    from akana_server.api.routes import chat as chat_routes
    from akana_server.skills.registry import reload_skills

    reload_skills()
    monkeypatch.setattr(chat_routes, "stream_user_chat", slow_echo_stream)

    app = create_app()
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn daemon thread did not start within 30 s"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        reload_skills()


@pytest.fixture
def page(live_server: str):
    """System Chrome (executable_path) — NO playwright install needed."""
    pw = pytest.importorskip("playwright.sync_api")
    if not os.path.exists(CHROME):
        pytest.skip(f"system chrome not found: {CHROME}")
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME, headless=True, args=["--no-sandbox"]
        )
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        # The first-run onboarding modal (aur-onb-backdrop) swallows the click;
        # set the flag BEFORE the page scripts run (init script runs at document_start).
        context.add_init_script(
            "try{localStorage.setItem('akana.onboarded','1')}catch(e){}"
        )
        pg = context.new_page()
        pg.goto(live_server, wait_until="domcontentloaded")
        pg.wait_for_selector("#msg", timeout=10_000)
        # Wait until the public API is ready (modules load with defer).
        pg.wait_for_function("() => !!window.AkanaChat", timeout=10_000)
        try:
            yield pg
        finally:
            context.close()
            browser.close()


# -- helpers --------------------------------------------------------------------


def _assistant_text(page) -> str:
    """Assistant bubbles of ONLY THE DISPLAYED (non-hidden) conv-pane.

    In parallel-chat each conversation lives in its OWN ``.conv-pane``; on switch
    the others become ``[hidden]`` (display:none) but stay in the DOM (background
    streaming continues). Reading all ``#log .bubble-bot`` would also include the
    hidden A pane's text → since in headless Chrome ``innerText`` does NOT return
    empty for a display:none subtree, the test would read the wrong (background)
    conversation's marker. Use ``:not([hidden])`` to take only the displayed
    conversation's bubbles (fall back to the old single-#log path if no pane)."""
    return page.evaluate(
        """() => {
            // If the pane model EXISTS, always scope to the displayed pane (even if
            // empty → ""), NEVER fall back to hidden panes. Fallback ONLY when there
            // is no .conv-pane at all (the old single-#log page). Otherwise, while B's
            // pane has no bubbles yet, the fallback would read hidden A's marker
            // (innerText doesn't empty display:none).
            const sel = document.querySelector('#log .conv-pane')
                ? '#log .conv-pane:not([hidden]) .bubble-bot, #log .conv-pane:not([hidden]) .bubble-assistant'
                : '#log .bubble-bot, #log .bubble-assistant';
            return Array.from(document.querySelectorAll(sel)).map(el => el.innerText).join('\\n');
        }"""
    )


def _send(page, text: str) -> None:
    """NORMAL send path: Enter (shell.js requestSubmit → forceImmediate=False).

    CAUTION: do NOT click the ``#send`` button — if the button is in a stale STOP
    mode (while another conversation streams), the click falls into
    'stop-then-send' (forceImmediate) and SKIPS the guard → masks the bug. Enter
    always runs the guarded path.
    """
    page.fill("#msg", text)
    page.focus("#msg")
    page.press("#msg", "Enter")


def _new_chat(page) -> None:
    page.click("#btn-new-conv")


def _send_mode(page) -> str:
    return page.evaluate("() => document.getElementById('send').dataset.mode")


def _wait_marker(page, deadline_s: float = 20.0) -> str:
    """Wait until the first BEGIN marker appears → return the conv id."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        m = _MARKER.search(_assistant_text(page))
        if m:
            return m.group(1)
        page.wait_for_timeout(100)
    raise AssertionError("BEGIN marker did not appear in time")


def _wait_done(page, conv_id: str, deadline_s: float = 25.0) -> None:
    """Wait until this conv's END marker appears in the displayed #log."""
    end = time.monotonic() + deadline_s
    needle = f"[{conv_id}]E"
    while time.monotonic() < end:
        if needle in _assistant_text(page):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"END marker did not appear: {conv_id}\nlast text:\n{_assistant_text(page)}")


def _switch(page, conv_id: str) -> None:
    """Click the real item in the archive list (real user path).

    If the item is not in the DOM (the list may have shrunk) fall back to the
    public switch — the switch logic (the code under test) is the same; the
    click target is not the bug.
    """
    sel = f'.chat-archive-item[data-conversation-id="{conv_id}"]'
    try:
        # The locator re-resolves on EVERY attempt → even if the archive list
        # re-renders (done/refresh) it won't hit the "Element is not attached"
        # race (the old query_selector+handle.click clicked a stale handle). The
        # click target/DOM churn is not the product bug; the switch LOGIC still
        # goes through the real UI handler.
        page.locator(sel).first.click(timeout=5_000)
    except Exception:
        # Item missing/unreachable → fall back to the public switch (switch logic under test is the same).
        page.evaluate("(id) => window.AkanaChat.switchChatConversation(id)", conv_id)


# -- (1) single-chat smoke test: are the harness + UI stream sound -----------------------


def test_single_chat_streams_in_browser(page) -> None:
    _send(page, "selam A")
    conv = _wait_marker(page)
    _wait_done(page, conv)
    text = _assistant_text(page)
    assert f"[{conv}]B" in text and f"[{conv}]E" in text, text


# -- (2) THE REAL PROOF: two parallel conversations + mid-stream switch, NO cross-talk ----------


def test_parallel_two_chats_no_crosstalk(page, request) -> None:
    # DETERMINISM: for the duration of this test the fake LLM HOLDS `done` (clear).
    # NEW DESIGN (connection cap): on switch, the LEAVING conversation's CLIENT
    # stream is dropped but the SERVER turn continues DETACHED. Holding `done`
    # keeps that detached turn ALIVE → "COMPLETE recovery via resume on return"
    # is proven deterministically (the turn can't finish naturally and miss the
    # proof before the poll). Released UNCONDITIONALLY at teardown (so the server
    # doesn't hang even if an assert fails/crashes). The END marker depends on
    # the LAST delta, not on `done` → _wait_done works during the hold too
    # (E ~SLOW_DELAY*(TAIL+2)).
    request.addfinalizer(_RELEASE_DONE.set)
    _RELEASE_DONE.clear()

    # Write to A (Enter = guarded path), start the stream
    _send(page, "mesaj A")
    conv_a = _wait_marker(page)

    # While A streams open a NEW conversation B (real + button) — mid-stream switch #1.
    # Once empty B is displayed the composer must be SEND (A's STOP must not stay stale).
    _new_chat(page)
    page.wait_for_function(
        "(a) => (sessionStorage.getItem('akana.conversationId') || '') !== a",
        arg=conv_a,
        timeout=10_000,
    )
    assert _send_mode(page) == "send", (
        "while empty B is displayed the composer is stuck in STOP (A's stale state) — "
        "the user cannot type into B / send normally"
    )

    # FIX #1 (parallel-chat): on switch, the LEAVING A's stream is NOT cut → A KEEPS
    # STREAMING client-side in its OWN hidden pane. The old "connection cap" abort
    # (abort→detach→resume) was unreliable → "the reply vanished when switching to
    # another chat"; it was reverted. PROOF: A is still stream-ACTIVE
    # (isConversationStreamActive(A)=true).
    # (Cost: ~3-4 parallel client-stream cap on HTTP/1.1 — a rare edge case.)
    page.wait_for_function(
        "(a) => window.AkanaChat.isConversationStreamActive(a) === true",
        arg=conv_a,
        timeout=10_000,
    )

    # Write to B (Enter = guarded path). The guard is PER-CONV (NOT GLOBAL): while A
    # runs in the background (on the server) B must still be sendable normally. With a
    # global guard, submitChatText would DROP B → the marker wait below would time out.
    _send(page, "mesaj B")
    conv_b = _wait_marker(page)
    assert conv_b != conv_a, "B must be a new conversation (parallel while A streams)"
    # PARALLEL N-STREAM (fix #1): A (background pane) + B (foreground) stream client-side
    # AT THE SAME TIME; each writes to its OWN pane → no cross-talk. The displayed B is active too.
    assert page.evaluate(
        "(b) => !!window.AkanaChat.isConversationStreamActive(b)", conv_b
    ), "foreground B must be client-streaming (while A also streams in the background)"

    # THE REAL PROOF — NO LOSS + NO cross-talk: switch BACK to A (mid-stream switch #2)
    # and wait for its finish. Since A was never interrupted, ALL its text (B…E) appears
    # in its OWN pane, COMPLETE, WITHOUT B's id leaking in (fix #1: background pane model
    # — the stream never breaks, no reattach/resume needed).
    _switch(page, conv_a)
    _wait_done(page, conv_a)
    a_text = _assistant_text(page)
    assert f"[{conv_a}]B" in a_text and f"[{conv_a}]E" in a_text, f"A incomplete:\n{a_text}"
    assert conv_b not in a_text, f"CROSS-TALK: B's id is in A's bubble:\n{a_text}"

    # Switch to B, wait for its finish, verify its cleanliness
    _switch(page, conv_b)
    _wait_done(page, conv_b)
    b_text = _assistant_text(page)
    assert f"[{conv_b}]B" in b_text and f"[{conv_b}]E" in b_text, f"B incomplete:\n{b_text}"
    assert conv_a not in b_text, f"CROSS-TALK: A's id is in B's bubble:\n{b_text}"
