"""Static assets with long-lived cache headers (cache-busted via ?v= query)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class CachedStaticFiles(StaticFiles):
    """Serve /static with immutable caching when the URL includes a version query."""

    _LONG_CACHE = "public, max-age=31536000, immutable"
    _SHORT_CACHE = "public, max-age=3600"

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code != 200:
            return response
        qs = scope.get("query_string", b"").decode("latin-1", errors="ignore")
        if "v=" in qs:
            response.headers["Cache-Control"] = self._LONG_CACHE
        elif path.endswith((".js", ".css", ".woff2", ".woff")):
            response.headers["Cache-Control"] = self._SHORT_CACHE
        return response


# -- automatic cache-bust: content-hash ?v= injection into host HTML ------------
#
# index.html / memory.html are served no-store (always fresh), so we can rewrite
# every /static reference's ?v= at request time to a hash of the file's bytes.
# Version changes iff the asset changes → browser re-fetches exactly when needed
# and caches forever otherwise. No more manual `?v=YYYYMMDD-uiNN` bumps.

# Captures the /static path (group 1); swallows any existing ?v=… so we replace it.
_STATIC_REF_RE = re.compile(r'''(/static/[^"'?\s]+)(?:\?v=[^"'\s]*)?''')

# Keyed by (path, mtime_ns, size) so an edited file re-hashes but unchanged files
# are served from cache — no md5 on every request.
_version_cache: dict[tuple[str, int, int], str] = {}


def asset_version(static_dir: Path, rel_path: str) -> str | None:
    """First 12 hex of the file's md5, cached by mtime+size; None if missing."""
    f = static_dir / rel_path
    try:
        st = f.stat()
    except OSError:
        return None
    key = (str(f), st.st_mtime_ns, st.st_size)
    cached = _version_cache.get(key)
    if cached is not None:
        return cached
    try:
        digest = hashlib.md5(f.read_bytes()).hexdigest()[:12]  # noqa: S324 - cache key, not security
    except OSError:
        return None
    _version_cache[key] = digest
    return digest


def render_versioned_html(html_path: Path, static_dir: Path) -> str:
    """Read a host HTML doc and rewrite every /static ref's ?v= to a content hash.

    Unknown/missing files keep their original reference (their manual ?v= acts as
    a fallback — e.g. when the doc is served by a plain static server in dev).
    """
    text = html_path.read_text(encoding="utf-8")

    def _sub(m: re.Match[str]) -> str:
        ref = m.group(1)  # e.g. /static/akana-shell.js
        ver = asset_version(static_dir, ref[len("/static/"):])
        return f"{ref}?v={ver}" if ver else m.group(0)

    return _STATIC_REF_RE.sub(_sub, text)
