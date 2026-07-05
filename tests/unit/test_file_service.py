"""FileEngine F0 — FileService: root jail, atomic write, operation log.

Scope: the root-jail matrix (inside ok / outside absolute / ``..`` / symlink escape),
empty-roots disabled contract, ``max_bytes`` truncation, atomic write + backup
(including the size limit) and the ``db/files.db`` operation log. (FULL AUTONOMY: the
old file_write risk/approval policy gate was removed; writes are allow-all.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

import akana_server.files.service as service_module
from akana_server.files.oplog import FileOpLog, reset_file_oplogs
from akana_server.files.service import (
    FileEngineNotConfigured,
    FileService,
)


@pytest.fixture(autouse=True)
def _isolated_oplogs(monkeypatch: pytest.MonkeyPatch):
    reset_file_oplogs()
    yield
    reset_file_oplogs()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def service(root: Path, data_dir: Path) -> FileService:
    return FileService((root,), data_dir=data_dir)


# -- root-jail matrix ----------------------------------------------------------------


def test_kok_icindeki_dosya_okunur(service: FileService, root: Path) -> None:
    (root / "not.txt").write_text("merhaba", encoding="utf-8")
    result = service.read_text(str(root / "not.txt"))
    assert result.text == "merhaba"
    assert result.truncated is False


def test_kok_disindaki_mutlak_yol_reddedilir(service: FileService, tmp_path: Path) -> None:
    outside = tmp_path / "disarida.txt"
    outside.write_text("gizli", encoding="utf-8")
    with pytest.raises(PermissionError):
        service.read_text(str(outside))


def test_nokta_nokta_kacisi_reddedilir(service: FileService, root: Path, tmp_path: Path) -> None:
    (tmp_path / "disarida.txt").write_text("gizli", encoding="utf-8")
    with pytest.raises(PermissionError):
        service.read_text(str(root / ".." / "disarida.txt"))


def _symlinks_supported() -> bool:
    """Windows needs Admin/Developer Mode to create symlinks; detect once so these
    escape-prevention tests SKIP (not FAIL) on an unprivileged box, while still running on
    POSIX and on privileged CI."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as _d:
        try:
            os.symlink(os.path.join(_d, "t"), os.path.join(_d, "l"))
            return True
        except (OSError, NotImplementedError):
            return False


_skip_no_symlink = pytest.mark.skipif(
    not _symlinks_supported(), reason="symlinks need Admin/Developer Mode on Windows"
)


@_skip_no_symlink
def test_symlink_kacisi_reddedilir(service: FileService, root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "disarida.txt"
    outside.write_text("gizli", encoding="utf-8")
    link = root / "masum_gorunen.txt"
    link.symlink_to(outside)
    with pytest.raises(PermissionError):
        service.read_text(str(link))


@_skip_no_symlink
def test_kok_icine_isaret_eden_symlink_serbest(service: FileService, root: Path) -> None:
    gercek = root / "gercek.txt"
    gercek.write_text("icerik", encoding="utf-8")
    link = root / "takma_ad.txt"
    link.symlink_to(gercek)
    assert service.read_text(str(link)).text == "icerik"


def test_yazim_da_kok_hapsinde(service: FileService, tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        service.write_text(str(tmp_path / "disarida.txt"), "x")


# -- empty roots = disabled (explicit error) -------------------------------------------


def test_bos_roots_her_islem_acik_hata(root: Path) -> None:
    svc = FileService(())
    assert svc.configured is False
    p = str(root / "x.txt")
    for cagri in (
        lambda: svc.read_text(p),
        lambda: svc.list_dir(str(root)),
        lambda: svc.stat(p),
        lambda: svc.write_text(p, "x"),
    ):
        with pytest.raises(FileEngineNotConfigured, match="AKANA_FILE_ROOTS"):
            cagri()


# -- read_text: max_bytes truncation -----------------------------------------------------


def test_max_bytes_kesme(service: FileService, root: Path) -> None:
    (root / "buyuk.txt").write_text("0123456789", encoding="utf-8")
    result = service.read_text(str(root / "buyuk.txt"), max_bytes=4)
    assert result.text == "0123"
    assert result.truncated is True
    assert result.size == 10


def test_olmayan_dosya_bulunamadi(service: FileService, root: Path) -> None:
    with pytest.raises(FileNotFoundError):
        service.read_text(str(root / "yok.txt"))


# -- list_dir / stat -------------------------------------------------------


def test_list_dir_derinlik_sinirli(service: FileService, root: Path) -> None:
    (root / "a").mkdir()
    (root / "a" / "derin.txt").write_text("x", encoding="utf-8")
    (root / "ust.txt").write_text("y", encoding="utf-8")
    seviye1 = {e["name"] for e in service.list_dir(str(root), depth=1)}
    assert seviye1 == {"a", "ust.txt"}
    seviye2 = {e["name"] for e in service.list_dir(str(root), depth=2)}
    assert "derin.txt" in seviye2


def test_stat_dosya_bilgisi(service: FileService, root: Path) -> None:
    (root / "n.txt").write_text("abc", encoding="utf-8")
    info = service.stat(str(root / "n.txt"))
    assert info["type"] == "file"
    assert info["size"] == 3
    with pytest.raises(FileNotFoundError):
        service.stat(str(root / "yok"))




# -- atomic write + backup ----------------------------------------------------------------


def test_yeni_dosya_yazimi_atomic_ve_backupsiz(service: FileService, root: Path) -> None:
    sonuc = service.write_text(str(root / "yeni.txt"), "ilk içerik")
    assert (root / "yeni.txt").read_text(encoding="utf-8") == "ilk içerik"
    assert sonuc["created"] is True
    assert sonuc["backup_path"] == ""
    assert sonuc["old_hash"] == ""
    # no leftover tmp file remains
    assert not list(root.glob("*.tmp"))


def test_ezme_oncesi_backup_alinir(service: FileService, root: Path, data_dir: Path) -> None:
    hedef = root / "var.txt"
    hedef.write_text("eski içerik", encoding="utf-8")
    sonuc = service.write_text(str(hedef), "yeni içerik")
    assert hedef.read_text(encoding="utf-8") == "yeni içerik"
    assert sonuc["created"] is False
    backup = Path(sonuc["backup_path"])
    assert backup.is_file()
    assert backup.parent == data_dir / "file_backups"
    assert backup.read_text(encoding="utf-8") == "eski içerik"
    assert sonuc["old_hash"] != "" and sonuc["old_hash"] != sonuc["new_hash"]


def test_boyut_sinirini_asan_dosyaya_backup_atlanir(
    service: FileService, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "MAX_BACKUP_BYTES", 4)
    hedef = root / "buyuk.txt"
    hedef.write_text("bu içerik sınırdan büyük", encoding="utf-8")
    sonuc = service.write_text(str(hedef), "yeni")
    assert hedef.read_text(encoding="utf-8") == "yeni"  # the write still proceeds
    assert sonuc["backup_path"] == ""


def test_dizin_uzerine_yazim_reddedilir(service: FileService, root: Path) -> None:
    (root / "dizin").mkdir()
    with pytest.raises(IsADirectoryError):
        service.write_text(str(root / "dizin"), "x")


# -- allow-all write (the old policy gate was removed) -------------------------------------


def test_yazim_her_zaman_serbest(
    service: FileService, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FULL AUTONOMY: writes are always allowed; policy_decision is always allow.
    sonuc = service.write_text(str(root / "serbest.txt"), "olur")
    assert sonuc["policy_decision"] == "allow"
    assert (root / "serbest.txt").read_text(encoding="utf-8") == "olur"


# -- operation log (db/files.db) -------------------------------------------------------------


def test_yazim_islem_loguna_hashlerle_duser(
    service: FileService, root: Path, data_dir: Path
) -> None:
    hedef = root / "log.txt"
    hedef.write_text("eski", encoding="utf-8")
    service.write_text(str(hedef), "yeni")
    oplog = FileOpLog.for_data_dir(data_dir)
    assert oplog.path == data_dir / "db" / "files.db"
    writes = [r for r in oplog.recent() if r["op"] == "write" and r["ok"]]
    assert writes
    kayit = writes[0]
    assert kayit["old_hash"] != "" and kayit["new_hash"] != ""
    assert kayit["old_hash"] != kayit["new_hash"]
    assert kayit["backup_path"] != ""


def test_okuma_islemleri_de_loglanir(service: FileService, root: Path, data_dir: Path) -> None:
    (root / "r.txt").write_text("x", encoding="utf-8")
    service.read_text(str(root / "r.txt"))
    service.list_dir(str(root))
    ops = {r["op"] for r in FileOpLog.for_data_dir(data_dir).recent()}
    assert {"read", "list"} <= ops


def test_data_dirsiz_serviste_log_sessizce_atlanir(root: Path) -> None:
    svc = FileService((root,), data_dir=None)
    sonuc = svc.write_text(str(root / "logsuz.txt"), "icerik")  # must not blow up
    assert sonuc["backup_path"] == ""  # no data_dir → backup is also skipped
