"""Voice routes: STT upload → Cursor SDK chat → optional Piper TTS."""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Annotated, Any

import ulid
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from akana_server.api.deps import require_akana_bearer
from akana_server.api.routes.chat import (
    ChatResponse,
    TokenUsage,
    TurnError,
    _mirror_cursor_agent_meta,
    _reset_cursor_bridge_for_conversation,
    guard_nonstreaming_turn,
    run_nonstreaming_turn,
)
from akana_server.api.routes.chat._base import _off_loop
from akana_server.api.routes.chat.chat_producer import _tool_only_summary
from akana_server.api.services import AppServices, get_services
from akana_server.chat_context import (
    async_llm_history_for_assemble,
    bind_conversation_llm,
    get_agent_id,
    make_bootstrap_retry_hooks,
    persist_agent_id,
    record_context_assemble_metrics,
)
from akana_server.audit import write_event as audit_write
from akana_server.events import EventHub
from akana_server.llm_settings import resolve_cursor_model_tag
from akana_server.conversation_service import ConversationService
from akana_server.observability import begin_turn
from akana_server.orchestrator.bridge_pool import cursor_reuse_agent_enabled
from akana_server.orchestrator.memory_tools import memory_mcp_servers
from akana_server.orchestrator.router import classify_intent
from akana_server.orchestrator.turn_writer import (
    persist_assistant_turn,
    persist_user_turn,
)
from akana_server.voice import (
    SttError,
    TtsError,
    WakeError,
    list_available_voices,
    resolve_tts_lang,
    resolve_tts_voice_path,
    resolve_voice_selection,
    score_wake_wav_bytes,
    stream_text_to_tts_chunks,
    strip_markdown_for_tts,
    synthesize_with_fallback,
    transcribe_wav_bytes,
)
from akana_server.voice import engines as tts_engines
from akana_server.voice.wake import (
    feature_download_status,
    feature_models_ready,
    get_oww_model_from_disk,
    trigger_feature_models_download,
    trigger_wake_model_download,
    wake_download_status,
    wake_model_ready,
)
from akana_server.voice.gemini_live import (
    resolve_voice_directive,
    resolve_voice_persona_prefix,
)
from akana_server.voice.streaming_tts import _engine_preference
from akana_server.voice_preferences import (
    load_voice_preferences,
    update_voice_preferences,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])

_VOICE_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_WAKE_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# After this many consecutive failed background download attempts, /voice/wake/config
# stops reporting an absent feature/wake model as "preparing" (still downloading) and
# instead reports status="error" with the download's last error — so the frontend can
# tell "the download keeps failing (offline/parked on backoff)" from "the first fetch is
# still running" and surface the browser-fallback guidance instead of polling forever.
_WAKE_DOWNLOAD_FAIL_THRESHOLD = 3


def _client_ip(request: Request) -> str | None:
    try:
        return request.client.host if request.client else None
    except AttributeError:
        return None


def _form_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def _parse_file_ids(raw: str | None) -> list[str]:
    """Convert a comma-separated upload-id string into a clean list.

    Composer attachments (image/PDF) arrive as "id1,id2,..." in the `file_ids`
    form field. Trim whitespace, drop empties and de-duplicate while preserving
    order (before passing them to gemini/openai NATIVE input). Returns an empty
    list if the field is absent.
    """
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        fid = part.strip()
        if fid and fid not in seen:
            seen.add(fid)
            out.append(fid)
    return out


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


class TtsRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000)
    lang: str | None = Field(default=None, max_length=8)


class TtsStreamRequest(BaseModel):
    # Higher cap than the one-shot request: the streaming path chunks by sentence,
    # so a long assistant message (e.g. a detailed explanation) is handled fine and
    # must not be rejected — that is the exact case this endpoint exists to speed up.
    text: str = Field(..., min_length=1, max_length=60000)
    lang: str | None = Field(default=None, max_length=8)


class VoicePreferencesPatch(BaseModel):
    wake_autostart: bool | None = None
    stream_tts: bool | None = None
    # The TTS engine and per-language (edge) voice names — without these fields the
    # user's selected voice was NOT persisted (if pydantic doesn't recognize a field
    # it silently drops it → reverting to the default voice on every request). See
    # bug: "the voice keeps reverting to the old voice".
    tts_engine: str | None = Field(default=None, max_length=16)
    tts_voice_tr: str | None = Field(default=None, max_length=64)
    tts_voice_en: str | None = Field(default=None, max_length=64)


@router.get("/voice/preferences", dependencies=[Depends(require_akana_bearer)])
async def get_voice_preferences(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    settings = services.settings
    prefs = await _off_loop(load_voice_preferences, settings.data_dir)
    return prefs.to_dict()


@router.patch("/voice/preferences", dependencies=[Depends(require_akana_bearer)])
async def patch_voice_preferences(
    body: VoicePreferencesPatch, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    settings = services.settings
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        prefs = await _off_loop(load_voice_preferences, settings.data_dir)
        return prefs.to_dict()
    prefs = await _off_loop(update_voice_preferences, settings.data_dir, patch)
    return prefs.to_dict()


@router.get("/voice/config", dependencies=[Depends(require_akana_bearer)])
async def get_voice_config(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    """Capability snapshot consumed by the dashboard.

    Lists BOTH engines' voices (edge neural + Piper) and the currently
    persisted selection so the Settings picker can show — and round-trip —
    the user's real choice. Previously only Piper voices were returned; the edge
    voice selection wasn't persisted because it didn't appear in the UI.
    """
    # The whole body is synchronous (piper glob + edge/xtts list_voices network/file
    # I/O + prefs read) — moved to a worker thread to avoid blocking the event loop.
    return await _off_loop(_build_voice_config, services.settings)


def _build_voice_config(settings: Any) -> dict[str, Any]:
    piper_voices = list_available_voices(settings)
    tts_ready = _has("piper") and any(
        v["exists"] and v.get("configured") for v in piper_voices
    )
    prefs = load_voice_preferences(settings.data_dir)
    edge_installed = _has("edge_tts")
    engine_voices: list[dict[str, Any]] = []
    for v in piper_voices:
        engine_voices.append({**v, "engine": "piper", "id": v.get("path")})
    if edge_installed:
        try:
            edge_engine = tts_engines.get("edge", settings)
            for v in edge_engine.list_voices():
                engine_voices.append(
                    {
                        "engine": "edge",
                        "id": v.get("id"),
                        "name": v.get("name") or v.get("id"),
                        "lang": v.get("lang", "?"),
                        "exists": True,
                        "configured": False,
                    }
                )
        except TtsError:
            pass
    if _has("TTS") and _has("torch"):  # local XTTS-v2 (optional engine)
        try:
            xtts_engine = tts_engines.get("xtts", settings)
            for v in xtts_engine.list_voices():
                engine_voices.append(
                    {
                        "engine": "xtts",
                        "id": v.get("id"),
                        "name": v.get("name") or v.get("id"),
                        "lang": v.get("lang", "?"),
                        "exists": True,
                        "configured": False,
                    }
                )
        except TtsError:
            pass
    return {
        "tts": {
            "engine": _engine_preference(settings, prefs),
            "engines": tts_engines.registered_engines(),
            "edge_installed": edge_installed,
            "selected_engine": prefs.tts_engine,
            "selected_voice_tr": prefs.tts_voice_tr,
            "selected_voice_en": prefs.tts_voice_en,
            "installed": _has("piper"),
            "ready": tts_ready,
            "voices_dir": str(settings.voices_dir),
            "primary_lang": settings.primary_lang,
            "max_chars": settings.voice_tts_max_chars,
            "voices": piper_voices,
            "engine_voices": engine_voices,
        },
        "stt": {
            "engine": "faster-whisper",
            "installed": _has("faster_whisper"),
            "model": settings.whisper_model,
            "compute_type": settings.whisper_compute_type,
            "device": settings.whisper_device,
            "browser_fallback": True,
        },
        "wake": {
            "engine": "openwakeword",
            "installed": _has("openwakeword"),
            "model": settings.wake_model,
            "threshold": settings.wake_threshold,
            "min_frames": settings.wake_min_frames,
            "browser_fallback": True,
        },
        # Gemini Live capability snapshot (Phase 2) — the UI shows the "Live (realtime)"
        # toggle ONLY when all three are true: SDK+key (available) + flag on (enabled)
        # + active provider is gemini (provider_is_gemini).
        "live": _live_capability(settings),
        # OpenAI Realtime capability snapshot — the twin of gemini Live; the UI shows/wires
        # the same "Live" toggle based on this block when provider==openai.
        "realtime": _realtime_capability(settings),
    }


def _live_capability(settings: Any) -> dict[str, Any]:
    """Capability block for the Gemini Live UI toggle (pure read; no network).

    ``available`` = SDK installed + key present; ``enabled`` = runtime flag;
    ``provider_is_gemini`` = whether the active LLM provider is gemini (Live is gemini-only)."""
    from akana_server.llm_settings import load_llm_settings, resolve_provider
    from akana_server.orchestrator.gemini_shared import (
        gemini_available,
        resolve_gemini_live_voice,
    )

    try:
        llm = load_llm_settings(settings.data_dir, settings)
        provider_is_gemini = resolve_provider(settings, llm) == "gemini"
    except Exception:  # pragma: no cover - a settings read must not break the toggle
        provider_is_gemini = False
    return {
        "enabled": bool(getattr(settings, "gemini_live_enabled", False)),
        "available": gemini_available(settings),
        "provider_is_gemini": provider_is_gemini,
        "voice": resolve_gemini_live_voice(settings),
    }


def _realtime_capability(settings: Any) -> dict[str, Any]:
    """Capability block for the OpenAI Realtime UI toggle (the twin of ``_live_capability``).

    ``available`` = key present (transport is fixed to websockets); ``enabled`` = runtime
    flag; ``provider_is_openai`` = whether the active LLM provider is openai (Realtime is
    openai-only). Audio I/O rate is 24k (the UI connects to ``/ws/voice/realtime`` based on this block)."""
    from akana_server.llm_settings import load_llm_settings, resolve_provider
    from akana_server.orchestrator.openai_shared import (
        openai_realtime_available,
        resolve_openai_realtime_voice,
    )

    try:
        llm = load_llm_settings(settings.data_dir, settings)
        provider_is_openai = resolve_provider(settings, llm) == "openai"
    except Exception:  # pragma: no cover - a settings read must not break the toggle
        provider_is_openai = False
    return {
        "enabled": bool(getattr(settings, "openai_realtime_enabled", False)),
        "available": openai_realtime_available(settings),
        "provider_is_openai": provider_is_openai,
        "voice": resolve_openai_realtime_voice(settings),
    }


@router.get("/voice/wake/config", dependencies=[Depends(require_akana_bearer)])
async def get_voice_wake_config(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    settings = services.settings
    # Server-side openWakeWord scoring is enabled when WAKE_MODEL is set (it defaults to
    # the bundled "hey_akana" model) AND openwakeword is installed AND the model actually
    # loads. When enabled, the frontend's default "model" wake source polls this scorer;
    # otherwise it falls back to the browser SpeechRecognition phrase-match. The user can
    # also pick the browser source explicitly in voice settings.
    #
    # We do NOT stop at "package importable + WAKE_MODEL set": on a fresh install the
    # shared openWakeWord feature models are absent, so Model() init deterministically
    # 503s. Reporting enabled=true there makes the frontend disable the browser fallback
    # and leaves wake with zero working path. So we attempt the (cached) load and only
    # report enabled=true when it succeeds; on failure we surface the init error as a hint
    # and let the browser fallback take over.
    # This endpoint is POLLED (onboarding + every wake-arm) and MUST return promptly —
    # it never waits on the network. When the shared openWakeWord feature models are
    # absent (fresh install), we do NOT download them inline; we fire a one-shot
    # BACKGROUND fetch (guarded by the wake module's backoff/negative-cache) and report
    # ``preparing`` so the frontend keeps the browser SpeechRecognition fallback and a
    # later poll flips to enabled=true once the download lands.
    configured = bool(settings.wake_model.strip()) and _has("openwakeword")
    framework = settings.wake_inference_framework
    server_scoring = False
    # Two INDEPENDENT downloads can be pending (each with its own backoff/negative-cache):
    # the shared feature models, and — when WAKE_MODEL is a bare pretrained name — the
    # wake model file itself. Track them separately so the hint below names the RIGHT one
    # and a permanently-failed download surfaces as status="error" (not perpetual
    # "preparing" that drives the frontend to poll forever).
    preparing_feature = False
    preparing_wake = False
    load_error: str | None = None
    if configured:
        # Disk-only readiness checks (no network). Neither download is fetched inline
        # here (this endpoint is polled + must stay offline-safe); each absent one kicks
        # off its own one-shot background fetch.
        feat_ready = await _off_loop(feature_models_ready, framework)
        wake_ready = await _off_loop(wake_model_ready, settings.wake_model)
        if not feat_ready:
            # Shared feature models absent → fetch in the background. If the background
            # fetch has already FAILED repeatedly (parked on backoff, e.g. offline),
            # surface that as a hard error instead of "still preparing" forever.
            await _off_loop(trigger_feature_models_download, framework)
            fails, last_err = await _off_loop(feature_download_status, framework)
            if fails >= _WAKE_DOWNLOAD_FAIL_THRESHOLD:
                load_error = last_err or "wake feature models could not be downloaded."
            else:
                preparing_feature = True
        if not wake_ready:
            # Wake model file absent. A bare pretrained name is downloadable → fetch in
            # the background (trigger no-ops for a missing local path). A repeatedly-failed
            # pretrained fetch surfaces as an error; a missing LOCAL path is a hard error
            # from the disk-only load below.
            await _off_loop(trigger_wake_model_download, settings.wake_model)
            fails, last_err = await _off_loop(wake_download_status, settings.wake_model)
            if fails >= _WAKE_DOWNLOAD_FAIL_THRESHOLD:
                # Only overwrite an existing feature load_error if none is set yet; the
                # feature-model failure (loaded first) is the more fundamental one.
                load_error = load_error or last_err or (
                    f"wake model {settings.wake_model!r} could not be downloaded."
                )
        if feat_ready and wake_ready:
            try:
                # Files are present → a Model() init only (cached on success); this never
                # touches the network, so a first poll after it returns 200, not 503.
                await _off_loop(
                    get_oww_model_from_disk, settings.wake_model, framework
                )
                server_scoring = True
            except WakeError as e:
                load_error = e.message
            except Exception as e:  # pragma: no cover - defensive: never break the toggle
                load_error = str(e)
        elif feat_ready and not wake_ready and not preparing_wake and load_error is None:
            # Feature models are ready but the wake model is not, and the background
            # fetch has not yet failed enough to be a hard error: preparing if it is a
            # downloadable pretrained name, otherwise a hard load error (missing local
            # path). get_oww_model_from_disk raises 400 for the latter, 503 for the former.
            try:
                await _off_loop(get_oww_model_from_disk, settings.wake_model, framework)
                server_scoring = True
            except WakeError as e:
                if e.status_code == 400:
                    load_error = e.message
                else:
                    preparing_wake = True
            except Exception as e:  # pragma: no cover - defensive: never break the toggle
                load_error = str(e)
    preparing = preparing_feature or preparing_wake
    if server_scoring:
        status = "ready"
        hint = "Local «Hey Akana» acoustic model is active — tune the threshold below."
    elif configured and load_error:
        # A hard load error (missing local path, or a repeatedly-failed/parked download)
        # takes precedence over "preparing" — the frontend stops polling and surfaces the
        # browser fallback instead of promising a switch-on that will never come.
        status = "error"
        hint = (
            f"Server wake model could not load ({load_error}) — «Hey Akana» is detected "
            "in the browser instead. Fix: `python akana.py add voice-full`."
        )
    elif preparing:
        status = "preparing"
        # Name the download that is actually pending so the hint is not misleading when
        # only the (tiny) wake model is downloading and the feature models are on disk.
        if preparing_feature:
            what = "downloading the shared feature models"
        else:
            what = "downloading the wake model"
        hint = (
            f"Preparing the server wake model ({what} in the background) — «Hey Akana» "
            "is detected in the browser meanwhile; this will switch on automatically "
            "once the download finishes."
        )
    else:
        status = "off"
        hint = (
            "Server wake model is off (install the voice extra to enable it); "
            "«Hey Akana» is detected in the browser instead."
        )
    return {
        "enabled": server_scoring,
        "status": status,
        "wake_model": settings.wake_model,
        "threshold": settings.wake_threshold,
        "min_frames": settings.wake_min_frames,
        "inference_framework": settings.wake_inference_framework,
        # Which background download (if any) is still pending — lets the frontend/operator
        # tell the two independent fetches apart without reading server logs. None when
        # nothing is downloading (ready/off/error).
        "preparing": (
            "feature" if preparing_feature else "wake" if preparing_wake else None
        ),
        "load_error": load_error,
        "hint": hint,
    }


@router.post("/voice/wake", dependencies=[Depends(require_akana_bearer)])
async def post_voice_wake(
    request: Request,
    audio: Annotated[UploadFile, File()],
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    if not audio.filename:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "BAD_REQUEST", "message": "The audio file name is missing."}},
        )
    # Read limit + 1 byte: oversize content is rejected without being fully loaded into memory (the uploads.py pattern).
    raw = await audio.read(_WAKE_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _WAKE_MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": {"code": "PAYLOAD_TOO_LARGE", "message": "The audio file is too large; try a shorter recording."}},
        )
    if not raw:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "BAD_REQUEST", "message": "Empty audio recording; please speak again."}},
        )
    settings = services.settings
    try:
        result = await score_wake_wav_bytes(raw, settings)
    except WakeError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"error": {"code": "WAKE_ERROR", "message": e.message}},
        ) from e
    # Wake polling is CONTINUOUS (~600ms, listening for "hey akana"). Writing audit
    # on every poll (even when not triggered) meant constant disk/SQLite writes +
    # audit DB bloat — needlessly keeping the server busy and slowing the WHOLE app
    # (chat included). Write audit ONLY on a REAL trigger ("hey akana" detected);
    # non-trigger polls (the overwhelming majority) pass silently.
    if result.triggered:
        await _off_loop(
            audit_write,
            settings.data_dir,
            "wake",
            client_ip=_client_ip(request),
            data={
                "wake_model": result.wake_model,
                "threshold": result.threshold,
                "max_score": result.max_score,
                "triggered": result.triggered,
                "audio_bytes": len(raw),
            },
        )
    return asdict(result)


@router.post("/voice/tts", dependencies=[Depends(require_akana_bearer)])
async def post_voice_tts(
    body: TtsRequest, services: AppServices = Depends(get_services)
) -> Response:
    """Synthesize ``text`` honoring the persisted engine/voice preference.

    Routes through the engine registry (``synthesize_with_fallback``) so the
    voice the user picked in Settings is the one they hear here too — the test
    button must match the streaming reply, otherwise the selected voice appears
    to "change". Piper stays, with a guaranteed automatic fallback to edge.
    """
    settings = services.settings
    try:
        lang = resolve_tts_lang(settings, tts_lang=body.lang)
        try:
            voice_path = resolve_tts_voice_path(settings, tts_lang=body.lang)
        except TtsError:
            # No Piper .onnx — but edge/xtts need none; synthesize_with_fallback resolves
            # the engine and raises a clear 503 only if NO engine is usable (mirror of
            # chat_producer's streaming path).
            voice_path = None
        spoken = strip_markdown_for_tts(body.text) or body.text
        audio, mime = await synthesize_with_fallback(
            spoken, settings, lang=lang, voice_path=voice_path, fallback_on_timeout=True
        )
    except TtsError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"error": {"code": "TTS_ERROR", "message": e.message}},
        ) from e
    return Response(
        content=audio,
        media_type=mime or "audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/voice/tts/stream", dependencies=[Depends(require_akana_bearer)])
async def post_voice_tts_stream(
    body: TtsStreamRequest, services: AppServices = Depends(get_services)
) -> StreamingResponse:
    """Stream sentence-level TTS for ``text`` as Server-Sent Events.

    Reuses the SAME per-sentence chunker + prefetch window + edge→piper fallback
    the voice-conversation path uses (``stream_text_to_tts_chunks``), feeding the
    whole message in as a single "delta". The FIRST audible audio then arrives
    after only the first sentence is synthesized instead of after the whole
    message — the read-aloud button's latency on long replies drops accordingly.

    Each SSE ``data:`` line is a JSON object ``{"seq", "audio_b64", "mime"}``;
    a terminal ``{"type": "end"}`` closes the stream and ``{"type": "error",
    "message"}`` reports a fatal synthesis failure (the client then falls back to
    the one-shot ``/voice/tts`` blob).
    """
    settings = services.settings
    try:
        # resolve_tts_lang 400s on an invalid ``lang`` (anything but auto/tr/en).
        # It runs BEFORE the StreamingResponse, so — unlike the mid-stream errors
        # below — it must map to a clean 400 here or FastAPI turns it into a raw
        # 500 and the request fails before the SSE stream (and its documented
        # one-shot fallback) can start (mirror of the one-shot post_voice_tts).
        lang = resolve_tts_lang(settings, tts_lang=body.lang)
        try:
            voice_path = resolve_tts_voice_path(settings, tts_lang=body.lang)
        except TtsError:
            # edge/xtts need no Piper .onnx; the chunker resolves the engine itself and
            # only fails if NO engine is usable (mirror of the one-shot path).
            voice_path = None
    except TtsError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"error": {"code": "TTS_ERROR", "message": e.message}},
        ) from e
    text = body.text

    async def _one_delta() -> AsyncIterator[str]:
        # The chunker splits this into sentences internally and prefetches ahead.
        yield text

    async def _events() -> AsyncIterator[bytes]:
        try:
            try:
                selection = resolve_voice_selection(
                    settings, lang=lang, voice_path=voice_path
                )
            except TtsError:
                selection = None
            async for chunk in stream_text_to_tts_chunks(
                _one_delta(), settings, voice_path=voice_path, selection=selection
            ):
                payload = {
                    "seq": chunk.get("seq"),
                    "audio_b64": chunk.get("audio_b64"),
                    "mime": chunk.get("mime"),
                }
                yield f"data: {json.dumps(payload)}\n\n".encode()
        except TtsError as e:
            yield f"data: {json.dumps({'type': 'error', 'message': e.message})}\n\n".encode()
            return
        except Exception as e:  # pragma: no cover - defensive: never leak a raw 500 mid-stream
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n".encode()
            return
        yield b'data: {"type": "end"}\n\n'

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # Disable proxy (nginx / Tailscale Serve) response buffering so chunks
            # flush to the client as each sentence is synthesized, not at the end.
            "X-Accel-Buffering": "no",
        },
    )


class TranscribeResponse(BaseModel):
    """STT-only response — no LLM call and no TTS.

    Voice conversation mode gets the transcript from here, then feeds the text to
    the normal chat path (``/chat/stream``). Since the policy gate is applied on
    the chat path, it is not repeated here. Silence/empty audio returns 200 + an
    empty ``transcript`` so the client can silently resume listening.
    """

    transcript: str = ""
    stt_lang: str | None = None


@router.post("/voice/transcribe", dependencies=[Depends(require_akana_bearer)])
async def post_voice_transcribe(
    audio: Annotated[UploadFile, File()],
    lang: Annotated[str | None, Form(max_length=16)] = None,
    services: AppServices = Depends(get_services),
) -> TranscribeResponse:
    """Transcribe speech to text only (Whisper) — no LLM/TTS.

    A corrupt/too-short WAV → 400, no STT backend → 503; silence → 200 + empty text.
    """
    if not audio.filename:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "BAD_REQUEST", "message": "The audio file name is missing."}},
        )
    # Read limit + 1 byte: oversize content is rejected without being fully loaded into memory (the uploads.py pattern).
    raw = await audio.read(_VOICE_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _VOICE_MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": {"code": "PAYLOAD_TOO_LARGE", "message": "The audio file is too large; try a shorter recording."}},
        )
    if not raw:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "BAD_REQUEST", "message": "Empty audio recording; please speak again."}},
        )
    settings = services.settings
    try:
        transcript, stt_lang = await transcribe_wav_bytes(raw, settings, language=lang)
    except SttError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"error": {"code": "STT_ERROR", "message": e.message}},
        ) from e
    return TranscribeResponse(transcript=transcript.strip(), stt_lang=stt_lang)


class VoiceResponse(ChatResponse):
    transcript: str = Field(..., min_length=1)
    stt_lang: str | None = None
    audio_wav_base64: str | None = None
    # Depending on the selected engine this may be WAV (piper) or MP3 (edge) — the
    # client reads the MIME to decode it correctly.
    audio_mime: str | None = None
    # Set when TTS was requested but synthesis failed AFTER the turn was already
    # persisted: the turn (text/transcript/turn_id) is still returned so the client
    # renders it, just without audio — the client can surface this and offer a retry
    # of only the read-aloud (POST /voice/tts) instead of re-sending the whole turn.
    tts_error: str | None = None


@router.post("/voice", dependencies=[Depends(require_akana_bearer)])
@guard_nonstreaming_turn(lambda a: a.get("conversation_id"))
async def post_voice(
    request: Request,
    audio: Annotated[UploadFile, File()],
    lang: Annotated[str | None, Form(max_length=16)] = None,
    tts: Annotated[str | None, Form()] = None,
    tts_lang: Annotated[str | None, Form(max_length=8)] = None,
    conversation_id: Annotated[str | None, Form(max_length=64)] = None,
    # Composer attachments: comma-separated upload ids (image/PDF). Passed to the
    # gemini/openai NATIVE input; ignored for cursor/claude/ollama.
    file_ids: Annotated[str | None, Form(max_length=4096)] = None,
    services: AppServices = Depends(get_services),
) -> VoiceResponse:
    if not audio.filename:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "BAD_REQUEST", "message": "The audio file name is missing."}},
        )
    # Read limit + 1 byte: oversize content is rejected without being fully loaded into memory (the uploads.py pattern).
    raw = await audio.read(_VOICE_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _VOICE_MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": {"code": "PAYLOAD_TOO_LARGE", "message": "The audio file is too large; try a shorter recording."}},
        )
    if not raw:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "BAD_REQUEST", "message": "Empty audio recording; please speak again."}},
        )

    settings = services.settings
    t0 = time.perf_counter()
    try:
        transcript, stt_lang = await transcribe_wav_bytes(raw, settings, language=lang)
    except SttError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"error": {"code": "STT_ERROR", "message": e.message}},
        ) from e

    if not transcript.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "STT_EMPTY", "message": "No speech detected; please try again."}},
        )

    # FULL AUTONOMY: the risk/approval gate is removed for voice requests — the
    # transcript is not blocked and no approval is requested; only the intent is
    # classified.
    intent = classify_intent(transcript)

    # Parse the composer attachments (image/PDF); if empty pass None and skip NATIVE input.
    parsed_file_ids = _parse_file_ids(file_ids)

    conv_id = conversation_id or str(ulid.new())
    # R3 #9: log correlation — the blocking (post_chat) and stream (post_chat_stream)
    # paths call begin_turn at the start of the turn; voice did not → all voice-turn
    # logs came out with trace_id=-, so a turn's STT→LLM→persist lines couldn't be correlated.
    begin_turn(conv_id, mode="voice")
    conv_svc = getattr(request.app.state, "conversation_service", None)
    if isinstance(conv_svc, ConversationService):
        await _off_loop(conv_svc.ensure, conv_id)
    history_msgs, _, history_skipped = await async_llm_history_for_assemble(
        request, conv_id
    )
    context_mode = record_context_assemble_metrics(skipped_resume=history_skipped)
    bootstrap_loader, bootstrap_hook = make_bootstrap_retry_hooks(request, conv_id)
    # v1 in-prompt memory injection was retired (#3/B): recall now comes from the
    # memory_search MCP tool.
    user_for_llm = transcript

    # Voice-mode hint — when the system prompt (persona/builtin.py) sees this tag it
    # gives a short, markdown-free, heading-free reply. It is placed at the very top
    # so the LLM sees the mode first, with the memory context and transcript after.
    user_for_llm = f"[mode: voice]\n{user_for_llm}"

    # Honor the CONFIGURED core prompt + persona binding (the same source the text
    # chat path uses). Without this the dispatcher falls back to the hardcoded
    # builtin CHAT_SYSTEM_PREFIX and a user-customized base_prompt would be ignored
    # in voice. Defensive: resolution failure → "" → dispatcher uses the builtin.
    voice_system_prompt = resolve_voice_persona_prefix(
        settings, app=request.app, conv_id=conv_id
    )
    # Append the user-editable, bilingual voice directive (short, markdown-free
    # spoken replies). Override or language-default; "" on any failure (the persona
    # still carries a brief [mode: voice] hint, so voice never breaks).
    voice_directive = resolve_voice_directive(settings)
    if voice_directive:
        voice_system_prompt = f"{voice_system_prompt}\n\n{voice_directive}".strip()

    # Busy-guard (Convergence A #2) is in the guard_nonstreaming_turn decorator (above).
    # #6/#7: the path that collects the STREAMING bridge → tool_calls (tool cards/ledger)
    # + agent reuse (no cold-start). Get the existing agent → pass it → persist what
    # comes back; voice used to NEITHER get NOR pass agent_id (cold-start every turn).
    # b16: bind the per-conversation effective LLM for the turn so the DISPATCHER resolves the
    # conversation's provider (not the global one) and the model tag comes from the conversation
    # settings. Without this, voice dispatched on the GLOBAL provider while get_agent_id's
    # leak-guard used the conversation override → the two disagreed and the other provider's
    # session id leaked in (wrong provider + "no conversation found"/empty response). Mirrors the
    # blocking post_chat path.
    with bind_conversation_llm(request, conv_id) as _eff_llm:
        agent_id = await _off_loop(get_agent_id, request, conv_id)
        # SHARED TURN CORE: voice runs the LLM through the SAME single-turn core as the
        # blocking POST /chat path (turn_core.run_nonstreaming_turn) instead of a
        # hand-synced copy of the dispatch + error mapping. Voice gains the streaming
        # producer's safeguards for free — empty-response retry, BreakerOpenError →
        # LLM_RATE_LIMITED, and the active-run bridge reset — and can no longer drift
        # from chat (rest-api:arch:1 / voice.py smell). Voice keeps ONLY its own
        # specifics: STT above, TTS below, the [mode: voice] hint + voice persona/
        # directive system prompt, the per-conversation model tag, and paired persist.
        try:
            outcome = await run_nonstreaming_turn(
                settings,
                user_for_llm,
                history=history_msgs,
                system_prompt=voice_system_prompt or None,
                model=resolve_cursor_model_tag(settings, _eff_llm),
                # conv_id forwarded for symmetry with the chat path (passed to the MCP
                # payload builder).
                mcp_servers=memory_mcp_servers(settings, conv_id),
                conversation_id=conv_id,
                agent_id=agent_id,
                reuse_agent=cursor_reuse_agent_enabled(),
                bootstrap_history_loader=bootstrap_loader,
                on_bootstrap_retry=bootstrap_hook,
                context_mode=context_mode,
                # Composer attachments wired in: upload ids parsed from the form's
                # `file_ids` field are passed to the gemini/openai NATIVE image input
                # (None if empty).
                file_ids=parsed_file_ids or None,
                on_active_run_reset=lambda: _reset_cursor_bridge_for_conversation(
                    request.app, conv_id
                ),
            )
        except TurnError as e:
            # BUG FIX (#5 orphan-turn): we do NOT persist the user turn BEFORE the LLM.
            # If the LLM fails, NO turn has been written here → so there's no answerless
            # "dangling user" turn (half a pair) + no counter drift + no corrupt context
            # feed. The core carries the streaming error codes (BAD_REQUEST / LLM_TIMEOUT
            # / LLM_RATE_LIMITED / LLM_UNAVAILABLE) — same mapping the blocking path uses.
            raise HTTPException(
                status_code=e.status_code,
                detail={"error": {"code": e.code, "message": e.message}},
            ) from e
        # #6: persist the bridge agent_id → the next voice turn reuses it (no cold-start).
        # Done INSIDE the bind_conversation_llm block so persist_agent_id reads the per-turn
        # provider snapshot to TAG the id — without the tag get_agent_id's leak-guard defaults
        # it to 'cursor' and a claude voice session never resumes / a claude uuid leaks into a
        # cursor resume. Off-load BOTH writes: they run a locked memory.db UPDATE txn
        # (busy_timeout=10000) that would freeze every SSE/WS/HTTP endpoint on the loop — the
        # blocking + streaming paths already offload this (routes.py / chat_producer.py).
        if outcome.agent_id:
            await _off_loop(persist_agent_id, request, conv_id, outcome.agent_id)
            await _off_loop(_mirror_cursor_agent_meta, request, conv_id, outcome.agent_id)
    text = outcome.text
    usage = outcome.usage
    tool_calls_resp = [c for c in outcome.tool_calls if isinstance(c, dict)]

    llm_latency_ms = int((time.perf_counter() - t0) * 1000)
    # Persist AFTER the LLM SUCCEEDS: the user + assistant turns are written TOGETHER,
    # only if there's a real response OR the turn ran tools. On an LLM error /
    # empty-and-toolless response neither is written → a half pair (orphan) is
    # structurally impossible.
    #
    # tool_calls MUST ride on the persisted assistant turn → a /messages reload returns
    # the tool cards (they were dropped before, so the cards vanished on reload). A
    # tool-only turn (tools ran, empty final text) still persists BOTH turns with a
    # placeholder body — mirror the streaming producer (_tool_only_summary); otherwise
    # the whole exchange (transcript + tools) is silently dropped from the archive.
    assistant_body = text if text.strip() else (
        _tool_only_summary(tool_calls_resp) if tool_calls_resp else ""
    )
    user_turn_id: str | None = None
    if assistant_body:
        user_turn_id = await _off_loop(
            persist_user_turn,
            conversation_id=conv_id,
            user_text=transcript,
            lang=stt_lang,
            # b31: persist the voice-turn attachments with the user turn (the text/stream path
            # already does). Without this the file_ids were sent to the LLM but dropped from the
            # archive → the attachment vanished from history on reload.
            file_ids=parsed_file_ids or None,
            data_dir=settings.data_dir,
        )
        await _off_loop(
            persist_assistant_turn,
            conversation_id=conv_id,
            assistant_text=assistant_body,
            user_turn_id=user_turn_id,
            lang=stt_lang,
            latency_ms=llm_latency_ms,
            intent=intent,
            tool_calls=tool_calls_resp or None,
            data_dir=settings.data_dir,
        )
    if isinstance(conv_svc, ConversationService):
        meta_after = await _off_loop(conv_svc.get, conv_id)
        new_history_len = int(meta_after.message_count) if meta_after else 0
    else:
        new_history_len = 0

    want_tts = _form_truthy(tts)
    audio_b64: str | None = None
    audio_mime: str | None = None
    tts_error: str | None = None
    if want_tts:
        try:
            # Separate variable: the TTS output language must NOT overwrite the
            # ``lang`` field in the response. ``lang`` reflects the turn's (persisted)
            # language = ``stt_lang``; the TTS language only goes to the synthesis engine.
            tts_out_lang = resolve_tts_lang(settings, tts_lang=tts_lang, stt_lang=stt_lang)
            try:
                vpath = resolve_tts_voice_path(settings, tts_lang=tts_lang, stt_lang=stt_lang)
            except TtsError:
                # edge/xtts need no Piper .onnx; the fallback resolves the engine and 503s
                # only if none is usable (mirror of the streaming path).
                vpath = None
            spoken = strip_markdown_for_tts(text) or text
            audio, audio_mime = await synthesize_with_fallback(
                spoken, settings, lang=tts_out_lang, voice_path=vpath, fallback_on_timeout=True
            )
            audio_b64 = base64.standard_b64encode(audio).decode("ascii")
        except TtsError as e:
            # Graceful degrade: the user + assistant turns are ALREADY persisted
            # (above), so aborting with a 5xx would leave a committed-but-invisible
            # turn — the user re-asks and the pair is duplicated. Instead return the
            # turn text with no audio + a tts_error hint, and still broadcast/audit
            # below (mirrors the streaming path, which emits tts_error but keeps
            # delivering text/done).
            log.warning("voice TTS failed after turn persisted (degrading to text-only): %s", e.message)
            audio_mime = None
            tts_error = e.message

    latency_ms = int((time.perf_counter() - t0) * 1000)
    turn_id = str(ulid.new())
    resp = VoiceResponse(
        turn_id=turn_id,
        text=text,
        lang=stt_lang,
        conversation_id=conv_id,
        history_turns=new_history_len,
        intent=intent,
        approval_required=False,
        transcript=transcript,
        stt_lang=stt_lang,
        audio_wav_base64=audio_b64,
        audio_mime=audio_mime,
        tts_error=tts_error,
        tool_calls=tool_calls_resp,
        memory_writes=[],
        latency_ms=latency_ms,
        tokens=TokenUsage(
            prompt=int(usage.get("prompt_tokens", 0) or 0),
            completion=int(usage.get("completion_tokens", 0) or 0),
        ),
    )
    hub = getattr(request.app.state, "event_hub", None)
    if isinstance(hub, EventHub):
        await hub.broadcast_json(
            {
                "type": "chat_done",
                "turn_id": turn_id,
                "conversation_id": conv_id,
                "intent": intent,
                "approval_required": False,
                "tool_calls_count": len(tool_calls_resp),
                "latency_ms": latency_ms,
                "preview": text[:400],
                "source": "voice",
            }
        )
    await _off_loop(
        audit_write,
        settings.data_dir,
        "voice",
        turn_id=turn_id,
        conv_id=conv_id,
        client_ip=_client_ip(request),
        data={
            "intent": intent,
            "approval_required": False,
            "stt_lang": stt_lang,
            "tts": want_tts,
            "tts_error": tts_error,
            "latency_ms": latency_ms,
            "audio_bytes": len(raw),
            "transcript_preview": transcript[:200],
            "assistant_preview": text[:200],
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        },
    )
    return resp
