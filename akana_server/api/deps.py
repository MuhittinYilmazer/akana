"""FastAPI dependencies (auth, settings, lazy services).

Core services come through :mod:`akana_server.api.services` (``get_services``)
in a typed form. In addition, the typed dependencies for the **lazy** services
(``file_service``, ``image_store``) live here: each is built once on
``app.state`` and cached (build-once), returning the same instance on
subsequent requests. Lazy caching is behind a typed ``Depends`` so the
signatures are explicit and testable:
``app.dependency_overrides[get_<svc>] = lambda: fake``.
"""

from __future__ import annotations

import hmac
import ipaddress
import threading

from fastapi import HTTPException, Request, WebSocket

from akana_server.config import Settings, allow_unauthenticated
from akana_server.files.service import FileService
from akana_server.multimodal.store import UploadStore

#: A request carrying any of these reached us THROUGH a reverse proxy (Tailscale
#: Serve, nginx, caddy…) — i.e. potentially from outside the host. A direct
#: loopback client sets none of them, so they distinguish "local" from "proxied".
_FORWARDED_HEADERS = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
)


def request_is_proxied(headers) -> bool:
    """True if the request shows reverse-proxy/forwarding evidence."""
    if any(h in headers for h in _FORWARDED_HEADERS):
        return True
    # Tailscale Serve injects identity headers (Tailscale-User-Login, …).
    return any(k.startswith("tailscale-user-") for k in headers.keys())


def _peer_is_loopback(conn) -> bool:
    """True only when the DIRECT peer address is loopback (127.0.0.0/8, ::1).

    Unknown/absent peer → False (untrusted). This is the REAL trust signal: a remote
    client connecting DIRECTLY to a non-loopback bind has a non-loopback peer even when
    it sends no forwarding headers, so header heuristics alone are not enough to decide
    "local". Works for both Request and WebSocket (both expose ``.client.host``).
    """
    client = getattr(conn, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_akana_bearer(request: Request) -> None:
    settings: Settings = request.app.state.settings
    # Trusted local owner = a DIRECT request from a LOOPBACK peer with no reverse-proxy
    # headers. Only that origin skips the token, so the local web UI "just works".
    # ANY other origin — proxied, OR a non-loopback peer connecting directly to a
    # non-loopback bind — MUST present AKANA_TOKEN when one is configured. (The old check
    # trusted ANY non-proxied request, so a direct remote connection bypassed auth.)
    proxied = request_is_proxied(request.headers)
    if not settings.api_token:
        # OPEN MODE (no token). A DIRECT (non-proxied) request is allowed — the startup
        # guard refuses a non-loopback bind without a token, so open mode is effectively
        # loopback-only. A PROXIED request still needs the explicit opt-in (request-layer
        # backstop that closes the Tailscale Serve hole). UNCHANGED behaviour.
        if not proxied:
            return
        if allow_unauthenticated():
            return
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "AUTH_REQUIRED",
                    "message": (
                        "This request reached the server through a reverse proxy but no "
                        "API token is configured. Set AKANA_TOKEN (recommended), or "
                        "AKANA_ALLOW_UNAUTHENTICATED=1 to allow unauthenticated access."
                    ),
                }
            },
        )
    # TOKEN CONFIGURED — a DIRECT request from a LOOPBACK peer (no proxy headers) still
    # skips it (the local UI "just works"); ANY other origin — proxied, OR a non-loopback
    # peer connecting DIRECTLY to a non-loopback bind — MUST present it. (The old check
    # trusted any non-proxied request, so a direct REMOTE connection bypassed auth.)
    if _peer_is_loopback(request) and not proxied:
        return
    auth = (request.headers.get("authorization") or "").strip()
    # Constant-time comparison (against a timing oracle) — same discipline as
    # ``hmac.compare_digest`` on the webhook paths. A plain ``!=`` short-circuits
    # at the first differing byte → the duration leaks the common-prefix length.
    if not hmac.compare_digest(auth, f"Bearer {settings.api_token}"):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "AUTH_INVALID",
                    "message": "Access key invalid or missing (Bearer token).",
                }
            },
        )


def require_akana_bearer_strict(request: Request) -> None:
    """Bearer gate for RAW-SECRET reveal routes — no loopback skip WHEN a token is set.

    ``require_akana_bearer`` trusts any DIRECT loopback peer so the local UI "just works".
    But "loopback peer" means same MACHINE, not same USER: on a multi-user host any other
    local OS account can ``curl http://127.0.0.1:.../reveal`` and read every stored secret
    in plaintext, defeating the at-rest crypto (0600 keyfile, icacls, Fernet) whose threat
    model is exactly other local users. So the RAW-value reveal endpoints require the
    configured token even on loopback.

    Only tightened when ``AKANA_TOKEN`` is set — with NO token the local-UI-just-works /
    open-mode behaviour is preserved unchanged (the owner opted out of a token, and the
    startup guard already keeps open mode effectively loopback-only). This is the ONE
    place the "loopback == owner" assumption is dropped, and only for raw-secret reads.
    """
    settings: Settings = request.app.state.settings
    if not settings.api_token:
        # No token configured → identical to require_akana_bearer's open mode (loopback-only
        # in practice; a proxied request still needs the explicit opt-in).
        require_akana_bearer(request)
        return
    # Token configured: require it on EVERY origin, including a direct loopback peer.
    auth = (request.headers.get("authorization") or "").strip()
    if not hmac.compare_digest(auth, f"Bearer {settings.api_token}"):
        # Dedicated code/message (distinct from the generic AUTH_INVALID): a loopback owner
        # whose browser has no token gets EVERY other route via the loopback skip, so a bare
        # "invalid/missing" here is baffling ("the rest of the app works"). Tell them WHERE to
        # put the token so the reveal handlers can surface an actionable hint verbatim.
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "AUTH_STRICT_REVEAL",
                    "message": (
                        "Revealing raw secrets requires your AKANA_TOKEN even from localhost "
                        "— paste it into the access-key field in Settings → Connection."
                    ),
                }
            },
        )


def authorize_websocket(websocket: WebSocket, token: str | None) -> bool:
    """Decide whether a WebSocket connection is authorized. The ONE shared gate.

    This is the socket-side mirror of :func:`require_akana_bearer` — the exact same
    token/proxy/loopback discipline, factored out so the security-critical logic lives
    in a single auditable place instead of three hand-maintained copies (``ws.py``,
    ``voice_live.py``, ``voice_realtime.py``).

    OPEN MODE (no token configured): a DIRECT (non-proxied) request is allowed — the
    startup guard refuses a non-loopback bind without a token, so open mode is
    effectively loopback-only. A PROXIED request still needs the explicit opt-in
    (request-layer backstop that closes the Tailscale Serve / reverse-proxy hole).

    TOKEN CONFIGURED: a DIRECT request from a LOOPBACK peer (no proxy headers) is
    trusted (the local UI "just works"); ANY other origin — proxied, OR a non-loopback
    peer connecting DIRECTLY to a non-loopback bind — MUST present the token via the
    ``token`` query parameter. Constant-time comparison against a timing oracle;
    ``token`` may be ``None`` → ``compare_digest`` needs the same type, so default to "".

    Returns ``True`` when the connection is authorized; ``False`` when the caller
    should ``close(1008)``. The caller is responsible for ``accept()``/``close()``.
    """
    settings: Settings = websocket.app.state.settings
    proxied = request_is_proxied(websocket.headers)
    if not settings.api_token:
        if proxied and not allow_unauthenticated():
            return False
        return True
    if _peer_is_loopback(websocket) and not proxied:
        return True
    return hmac.compare_digest(token or "", settings.api_token)


# -- lazy services (build-once, cache on app.state) -------------------------------


def get_file_service(request: Request) -> FileService:
    """FileEngine service — built from settings on first access and cached on ``app.state``."""
    svc = getattr(request.app.state, "file_service", None)
    if svc is None:
        svc = FileService.from_settings(request.app.state.settings)
        request.app.state.file_service = svc
    return svc


#: Guards the lazy ``app.state.image_store`` build so it is SINGLE-INSTANCE per
#: process. The bare check-then-set used to let two concurrent requests (this
#: dep + the chat ``gates._image_store`` seam) each construct a DISTINCT
#: UploadStore with its OWN ``threading.Lock``; the two ``save`` critical
#: sections then ran in parallel and could both INSERT the same UNIQUE(sha256)
#: → uncaught IntegrityError (HTTP 500) + orphan file. ``gates._image_store``
#: shares THIS lock so only one store is ever built.
_IMAGE_STORE_LOCK = threading.Lock()


def get_image_store(request: Request) -> UploadStore:
    """Upload store — sets up the sqlite schema on first access and caches it on ``app.state``."""
    store = getattr(request.app.state, "image_store", None)
    if store is None:
        # Double-checked lock: single-instance the build so concurrent requests
        # can't create two stores with independent locks (dedup-race root cause).
        with _IMAGE_STORE_LOCK:
            store = getattr(request.app.state, "image_store", None)
            if store is None:
                settings: Settings = request.app.state.settings
                store = UploadStore.for_settings(settings)
                request.app.state.image_store = store
    return store
