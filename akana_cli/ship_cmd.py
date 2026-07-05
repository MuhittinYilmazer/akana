"""Pack a portable tarball — venv, node_modules, secrets excluded."""

from __future__ import annotations

import tarfile
import time
from pathlib import Path

from akana_cli import io
from akana_cli.paths import REPO_ROOT

_EXCLUDED_NAMES = {".env", ".env.local"}
_EXCLUDED_SUFFIXES = (".pyc",)
_EXCLUDED_DIR_PARTS = {
    "venv",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".git",
}


def _excluded(rel: Path) -> bool:
    parts = rel.parts
    if any(p in _EXCLUDED_DIR_PARTS for p in parts):
        return True
    name = rel.name
    if name in _EXCLUDED_NAMES:
        return True
    # EVERY .env* may contain real secrets (.env.test/.production/.dev/.envrc…) →
    # exclude them. The only exception is .env.example (a secret-free template that
    # should ship with the OSS package).
    if name.startswith(".env") and name != ".env.example":
        return True
    if name.startswith("akana-") and name.endswith(".tar.gz"):
        return True
    return any(name.endswith(s) for s in _EXCLUDED_SUFFIXES)


def run_ship(out_dir: Path | None = None) -> int:
    target_dir = (out_dir or REPO_ROOT).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    out_path = target_dir / f"akana-{stamp}.tar.gz"

    io.step(f"Packaging → {out_path}")

    # Pack under a top-level `akana-<stamp>/` dir so the recipient's `cd akana-*`
    # (printed below) actually has a directory to enter.
    top = f"akana-{stamp}"

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        rel = Path(info.name)
        # tar.add(arcname=top) prefixes every entry with `top`; strip it to recover the
        # repo-relative path the exclusion rules expect.
        if rel.parts and rel.parts[0] == top:
            rel = Path(*rel.parts[1:]) if len(rel.parts) > 1 else Path()
        if _excluded(rel):
            return None
        return info

    with tarfile.open(out_path, "w:gz") as tar:
        tar.add(str(REPO_ROOT), arcname=top, filter=_filter)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    io.ok(f"{out_path.name} ({size_mb:.1f} MB)")
    print(
        f"  Recipient: tar xzf {out_path.name} && cd akana-* && "
        "python akana.py setup && python akana.py start"
    )
    return 0
