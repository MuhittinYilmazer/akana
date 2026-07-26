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
import logging
import threading
from urllib.parse import parse_qs

from fastapi import HTTPException, Request, WebSocket
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from akana_server.config import Settings, allow_unauthenticated
from akana_server.files.service import FileService
from akana_server.multimodal.store import UploadStore

log = logging.getLogger(__name__)

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


def _token_matches(candidate: str, expected: str) -> bool:
    """Constant-time token comparison the client cannot crash.

    ``hmac.compare_digest`` on *str* RAISES TypeError the moment either side holds a
    non-ASCII character — and headers/query strings reach us latin-1-decoded, so a
    remote client putting a single byte ≥ 0x80 in ``Authorization`` (or ``?token=``)
    turned the gate into an unhandled 500 with a traceback instead of a clean 401.
    Comparing UTF-8 bytes has no such restriction and keeps the timing property that
    made ``compare_digest`` the choice here (a plain ``!=`` leaks the common prefix
    length through its duration).
    """
    return hmac.compare_digest(
        candidate.encode("utf-8", "replace"), expected.encode("utf-8", "replace")
    )


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


#: Suffixes no third party can point at this machine through public DNS: the reserved
#: / non-registrable namespaces (RFC 6762 ``.local``, RFC 8375 ``.home.arpa``, RFC 6761
#: ``.localhost``, the de-facto ``.internal``) plus ``.ts.net`` — Tailscale MagicDNS,
#: which resolves only inside the owner's own tailnet and is exactly the name
#: ``tailscale serve``/``funnel`` puts in the Host header.
_RESERVED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".ts.net")


def _host_name(host_header: str) -> str:
    """Hostname from a ``Host`` header — lowercased, port stripped, IPv6-literal aware.

    ``header.split(":")[0]`` (what starlette's TrustedHostMiddleware does) turns
    ``[::1]:8766`` into ``"["``, so a genuine IPv6 loopback browser would be refused.
    """
    h = host_header.strip().lower()
    if h.startswith("["):  # [::1] / [::1]:8766 — the port is outside the brackets
        end = h.find("]")
        return h[: end + 1] if end != -1 else h
    h = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    # Absolute-FQDN form ("localhost.", "box.example.") names the same host.
    return h[:-1] if h.endswith(".") else h


def _is_ip_literal(host: str) -> bool:
    h = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    h = h.split("%", 1)[0]  # link-local zone id (fe80::1%eth0)
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def host_header_allowed(host_header: str, settings: Settings | None) -> bool:
    """Could this ``Host`` be a name an attacker resolved to our address?

    Everything a rebinding page can put here is a name it OWNS in public DNS, so the
    allowlist is "names that cannot be obtained that way", derived from the running
    configuration rather than a fixed list:

    * no Host at all — an HTTP/1.0 client, never a browser, so never a rebind;
    * an IP literal — reaching us by address means no name was resolved to get here
      (and a page whose origin is an IP literal cannot be re-pointed elsewhere);
    * a single-label name (``localhost``, ``akana-box``, ``testserver``) — not
      registrable in public DNS, only reachable via a local/intranet resolver;
    * a reserved suffix (:data:`_RESERVED_HOST_SUFFIXES`) — mDNS + MagicDNS;
    * the host this instance was configured to bind (``AKANA_HOST``), when that is a
      name rather than an address.
    """
    host = _host_name(host_header or "")
    if not host:
        return True
    if _is_ip_literal(host):
        return True
    if "." not in host:
        return True
    if host.endswith(_RESERVED_HOST_SUFFIXES):
        return True
    configured = _host_name((getattr(settings, "server_host", "") or ""))
    return bool(configured) and host == configured


def _carries_valid_token(scope: Scope, headers: Headers, settings: Settings | None) -> bool:
    """True when the request presents the configured ``AKANA_TOKEN``.

    A rebinding page reaches us from its OWN origin, so it cannot read the token out
    of the owner's ``localStorage`` — a request that proves knowledge of it is not the
    attack this guard exists for, and letting it through keeps an authenticated
    reverse-proxy deployment under a custom domain working. Both channels the app
    already uses are accepted: the bearer header (HTTP) and ``?token=`` (WebSocket).
    An UNSET token must never satisfy this (``Bearer `` == ``Bearer `` is True).
    """
    token = (getattr(settings, "api_token", "") or "").strip()
    if not token:
        return False
    auth = (headers.get("authorization") or "").strip()
    if _token_matches(auth, f"Bearer {token}"):
        return True
    query = parse_qs(scope.get("query_string", b"").decode("latin-1", "replace"))
    return any(_token_matches(v, token) for v in query.get("token", []))


class HostHeaderGuard:
    """Reject requests whose ``Host`` names a domain that could have been rebound here.

    DNS REBINDING is the standing hole under "a loopback peer IS the owner"
    (:func:`_peer_is_loopback`, and the loopback skip in :func:`require_akana_bearer`).
    A page served from ``http://evil.example`` re-resolves its own name to 127.0.0.1
    after the first load and then talks to this server from that origin: the browser
    treats it as same-origin, so the SOP/CORS wall that normally stops a web page from
    reading a localhost API is gone, and every route answers as the trusted owner. The
    peer address really is loopback, so no peer-side check can tell them apart — the
    Host header, which still carries the attacker's domain, is the only signal left.

    Outermost middleware (added last) so a rejected Host never reaches a route,
    static file or WebSocket handler. Settings are read per request off ``app.state``
    because the middleware is constructed before the lifespan populates them.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            state = getattr(scope.get("app"), "state", None)
            settings = getattr(state, "settings", None)
            headers = Headers(scope=scope)
            host = headers.get("host", "")
            if not host_header_allowed(host, settings) and not _carries_valid_token(
                scope, headers, settings
            ):
                log.warning(
                    "rejected request with untrusted Host header %r (possible DNS "
                    "rebinding; a reverse proxy under a custom domain must send AKANA_TOKEN)",
                    host[:128],
                )
                await self._reject(scope, receive, send)
                return
        await self.app(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # Closing BEFORE accept is the ASGI way to refuse a handshake (the server
            # turns it into an HTTP rejection); an http.response.start would be invalid here.
            await send({"type": "websocket.close", "code": 1008})
            return
        response = PlainTextResponse(
            "Invalid Host header. Akana only answers on its own address/hostname; "
            "if you reach it through a reverse proxy under a custom domain, set "
            "AKANA_TOKEN and send it with the request.",
            status_code=400,
        )
        await response(scope, receive, send)


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
    # Constant-time comparison (against a timing oracle) — see :func:`_token_matches`.
    if not _token_matches(auth, f"Bearer {settings.api_token}"):
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
    if not _token_matches(auth, f"Bearer {settings.api_token}"):
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
    ``token`` query parameter. Constant-time comparison against a timing oracle
    (:func:`_token_matches`); ``token`` may be ``None`` → default to "".

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
    return _token_matches(token or "", settings.api_token)


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
