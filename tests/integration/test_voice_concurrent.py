"""Voice surface concurrency — POST /voice over the REAL ASGI stack (owner #2 concern).

Since ``post_voice`` uses ``Depends(get_services)`` it cannot be called directly;
it is driven over the REAL ASGI stack (``httpx.ASGITransport``). The STT (Whisper) and
LLM (cursor) external seams are monkeypatched in the ``voice`` module — no real
network/model. Each STT transcript echoes which conv it belongs to → cross-talk detector.

NOTE: the voice path does NOT register an ``_ActiveTurn`` (follower/replay buffer); but
the ``guard_nonstreaming_turn`` decorator writes the conv into ``_nonstreaming_busy``
and ``_is_turn_running`` looks at BOTH the ``_active_turns`` (stream) AND the
``_nonstreaming_busy`` (voice/blocking) ledger → the voice↔stream busy-guard sees both.
These tests verify data integrity in voice↔voice and voice↔stream concurrency
(user/assistant pairs not getting mixed up, none lost) and that concurrent
voice+stream on the SAME conv runs a single turn while the other gets a 202/409 (NO
nested LLM start).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from akana_server.api.app import create_app
from akana_server.api.routes import chat as chat_routes


@pytest.fixture
def app_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    monkeypatch.setenv("AKANA_MEMORY_LLM_CAPTURE", "0")
    return tmp_path


def _install_voice_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """STT: a fixed transcript per conv (conv marker embedded in the audio bytes).

    The client sends the WAV bytes as ``b"WAV:" + conv_id``; the fake STT decodes
    this and returns ``"konuş <conv_id>"``. The fake LLM echoes the transcript →
    the response text carries the conv_id (persist cross-talk detector).
    """

    async def _fake_transcribe(raw: bytes, settings: Any, language: Any = None) -> tuple[str, str]:
        await asyncio.sleep(0.02)  # force the turns to actually run interleaved
        tag = raw.decode("utf-8", "replace")
        conv = tag.split("WAV:", 1)[-1] if "WAV:" in tag else "?"
        return f"konuş {conv}", "tr"

    async def _fake_complete(settings: Any, user_for_llm: str, *_a: Any, **kw: Any) -> tuple[str, dict[str, Any]]:
        await asyncio.sleep(0.02)
        conv_id = str(kw.get("conversation_id") or "?")
        return f"VOICE[{conv_id}]", {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes", _fake_transcribe
    )
    # Voice now dispatches through the shared turn core, which reads
    # complete_chat_with_usage from the chat package namespace at call time.
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _fake_complete
    )


def _voice_files(conv_id: str) -> dict[str, Any]:
    return {
        "audio": ("clip.wav", b"WAV:" + conv_id.encode("utf-8"), "audio/wav"),
    }


def test_concurrent_voice_distinct_conversations_no_crosstalk(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent POST /voice to 5 DISTINCT conversations — response/transcript must not mix.

    SYMPTOM target: concurrent voice turns mix up the conv_ids (A's response goes
    to B). Each response must carry its OWN conv marker, the transcript its OWN conv.
    """
    _install_voice_fakes(monkeypatch)
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                conv_ids = [f"voice-{i}" for i in range(5)]

                async def _post(cid: str):
                    r = await client.post(
                        "/api/v1/voice",
                        files=_voice_files(cid),
                        data={"conversation_id": cid},
                    )
                    return cid, r

                results = await asyncio.gather(*(_post(c) for c in conv_ids))
            for cid, r in results:
                assert r.status_code == 200, f"{cid}: {r.status_code} {r.text[:200]}"
                data = r.json()
                assert data["conversation_id"] == cid
                assert data["transcript"] == f"konuş {cid}", (
                    f"{cid}: transcript got mixed up: {data['transcript']!r}"
                )
                assert data["text"] == f"VOICE[{cid}]", (
                    f"CROSS-TALK (voice): {cid} received {data['text']!r}"
                )

    asyncio.run(main())


def test_concurrent_voice_distinct_conversations_persist_correctly(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AFTER concurrent voice turns each conv's episodic record must contain its own pair.

    SYMPTOM target: the voice synchronous persist (on the loop) mixes up / loses
    turns under concurrent writes.
    """
    _install_voice_fakes(monkeypatch)
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                conv_ids = [f"vpersist-{i}" for i in range(5)]

                async def _post(cid: str):
                    return await client.post(
                        "/api/v1/voice",
                        files=_voice_files(cid),
                        data={"conversation_id": cid},
                    )

                await asyncio.gather(*(_post(c) for c in conv_ids))
            svc = app.state.conversation_service
            for cid in conv_ids:
                msgs = svc.list_messages(cid, limit=10)
                pairs = [(m.role, m.content) for m in msgs]
                assert ("user", f"konuş {cid}") in pairs, (
                    f"{cid}: user turn wrong/missing: {pairs}"
                )
                asst = [c for r, c in pairs if r == "assistant"]
                assert asst and f"VOICE[{cid}]" in asst[-1], (
                    f"{cid}: assistant persist cross-talk/loss: {asst}"
                )

    asyncio.run(main())


def test_concurrent_voice_same_conversation_no_crash(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3 concurrent POST /voice to the SAME conv — no crash, user turns not lost.

    SYMPTOM target: because voice does not register an ``_ActiveTurn``, ``_raise_if_turn_busy``
    does NOT catch concurrent voice requests → both run. In the worst case the
    response order may be indeterminate but the SERVER MUST NOT CRASH and no user turn
    may silently DROP (3 requests → at least 3 user turns expected). This is an
    observation test documenting that there is NO queue in voice.
    """
    _install_voice_fakes(monkeypatch)
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                cid = "voice-same"

                async def _post(i: int):
                    return await client.post(
                        "/api/v1/voice",
                        files={"audio": ("c.wav", b"WAV:" + cid.encode(), "audio/wav")},
                        data={"conversation_id": cid},
                    )

                results = await asyncio.gather(*(_post(i) for i in range(3)))
            # None should be 5xx (crash); 200 or (since there is no queue) 200.
            for r in results:
                assert r.status_code < 500, f"server error under voice concurrency: {r.status_code} {r.text[:200]}"
            ok = [r for r in results if r.status_code == 200]
            assert ok, f"no successful voice response: {[r.status_code for r in results]}"
            svc = app.state.conversation_service
            msgs = svc.list_messages(cid, limit=20)
            user_turns = [m for m in msgs if m.role == "user"]
            # Every successful request must write a user turn (no silent loss).
            assert len(user_turns) >= len(ok), (
                f"user turn loss under voice concurrency: "
                f"{len(ok)} successes but {len(user_turns)} user turns"
            )

    asyncio.run(main())


def test_voice_then_concurrent_stream_distinct_conversations(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Voice turn + stream turn concurrent on DISTINCT conversations — the two surfaces must not mix.

    SYMPTOM target: voice (queueless, synchronous persist) and stream (detached)
    corrupt each other's episodic record while running at the same time.
    """
    _install_voice_fakes(monkeypatch)

    async def _stream_fake(settings: Any, user_for_llm: str, *_a: Any, **kw: Any):
        conv_id = str(kw.get("conversation_id") or "?")
        for piece in (f"STREAM[{conv_id}]", "!"):
            yield {"delta": piece, "done": False}
            await asyncio.sleep(0.02)
        yield {
            "done": True,
            "text": f"STREAM[{conv_id}]!",
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(chat_routes, "stream_user_chat", _stream_fake)
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                voice_cid = "mix-voice"
                stream_cid = "mix-stream"

                async def _do_voice():
                    return await client.post(
                        "/api/v1/voice",
                        files=_voice_files(voice_cid),
                        data={"conversation_id": voice_cid},
                    )

                async def _do_stream():
                    body = ""
                    async with client.stream(
                        "POST",
                        "/api/v1/chat/stream",
                        json={"text": "yaz", "conversation_id": stream_cid},
                    ) as resp:
                        async for chunk in resp.aiter_text():
                            body += chunk
                    return body

                vresp, sbody = await asyncio.gather(_do_voice(), _do_stream())
                assert vresp.status_code == 200
                assert vresp.json()["text"] == f"VOICE[{voice_cid}]"
                assert f"STREAM[{stream_cid}]" in sbody
                # Let the stream turn finish.
                await asyncio.sleep(0.1)
            svc = app.state.conversation_service
            # voice conv contains only the voice pair, stream conv only the stream pair.
            voice_msgs = [(m.role, m.content) for m in svc.list_messages(voice_cid, limit=10)]
            stream_msgs = [(m.role, m.content) for m in svc.list_messages(stream_cid, limit=10)]
            assert ("assistant", f"VOICE[{voice_cid}]") in voice_msgs
            assert any(r == "assistant" and f"STREAM[{stream_cid}]" in c for r, c in stream_msgs)
            # No cross leakage.
            assert not any("STREAM[" in c for r, c in voice_msgs if r == "assistant")
            assert not any("VOICE[" in c for r, c in stream_msgs if r == "assistant")

    asyncio.run(main())


class _LLMOverlap:
    """Tracks the number of LLM runs running CONCURRENTLY on the same conv (keeps the peak).

    The counter increments on entry and decrements on exit for both voice
    (``complete_chat_with_usage``) and stream (``stream_user_chat``). Since the two
    surfaces share the SAME conv's ``agent_id``, peak>1 = nested LLM/persist
    (the root of the bridge "active run" error). If the busy-guard works, peak=1.
    """

    def __init__(self) -> None:
        self.live: dict[str, int] = {}
        self.peak: dict[str, int] = {}

    def enter(self, conv_id: str) -> None:
        n = self.live.get(conv_id, 0) + 1
        self.live[conv_id] = n
        self.peak[conv_id] = max(self.peak.get(conv_id, 0), n)

    def exit(self, conv_id: str) -> None:
        self.live[conv_id] = max(0, self.live.get(conv_id, 0) - 1)


def test_same_conversation_voice_and_stream_no_nested_llm(
    app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONCURRENT POST /voice + POST /chat/stream on the SAME conv — a single turn runs.

    SYMPTOM target (R3 #1): while the voice turn waits on STT a stream turn arrives
    on the same conv; the old ``_start_detached_chat_turn`` only looked at
    ``_active_turns`` so it SKIPPED the busy-guard and ran a 2nd parallel LLM/persist
    on the SAME agent_id (bridge "active run" + nested turn). After the fix
    one runs the full turn, the other gets a 202 (queue) / 409 (busy) and on the SAME conv
    the concurrent LLM run peak does NOT EXCEED 1 (no nested LLM start).
    """
    overlap = _LLMOverlap()
    _install_voice_fakes(monkeypatch)

    async def _voice_complete(
        settings: Any, user_for_llm: str, *_a: Any, **kw: Any
    ) -> tuple[str, dict[str, Any]]:
        conv_id = str(kw.get("conversation_id") or "?")
        overlap.enter(conv_id)
        try:
            await asyncio.sleep(0.05)  # give a window for the turn to overlap with the stream
            return f"VOICE[{conv_id}]", {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "tool_calls": [],
            }
        finally:
            overlap.exit(conv_id)

    async def _stream_fake(settings: Any, user_for_llm: str, *_a: Any, **kw: Any):
        conv_id = str(kw.get("conversation_id") or "?")
        overlap.enter(conv_id)
        try:
            for piece in (f"STREAM[{conv_id}]", "!"):
                yield {"delta": piece, "done": False}
                await asyncio.sleep(0.05)
            yield {
                "done": True,
                "text": f"STREAM[{conv_id}]!",
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "tool_calls": []},
                "status": "finished",
                "tool_calls": [],
            }
        finally:
            overlap.exit(conv_id)

    # replace the voice fake with the overlap-tracking version (add the stream too).
    # Voice dispatches via the shared turn core → patch the chat-package symbol.
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _voice_complete
    )
    monkeypatch.setattr(chat_routes, "stream_user_chat", _stream_fake)
    app = create_app()

    async def main() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                cid = "same-voice-stream"

                async def _do_voice():
                    return await client.post(
                        "/api/v1/voice",
                        files=_voice_files(cid),
                        data={"conversation_id": cid},
                    )

                async def _do_stream():
                    r = await client.post(
                        "/api/v1/chat/stream",
                        json={"text": "yaz", "conversation_id": cid},
                    )
                    return r

                vresp, sresp = await asyncio.gather(_do_voice(), _do_stream())
                # Let the turn(s) finish (including the background detached stream).
                await asyncio.sleep(0.2)

            # 1) None should be 5xx (crash / bridge "active run").
            for tag, r in (("voice", vresp), ("stream", sresp)):
                assert r.status_code < 500, (
                    f"{tag}: server error on concurrent voice+stream: "
                    f"{r.status_code} {r.text[:200]}"
                )

            # 2) Both ends of the race are LEGITIMATE: whoever registers first runs
            #    the turn (200), the other is rejected via busy — 202 (queue) / 409 (busy) or
            #    404 (busy seen but the conv is not yet usable from the stream's
            #    perspective: before the voice ensure). There can NEVER be a parallel
            #    second turn (both 200). The 200s must be exactly ONE.
            assert sresp.status_code in (200, 202, 404, 409), (
                f"stream unexpected status: {sresp.status_code} {sresp.text[:200]}"
            )
            assert vresp.status_code in (200, 404, 409), (
                f"voice unexpected status: {vresp.status_code} {vresp.text[:200]}"
            )
            ran = [
                tag
                for tag, r in (("voice", vresp), ("stream", sresp))
                if r.status_code == 200
            ]
            # EXACTLY one turn ran: neither both rejected (busy-guard deadlock),
            # nor both ran (busy-guard skipped = R3 #1 bug).
            assert ran == ["voice"] or ran == ["stream"], (
                f"number of turns running on the SAME conv is not 1: {ran} "
                f"(voice={vresp.status_code} stream={sresp.status_code})"
            )

            # 3) ROOT ASSERTION: on the same conv the concurrent LLM run peak must not exceed 1
            #    (nested LLM/persist = the signature of the R3 #1 bug: overlap=2).
            assert overlap.peak.get(cid, 0) <= 1, (
                f"nested LLM start detected (overlap peak={overlap.peak.get(cid)}); "
                "busy-guard did not block the 2nd parallel turn on the same conv"
            )

    asyncio.run(main())
