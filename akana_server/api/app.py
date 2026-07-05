"""FastAPI application — Akana UI + multi-provider LLM backend."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware, GZipResponder
from starlette.types import Message, Receive, Scope, Send

from akana_server.api.static_files import (
    CachedStaticFiles,
    render_versioned_html,
)

from akana_server.api import ws as ws_routes
from akana_server.api.routes import voice_live as voice_live_routes
from akana_server.api.routes import voice_realtime as voice_realtime_routes
from akana_server.api.deps import require_akana_bearer
from akana_server.api.routes import chat as chat_routes
from akana_server.api.routes import connectors as connectors_routes
from akana_server.api.routes import conversations as conversations_routes
from akana_server.api.routes import credentials as credentials_routes
from akana_server.api.routes import vault as vault_routes
from akana_server.api.routes import files as files_routes
from akana_server.api.routes import llm_settings as llm_settings_routes
from akana_server.api.routes import memory as memory_routes
from akana_server.api.routes import network as network_routes
from akana_server.api.routes import packs as packs_routes
from akana_server.api.routes import personas as personas_routes
from akana_server.api.routes import runtime_settings as runtime_settings_routes
from akana_server.api.routes import system as system_routes
from akana_server.api.routes import tailscale as tailscale_routes
from akana_server.api.routes import skills as skills_routes
from akana_server.api.routes import tools as tools_routes
from akana_server.api.routes import uploads as uploads_routes
from akana_server.api.routes import voice as voice_routes
from akana_server.conversation_service import ConversationService
from akana_server.config import Settings, ensure_data_dirs, load_settings
from akana_server.events import EventHub
from akana_server.llm_settings import (
    load_llm_settings,
    resolve_claude_model_tag,
    resolve_cursor_model_tag,
    resolve_gemini_model_tag,
    resolve_ollama_model_tag,
    resolve_openai_model_tag,
    resolve_provider,
)

_PHASE = "P0"
_API_MARKER = "0.2.0"


def _web_ui_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "web_ui"


def setup_file_logging(data_dir: Path) -> Path:
    """Persistent file log: ``<data_dir>/logs/server.log`` (P0 stability).

    For live "page is frozen, restart required" class failures, a
    RotatingFileHandler (~2MB, 3 backups) is attached to the app + uvicorn
    loggers so evidence survives the restart. Idempotent: a second handler
    is not added to the same file (safe across multiple lifespans in tests).
    Uncaught request exceptions land in this file with a traceback via
    uvicorn.error.
    """
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "server.log"

    def _ours(h: logging.Handler) -> bool:
        return getattr(h, "_akana_server_log", False)

    loggers = [
        logging.getLogger(),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.access"),
    ]
    existing = next((h for lg in loggers for h in lg.handlers if _ours(h)), None)
    if existing is not None and getattr(existing, "baseFilename", None) == str(
        log_path
    ):
        return log_path  # idempotent: no second handler on the same file

    # If data_dir changed (test isolation), the old handler is removed — so
    # handlers/fds don't accumulate on the root logger.
    for lg in loggers:
        for h in [h for h in lg.handlers if _ours(h)]:
            lg.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler._akana_server_log = True  # type: ignore[attr-defined]
    handler.setLevel(logging.INFO)
    # Turn correlation (Step A): a filter bound to the handler stamps every
    # record with `trace_id` before formatting → `[%(trace_id)s]` is always
    # populated. A single turn can be traced across gate→provider→stream→persist
    # with `grep trace_id`.
    from akana_server.observability import TurnLogFilter

    handler.addFilter(TurnLogFilter())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(trace_id)s] %(name)s: %(message)s"
        )
    )
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    # uvicorn loggers do NOT propagate to root (they have their own handler
    # config) — so the handler is attached directly for the access + error
    # lines (the "Exception in ASGI application" with traceback). uvicorn.error
    # propagates to "uvicorn"; to avoid duplicate lines it is only added to the
    # top two.
    logging.getLogger("uvicorn").addHandler(handler)
    logging.getLogger("uvicorn.access").addHandler(handler)
    # python= + repo= let server.log identify WHICH install/interpreter is running
    # when two copies share one data dir (a foreign clean-room copy binding the real
    # port would otherwise be indistinguishable from the real server here).
    logging.getLogger("akana_server").info(
        "=== akana server log started (pid=%s, data_dir=%s, python=%s, repo=%s) ===",
        os.getpid(),
        data_dir,
        sys.executable,
        Path(__file__).resolve().parents[2],
    )
    return log_path


def _maybe_prewarm_xtts(settings: Settings) -> None:
    """If ``tts_engine=xtts``, load XTTS-v2 AT STARTUP in a background thread.

    The cold start (~38s, ~4GB VRAM→model) shifts from the first voice response
    to startup → the first response is fast too. Only when xtts is selected
    (no wasted VRAM/time on auto/edge/piper); skipped if coqui-tts/torch is
    missing. Daemon thread + all errors are swallowed → prewarm NEVER breaks
    startup (if it fails, the first synthesis loads lazily).
    """
    _log = logging.getLogger("akana_server")
    try:
        import os
        import threading

        from akana_server.voice_preferences import load_voice_preferences

        pref = (os.environ.get("AKANA_TTS_ENGINE", "") or "").strip().lower()
        if not pref:
            pref = (
                (load_voice_preferences(settings.data_dir).tts_engine or "")
                .strip()
                .lower()
            )
        if pref != "xtts":
            return
        from akana_server.voice.engines.xtts import XttsEngine, prewarm

        if not XttsEngine(settings).available():
            _log.info("XTTS prewarm skipped: coqui-tts/torch not installed")
            return
        threading.Thread(target=prewarm, name="xtts-prewarm", daemon=True).start()
        _log.info("XTTS-v2 prewarm started (tts_engine=xtts, in background)")
    except Exception:  # pragma: no cover - prewarm must not break startup
        _log.warning("XTTS prewarm hook skipped", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    ensure_data_dirs(settings.data_dir)
    setup_file_logging(settings.data_dir)
    # BUG 1: ORPHAN REAPER — if the previous session was abruptly killed via
    # SIGKILL, the lifespan finally never ran, and a surviving bridge daemon /
    # claude CLI orphan may be left behind. At bootstrap, clean up stale pid
    # groups (SIGTERM→SIGKILL). The reaper never breaks startup.
    try:
        from akana_server.orchestrator.llm_process import (
            reap_orphan_llm_processes,
        )

        reaped = reap_orphan_llm_processes(settings.data_dir)
        if any(r.get("reaped") for r in reaped):
            logging.getLogger("akana_server").warning(
                "reaped %d orphan LLM processes at startup",
                sum(1 for r in reaped if r.get("reaped")),
            )
    except Exception:  # pragma: no cover - reaper must not break startup
        logging.getLogger("akana_server").warning(
            "orphan LLM reaper hata verdi", exc_info=True
        )
    from akana_server.runtime_settings import (
        apply_runtime_overrides,
        bind_runtime_data_dir,
    )

    # RuntimeSettings: restart-required keys (Telegram) are applied to Settings
    # at startup; the store is bound for modules that don't carry settings, such
    # as planner/context.
    settings = apply_runtime_overrides(settings)
    bind_runtime_data_dir(settings.data_dir)
    app.state.settings = settings
    # MCP self-check (best-effort, non-blocking): spawn the built-in MCP servers once
    # and log whether they handshake, so server.log answers "is memory connected?" at
    # boot. Skipped under pytest (no subprocess spawns in the suite); disable with
    # AKANA_MCP_SELFCHECK=0. Stored on app.state so the task isn't GC'd mid-flight.
    if "pytest" not in sys.modules:
        from akana_server.orchestrator.mcp_selfcheck import (
            run_mcp_selfcheck,
            selfcheck_enabled,
        )

        if selfcheck_enabled():
            app.state.mcp_selfcheck_task = asyncio.create_task(
                run_mcp_selfcheck(settings)
            )
    app.state.event_hub = EventHub()
    # Memory build lock — created EAGERLY here so the first-touch race is closed:
    # if two concurrent first requests both lazily minted a Lock on app.state, each
    # would hold a DIFFERENT lock and both could run the build → a second, never-
    # detached VectorIndexer subscribed to the same Memory bus (#19 leak). One lock,
    # present before any request, serialises the build/rebuild for good.
    app.state.memory_build_lock = threading.Lock()
    llm = load_llm_settings(settings.data_dir, settings)
    # Conversation list/archive/history share one canonical store: ``memory.db``
    # (unified memory).
    app.state.conversation_service = ConversationService(settings.data_dir)
    app.state.llm_settings = llm
    # Packs default to ON: the content of every *enabled* discovered pack (skills/
    # personas/memory/plugins) is registered here once. Packs disabled via the
    # lifecycle (persisted in data_dir/packs_state.json) are loaded but not
    # registered; enable/disable hot-reload at runtime through the /packs API.
    from akana_server.packs.host import AkanaPackHost

    pack_host = AkanaPackHost(settings.data_dir)
    try:
        activated = pack_host.register_all()
        logging.getLogger("akana_server").info("packs: %d active", len(activated))
    except Exception:
        logging.getLogger("akana_server").warning(
            "pack register_all failed", exc_info=True
        )
    app.state.pack_host = pack_host
    from akana_server.orchestrator import session_closer_service

    session_closer_service.start_session_closer(app)
    from akana_server.orchestrator import summary_consolidation_service

    summary_consolidation_service.start_summary_consolidation(app)
    _maybe_prewarm_xtts(settings)
    from akana_server.connectors import service as connectors_service

    # ConnectorEngine F0-F1 — disabled by default (AKANA_TELEGRAM_ENABLED=false);
    # if no channel is active an empty registry is bound, and GET /connectors
    # still works.
    await connectors_service.start_connectors(app)
    try:
        yield
    finally:
        # SHUTDOWN FLAG: break the drain↔turn mutual recursion. As soon as an
        # active turn finishes, its finally spawns _maybe_drain_queue, which in
        # turn opens a NEW active turn via _start_detached_chat_turn. Once the
        # flag is set both return early → the drained task set does NOT refill,
        # and no new turn is BORN during shutdown (this was the root cause of
        # "Task destroyed but pending" + partial writes when bridge_pool was
        # torn down). Must be set BEFORE cancel/await.
        app.state.chat_shutting_down = True
        # Stop the MCP self-check probe if it is still handshaking — otherwise the task (and
        # the MCP subprocess it spawns) is orphaned at shutdown.
        sc_task = getattr(app.state, "mcp_selfcheck_task", None)
        if sc_task is not None and not sc_task.done():
            sc_task.cancel()
            try:
                await sc_task
            except (asyncio.CancelledError, Exception):  # pragma: no cover - cleanup best-effort
                pass
        # First, the fire-and-forget background tasks (memory capture + queue
        # drain) are cancelled → so they don't spawn a NEW turn during shutdown
        # and don't cause a dead-daemon error / partial write when the
        # shutdown_bridge_pool daemon below is torn down.
        await chat_routes.shutdown_background_tasks(app)
        # UNBREAKABLE RESPONSE: chat turns running independently of the client
        # are cleanly cancelled (the partial response is persisted) — so no
        # buffer-producing turn remains while the other services shut down.
        await chat_routes.shutdown_active_turns(app)
        await connectors_service.stop_connectors(app)
        await session_closer_service.stop_session_closer(app)
        await summary_consolidation_service.stop_summary_consolidation(app)
        from akana_server.orchestrator.bridge_pool import shutdown_bridge_pool

        await shutdown_bridge_pool()


class _StreamAwareGZipResponder(GZipResponder):
    """GZipResponder that passes ``text/event-stream`` bodies through untouched.

    Global gzip buffers streaming bodies: for ``more_body=True`` chunks the base
    responder writes each SSE event into a ``gzip.GzipFile`` and emits
    ``getvalue()`` without a sync-flush, so zlib holds the (tiny) events in its
    internal buffer until the stream closes — the browser's incremental reader
    gets nothing until the whole turn ends, defeating live chat/voice streaming.
    We reuse the base responder's own pre-set-content-encoding pass-through path:
    when the response advertises ``text/event-stream`` we flip
    ``content_encoding_set`` so every body message is forwarded verbatim.
    """

    async def send_with_gzip(self, message: Message) -> None:
        if message["type"] == "http.response.start" and not self.started:
            headers = Headers(raw=message["headers"])
            media_type = headers.get("content-type", "").split(";", 1)[0].strip()
            if media_type == "text/event-stream":
                self.content_encoding_set = True
                self.initial_message = message
                return
        await super().send_with_gzip(message)


class StreamAwareGZipMiddleware(GZipMiddleware):
    """``GZipMiddleware`` that never compresses SSE (``text/event-stream``) responses."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            if "gzip" in headers.get("Accept-Encoding", ""):
                responder = _StreamAwareGZipResponder(
                    self.app, self.minimum_size, compresslevel=self.compresslevel
                )
                await responder(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(title="Akana", version="0.2.0", lifespan=lifespan)
    app.add_middleware(StreamAwareGZipMiddleware, minimum_size=500)

    @app.get("/health", dependencies=[Depends(require_akana_bearer)])
    async def health() -> dict[str, Any]:
        from akana_server.orchestrator.cursor_provider import runtime_cursor_key

        s: Settings = app.state.settings
        return {
            "status": "ok",
            "phase": _PHASE,
            "api": _API_MARKER,
            "service": "akana",
            "cursor_api_key_set": bool(runtime_cursor_key(s)),
            "workspace": str(s.workspace),
            "server": {"host": s.server_host, "port": s.server_port},
        }

    @app.get("/api/v1/system/status", dependencies=[Depends(require_akana_bearer)])
    async def system_status(request: Request) -> dict[str, Any]:
        settings: Settings = request.app.state.settings
        llm = getattr(request.app.state, "llm_settings", None) or load_llm_settings(
            settings.data_dir, settings
        )
        from akana_server.orchestrator.claude_catalog import probe_claude_api
        from akana_server.orchestrator.cursor_catalog import probe_cursor_api
        from akana_server.orchestrator.gemini_catalog import probe_gemini_api
        from akana_server.orchestrator.openai_catalog import probe_openai_api
        from akana_server.secret_store import get_secret

        active_model = resolve_cursor_model_tag(settings, llm)
        provider = resolve_provider(settings, llm)
        claude_tag = resolve_claude_model_tag(settings, llm)
        ollama_tag = resolve_ollama_model_tag(settings, llm)
        gemini_tag = resolve_gemini_model_tag(settings, llm)
        openai_tag = resolve_openai_model_tag(settings, llm)
        active_tag = (
            claude_tag
            if provider == "claude"
            else ollama_tag
            if provider == "ollama"
            else gemini_tag
            if provider == "gemini"
            else openai_tag
            if provider == "openai"
            else active_model
        )
        claude_token_set = bool(get_secret(settings.data_dir, "claude_oauth_token"))
        cursor_probe = await probe_cursor_api(settings)
        claude_probe = await probe_claude_api(settings)
        gemini_probe = await probe_gemini_api(settings)
        openai_probe = await probe_openai_api(settings)
        return {
            "phase": _PHASE,
            "python": sys.version.split()[0],
            "server": {"host": settings.server_host, "port": settings.server_port},
            "active_provider": provider,
            "chat_path": provider,
            "model": {
                "cursor_tag": active_model,
                "claude_tag": claude_tag,
                "ollama_tag": ollama_tag,
                "gemini_tag": gemini_tag,
                "openai_tag": openai_tag,
                # The single source-of-truth fields: the UI model-pill reads
                # these. agent_id (=provider) is kept for backward compatibility.
                "provider": provider,
                "active_tag": active_tag,
                "agent_id": provider,
            },
            "chat_max_turns": llm.chat_max_turns,
            "dependencies": {
                "cursor_api": cursor_probe,
                "claude_cli": {
                    "bin": settings.claude_bin,
                    "oauth_token_set": claude_token_set,
                    # Live Anthropic /v1/models reachability (symmetric with
                    # cursor_api): token_set ALSO counts the credentials file
                    # (not just setup-token).
                    "reachable": claude_probe.get("reachable", False),
                    "token_set": claude_probe.get("token_set", claude_token_set),
                    "model_count": claude_probe.get("model_count", 0),
                    "error": claude_probe.get("error"),
                    # Language-neutral tag (no_session | token_expired | unreachable) the
                    # onboarding wizard localizes; auth-certain codes drive a hard revert.
                    "error_code": claude_probe.get("error_code"),
                },
                # Live Gemini API health (symmetric with cursor/claude): is the
                # key set + can google-genai models.list reach it (cached).
                "gemini_api": gemini_probe,
                # Live OpenAI API health (symmetric with gemini): is the key set
                # + can /models reach it (cached, raw httpx).
                "openai_api": openai_probe,
            },
            "memory": {
                "episodic_db": str(settings.data_dir / "db" / "episodic.db"),
                "event_log": str(settings.data_dir / "event_log.jsonl"),
            },
        }

    # index.html / memory.html must NEVER be cached: static assets are cached
    # immutably via `?v=`, but if these host documents are cached the browser
    # serves OLD version references (stale ?v=) → cache-bust stops working and
    # the new JS never loads (live incident: UI fixes "didn't land"). no-store
    # guarantees fresh HTML → fresh ?v= → fresh JS on every navigation.
    #
    # Automatic cache-bust: each static reference's `?v=` is rewritten at render
    # time with the file's content hash (render_versioned_html). No manual `?v=`
    # bump needed — the version changes only when the file changes, otherwise
    # the cache is effectively permanent.
    _NO_STORE = {"Cache-Control": "no-store, must-revalidate"}
    _static_dir = _web_ui_dir() / "static"

    @app.get("/")
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(
            render_versioned_html(_web_ui_dir() / "index.html", _static_dir),
            headers=_NO_STORE,
        )

    # PWA share_target (GET): returns the SPA so the client reads the shared
    # text from the query string (?title/&text/&url). Public, no bearer.
    @app.get("/share-target")
    async def share_target() -> HTMLResponse:
        return HTMLResponse(
            render_versioned_html(_web_ui_dir() / "index.html", _static_dir),
            headers=_NO_STORE,
        )

    @app.get("/memory")
    async def memory_studio() -> HTMLResponse:
        return HTMLResponse(
            render_versioned_html(_web_ui_dir() / "memory.html", _static_dir),
            headers=_NO_STORE,
        )

    # PWA: manifest + service worker served from root (public, no bearer) so the
    # browser can fetch them before any token is attached. The SW lives at "/sw.js"
    # and the Service-Worker-Allowed header lets it control the whole "/" scope.
    @app.get("/manifest.webmanifest")
    async def web_manifest() -> FileResponse:
        return FileResponse(
            _web_ui_dir() / "manifest.webmanifest",
            media_type="application/manifest+json",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/sw.js")
    async def service_worker() -> FileResponse:
        return FileResponse(
            _web_ui_dir() / "sw.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    static_dir = _web_ui_dir() / "static"
    if static_dir.is_dir():
        app.mount("/static", CachedStaticFiles(directory=static_dir), name="static")

    app.include_router(chat_routes.router, prefix="/api/v1")
    app.include_router(connectors_routes.router, prefix="/api/v1")
    app.include_router(conversations_routes.router, prefix="/api/v1")
    app.include_router(credentials_routes.router, prefix="/api/v1")
    app.include_router(vault_routes.router, prefix="/api/v1")
    app.include_router(files_routes.router, prefix="/api/v1")
    app.include_router(network_routes.router, prefix="/api/v1")
    app.include_router(voice_routes.router, prefix="/api/v1")
    app.include_router(system_routes.router, prefix="/api/v1")
    app.include_router(tailscale_routes.router, prefix="/api/v1")
    app.include_router(llm_settings_routes.router, prefix="/api/v1")
    app.include_router(runtime_settings_routes.router, prefix="/api/v1")
    app.include_router(tools_routes.router, prefix="/api/v1")
    app.include_router(uploads_routes.router, prefix="/api/v1")
    app.include_router(skills_routes.router, prefix="/api/v1")
    app.include_router(personas_routes.router, prefix="/api/v1")
    app.include_router(packs_routes.router, prefix="/api/v1")
    app.include_router(memory_routes.router, prefix="/api/v1")
    app.include_router(ws_routes.router)
    app.include_router(voice_live_routes.router)
    app.include_router(voice_realtime_routes.router)

    return app


app = create_app()
