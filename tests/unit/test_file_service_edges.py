"""FileEngine F0 — boundary-value tests (comprehensive quality turn).

Beyond the existing ``test_file_service.py``, this locks only EDGE cases:
unicode/whitespace/expanduser paths, empty-path rejection, ``max_bytes`` floor/huge
clamp, concurrent same-file atomic write, ``list_dir`` not descending a symlink-dir
+ the ``MAX_LIST_ENTRIES`` clamp, oplog hash integrity + the ``recent`` limit clamp.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

import akana_server.files.service as service_module
from akana_server.files.oplog import FileOpLog, reset_file_oplogs
from akana_server.files.service import (
    MAX_READ_BYTES,
    FileService,
)


@pytest.fixture(autouse=True)
def _isolated():
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


# -- path normalization boundaries ----------------------------------------------------


def test_bos_yol_value_error(service: FileService) -> None:
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="cannot be empty"):
            service.read_text(bad)


def test_unicode_ve_bosluklu_dosya_adi(service: FileService, root: Path) -> None:
    ad = root / "şükrü dosya çğöü.txt"
    ad.write_text("içerik 🚀", encoding="utf-8")
    res = service.read_text(str(ad))
    assert res.text == "içerik 🚀"
    assert res.size == len("içerik 🚀".encode("utf-8"))


def test_expanduser_kok_disina_cikamaz(service: FileService) -> None:
    # ``~`` expands to the home directory; if it is not an allowlist root it is rejected.
    with pytest.raises(PermissionError):
        service.read_text("~/kesinlikle_yok_olmali_12345.txt")


def test_kok_prefix_benzeri_kardes_dizin_reddedilir(
    tmp_path: Path, data_dir: Path
) -> None:
    # /x/root and /x/root_yan are different roots — prefix matching must not leak.
    base = tmp_path / "x"
    base.mkdir()
    kok = base / "root"
    kok.mkdir()
    yan = base / "root_yan"
    yan.mkdir()
    (yan / "gizli.txt").write_text("sızma", encoding="utf-8")
    svc = FileService((kok,), data_dir=data_dir)
    with pytest.raises(PermissionError):
        svc.read_text(str(yan / "gizli.txt"))


# -- max_bytes floor/ceiling clamp ----------------------------------------------------


def test_max_bytes_taban_en_az_bir(service: FileService, root: Path) -> None:
    (root / "z.txt").write_text("abcdef", encoding="utf-8")
    # max_bytes=0 → at least 1 byte is read (limit = max(1, ...)).
    res = service.read_text(str(root / "z.txt"), max_bytes=0)
    assert res.text == "a"
    assert res.truncated is True
    # a negative value is also clamped to the floor.
    assert service.read_text(str(root / "z.txt"), max_bytes=-99).text == "a"


def test_max_bytes_tavan_kelepcesi(service: FileService, root: Path) -> None:
    (root / "z.txt").write_text("kısa", encoding="utf-8")
    # a huge max_bytes drops to MAX_READ_BYTES but the small file is read in full.
    res = service.read_text(str(root / "z.txt"), max_bytes=MAX_READ_BYTES * 100)
    assert res.text == "kısa"
    assert res.truncated is False


def test_max_bytes_tam_dosya_boyutu_kesilmez(service: FileService, root: Path) -> None:
    (root / "tam.txt").write_text("0123456789", encoding="utf-8")
    res = service.read_text(str(root / "tam.txt"), max_bytes=10)
    assert res.text == "0123456789"
    assert res.truncated is False  # size == limit → not considered truncated


# -- concurrent same-file write (atomic guarantee) ------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX os.replace tolerates concurrent atomic replace of the same target; "
    "Windows raises PermissionError (sharing violation) when the target is open by "
    "another thread mid-replace",
)
def test_eszamanli_ayni_dosya_yazimi_tutarli(service: FileService, root: Path) -> None:
    hedef = root / "yaris.txt"
    contents = [f"içerik-{i}-" + "x" * 2000 for i in range(8)]
    errors: list[Exception] = []

    def yaz(c: str) -> None:
        try:
            service.write_text(str(hedef), c)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=yaz, args=(c,)) for c in contents]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # os.replace is atomic: the final content is ALWAYS one of those written (no partial mix).
    final = hedef.read_text(encoding="utf-8")
    assert final in contents
    # no tmp leftover remains.
    assert not list(root.glob(".*tmp")) and not list(root.glob("*.tmp"))


# -- list_dir boundaries --------------------------------------------------------------


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
def test_list_dir_symlink_dizine_inmez(
    service: FileService, root: Path, tmp_path: Path
) -> None:
    disari = tmp_path / "disari"
    disari.mkdir()
    (disari / "gizli.txt").write_text("x", encoding="utf-8")
    (root / "link_dizin").symlink_to(disari, target_is_directory=True)
    entries = service.list_dir(str(root), depth=3)
    names = {e["name"] for e in entries}
    # the link entry is visible (of type symlink) but is not descended INTO → no gizli.txt.
    assert "link_dizin" in names
    assert "gizli.txt" not in names
    link_entry = next(e for e in entries if e["name"] == "link_dizin")
    assert link_entry["type"] == "symlink"


def _make_junction(link: Path, target: Path) -> bool:
    """Create a Windows directory junction link→target (no admin needed).

    Returns False when not on Windows or mklink is unavailable so the test skips
    rather than fails.
    """
    if sys.platform != "win32":
        return False
    import subprocess

    try:
        res = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return res.returncode == 0 and link.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")
def test_list_dir_junction_dizine_inmez(
    service: FileService, root: Path, tmp_path: Path
) -> None:
    """A Windows directory junction (is_symlink()==False, is_dir()==True) inside
    a root must NOT be descended: pre-fix _entry_type classified it 'dir' and
    _walk recursed with no per-child allowlist re-check, leaking the names/sizes
    of files that physically live OUTSIDE the allowlist over list_dir."""
    disari = tmp_path / "disari_junction"
    disari.mkdir()
    (disari / "secret.txt").write_text("classified", encoding="utf-8")
    jump = root / "jump"
    if not _make_junction(jump, disari):
        pytest.skip("mklink /J unavailable in this environment")

    entries = service.list_dir(str(root), depth=3)
    names = {e["name"] for e in entries}

    # The junction itself is visible (reported as a symlink) but NOT descended.
    assert "jump" in names
    assert "secret.txt" not in names
    jump_entry = next(e for e in entries if e["name"] == "jump")
    assert jump_entry["type"] == "symlink"
    # And no entry path leaks a child under the junction.
    assert not any("secret.txt" in e["path"] for e in entries)


def test_list_dir_max_entries_kelepcesi(
    service: FileService, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "MAX_LIST_ENTRIES", 5)
    for i in range(20):
        (root / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
    entries = service.list_dir(str(root), depth=1)
    assert len(entries) == 5  # stops at the upper bound


def test_list_dir_depth_kelepcesi(
    service: FileService, root: Path
) -> None:
    # depth=0 → at least 1; a very large depth drops to MAX_LIST_DEPTH.
    (root / "a").mkdir()
    (root / "a" / "x.txt").write_text("x", encoding="utf-8")
    seviye0 = {e["name"] for e in service.list_dir(str(root), depth=0)}
    assert seviye0 == {"a"}  # depth clamped to the floor (1)


def test_list_dir_dosyaya_isaret_edilince_hata(service: FileService, root: Path) -> None:
    (root / "tek.txt").write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        service.list_dir(str(root / "tek.txt"))


# -- oplog integrity ------------------------------------------------------------------


def test_oplog_recent_limit_kelepcesi(data_dir: Path) -> None:
    oplog = FileOpLog.for_data_dir(data_dir)
    for i in range(10):
        oplog.append(op="read", path=f"/x/{i}")
    assert len(oplog.recent(limit=3)) == 3
    assert len(oplog.recent(limit=0)) == 1  # floor 1
    assert len(oplog.recent(limit=99999)) == 10  # ceiling 500, 10 records
    assert oplog.count() == 10


def test_oplog_append_only_sira_korunur(data_dir: Path) -> None:
    oplog = FileOpLog.for_data_dir(data_dir)
    for i in range(5):
        oplog.append(op="write", path=f"/p/{i}", new_hash=f"h{i}")
    rows = oplog.recent()
    # newest first by strict insertion order (rowid DESC); ULID id is NOT monotonic
    # within a millisecond, so same-ms appends must still come back h4..h0.
    assert [r["new_hash"] for r in rows] == ["h4", "h3", "h2", "h1", "h0"]


def test_oplog_data_dir_yoksa_none(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from akana_server.files.oplog import get_file_oplog

    assert get_file_oplog(SimpleNamespace(data_dir=None)) is None
