"""Vault encryption primitives — master-key provider + authenticated cipher.

The master key lives OUTSIDE the data dir by default
(``$XDG_CONFIG_HOME/akana/vault.key``, 0600) so a backup or accidental
sync of ``~/.akana`` never carries the key. Resolution order:

    1. ``AKANA_VAULT_KEY``      — raw urlsafe-base64 Fernet key (CI/advanced)
    2. ``AKANA_VAULT_KEYFILE``  — explicit keyfile path
    3. keyring                    — when ``AKANA_VAULT_KEYRING=1`` and available
    4. default keyfile            — generated on first use

Encryption is Fernet (AES-128-CBC + HMAC-SHA256). Ciphertext is tagged with a
``vault1:`` prefix so storage layers can tell our blobs from legacy plaintext
(and migrate the latter transparently). If ``cryptography`` is unavailable the
layer degrades to passthrough so the app still boots; in a normal install it is
a hard dependency and this path is never taken.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: Windows ``msvcrt.locking`` blocks ~10s per attempt then raises; retry up to this long so
#: brief cross-process contention does not fail a vault write.
_WIN_LOCK_DEADLINE_S = 30.0

#: Windows ``os.replace`` (MoveFileEx) raises PermissionError (WinError 5) if any other
#: process holds a handle open on the destination — e.g. a lock-free reader mid-``read_bytes``.
#: Retry the rename for up to this long so a brief concurrent read does not fail the write.
_WIN_REPLACE_DEADLINE_S = 1.5

try:  # fcntl is POSIX-only; cross-process locking degrades to a no-op without it.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

try:  # msvcrt is Windows-only — provides the cross-process lock where fcntl is absent.
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None

try:  # pragma: no cover - import guard
    from cryptography.fernet import Fernet, InvalidToken

    _HAVE_CRYPTO = True
# A BROKEN native binding (e.g. system-python cryptography whose _cffi_backend
# was removed by a package manager) raises pyo3_runtime.PanicException, which
# subclasses BaseException — NOT Exception — and would slip past a narrow guard,
# tracebacking any caller that imports this module. Absent, present-but-broken,
# or wedged native crypto must all degrade the same way: the layer becomes
# passthrough so the app still boots and the writer fails closed elsewhere.
except BaseException:  # pragma: no cover - degraded mode
    Fernet = None  # type: ignore[assignment,misc]

    class InvalidToken(Exception):  # type: ignore[no-redef]
        ...

    _HAVE_CRYPTO = False

ENC_PREFIX = b"vault1:"

_ENV_KEY = "AKANA_VAULT_KEY"
_ENV_KEYFILE = "AKANA_VAULT_KEYFILE"
_ENV_KEYRING = "AKANA_VAULT_KEYRING"
_KEYRING_SERVICE = "akana-vault"
_KEYRING_USER = "master"

_lock = threading.Lock()
_cache: dict[str, bytes] = {}
_decrypt_failures = 0


def note_decrypt_failure(source: str = "") -> None:
    """Record that a tagged ciphertext could not be decrypted.

    A wrong/missing master key otherwise looks identical to "no data" — this
    makes the failure observable (loud log + :func:`health`) so the UI can warn
    instead of silently showing an empty vault.
    """
    global _decrypt_failures
    _decrypt_failures += 1
    log.error(
        "vault: encrypted data present but undecryptable (source=%r) — wrong or missing master key?",
        source,
    )


def _key_source() -> str:
    if os.environ.get(_ENV_KEY, "").strip():
        return "env"
    if os.environ.get(_ENV_KEYRING, "").strip().lower() in ("1", "true", "yes"):
        return "keyring"
    return "keyfile"


def health() -> dict:
    """Encryption health for status surfaces — carries no secrets or key material."""
    return {
        "available": _HAVE_CRYPTO,
        "key_source": _key_source(),
        "decrypt_failures": _decrypt_failures,
        "healthy": _HAVE_CRYPTO and _decrypt_failures == 0,
    }


@contextlib.contextmanager
def file_lock(lock_path: Path | str):
    """Best-effort cross-process exclusive lock. POSIX ``flock`` / Windows ``msvcrt``.

    Serialises read-modify-write of the secret stores across processes (e.g. the
    server UI and a pack script refreshing a token at the same time) so updates
    aren't lost. NOT reentrant — never nest two locks on the same path in one process.
    Degrades to a no-op only if NEITHER backend is available.
    """
    path = Path(lock_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if fcntl is None and msvcrt is None:
        yield
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)  # blocks until acquired
            acquired = True
        else:
            # Windows: lock 1 byte (range may extend past EOF — allowed). Each call blocks
            # ~10s then RAISES if still locked; retry to a deadline so brief contention does
            # not fail the write outright.
            deadline = time.monotonic() + _WIN_LOCK_DEADLINE_S
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
        yield
    finally:
        try:
            # Unlock ONLY if we actually acquired it — otherwise the unlock itself raises and
            # MASKS the original lock-acquisition error (the real reason the write failed).
            if acquired:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


def _config_home() -> Path:
    # An explicit XDG_CONFIG_HOME wins on every platform (deliberate override).
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "akana"
    if os.name == "nt":
        # Idiomatic Windows location is %APPDATA%, but keep using an already-present
        # legacy ~/.config\akana key so upgraders don't lose their master key
        # (changing the keyfile path = a previously-encrypted vault can't be decrypted).
        legacy = Path.home() / ".config" / "akana"
        if (legacy / "vault.key").is_file():
            return legacy
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            return Path(appdata) / "akana"
        return legacy  # APPDATA unset (rare) → fall back to the dot-dir
    return Path.home() / ".config" / "akana"


def default_keyfile() -> Path:
    return _config_home() / "vault.key"


def _read_keyfile(path: Path) -> bytes | None:
    try:
        data = path.read_bytes().strip()
    except OSError:
        return None
    return data or None


def _restrict_to_owner(path: Path) -> None:
    """Restrict a path to the current user only.

    On POSIX the caller already enforced this with ``chmod(0o600)``. On Windows
    ``chmod`` only toggles the read-only bit — it does NOT map to an NTFS ACL, so
    the master key would inherit the profile's ACEs instead of being owner-only.
    ``icacls`` drops inheritance and grants just the current user. Best-effort:
    a failure here must never break key generation.
    """
    if os.name != "nt":
        return
    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - non-Windows / icacls absent
        log.debug("vault: icacls hardening skipped for %s", path)


def write_private_bytes_atomic(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` as an owner-only (0600) file.

    tmp-in-the-same-dir + ``os.replace``: the reader never sees a half-written file
    under ``path`` (all-or-nothing). The temp is created 0600 via ``os.open``; the
    explicit ``chmod`` re-asserts 0600 (``O_CREAT`` mode is masked by umask) and is
    best-effort (a file opened 0600 is never MORE permissive, so a chmod failure is
    harmless). On Windows ``chmod`` does not map to an NTFS ACL, so
    :func:`_restrict_to_owner` re-asserts owner-only via ``icacls`` (no-op on POSIX).
    The single owner-only write path for every secret/key blob
    (``secret_store``, ``secure_vault``, the master keyfile)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    # BUG (Windows race): os.replace/MoveFileEx raises PermissionError if a concurrent
    # lock-free reader (e.g. the akana_vault MCP child mid-read_bytes) holds a handle on
    # the destination open. That read window is brief, so retry the rename to a short
    # deadline before giving up; clean up the tmp file if the replace never succeeds so we
    # don't leak a *.tmp with secret material next to the target. (No-op on POSIX, where
    # replacing over an open file is always allowed and PermissionError does not arise.)
    deadline = time.monotonic() + _WIN_REPLACE_DEADLINE_S
    while True:
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if time.monotonic() >= deadline:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
            time.sleep(0.05)
    _restrict_to_owner(path)


def _write_keyfile(path: Path, key: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    _restrict_to_owner(path.parent)  # the dir; the keyfile itself is hardened in the writer
    write_private_bytes_atomic(path, key)


def _keyfile_lock_path(keyfile: Path) -> Path:
    """Cross-process lock path for minting ``keyfile`` — kept OUT of the key's own dir.

    It must NOT be a sibling of the keyfile: ``_write_keyfile`` hardens the keyfile's
    parent with ``icacls /inheritance:r`` (Windows), which strips inherited ACEs from
    everything in that dir — including a sibling lock file, leaving it with an EMPTY
    DACL so the very next ``os.open`` of the lock fails with PermissionError (WinError 5)
    and every subsequent mint/rotation is wedged. Placing the lock in the system temp
    dir, keyed by a hash of the keyfile path (stable per keyfile, distinct across
    tests/env overrides), sidesteps that entirely. The lock file holds no secret
    material — only the keyfile (in its hardened dir) does.
    """
    digest = hashlib.sha256(str(keyfile).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"akana-vault-key-{digest}.lock"


def _keyring_lock_path() -> Path:
    """Cross-process lock path for minting the keyring master key (system temp dir).

    Same rationale as :func:`_keyfile_lock_path`: serialise first-time minting across
    the server and the akana_vault MCP child, which share one keyring entry. Keyed by
    the keyring service/user so it is stable per entry and distinct across env overrides.
    """
    digest = hashlib.sha256(
        f"{_KEYRING_SERVICE}:{_KEYRING_USER}".encode("utf-8")
    ).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"akana-vault-keyring-{digest}.lock"


def _get_or_create_keyfile(keyfile: Path) -> bytes:
    """Return the keyfile's master key, minting + persisting one on first use.

    BUG (cross-process key race): mint-and-write was previously guarded only by the
    per-process ``_lock``, so two separate PROCESSES (the server and the akana_vault
    MCP child share the default keyfile) that both find the keyfile absent each
    generated a DIFFERENT key and both wrote ``vault.key`` (last-writer-wins). The
    loser then encrypted secrets with a key no longer on disk → undecryptable blobs
    after restart (silent secret loss). Serialise the read-generate-write across
    processes with the cross-process ``file_lock`` and DOUBLE-CHECK the keyfile inside
    the lock: a peer that won the race has now written it, so we load its key instead
    of minting a second one.

    ``file_lock`` is not reentrant; the lock path (a temp-dir sidecar, see
    :func:`_keyfile_lock_path`) is distinct from any caller's DATA-DIR ``.vault.lock``,
    so there is no self-deadlock even when called from inside a ``secure_vault`` write.
    """
    with file_lock(_keyfile_lock_path(keyfile)):
        existing = _read_keyfile(keyfile)  # double-check: a peer may have just written it
        if existing:
            return existing
        key = Fernet.generate_key()
        _write_keyfile(keyfile, key)
        return key


def _keyring_get() -> bytes | None:
    try:
        import keyring

        val = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
        return val.encode("utf-8") if val else None
    except Exception:
        return None


def _keyring_set(key: bytes) -> bool:
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, key.decode("utf-8"))
        return True
    except Exception:
        return False


def get_master_key() -> bytes:
    """Resolve (or generate) the Fernet master key. Cached per source."""
    if not _HAVE_CRYPTO:
        return b""

    env_key = os.environ.get(_ENV_KEY, "").strip()
    if env_key:
        return env_key.encode("utf-8")

    use_keyring = os.environ.get(_ENV_KEYRING, "").strip().lower() in ("1", "true", "yes")
    keyfile = Path(
        os.environ.get(_ENV_KEYFILE, "").strip() or default_keyfile()
    ).expanduser()
    cache_id = "keyring" if use_keyring else f"file:{keyfile}"

    with _lock:
        cached = _cache.get(cache_id)
        if cached:
            return cached

        key: bytes | None = None
        if use_keyring:
            key = _keyring_get()
            if not key:
                # keyring empty/unavailable — honor a keyfile written by a prior
                # keyring-down run BEFORE minting a fresh key. Without this, a box
                # where keyring is enabled but its backend is broken (no D-Bus /
                # SecretService) mints a NEW master key on every restart and
                # orphans every previously-encrypted secret (silent data loss).
                key = _read_keyfile(keyfile)
            if not key:
                # First-time mint. Serialise across processes (the server and the
                # akana_vault MCP child share one keyring entry) with the cross-process
                # file_lock and DOUBLE-CHECK the keyring inside it: a peer that won the
                # race has already written it, so adopt its key instead of minting a
                # divergent second one and clobbering the winner (last-writer-wins →
                # permanent secret loss after restart).
                with file_lock(_keyring_lock_path()):
                    key = _keyring_get() or _read_keyfile(keyfile)
                    if not key:
                        key = Fernet.generate_key()
                        if _keyring_set(key):
                            # Re-read to adopt whatever actually landed in the keyring —
                            # if a peer's set interleaved, its value is authoritative.
                            key = _keyring_get() or key
                        else:
                            # keyring unavailable at runtime → fall back to keyfile via
                            # the lock-guarded double-checked helper (same race as the
                            # plain-keyfile branch); adopt a peer's keyfile if present.
                            key = _get_or_create_keyfile(keyfile)
        else:
            key = _read_keyfile(keyfile)
            if not key:
                # Lock-guarded, double-checked mint-or-load — see _get_or_create_keyfile.
                key = _get_or_create_keyfile(keyfile)

        _cache[cache_id] = key
        return key


def reset_cache() -> None:
    """Drop the cached master key + decrypt-failure counter (tests / key rotation)."""
    global _decrypt_failures
    with _lock:
        _cache.clear()
        _decrypt_failures = 0


def _fernet():
    if not _HAVE_CRYPTO:
        return None
    try:
        return Fernet(get_master_key())
    except Exception as exc:  # malformed key, etc.
        log.error("vault cipher init failed: %s", exc)
        return None


def is_encrypted(raw: bytes) -> bool:
    return bool(raw) and raw.startswith(ENC_PREFIX)


def encrypt_str(plaintext: str) -> bytes:
    """Encrypt ``plaintext`` into a tagged blob.

    FAIL-CLOSED: if cryptography is missing or the master key is corrupt, it
    RAISES — it NEVER falls back to plaintext. (The old behavior wrote secrets
    unencrypted to secrets.json/vault; silent degradation was a security hole.)
    """
    f = _fernet()
    if f is None:
        raise RuntimeError(
            "cannot encrypt secret (cryptography missing or master key corrupt) — "
            "refusing to write plaintext. Run `pip install cryptography`."
        )
    return ENC_PREFIX + f.encrypt(plaintext.encode("utf-8"))


def decrypt_to_str(raw: bytes) -> str | None:
    """Decrypt a tagged blob. Returns ``None`` if not ours or undecryptable."""
    if not is_encrypted(raw):
        return None
    f = _fernet()
    if f is None:
        return None
    try:
        return f.decrypt(raw[len(ENC_PREFIX):]).decode("utf-8")
    except (InvalidToken, Exception):
        return None


def load_text(raw: bytes) -> str | None:
    """Decode stored bytes to text, transparently handling encrypted vs legacy plaintext.

    - tagged ciphertext → decrypted text (``None`` if corrupt)
    - anything else      → UTF-8 decoded as legacy plaintext (``None`` if not UTF-8)
    """
    if not raw:
        return None
    if is_encrypted(raw):
        text = decrypt_to_str(raw)
        if text is None:
            note_decrypt_failure()
        return text
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


class VaultUndecryptableError(RuntimeError):
    """Raised by writers when the on-disk blob is present but won't decrypt."""


def assert_writable(path: Path | str) -> None:
    """WRITE-path guard: refuse to overwrite when the master key is wrong/corrupt.

    READ paths (degrading to {}) cannot distinguish "no file" from "file exists
    but cannot be decrypted"; a read-modify-write would start from this empty
    base and PERMANENTLY delete all secrets by re-encrypting them with the new
    key. The writer calls this inside its lock (BEFORE computing the merge
    state): if the blob is populated, in our encrypted format, and cannot be
    decrypted, we stop the write (to prevent secret loss). If the file is
    missing / empty / legacy plaintext there is no problem — the write is
    allowed.
    """
    try:
        blob = Path(path).read_bytes()
    except OSError:
        return
    if blob and is_encrypted(blob) and decrypt_to_str(blob) is None:
        raise VaultUndecryptableError(
            "vault undecryptable — wrong/corrupt master key; refusing to "
            "overwrite (to prevent secret loss)."
        )
