"""SecureVault — the single authority for local credential storage.

Provides one unified API over two on-disk partitions:

- **Scalar secrets** — one deterministic store per key, partitioned by name:
  * system provider keys (:data:`secret_store.ALLOWED_KEYS`, e.g. ``openai_api_key``)
    live ONLY in ``secrets.json`` (``secret_store``), because every provider resolves
    its key from there directly; the credentials API owns that store.
  * every OTHER scalar ("extended" keys, e.g. ``github_token``) lives ONLY in the
    encrypted ``vault/keys.json`` keyfile.
  A scalar's owning store is decided solely by ``key in ALLOWED_KEYS`` on EVERY
  read/write/delete — there is a single lookup chain, so ``vault_get`` always returns
  exactly the value a provider (or the credentials API) would use. Legacy installs that
  wrote an ``ALLOWED_KEYS`` name into the keyfile are healed transparently: reads ignore
  the stray keyfile copy and the next scalar write purges it (migrate-on-write).
- **Multi-file credential profiles** under ``credentials/<namespace>/<profile>/``.

Access-gating: vault access is all-or-nothing by explicit owner decision — the
assistant's OWN vault tools are ungated (the model may read OR mutate any secret;
every access is audited). There is no per-pack secret scoping. Future tiers: OS
keychain, Bitwarden, age+SOPS.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from akana_server import vault_crypto
from akana_server.secret_store import ALLOWED_KEYS, get_secret, load_secrets, set_secrets

log = logging.getLogger(__name__)

CREDENTIALS_DIR = "credentials"
VAULT_DIR = "vault"
VAULT_KEYS_FILE = "keys.json"
VAULT_AUDIT_FILE = "vault_access.jsonl"
#: Encrypted ``{key: value}`` bundle inside a profile dir (structured account
#: fields, e.g. ``{"username": ..., "password": ...}``).
FIELDS_FILE = "secrets.enc"

#: Write-boundary guards: secret key-name charset + value/count caps.
_SECRET_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_SECRET_VALUE_LEN = 8192
MAX_KEYS_PER_WRITE = 64
#: Rotate the access log once it exceeds this (keeps at most ~2x on disk).
AUDIT_MAX_BYTES = 5 * 1024 * 1024
#: Retry a profile ``rmtree`` for up to this long — on Windows a concurrent lock-free
#: reader's open handle on ``secrets.enc`` makes the delete fail transiently (same
#: open-handle window ``vault_crypto.write_private_bytes_atomic`` retries around).
_RMTREE_DEADLINE_S = 1.5


def _validate_secret_key(key: str) -> str:
    k = (key or "").strip()
    if not _SECRET_KEY_RE.match(k):
        raise ValueError(f"invalid secret key name: {key!r}")
    return k


def _lock_path(data_dir: Path) -> Path:
    return resolve_data_dir(data_dir) / ".vault.lock"


def _clean_secret_patch(patch: dict) -> dict[str, str]:
    """Validate + normalise a write patch *before* taking any lock.

    Non-empty values: key-name validated, length capped. Empty/None values map
    to ``""`` (a clear marker). Raises ``ValueError`` on bad key or oversize value.
    """
    patch = patch or {}
    if len(patch) > MAX_KEYS_PER_WRITE:
        raise ValueError(f"too many keys in one write (max {MAX_KEYS_PER_WRITE})")
    clean: dict[str, str] = {}
    for key, value in patch.items():
        if not isinstance(key, str) or not key.strip():
            continue
        text = value.strip() if isinstance(value, str) else ""
        if text:
            valid = _validate_secret_key(key)
            if len(text) > MAX_SECRET_VALUE_LEN:
                raise ValueError(f"value too long for {valid!r} (max {MAX_SECRET_VALUE_LEN})")
            clean[valid] = text
        else:
            clean[key.strip()] = ""
    return clean


def _apply_secret_patch(current: dict[str, str], clean: dict[str, str]) -> None:
    for key, text in clean.items():
        if text:
            current[key] = text
        else:
            current.pop(key, None)

_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# Known legacy credential locations, keyed by namespace → [(src_dir, namespace, profile)].
# Empty by default: a fresh open-source install has no prior on-disk layout to migrate
# from. :func:`migrate_legacy` still works — tests and any future in-tree migration add
# entries here — but nothing hardcoded ships (the old ``~/.openclaw`` pre-rename path was
# unreachable on every fresh install and leaked the former product name).
LEGACY_CREDENTIAL_DIRS: dict[str, list[tuple[Path, str, str]]] = {}

_lock = threading.Lock()


def resolve_data_dir(data_dir: Path | str | None = None) -> Path:
    if data_dir is not None:
        return Path(data_dir).expanduser()
    env = os.environ.get("AKANA_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".akana"


def credentials_root(data_dir: Path | str) -> Path:
    return resolve_data_dir(data_dir) / CREDENTIALS_DIR


def _vault_keys_path(data_dir: Path) -> Path:
    return resolve_data_dir(data_dir) / VAULT_DIR / VAULT_KEYS_FILE


def _validate_namespace(namespace: str) -> str:
    ns = (namespace or "").strip()
    if not _NAMESPACE_RE.match(ns):
        raise ValueError(f"invalid credential namespace: {namespace!r}")
    return ns


def _validate_profile(profile: str) -> str:
    pf = (profile or "default").strip() or "default"
    if not _PROFILE_RE.match(pf):
        raise ValueError(f"invalid credential profile: {profile!r}")
    return pf


def _chmod_private(path: Path, mode: int = 0o700) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _ensure_private_dir(path: Path, *, data_dir: Path | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _chmod_private(path, 0o700)
    # Tighten the vault/credentials ancestor dirs to 0700 — but ONLY within the data-dir
    # subtree, and stop AT the data dir. Past releases used a ``parent == path.anchor`` test
    # that never matched (Path.__eq__ with a str returns False), so the loop ran to the fs
    # root and could chmod 0700 an UNRELATED ancestor literally named 'vault'/'credentials'
    # (e.g. a shared /srv/vault), locking out other users/services on POSIX. Bound the walk
    # to the data dir so only OUR ``<data_dir>/vault`` and ``<data_dir>/credentials`` are
    # touched. ``data_dir`` is resolved from the env/default when not passed.
    stop = resolve_data_dir(data_dir)
    for parent in path.parents:
        if parent == stop or not parent.is_relative_to(stop):
            break
        if parent.is_dir() and parent.name in (CREDENTIALS_DIR, VAULT_DIR):
            _chmod_private(parent, 0o700)
    return path


def credential_dir(
    data_dir: Path | str,
    namespace: str,
    profile: str = "default",
    *,
    create: bool = False,
) -> Path:
    """Return ``credentials/<namespace>/<profile>/`` (0700 when ``create=True``)."""
    ns = _validate_namespace(namespace)
    pf = _validate_profile(profile)
    dd = resolve_data_dir(data_dir)
    path = credentials_root(dd) / ns / pf
    if create:
        _ensure_private_dir(path, data_dir=dd)
    return path


def list_namespaces(data_dir: Path | str) -> list[str]:
    root = credentials_root(data_dir)
    if not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and _NAMESPACE_RE.match(p.name)
    )


def list_profiles(data_dir: Path | str, namespace: str) -> list[str]:
    ns_root = credentials_root(data_dir) / _validate_namespace(namespace)
    if not ns_root.is_dir():
        return []
    return sorted(
        p.name
        for p in ns_root.iterdir()
        if p.is_dir() and _PROFILE_RE.match(p.name)
    )


def profile_status(data_dir: Path | str, namespace: str, profile: str = "default") -> dict:
    path = credential_dir(data_dir, namespace, profile)
    files = 0
    if path.is_dir():
        files = sum(1 for p in path.iterdir() if p.is_file())
    return {
        "namespace": _validate_namespace(namespace),
        "profile": _validate_profile(profile),
        "path": str(path),
        "exists": path.is_dir(),
        "file_count": files,
        "populated": path.is_dir() and files > 0,
    }


def _load_vault_keys(data_dir: Path) -> dict[str, str]:
    path = _vault_keys_path(data_dir)
    try:
        blob = path.read_bytes()
    except OSError:
        return {}
    text = vault_crypto.load_text(blob)
    if text is None:
        return {}
    try:
        raw = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: value.strip()
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }


def _write_vault_keys_atomic(data_dir: Path, data: dict[str, str]) -> None:
    dd = resolve_data_dir(data_dir)
    vault_dir = _ensure_private_dir(dd / VAULT_DIR, data_dir=dd)
    path = vault_dir / VAULT_KEYS_FILE
    payload = vault_crypto.encrypt_str(json.dumps(data, indent=2, sort_keys=True) + "\n")
    vault_crypto.write_private_bytes_atomic(path, payload)


def _purge_migrated_allowed_keys(current: dict[str, str]) -> bool:
    """Drop any :data:`ALLOWED_KEYS` names from a keyfile map in place (migrate-on-write).

    Extended scalars own the keyfile; system provider keys own ``secret_store``. A legacy
    install may still hold an ``ALLOWED_KEYS`` name in the keyfile (older ``set_scalars``
    wrote every name there). Any keyfile read-modify-write strips those stray copies so the
    partition self-heals and the keyfile can never shadow ``secret_store``. Returns whether
    anything was removed (so the caller knows the write is worthwhile even with no patch).
    """
    stale = [k for k in current if k in ALLOWED_KEYS]
    for k in stale:
        current.pop(k, None)
    return bool(stale)


def _migrate_allowed_keys_out_of_keyfile(data_dir: Path) -> None:
    """Standalone, lock-guarded purge of stray ``ALLOWED_KEYS`` names from the keyfile.

    Used by the system-key write/delete paths, which touch ``secret_store`` (the authority)
    but not the keyfile — this sweeps any legacy keyfile copy of the same name so it can
    never resurface via a stale read. A no-op (and no write) when the keyfile is absent or
    already clean, so it stays cheap on the common path. Best-effort: never re-raises, so a
    housekeeping failure can't fail the primary secret write.

    ``set_secrets`` has already released its ``.vault.lock`` before this runs, so acquiring
    the same (non-reentrant) lock here does not self-deadlock.
    """
    try:
        with vault_crypto.file_lock(_lock_path(data_dir)), _lock:
            path = _vault_keys_path(data_dir)
            if not path.exists():
                return
            vault_crypto.assert_writable(path)
            current = _load_vault_keys(data_dir)
            if _purge_migrated_allowed_keys(current):
                _write_vault_keys_atomic(data_dir, current)
    except vault_crypto.VaultUndecryptableError:
        # Wrong/corrupt master key: leave the keyfile untouched (assert_writable already
        # protects the real write paths). Housekeeping must not raise here.
        pass


def get_scalar(data_dir: Path | str, key: str, *, consumer: str = "system") -> str | None:
    """Resolve a scalar secret through the SINGLE lookup chain (no dual-store read).

    A scalar's owning store is decided by its name: an :data:`ALLOWED_KEYS` name resolves
    from ``secret_store`` (``secrets.json``) — the same value every provider reads — and any
    other name resolves from the encrypted keyfile (``vault/keys.json``). The two stores
    are partitioned, so there is exactly one place to look and ``vault_get`` can never
    surface a value that differs from what the provider (or credentials API) uses.
    """
    key = (key or "").strip()
    if not key:
        return None
    dd = resolve_data_dir(data_dir)
    value = get_secret(dd, key) if key in ALLOWED_KEYS else _load_vault_keys(dd).get(key)
    if value:
        audit_access(dd, {"action": "get_scalar", "key": key, "consumer": consumer, "hit": True})
    return value or None


def set_scalar(data_dir: Path | str, key: str, value: str | None, *, consumer: str = "system") -> None:
    key = (key or "").strip()
    if not key:
        raise ValueError("key required")
    dd = resolve_data_dir(data_dir)
    text = value.strip() if isinstance(value, str) else ""
    # Reject oversize BEFORE writing — matches _clean_secret_patch (set_scalars/set_fields)
    # so no write path silently truncates a secret and reports success.
    if len(text) > MAX_SECRET_VALUE_LEN:
        raise ValueError(f"value too long for {key!r} (max {MAX_SECRET_VALUE_LEN})")
    if key in ALLOWED_KEYS:
        # System provider key → secret_store is the ONE authority. Also purge a stray
        # legacy copy from the keyfile so it can never shadow this write (migrate-on-write).
        set_secrets(dd, {key: text})
        _migrate_allowed_keys_out_of_keyfile(dd)
    else:
        with vault_crypto.file_lock(_lock_path(dd)), _lock:
            # With a wrong/corrupt master key, refuse to overwrite keys.json from an empty base.
            vault_crypto.assert_writable(_vault_keys_path(dd))
            current = _load_vault_keys(dd)
            _purge_migrated_allowed_keys(current)  # migrate-on-write: heal legacy strays
            if text:
                current[_validate_secret_key(key)] = text
            else:
                current.pop(key, None)
            _write_vault_keys_atomic(dd, current)
    audit_access(
        dd,
        {"action": "set_scalar", "key": key, "consumer": consumer, "cleared": not bool(text)},
    )


def delete_scalar(data_dir: Path | str, key: str, *, consumer: str = "dashboard") -> bool:
    """Remove a scalar secret from its ONE owning store. Returns True iff it existed.

    Single-store delete, matching the single-store read: an :data:`ALLOWED_KEYS` name is
    cleared from ``secret_store`` (its authority); any other name from the encrypted keyfile.
    Mirrors :func:`delete_profile`'s honest signal — deleting an absent key is a no-op that
    returns False, so callers can tell the user/model nothing was there. A legacy keyfile
    copy of an ``ALLOWED_KEYS`` name is swept transparently (migrate-on-write) so it can't
    resurface, but that housekeeping never turns an honest "absent" into a phantom removal.
    """
    key = (key or "").strip()
    if not key:
        raise ValueError("key required")
    dd = resolve_data_dir(data_dir)
    if key in ALLOWED_KEYS:
        existed = bool(get_secret(dd, key))
        if existed:
            set_secrets(dd, {key: ""})
        # Sweep any stray legacy keyfile copy regardless (does not affect `existed`).
        _migrate_allowed_keys_out_of_keyfile(dd)
    else:
        with vault_crypto.file_lock(_lock_path(dd)), _lock:
            # Wrong/corrupt master key must not silently wipe the keyfile.
            vault_crypto.assert_writable(_vault_keys_path(dd))
            current = _load_vault_keys(dd)
            existed = key in current
            if existed:
                current.pop(key, None)
                _write_vault_keys_atomic(dd, current)
    audit_access(
        dd,
        {"action": "delete_scalar", "key": key, "consumer": consumer, "removed": existed},
    )
    return existed


def load_scalars(data_dir: Path | str) -> dict[str, str]:
    """Extended (non-system) keyfile scalars from ``vault/keys.json`` (raw values; callers mask).

    Enforces the partition on READ: any :data:`ALLOWED_KEYS` name is filtered out, because
    those are owned by ``secret_store`` and surfaced from there. A legacy keyfile that still
    holds such a name never leaks it here (it stays invisible until the next write migrates
    it out), so the keyfile view and the ``secret_store`` view never overlap or disagree.
    """
    return {
        key: value
        for key, value in _load_vault_keys(resolve_data_dir(data_dir)).items()
        if key not in ALLOWED_KEYS
    }


def set_scalars(data_dir: Path | str, patch: dict, *, consumer: str = "dashboard") -> dict[str, str]:
    """Merge a scalar patch, routing each key to its owning store (empty/None clears).

    Enforces the partition so a raw multi-key write can never create a second source of
    truth: any :data:`ALLOWED_KEYS` name in ``patch`` is applied to ``secret_store``
    (``secrets.json``) — where providers read it — and every other name to the encrypted
    keyfile. Legacy strays are healed on the way through (migrate-on-write). Returns the new
    KEYFILE state (extended scalars); system credentials remain owned by the credentials API.
    """
    dd = resolve_data_dir(data_dir)
    clean = _clean_secret_patch(patch)
    system_patch = {k: v for k, v in clean.items() if k in ALLOWED_KEYS}
    keyfile_patch = {k: v for k, v in clean.items() if k not in ALLOWED_KEYS}
    if system_patch:
        # Route system provider keys to their authority; set_secrets ignores empty values
        # as a clear, matching _apply_secret_patch semantics for the keyfile below.
        set_secrets(dd, system_patch)
    with vault_crypto.file_lock(_lock_path(dd)), _lock:
        # With a wrong/corrupt master key, refuse to overwrite keys.json from an empty base.
        vault_crypto.assert_writable(_vault_keys_path(dd))
        current = _load_vault_keys(dd)
        _purge_migrated_allowed_keys(current)  # migrate-on-write: drop any legacy ALLOWED strays
        _apply_secret_patch(current, keyfile_patch)
        _write_vault_keys_atomic(dd, current)
    audit_access(
        dd,
        {"action": "set_scalars", "consumer": consumer, "keys": sorted(clean.keys())},
    )
    return current


def _fields_file(data_dir: Path, namespace: str, profile: str) -> Path:
    return credential_dir(data_dir, namespace, profile) / FIELDS_FILE


def _read_fields(data_dir: Path, namespace: str, profile: str) -> dict[str, str]:
    try:
        blob = _fields_file(data_dir, namespace, profile).read_bytes()
    except OSError:
        return {}
    text = vault_crypto.load_text(blob)
    if text is None:
        return {}
    try:
        raw = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key.strip(): value.strip()
        for key, value in raw.items()
        if isinstance(key, str) and key.strip() and isinstance(value, str) and value.strip()
    }


def _write_fields_atomic(path: Path, data: dict[str, str]) -> None:
    payload = vault_crypto.encrypt_str(json.dumps(data, indent=2, sort_keys=True) + "\n")
    vault_crypto.write_private_bytes_atomic(path, payload)


def load_fields(
    data_dir: Path | str,
    namespace: str,
    profile: str = "default",
    *,
    consumer: str = "system",
    audit: bool = True,
) -> dict[str, str]:
    """Decrypted ``{key: value}`` bundle for ``<namespace>/<profile>``.

    Pack consumption reads through here (``audit=True``); the UI passes
    ``audit=False`` for masked listing so it doesn't spam the access log.
    """
    dd = resolve_data_dir(data_dir)
    ns = _validate_namespace(namespace)
    pf = _validate_profile(profile)
    fields = _read_fields(dd, ns, pf)
    if audit and fields:
        audit_access(
            dd,
            {
                "action": "load_fields",
                "namespace": ns,
                "profile": pf,
                "consumer": consumer,
                "keys": sorted(fields),
            },
        )
    return fields


def set_fields(
    data_dir: Path | str,
    namespace: str,
    patch: dict,
    profile: str = "default",
    *,
    consumer: str = "dashboard",
) -> dict[str, str]:
    """Merge structured fields into ``<namespace>/<profile>`` (empty/None clears)."""
    dd = resolve_data_dir(data_dir)
    ns = _validate_namespace(namespace)
    pf = _validate_profile(profile)
    clean = _clean_secret_patch(patch)
    with vault_crypto.file_lock(_lock_path(dd)), _lock:
        # With a wrong/corrupt master key, refuse to overwrite the profile fields from an empty base.
        vault_crypto.assert_writable(_fields_file(dd, ns, pf))
        current = _read_fields(dd, ns, pf)
        _apply_secret_patch(current, clean)
        path = credential_dir(dd, ns, pf, create=True) / FIELDS_FILE
        _write_fields_atomic(path, current)
    audit_access(
        dd,
        {
            "action": "set_fields",
            "namespace": ns,
            "profile": pf,
            "consumer": consumer,
            "keys": sorted(clean.keys()),
        },
    )
    return current


def delete_field(
    data_dir: Path | str,
    namespace: str,
    field: str,
    profile: str = "default",
    *,
    consumer: str = "dashboard",
) -> bool:
    """Remove ONE field from ``<namespace>/<profile>``. Returns True iff it existed.

    Mirrors :func:`delete_profile`'s honest signal — clearing an absent field is a
    no-op that returns False. To drop the whole profile use :func:`delete_profile`.
    """
    field = (field or "").strip()
    if not field:
        raise ValueError("field required")
    dd = resolve_data_dir(data_dir)
    ns = _validate_namespace(namespace)
    pf = _validate_profile(profile)
    with vault_crypto.file_lock(_lock_path(dd)), _lock:
        # Wrong/corrupt master key must not silently wipe the profile fields.
        vault_crypto.assert_writable(_fields_file(dd, ns, pf))
        current = _read_fields(dd, ns, pf)
        existed = field in current
        if existed:
            current.pop(field, None)
            path = credential_dir(dd, ns, pf, create=True) / FIELDS_FILE
            _write_fields_atomic(path, current)
    audit_access(
        dd,
        {
            "action": "delete_field",
            "namespace": ns,
            "profile": pf,
            "field": field,
            "consumer": consumer,
            "removed": existed,
        },
    )
    return existed


def delete_profile(
    data_dir: Path | str,
    namespace: str,
    profile: str = "default",
    *,
    consumer: str = "dashboard",
) -> dict:
    """Permanently remove ``credentials/<namespace>/<profile>/`` — the whole profile.

    A real delete (``rmtree``), unlike clearing fields one by one: the dir and its
    encrypted bundle are gone afterwards. Idempotent — removing a missing profile is a
    no-op (``removed=False``). Locked + audited like the write paths; prunes the parent
    namespace dir when it becomes empty so listings stay clean.
    """
    dd = resolve_data_dir(data_dir)
    ns = _validate_namespace(namespace)
    pf = _validate_profile(profile)
    path = credential_dir(dd, ns, pf)
    with vault_crypto.file_lock(_lock_path(dd)), _lock:
        existed = path.is_dir()
        if existed:
            # On Windows, rmtree fails with PermissionError if another process holds a
            # lock-free handle open on secrets.enc (the exact open-handle race
            # write_private_bytes_atomic retries around). Retry to a short deadline, then
            # report success from an ACTUAL post-check (path.is_dir()) — NOT the pre-captured
            # `existed` — so a silently-failed delete never claims the credential is gone.
            deadline = time.monotonic() + _RMTREE_DEADLINE_S
            while True:
                shutil.rmtree(path, ignore_errors=True)
                if not path.is_dir() or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        removed = existed and not path.is_dir()
        ns_dir = path.parent
        try:  # tidy an orphaned namespace dir (no profiles left)
            if ns_dir.is_dir() and not any(ns_dir.iterdir()):
                ns_dir.rmdir()
        except OSError:
            pass
    audit_access(
        dd,
        {
            "action": "delete_profile",
            "namespace": ns,
            "profile": pf,
            "consumer": consumer,
            "removed": removed,
        },
    )
    return {"namespace": ns, "profile": pf, "removed": removed}


def audit_access(data_dir: Path | str, event: dict) -> None:
    dd = resolve_data_dir(data_dir)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    # Best-effort, end-to-end: audit MUST NOT raise (matches audit.write_event's contract).
    # Every caller runs audit_access AFTER the actual mutation/read, so a failure here
    # (e.g. a stray regular file named `audit` in the data dir → mkdir FileExistsError, or an
    # unwritable data dir → PermissionError) would otherwise make a SUCCEEDED vault op report
    # failure/HTTP 500 — so dir creation is inside the guard too.
    #
    # Serialize rotation against appends. "a" mode lacks atomic O_APPEND on Windows, so
    # concurrent audited writes could otherwise interleave into a malformed JSONL line, or
    # a rotation could rename the log between another writer's size check and its open. The
    # threading `_lock` only covers THIS process, but the akana_vault MCP child is a
    # SEPARATE process appending to the same log — so take the cross-process `file_lock`
    # too (as every data-write path does), else the child and server still race here.
    try:
        audit_dir = _ensure_private_dir(dd / "audit", data_dir=dd)
        path = audit_dir / VAULT_AUDIT_FILE
        with vault_crypto.file_lock(path.with_name(path.name + ".lock")), _lock:
            # Size-capped rotation: keep current + one previous generation.
            if path.exists() and path.stat().st_size > AUDIT_MAX_BYTES:
                path.replace(path.with_name(path.name + ".1"))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.debug("vault audit write failed: %s", exc)


def migrate_legacy(
    data_dir: Path | str,
    namespace: str,
    profile: str = "default",
    *,
    force: bool = False,
) -> dict:
    """Copy legacy credential dir into SecureVault if destination is empty."""
    dd = resolve_data_dir(data_dir)
    ns = _validate_namespace(namespace)
    pf = _validate_profile(profile)
    dest = credential_dir(dd, ns, pf, create=True)
    status = profile_status(dd, ns, pf)
    if status["populated"] and not force:
        return {"migrated": False, "reason": "destination_populated", **status}

    sources = LEGACY_CREDENTIAL_DIRS.get(ns, [])
    for src, src_ns, src_pf in sources:
        if src_ns != ns or src_pf != pf:
            continue
        if not src.is_dir() or not any(src.iterdir()):
            continue
        if dest.exists() and any(dest.iterdir()):
            if not force:
                continue
            for child in dest.iterdir():
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child)
        for item in src.iterdir():
            target = dest / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        audit_access(
            dd,
            {
                "action": "migrate_legacy",
                "namespace": ns,
                "profile": pf,
                "source": str(src),
                "files": status.get("file_count", 0),
            },
        )
        return {
            "migrated": True,
            "source": str(src),
            **profile_status(dd, ns, pf),
        }
    return {"migrated": False, "reason": "no_legacy_source", **status}


def vault_summary(data_dir: Path | str) -> dict:
    dd = resolve_data_dir(data_dir)
    namespaces = []
    for ns in list_namespaces(dd):
        profiles = []
        for pf in list_profiles(dd, ns):
            st = profile_status(dd, ns, pf)
            profiles.append(
                {
                    "profile": pf,
                    "populated": st["populated"],
                    "file_count": st["file_count"],
                }
            )
        namespaces.append({"namespace": ns, "profiles": profiles})
    return {
        "data_dir": str(dd),
        "credentials_root": str(credentials_root(dd)),
        "namespaces": namespaces,
    }


def inventory(data_dir: Path | str) -> dict:
    """Names-only listing for tool discovery — scalar keys + credential fields, NO values.

    Lets a caller (e.g. the vault MCP/native tools) tell the model WHAT secrets exist
    without exposing any value. Reads field names with ``audit=False`` (listing must not
    spam the access log).

    Scalars are the union of the two partitions ``vault_get`` reads: extended keys from the
    encrypted keyfile (:func:`load_scalars`, ALLOWED names already filtered out) and the set
    system provider keys in ``secret_store`` (:data:`ALLOWED_KEYS`). Because the partitions
    never overlap, discover and read are symmetric — every listed name is fetchable, and
    every fetchable name is listed.
    """
    dd = resolve_data_dir(data_dir)
    scalars = sorted(set(load_scalars(dd)) | set(load_secrets(dd)))
    credentials: list[dict] = []
    for ns in list_namespaces(dd):
        for pf in list_profiles(dd, ns):
            fields = sorted(load_fields(dd, ns, pf, audit=False).keys())
            credentials.append({"namespace": ns, "profile": pf, "fields": fields})
    return {"scalars": scalars, "credentials": credentials}
