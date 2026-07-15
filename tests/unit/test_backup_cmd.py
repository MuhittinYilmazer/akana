"""``akana backup`` / ``akana restore`` roundtrip — hermetic (tmp data dirs, no server).

Locks the contract: SQLite is snapshotted via the online backup API (row survives), the
denylist excludes regenerable/transient trees + lock/tmp + db/skills.db, unknown root
stores are included (forward-compat), secrets are carried, the manifest hash-verifies on
restore, restore HARD-refuses while the server runs, --force moves an existing dir aside,
and the archive is rejected on tamper / path traversal.
"""

from __future__ import annotations

import io as _io
import sqlite3
import tarfile
from pathlib import Path

import pytest

from akana_cli import backup_cmd


def _make_data_dir(root: Path) -> Path:
    d = root / "data"
    (d / "db").mkdir(parents=True)
    # a real SQLite DB with a row (proves the online-backup snapshot is consistent)
    con = sqlite3.connect(str(d / "db" / "memory.db"))
    con.execute("CREATE TABLE facts (id INTEGER, v TEXT)")
    con.execute("INSERT INTO facts VALUES (1, 'hatirla')")
    con.commit()
    con.close()
    (d / "db" / "skills.db").write_bytes(b"FTS-INDEX-regenerable")  # must be EXCLUDED
    (d / "runtime_settings.json").write_text('{"language": "tr"}', encoding="utf-8")
    (d / "new_unknown_store.json").write_text("{}", encoding="utf-8")  # forward-compat include
    (d / "secrets.json").write_text("vault1:ciphertext", encoding="utf-8")
    (d / "vault").mkdir()
    (d / "vault" / "keys.json").write_text("vault1:keys", encoding="utf-8")
    (d / "credentials" / "telegram" / "default").mkdir(parents=True)
    (d / "credentials" / "telegram" / "default" / "secrets.enc").write_bytes(b"enc")
    (d / "uploads").mkdir()
    (d / "uploads" / "pic.png").write_bytes(b"PNG")
    # excluded trees
    (d / "logs").mkdir(); (d / "logs" / "server.log").write_text("noise", encoding="utf-8")
    (d / "run").mkdir(); (d / "run" / "pid.json").write_text("{}", encoding="utf-8")
    (d / "voices").mkdir(); (d / "voices" / "amy.onnx").write_bytes(b"MODEL")
    (d / "schedules.json.lock").write_text("", encoding="utf-8")  # lock sidecar excluded
    return d


@pytest.fixture(autouse=True)
def _no_server(monkeypatch):
    monkeypatch.setattr(backup_cmd, "_server_might_be_running", lambda: False)


def test_backup_then_restore_roundtrip(tmp_path, monkeypatch):
    src = _make_data_dir(tmp_path)
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    out = tmp_path / "backups"
    assert backup_cmd.run_backup(out) == 0
    archive = next(out.glob("akana-backup-*.tar.gz"))

    # inspect the archive's manifest: what got included / excluded
    with tarfile.open(archive, "r:gz") as tar:
        names = {m.name for m in tar.getmembers()}
        man = _io.TextIOWrapper(tar.extractfile("akana-data/manifest.json")).read()
    files = set(__import__("json").loads(man)["files"])
    assert "db/memory.db" in files and "runtime_settings.json" in files
    assert "new_unknown_store.json" in files  # forward-compat: unknown root store included
    assert "secrets.json" in files and "vault/keys.json" in files
    assert "credentials/telegram/default/secrets.enc" in files
    assert "db/skills.db" not in files  # regenerable FTS excluded
    assert not any(f.startswith(("logs/", "run/", "voices/")) for f in files)  # transient excluded
    assert not any(f.endswith(".lock") for f in files)

    # restore into a fresh data dir
    dst = tmp_path / "restored"
    monkeypatch.setenv("AKANA_DATA_DIR", str(dst))
    assert backup_cmd.run_restore(archive) == 0

    # the SQLite row survived the online-backup snapshot
    con = sqlite3.connect(str(dst / "db" / "memory.db"))
    assert con.execute("SELECT v FROM facts WHERE id=1").fetchone()[0] == "hatirla"
    con.close()
    assert (dst / "runtime_settings.json").read_text(encoding="utf-8") == '{"language": "tr"}'
    assert (dst / "credentials" / "telegram" / "default" / "secrets.enc").read_bytes() == b"enc"
    assert not (dst / "db" / "skills.db").exists()  # excluded → not restored
    # excluded CONTENT is absent (the empty skeleton dirs are re-created by ensure_data_dirs)
    assert not (dst / "logs" / "server.log").exists()
    assert not (dst / "voices" / "amy.onnx").exists()
    assert not (dst / "run" / "pid.json").exists()
    assert "manifest.json" not in {p.name for p in dst.rglob("*")}  # manifest not restored


def test_include_voices_flag(tmp_path, monkeypatch):
    src = _make_data_dir(tmp_path)
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    out = tmp_path / "b"
    assert backup_cmd.run_backup(out, include_voices=True) == 0
    archive = next(out.glob("*.tar.gz"))
    with tarfile.open(archive, "r:gz") as tar:
        names = {m.name for m in tar.getmembers()}
    assert "akana-data/voices/amy.onnx" in names  # opted in → included


def test_restore_refuses_while_server_running(tmp_path, monkeypatch):
    src = _make_data_dir(tmp_path)
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    out = tmp_path / "b"
    backup_cmd.run_backup(out)
    archive = next(out.glob("*.tar.gz"))
    monkeypatch.setattr(backup_cmd, "_server_might_be_running", lambda: True)
    dst = tmp_path / "restored"
    monkeypatch.setenv("AKANA_DATA_DIR", str(dst))
    assert backup_cmd.run_restore(archive) == 1  # HARD refuse
    assert not dst.exists()  # nothing was written


def test_restore_existing_dir_needs_force(tmp_path, monkeypatch):
    src = _make_data_dir(tmp_path)
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    out = tmp_path / "b"
    backup_cmd.run_backup(out)
    archive = next(out.glob("*.tar.gz"))
    # restore target already has data
    dst = tmp_path / "restored"
    (dst / "db").mkdir(parents=True)
    (dst / "existing.txt").write_text("keep-me", encoding="utf-8")
    monkeypatch.setenv("AKANA_DATA_DIR", str(dst))
    assert backup_cmd.run_restore(archive) == 1  # refuses without --force
    assert (dst / "existing.txt").exists()
    # with --force the old dir is moved aside and the restore lands
    assert backup_cmd.run_restore(archive, force=True) == 0
    assert (dst / "db" / "memory.db").exists()
    aside = list(tmp_path.glob("restored.pre-restore-*"))
    assert aside and (aside[0] / "existing.txt").read_text(encoding="utf-8") == "keep-me"


def test_tamper_is_rejected(tmp_path, monkeypatch):
    src = _make_data_dir(tmp_path)
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    out = tmp_path / "b"
    backup_cmd.run_backup(out)
    archive = next(out.glob("*.tar.gz"))
    # rewrite one member's bytes so its sha256 no longer matches the manifest
    import gzip
    import shutil
    unpacked = tmp_path / "u"
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(unpacked)
    (unpacked / "akana-data" / "runtime_settings.json").write_text('{"language":"HACKED"}', encoding="utf-8")
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(tampered, "w:gz") as tar:
        tar.add(unpacked / "akana-data", arcname="akana-data")
    dst = tmp_path / "restored"
    monkeypatch.setenv("AKANA_DATA_DIR", str(dst))
    assert backup_cmd.run_restore(tampered) == 1  # hash mismatch → refuse
    assert not dst.exists()


def test_wal_sidecars_are_excluded(tmp_path, monkeypatch):
    """The SQLite -wal/-shm sidecars must NOT be backed up — carrying them replays stale
    WAL onto the online snapshot at restore (silent data corruption)."""
    src = _make_data_dir(tmp_path)
    (src / "db" / "memory.db-wal").write_bytes(b"STALE-WAL")
    (src / "db" / "memory.db-shm").write_bytes(b"SHM")
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    out = tmp_path / "b"
    backup_cmd.run_backup(out)
    archive = next(out.glob("*.tar.gz"))
    with tarfile.open(archive, "r:gz") as tar:
        names = {m.name for m in tar.getmembers()}
    assert not any(n.endswith(("-wal", "-shm")) for n in names)
    assert "akana-data/db/memory.db" in names  # the snapshot itself IS there


def test_restore_into_empty_dir_lands_at_top_level(tmp_path, monkeypatch):
    """Restoring into a pre-created EMPTY data dir must place db/ AT the root, not nested
    one level deep in an akana-data/ subdir."""
    src = _make_data_dir(tmp_path)
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    out = tmp_path / "b"
    backup_cmd.run_backup(out)
    archive = next(out.glob("*.tar.gz"))
    dst = tmp_path / "restored"
    dst.mkdir()  # pre-created empty (e.g. ensure_data_dirs ran)
    monkeypatch.setenv("AKANA_DATA_DIR", str(dst))
    assert backup_cmd.run_restore(archive) == 0
    assert (dst / "db" / "memory.db").exists()  # top-level, not dst/akana-data/db/...
    assert not (dst / "akana-data").exists()


def test_one_bad_db_does_not_abort_backup(tmp_path, monkeypatch):
    """A foreign/garbage db/*.db falls back to a raw copy so the rest of the backup lives."""
    src = _make_data_dir(tmp_path)
    (src / "db" / "files.db").write_bytes(b"NOT-A-SQLITE-DB")  # garbage
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    out = tmp_path / "b"
    assert backup_cmd.run_backup(out) == 0  # did not abort
    archive = next(out.glob("*.tar.gz"))
    with tarfile.open(archive, "r:gz") as tar:
        names = {m.name for m in tar.getmembers()}
    assert "akana-data/db/files.db" in names and "akana-data/db/memory.db" in names


def test_out_names_a_file(tmp_path, monkeypatch):
    src = _make_data_dir(tmp_path)
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    target = tmp_path / "nested" / "my-backup.tar.gz"
    assert backup_cmd.run_backup(target) == 0
    assert target.is_file()  # the archive is the named file, not a dir containing it


def test_unlisted_member_is_rejected(tmp_path, monkeypatch):
    """An archive whose tree contains a file absent from the manifest is refused (a
    tampered archive can't smuggle an unverified file into the data dir)."""
    src = _make_data_dir(tmp_path)
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    out = tmp_path / "b"
    backup_cmd.run_backup(out)
    archive = next(out.glob("*.tar.gz"))
    unpacked = tmp_path / "u"
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(unpacked)
    (unpacked / "akana-data" / "smuggled.sh").write_text("evil", encoding="utf-8")  # not in manifest
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(tampered, "w:gz") as tar:
        tar.add(unpacked / "akana-data", arcname="akana-data")
    dst = tmp_path / "restored"
    monkeypatch.setenv("AKANA_DATA_DIR", str(dst))
    assert backup_cmd.run_restore(tampered) == 1
    assert not dst.exists()


def test_path_traversal_member_is_rejected(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        data = b"pwned"
        info = tarfile.TarInfo(name="akana-data/../../escape.txt")
        info.size = len(data)
        tar.addfile(info, _io.BytesIO(data))
    with pytest.raises(tarfile.TarError):
        with tarfile.open(evil, "r:gz") as tar:
            backup_cmd._safe_extract(tar, tmp_path / "out")
