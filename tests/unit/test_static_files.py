"""Static file cache headers."""

from __future__ import annotations

from akana_server.api.static_files import CachedStaticFiles


def test_cached_static_files_class_exists() -> None:
    assert issubclass(CachedStaticFiles, object)
    assert CachedStaticFiles._LONG_CACHE.startswith("public")
