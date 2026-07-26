"""``akana backup`` / ``akana restore`` — snapshot and restore the whole data dir.

Your data lives OUTSIDE the repo in ``~/.akana`` (or ``$AKANA_DATA_DIR``): the memory
databases, personas, uploads, settings, schedules, and the encrypted secret stores. This
packs the PRECIOUS state into one ``.tar.gz`` you can copy off-machine, and restores it.

What it does carefully:
  • SQLite (memory.db / persona.db / multimodal.db) is copied with the online BACKUP API,
    not a raw file copy — WAL mode means a naive copy of a live DB can capture a torn or
    stale snapshot (missing its ``-wal`` tail). The regenerable FTS index (db/skills.db)
    is skipped; it rebuilds on next start.
  • JSON/YAML stores are written atomically (tmp + os.replace) by the app, so a plain copy
    always sees a COMPLETE old-or-new file — no lock dance needed.
  • Regenerable/transient trees are excluded (logs/ run/ agent_chat/ *.lock *.tmp, the
    empty legacy conversations/ and the dead approvals/); voices/ (.onnx models,
    re-downloadable) is excluded unless ``--include-voices``. The exclude list is a
    denylist, so an unknown NEW store at the data-dir root is backed up by default.
  • The secret stores (secrets.json, vault/, credentials/) ARE included, but they are
    Fernet-encrypted and the MASTER KEY lives OUTSIDE the data dir BY DESIGN — so a
    default backup is ciphertext, restorable only on the same machine / with the same key.
    ``--include-vault-key`` bundles the key too (cross-machine restore) with a LOUD warning:
    the archive then contains everything needed to read your secrets in plaintext.

Restore HARD-REFUSES while the server is running (its in-process caches would make a
file-level restore partially invisible / corrupt), verifies the manifest hashes, moves any
existing data dir aside first, and re-hardens the secret dirs to owner-only after extract.

Both directions treat the user's existing artifact as sacred until the new one is proven:
``backup`` builds a ``*.partial-<pid>`` and os.replace()s it over the target only once the
manifest is in (so a failed nightly run never eats last night's archive), and ``restore``
copies the verified tree to a SIBLING of the data dir before touching anything, so the
only remaining step is a same-filesystem rename it can roll back.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

from akana_cli import i18n, io
from akana_cli.env_util import load_repo_dotenv
from akana_cli.paths import default_data_dir, expand_user_path

#: Directory names under the data dir that are regenerable/transient — never backed up.
_EXCLUDE_DIRS = frozenset({
    "logs", "run", "agent_chat", "conversations", "approvals", "file_backups", "__pycache__",
})
#: File suffixes never backed up (lock sidecars + atomic-write temporaries).
_EXCLUDE_SUFFIXES = (".lock", ".tmp")
#: Regenerable FTS index (rebuilt from skills/ on next start).
_SKIP_FILES = frozenset({"db/skills.db"})

_MANIFEST_NAME = "manifest.json"
_ARCHIVE_ROOT = "akana-data"  # everything under the data dir goes here in the tar
#: Archive layout version. Restore REFUSES anything else: the field only earns its keep
#: if the reader enforces it, otherwise the first format bump silently mis-restores on
#: every already-shipped CLI instead of saying "made by a newer Akana".
_BACKUP_FORMAT = 1


def _resolve_data_dir() -> Path:
    load_repo_dotenv()
    env = os.environ.get("AKANA_DATA_DIR", "").strip()
    return expand_user_path(env) if env else default_data_dir()


def _server_might_be_running() -> bool:
    try:
        from akana_cli.env_util import server_host_port
        from akana_cli.stop_cmd import find_pids_on_port

        host, port = server_host_port()
        return bool(find_pids_on_port(port, host))
    except Exception:
        return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sqlite_online_backup(src: Path, dst: Path) -> None:
    """Consistent snapshot of a (possibly live, WAL-mode) SQLite DB via the backup API."""
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _rel_excluded(rel: str, *, include_voices: bool) -> bool:
    parts = Path(rel).parts
    if parts and parts[0] in _EXCLUDE_DIRS:
        return True
    if not include_voices and parts and parts[0] == "voices":
        return True
    if rel in _SKIP_FILES:
        return True
    # CRITICAL: the SQLite WAL/SHM/journal sidecars must NOT be backed up. The online
    # backup API already writes a checkpointed, self-contained snapshot; carrying the
    # live, still-diverging -wal alongside it makes SQLite REPLAY stale WAL onto the
    # snapshot at restore → resurrected/mutated rows or structural corruption.
    if rel.startswith("db/") and rel.endswith(("-wal", "-shm", "-journal")):
        return True
    return rel.endswith(_EXCLUDE_SUFFIXES)


def _active_keyfile() -> Path | None:
    """The keyfile the vault will actually READ, or None if it can't be resolved.

    ``AKANA_VAULT_KEYFILE`` wins over the default location (``AKANA_VAULT_KEY`` and
    keyring are not files at all — see _vault_key_source_mismatch for those).
    """
    env = os.environ.get("AKANA_VAULT_KEYFILE", "").strip()
    if env:
        return expand_user_path(env)
    try:
        from akana_server.vault_crypto import default_keyfile

        return default_keyfile()
    except Exception:  # noqa: BLE001 — an unimportable server package is not fatal here
        return None


def _vault_key_source_mismatch(default_key: Path) -> str:
    """Env var whose key the vault would use INSTEAD of ``default_key`` ("" = none).

    Key resolution is AKANA_VAULT_KEY → AKANA_VAULT_KEYFILE → keyring → default keyfile.
    ``--include-vault-key`` can only bundle the default FILE, so on a machine using any
    of the first three the bundled key may be a stale leftover that decrypts nothing on
    the target machine — the one failure this flag exists to prevent.
    """
    if os.environ.get("AKANA_VAULT_KEY", "").strip():
        return "AKANA_VAULT_KEY"
    kf = os.environ.get("AKANA_VAULT_KEYFILE", "").strip()
    if kf:
        try:
            same = expand_user_path(kf).resolve() == default_key.resolve()
        except OSError:
            same = False
        if not same:
            return "AKANA_VAULT_KEYFILE"
    if os.environ.get("AKANA_VAULT_KEYRING", "").strip().lower() in ("1", "true", "yes", "on"):
        return "AKANA_VAULT_KEYRING"
    return ""


def _readable_credential_paths(data_dir: Path) -> list[str]:
    """Reasons this archive is NOT the ciphertext-only blob the closing note promises.

    Two SUPPORTED states put readable credentials in the tar and must suppress that
    note: a legacy plaintext ``secrets.json`` (secret_store still reads pre-vault1
    files and only re-encrypts on the next write, so it can persist for years), and a
    vault keyfile resolving to a path INSIDE the data dir — where the denylist sweeps
    it in, shipping the master key next to the ciphertext it opens.
    """
    hits: list[str] = []
    sj = data_dir / "secrets.json"
    try:
        if sj.is_file() and not sj.read_bytes().lstrip().startswith(b"vault1:"):
            hits.append("secrets.json")
    except OSError:
        pass
    key = _active_keyfile()
    if key is not None:
        try:
            resolved_key = key.resolve()
            resolved_dir = data_dir.resolve()
            if resolved_key.is_file() and resolved_dir in resolved_key.parents:
                hits.append(str(resolved_key.relative_to(resolved_dir)))
        except (OSError, ValueError):
            pass
    return hits


def _iter_data_files(data_dir: Path, *, include_voices: bool):
    """Yield (abs_path, rel_posix) for every file to back up (denylist-filtered)."""
    for abs_path in sorted(data_dir.rglob("*")):
        if not abs_path.is_file():
            continue
        rel = abs_path.relative_to(data_dir).as_posix()
        if _rel_excluded(rel, include_voices=include_voices):
            continue
        yield abs_path, rel


def run_backup(
    out: Path | None = None, *, include_voices: bool = False, include_vault_key: bool = False
) -> int:
    data_dir = _resolve_data_dir()
    if not data_dir.exists():
        io.fail(i18n.t("backup.no_data_dir", path=data_dir))
        return 1
    io.step(i18n.t("backup.backing_up", path=data_dir))
    if _server_might_be_running():
        # SAFE to run hot: SQLite uses the online backup API, JSON is atomic. Just note it.
        io.warn(i18n.t("backup.server_running"))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = (out or Path.cwd()).expanduser()
    # A path ending in .tar.gz/.tgz names the archive FILE; otherwise it is a directory
    # the timestamped archive lands in.
    if out.name.endswith(".tar.gz") or out.suffix == ".tgz":
        archive = out
        archive.parent.mkdir(parents=True, exist_ok=True)
    else:
        out.mkdir(parents=True, exist_ok=True)
        archive = out / f"akana-backup-{stamp}.tar.gz"

    manifest: dict = {
        "akana_backup_format": _BACKUP_FORMAT,
        "created_at": stamp,
        "data_dir": str(data_dir),
        "include_voices": include_voices,
        "includes_vault_key": False,
        "files": {},
    }

    # NEVER destroy the previous good archive until the new one is COMPLETE. A nightly
    # `akana backup D:/backups/akana.tar.gz` reuses one filename, and opening the final
    # path "w:gz" truncates last night's archive the instant this run STARTS — so a run
    # that then fails (or is interrupted) leaves the user with neither. Build into a
    # sibling partial and os.replace() it into place only once the manifest is in.
    partial = archive.with_name(archive.name + f".partial-{os.getpid()}")
    try:
        with tempfile.TemporaryDirectory(prefix="akana-backup-") as tmp, tarfile.open(partial, "w:gz") as tar:
            tmp_dir = Path(tmp)
            for abs_path, rel in _iter_data_files(data_dir, include_voices=include_voices):
                arc = f"{_ARCHIVE_ROOT}/{rel}"
                # Stage EVERY file to a temp copy, then hash+add the STAGED bytes — so the
                # manifest hash always matches the archived content even if a live server
                # rewrites the source between the hash and the add (hot-backup race).
                staged = tmp_dir / rel.replace("/", "__")
                if rel.startswith("db/") and rel.endswith(".db"):
                    try:
                        _sqlite_online_backup(abs_path, staged)
                    except sqlite3.Error as exc:
                        # Not a valid SQLite DB (foreign/0-byte/garbage named .db) — a raw
                        # copy keeps the rest of the backup alive instead of aborting all.
                        io.warn(i18n.t("backup.db_raw_copy", name=rel, exc=exc))
                        shutil.copy2(abs_path, staged)
                else:
                    shutil.copy2(abs_path, staged)
                manifest["files"][rel] = _sha256(staged)
                tar.add(staged, arcname=arc)

            if include_vault_key:
                from akana_server.vault_crypto import default_keyfile

                key = default_keyfile()
                override = _vault_key_source_mismatch(key)
                if override:
                    # The bundled file is NOT provably the key the vault reads — say so
                    # here, not on the target machine after the source machine is gone.
                    io.warn(i18n.t("backup.vault_key_not_active", var=override, path=key))
                if key.is_file():
                    io.warn(i18n.t("backup.vault_key_warning"))
                    manifest["includes_vault_key"] = True
                    manifest["files"]["__vault_key__"] = _sha256(key)
                    tar.add(key, arcname=f"{_ARCHIVE_ROOT}/__vault_key__")
                else:
                    io.warn(i18n.t("backup.vault_key_missing"))

            # manifest last so it reflects every file
            man_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
            info = tarfile.TarInfo(name=f"{_ARCHIVE_ROOT}/{_MANIFEST_NAME}")
            info.size = len(man_bytes)
            import io as _io

            tar.addfile(info, _io.BytesIO(man_bytes))
        os.replace(partial, archive)  # the only step that touches the user's target path
    except (OSError, sqlite3.Error, tarfile.TarError) as exc:
        io.fail(i18n.t("backup.failed", exc=exc))
        try:
            partial.unlink(missing_ok=True)  # only the partial — never the good archive
        except OSError:
            pass
        return 1

    size_mb = archive.stat().st_size / (1024 * 1024)
    io.ok(i18n.t("backup.done", path=archive, count=len(manifest["files"]), mb=f"{size_mb:.1f}"))
    # The ciphertext note is a claim the user makes an off-machine STORAGE decision on
    # ("it's encrypted, Dropbox is fine"), so it may only be printed after looking.
    readable = _readable_credential_paths(data_dir)
    if readable:
        io.warn(i18n.t("backup.plaintext_secrets", files=", ".join(readable)))
    elif not include_vault_key:
        print("  " + i18n.t("backup.ciphertext_note"))
    return 0


def _reharden_secret_dirs(data_dir: Path) -> None:
    """Restore owner-only perms on the secret trees (tar/zip round-trips lose them)."""
    try:
        from akana_server.secure_vault import _chmod_private
        from akana_server.vault_crypto import _restrict_to_owner
    except Exception:
        _chmod_private = None  # type: ignore[assignment]
        _restrict_to_owner = None  # type: ignore[assignment]
    for sub in ("vault", "credentials"):
        d = data_dir / sub
        if not d.exists():
            continue
        if _chmod_private is not None:
            _chmod_private(d, 0o700)
        else:
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass
        # The DIRECTORY is hardened too, not just the files: a tar round-trip leaves the
        # secret trees carrying whatever the parent profile hands down (on Windows that
        # includes Administrators/SYSTEM and full enumeration). This used to be skipped
        # because `icacls <dir> /inheritance:r /grant:r <user>:F` — a NON-inheritable
        # replacement grant — makes NTFS propagate the inheritance removal downward and
        # leaves every descendant with an EMPTY DACL, locking the owner out of their own
        # secrets. vault_crypto._restrict_to_owner now emits `(OI)(CI)F` for directories
        # and the plain grant for leaves, so hardening top-down is safe: each child either
        # inherits full control from its parent or gets its own explicit grant.
        if _restrict_to_owner is not None:
            _restrict_to_owner(d)
        for p in sorted(d.rglob("*")):  # parents before children (top-down grants)
            try:
                os.chmod(p, 0o700 if p.is_dir() else 0o600)
            except OSError:
                pass
            if _restrict_to_owner is not None:
                _restrict_to_owner(p)
    # secrets.json at the root is also owner-only
    sj = data_dir / "secrets.json"
    if sj.is_file():
        try:
            os.chmod(sj, 0o600)
        except OSError:
            pass
        if _restrict_to_owner is not None:
            _restrict_to_owner(sj)


def run_restore(archive: Path, *, force: bool = False) -> int:
    archive = archive.expanduser()
    if not archive.is_file():
        io.fail(i18n.t("restore.no_archive", path=archive))
        return 1
    if _server_might_be_running():
        # HARD block: in-process caches (RuntimeStore mtime, vault key cache, store
        # registries) would make a file-level restore partially invisible or corrupt.
        io.fail(i18n.t("restore.server_running"))
        return 1

    data_dir = _resolve_data_dir()
    io.step(i18n.t("restore.restoring", src=archive, dst=data_dir))

    # 1) extract into a temp dir + verify the manifest hashes BEFORE touching the data dir.
    try:
        with tempfile.TemporaryDirectory(prefix="akana-restore-") as tmp:
            tmp_dir = Path(tmp)
            with tarfile.open(archive, "r:gz") as tar:
                _safe_extract(tar, tmp_dir)
            root = tmp_dir / _ARCHIVE_ROOT
            man_path = root / _MANIFEST_NAME
            if not man_path.is_file():
                io.fail(i18n.t("restore.bad_archive"))
                return 1
            manifest = json.loads(man_path.read_text(encoding="utf-8"))
            fmt = manifest.get("akana_backup_format")
            if fmt != _BACKUP_FORMAT:
                # Forward-compat refusal: a newer layout restored under today's rules
                # produces a wrong data dir with no error at all.
                io.fail(i18n.t("restore.bad_format", got=fmt, want=_BACKUP_FORMAT))
                return 1
            bad = _verify_hashes(root, manifest)
            if bad:
                io.fail(i18n.t("restore.hash_mismatch", files=", ".join(bad[:5])))
                return 1
            # Manifest must be AUTHORITATIVE for the file set: refuse any extracted member
            # not listed (it would land in the data dir unverified). Ignore the manifest
            # file itself and the (separately handled, hash-listed) vault key blob.
            listed = set(manifest.get("files") or {})
            actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
            extra = actual - listed - {_MANIFEST_NAME, "__vault_key__"}
            if extra:
                io.fail(i18n.t("restore.unlisted", files=", ".join(sorted(extra)[:5])))
                return 1

            # 2) refuse a populated target BEFORE any destructive step (unchanged rule).
            occupied = data_dir.is_dir() and any(data_dir.iterdir())
            if occupied and not force:
                io.fail(i18n.t("restore.exists", path=data_dir))
                return 1

            man_path.unlink(missing_ok=True)  # the manifest itself is not restored
            vault_key_blob = root / "__vault_key__"
            restored_key = vault_key_blob.is_file()
            if restored_key:
                vault_key_blob.unlink()  # handled separately below, not under data_dir

            # 3) Land the verified tree NEXT TO the data dir FIRST. The extract lives in
            # system temp, which can be a DIFFERENT filesystem: shutil.move then degrades
            # to copytree+delete and can die halfway (ENOSPC, a Windows-illegal name from
            # a Linux-made archive, a >260-char path, Ctrl+C). Doing that copy while the
            # data dir is still untouched is what makes a failure here a NO-OP — the old
            # code did it after the rename-aside, leaving an arbitrary PREFIX of the
            # archive in place that the server boots on without complaint.
            data_dir.parent.mkdir(parents=True, exist_ok=True)
            staging = data_dir.with_name(f"{data_dir.name}.restoring-{os.getpid()}")
            shutil.rmtree(staging, ignore_errors=True)  # leftover from an earlier crash
            try:
                shutil.move(str(root), str(staging))  # shutil.Error subclasses OSError
            except OSError as exc:
                shutil.rmtree(staging, ignore_errors=True)
                io.fail(i18n.t("restore.failed", exc=exc))
                print("  " + i18n.t("restore.unchanged", path=data_dir))
                return 1

            # 4) Swap it in. Both renames are SIBLINGS on one filesystem, so the window
            # where the data dir is missing is one rename wide; if it still fails, the
            # aside dir goes straight back — the user must never be left depending on
            # noticing a .pre-restore-* directory.
            aside: Path | None = None
            try:
                if data_dir.exists():
                    if occupied:
                        aside = data_dir.with_name(
                            data_dir.name + f".pre-restore-{time.strftime('%Y%m%d-%H%M%S')}"
                        )
                        data_dir.rename(aside)
                        io.warn(i18n.t("restore.moved_aside", path=aside))
                    else:
                        data_dir.rmdir()  # empty → remove, else the rename would nest
                staging.rename(data_dir)
            except OSError as exc:
                io.fail(i18n.t("restore.failed", exc=exc))
                shutil.rmtree(staging, ignore_errors=True)
                if aside is None:
                    try:
                        # only an EMPTY dir can have been removed above — put it back so
                        # "nothing was changed" is literally true.
                        data_dir.mkdir(parents=True, exist_ok=True)
                    except OSError:
                        pass
                    print("  " + i18n.t("restore.unchanged", path=data_dir))
                else:
                    try:
                        aside.rename(data_dir)
                        print("  " + i18n.t("restore.rolled_back", path=data_dir))
                    except OSError:
                        # Nothing left but to name BOTH paths: the data dir is incomplete
                        # and the real data is in a directory the user has never heard of.
                        io.fail(i18n.t("restore.rollback_failed", aside=aside, path=data_dir))
                return 1

            if restored_key and manifest.get("includes_vault_key"):
                _restore_vault_key(archive)  # re-extract the key to its real location
    except (OSError, tarfile.TarError, ValueError) as exc:
        io.fail(i18n.t("restore.failed", exc=exc))
        return 1

    # 3) recreate the dir skeleton + re-harden the secret trees.
    try:
        from akana_server.config import ensure_data_dirs

        ensure_data_dirs(data_dir)
    except Exception:
        pass
    _reharden_secret_dirs(data_dir)

    io.ok(i18n.t("restore.done", path=data_dir))
    print("  " + i18n.t("restore.restart_hint"))
    return 0


def _verify_hashes(root: Path, manifest: dict) -> list[str]:
    """Return the rel-paths whose on-disk hash disagrees with the manifest (empty = OK)."""
    bad: list[str] = []
    for rel, want in (manifest.get("files") or {}).items():
        if rel == "__vault_key__":
            p = root / "__vault_key__"
        else:
            p = root / rel
        if not p.is_file() or _sha256(p) != want:
            bad.append(rel)
    return bad


def _restore_vault_key(archive: Path) -> None:
    """Re-extract a bundled master key to its real (outside-data-dir) location."""
    from akana_server.vault_crypto import _restrict_to_owner, default_keyfile

    dest = default_keyfile()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.extractfile(f"{_ARCHIVE_ROOT}/__vault_key__")
        if member is None:
            return
        data = member.read()
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    _restrict_to_owner(dest)
    io.warn(i18n.t("restore.vault_key_written", path=dest))
    # The key only helps if the TARGET machine resolves to this path — an
    # AKANA_VAULT_KEY/KEYFILE/keyring configuration makes the write a silent no-op and
    # every secret undecryptable despite the reassuring line above.
    override = _vault_key_source_mismatch(dest)
    if override:
        io.warn(i18n.t("restore.vault_key_not_read", var=override, path=dest))


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract with path-traversal protection: only regular files/dirs, none escaping dest."""
    dest = dest.resolve()
    for member in tar.getmembers():
        # Only plain files and directories — reject symlink/hardlink/device/fifo members.
        if not (member.isfile() or member.isdir()):
            raise tarfile.TarError(f"archive contains a non-regular member ({member.name!r}); refusing")
        target = (dest / member.name).resolve()
        # Proper containment: target must BE dest or a descendant — `startswith(str(dest))`
        # would wrongly accept a sibling like `<dest>-evil`.
        if target != dest and dest not in target.parents:
            raise tarfile.TarError(f"unsafe path in archive: {member.name!r}")
    tar.extractall(dest)
