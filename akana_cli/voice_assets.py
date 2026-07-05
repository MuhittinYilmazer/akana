"""Download Piper voice models (Windows + Linux, no bash/curl)."""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from akana_cli.env_util import load_repo_dotenv
from akana_cli.paths import default_data_dir, expand_user_path

HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


@dataclass(frozen=True)
class PiperVoice:
    """One selectable Piper voice.

    ``name`` is the model file stem (``en_US-amy-medium``) and ``subpath`` is its
    directory under ``rhasspy/piper-voices`` on HuggingFace. ``lang``/``desc`` drive
    the interactive picker; ``default`` marks the voices installed when the user makes
    no explicit choice (and for the non-interactive ``--voice`` path)."""

    name: str
    subpath: str
    lang: str
    desc: str
    default: bool = False


#: The selectable catalog shown in the interactive picker. Order = menu order.
#: Includes the two SHIPPED defaults (TR dfki + EN amy) plus a few more EN/TR
#: options so users can pick a voice that fits — install only the chosen ones.
PIPER_CATALOG: tuple[PiperVoice, ...] = (
    PiperVoice("tr_TR-dfki-medium", "tr/tr_TR/dfki/medium", "tr", "Turkish — dfki (medium)", default=True),
    PiperVoice("tr_TR-fahrettin-medium", "tr/tr_TR/fahrettin/medium", "tr", "Turkish — fahrettin (medium)"),
    PiperVoice("en_US-amy-medium", "en/en_US/amy/medium", "en", "English (US) — amy (medium)", default=True),
    PiperVoice("en_US-lessac-medium", "en/en_US/lessac/medium", "en", "English (US) — lessac (medium)"),
    PiperVoice("en_US-ryan-high", "en/en_US/ryan/high", "en", "English (US) — ryan (high)"),
    PiperVoice("en_GB-alba-medium", "en/en_GB/alba/medium", "en", "English (GB) — alba (medium)"),
)

#: name -> PiperVoice for quick lookup.
PIPER_BY_NAME: dict[str, PiperVoice] = {v.name: v for v in PIPER_CATALOG}

#: The default selection (voice_name, subpath) installed when no explicit choice is
#: made — the two SHIPPED voices (TR dfki + EN amy). Kept as a plain tuple of pairs so
#: the non-interactive ``--voice`` path and existing callers/tests stay stable.
PIPER_VOICES: tuple[tuple[str, str], ...] = tuple(
    (v.name, v.subpath) for v in PIPER_CATALOG if v.default
)


def default_voice_names() -> list[str]:
    """The catalog voices marked as default (shipped TR + EN)."""
    return [v.name for v in PIPER_CATALOG if v.default]


def resolve_voices_dir() -> Path:
    load_repo_dotenv()
    data_dir = default_data_dir()
    raw = os.environ.get("AKANA_VOICES_DIR", "").strip()
    if raw:
        return expand_user_path(raw)
    return data_dir / "voices"


def _voice_urls(name: str, subpath: str) -> tuple[str, str]:
    base = f"{HF_BASE}/{subpath}/{name}"
    return f"{base}.onnx", f"{base}.onnx.json"


def _remove_broken_symlink(path: Path) -> None:
    if path.is_symlink() and not path.exists():
        path.unlink(missing_ok=True)


def _nonempty(path: Path) -> bool:
    """True if `path` exists with a positive size (a truncated/0-byte file is 'missing')."""
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _expected_length(raw: str | None) -> int | None:
    """Parse a Content-Length header into an int, or None if it isn't usable.

    A malformed/absent header must SKIP the short-read check, never abort a
    download whose body actually arrived in full — a CDN/proxy can (RFC-legally)
    duplicate the header into a comma-joined value like ``1234, 1234`` that
    ``int()`` rejects. We take the first comma-separated part when the values
    agree; anything still unparseable yields None (validation skipped)."""
    if raw is None:
        return None
    first = raw.split(",", 1)[0].strip()
    try:
        return int(first)
    except ValueError:
        return None


def _download(url: str, dest: Path, *, retries: int = 3) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Write to a .part sidecar and os.replace() into place only after the full body is on
    # disk: a Ctrl+C/kill mid-write (setup's voice step is exactly where users abort a big
    # download) then leaves NO truncated final file to masquerade as "already present" — the
    # next run re-downloads. os.replace is atomic on both Windows and POSIX. Content-Length,
    # when the server sends it, catches a short read before we commit.
    part = dest.with_name(dest.name + ".part")
    last_err: Exception | None = None
    for _attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "akana-setup/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
                data = resp.read()
                expected = _expected_length(resp.headers.get("Content-Length"))
            # Only validate when the server sent a PARSEABLE length. Check on empty
            # bodies too: a Content-Length>0 whose body reads as b'' (reset/truncated
            # connection that doesn't raise) would otherwise commit a 0-byte file as a
            # "successful" download and print ✓ this same run. A malformed header
            # (unparseable → expected None) skips the check rather than killing a body
            # that fully arrived.
            if expected is not None and len(data) != expected:
                raise OSError(f"short read: got {len(data)} of {expected} bytes")
            part.write_bytes(data)
            os.replace(part, dest)
            return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_err = exc
            part.unlink(missing_ok=True)
    assert last_err is not None
    raise RuntimeError(f"download failed ({url}): {last_err}") from last_err


def ensure_voice(name: str, subpath: str, voices_dir: Path, *, verbose: bool = True) -> bool:
    """Download one voice if missing. Returns True if already present."""
    onnx = voices_dir / f"{name}.onnx"
    cfg = voices_dir / f"{name}.onnx.json"
    _remove_broken_symlink(onnx)
    _remove_broken_symlink(cfg)

    # A pre-existing pair only counts as present if BOTH files carry real bytes. A truncated
    # .onnx from an interrupted pre-atomic-download install (0-byte, or a stray .onnx.part)
    # otherwise passes is_file() forever and Piper fails at runtime with an opaque ONNX load
    # error that the installer can never heal ("already present"). A non-positive size → treat
    # as missing and re-download.
    if onnx.is_file() and cfg.is_file() and _nonempty(onnx) and _nonempty(cfg):
        if verbose:
            print(f"  ✓ {name} (already present)")
        return True

    onnx_url, cfg_url = _voice_urls(name, subpath)
    if verbose:
        print(f"  ↓ {name} downloading…")
    _download(onnx_url, onnx)
    _download(cfg_url, cfg)
    if verbose:
        print(f"  ✓ {name}")
    return False


def _resolve_selection(
    selection: Iterable[str] | None,
) -> tuple[tuple[str, str], ...]:
    """Turn a caller's voice choice into concrete (name, subpath) pairs.

    ``selection`` is an iterable of catalog voice NAMES (e.g. ``en_US-amy-medium``).
    ``None`` → the shipped defaults (TR dfki + EN amy). Unknown names are silently
    ignored (a stale name shouldn't abort the whole voice install); if the result is
    empty (all unknown, or an explicit empty selection) we fall back to the defaults so
    voice output is never left with zero voices."""
    if selection is None:
        return PIPER_VOICES
    resolved: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in selection:
        voice = PIPER_BY_NAME.get(name)
        if voice is not None and voice.name not in seen:
            resolved.append((voice.name, voice.subpath))
            seen.add(voice.name)
    return tuple(resolved) or PIPER_VOICES


def install_piper_voices(
    *,
    selection: Sequence[str] | None = None,
    voices_dir: Path | None = None,
    verbose: bool = True,
) -> Path:
    """Download the SELECTED Piper voices into the data voices directory.

    ``selection`` is a list of catalog voice names (see ``PIPER_CATALOG``); ``None``
    installs the shipped defaults (TR dfki + EN amy), which keeps the non-interactive
    ``--voice`` / CI path prompt-free."""
    target = voices_dir or resolve_voices_dir()
    target.mkdir(parents=True, exist_ok=True)
    if verbose:
        print(f"Voices dir: {target}")
        print("Piper voices:")
    for name, subpath in _resolve_selection(selection):
        ensure_voice(name, subpath, target, verbose=verbose)
    return target
