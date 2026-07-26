"""What the USER is left with when a CLI command goes wrong.

Every command covered here is a tool of last resort or a first-run gate, so the
contract under test is about the aftermath, not the return value:

  • a failed ``restore`` must leave the data dir it started with — never a
    half-written mix the server boots on without complaint;
  • a failed ``backup`` must never destroy the previous good archive;
  • a green doctor line must be backed by a real probe of the thing it claims;
  • ``setup`` must record the provider where the SERVER reads it;
  • ``--repair`` must not report success after a half-deleted venv.
"""

from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest

from akana_cli import backup_cmd, i18n


@pytest.fixture(autouse=True)
def _english():
    """Assertions below quote i18n keys, but the fixed strings are read by humans in
    whatever language the session left behind — pin it so failures read clearly."""
    prev = i18n.get_lang()
    i18n.set_lang("en")
    yield
    i18n.set_lang(prev)


@pytest.fixture(autouse=True)
def _no_server(monkeypatch):
    monkeypatch.setattr(backup_cmd, "_server_might_be_running", lambda: False)


def _seed_data_dir(d: Path) -> Path:
    (d / "db").mkdir(parents=True)
    con = sqlite3.connect(str(d / "db" / "memory.db"))
    con.execute("CREATE TABLE facts (id INTEGER, v TEXT)")
    con.execute("INSERT INTO facts VALUES (1, 'from-the-archive')")
    con.commit()
    con.close()
    (d / "runtime_settings.json").write_text('{"language": "en"}', encoding="utf-8")
    (d / "secrets.json").write_text("vault1:ciphertext", encoding="utf-8")
    return d


def _make_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    src = _seed_data_dir(tmp_path / "src")
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    out = tmp_path / "archives"
    assert backup_cmd.run_backup(out) == 0
    return next(out.glob("*.tar.gz"))


def _live_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data dir with the user's own irreplaceable state, set as the restore target."""
    dst = tmp_path / "live"
    (dst / "db").mkdir(parents=True)
    (dst / "my_notes.json").write_text("irreplaceable", encoding="utf-8")
    monkeypatch.setenv("AKANA_DATA_DIR", str(dst))
    return dst


# ── restore: a failure must not leave a half-written data dir ────────────────
def test_failed_force_restore_leaves_the_live_data_dir_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The extract→data-dir hand-off degrades to copytree across filesystems (system
    temp on C:, data dir elsewhere). If it dies partway — ENOSPC, a long path, Ctrl+C —
    the user must still have the data dir they started with, not a prefix of the archive
    with their real data stranded in an unadvertised .pre-restore-* dir."""
    archive = _make_archive(tmp_path, monkeypatch)
    dst = _live_data_dir(tmp_path, monkeypatch)

    def _half_move(src: str, dest: str, *a, **k):  # noqa: ANN002, ANN003
        target = Path(dest)
        (target / "db").mkdir(parents=True, exist_ok=True)
        (target / "db" / "memory.db").write_bytes(b"partial")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backup_cmd.shutil, "move", _half_move)

    rc = backup_cmd.run_restore(archive, force=True)
    out = capsys.readouterr().out

    assert rc == 1
    assert (dst / "my_notes.json").is_file(), (
        f"a failed restore destroyed the live data dir; left behind: "
        f"{sorted(p.name for p in dst.rglob('*'))}\n{out}"
    )
    assert (dst / "my_notes.json").read_text(encoding="utf-8") == "irreplaceable"
    assert not list(tmp_path.glob("live.pre-restore-*")), (
        "the old data dir was stranded aside even though the restore never completed"
    )
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("live.restoring")], (
        "a partial staging tree was left next to the data dir"
    )


def test_restore_rolls_back_when_the_swap_into_place_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Last line of defence: if the final same-filesystem swap fails after the old dir
    was moved aside, the old dir must be put BACK — the user's data may not depend on
    them noticing a .pre-restore-* directory."""
    archive = _make_archive(tmp_path, monkeypatch)
    dst = _live_data_dir(tmp_path, monkeypatch)

    real_rename = Path.rename

    def _boom(self: Path, target):  # noqa: ANN001, ANN202
        if ".restoring" in self.name:
            raise OSError(13, "Permission denied")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", _boom)

    rc = backup_cmd.run_restore(archive, force=True)
    out = capsys.readouterr().out

    assert rc == 1, f"the swap was never staged, so there was nothing to roll back\n{out}"
    assert (dst / "my_notes.json").read_text(encoding="utf-8") == "irreplaceable", (
        f"the old data dir was not rolled back\n{out}"
    )


def test_restore_failure_message_names_the_aside_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When rollback itself is impossible, the user must be told WHERE their data is —
    'Restore failed: <errno>' alone leaves them with no next step."""
    archive = _make_archive(tmp_path, monkeypatch)
    _live_data_dir(tmp_path, monkeypatch)

    real_rename = Path.rename

    def _boom(self: Path, target):  # noqa: ANN001, ANN202
        # the swap fails AND the aside dir refuses to move back (a held handle)
        if ".restoring" in self.name or ".pre-restore-" in self.name:
            raise OSError(13, "Permission denied")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", _boom)

    rc = backup_cmd.run_restore(archive, force=True)
    out = capsys.readouterr().out

    assert rc == 1
    aside = [p for p in tmp_path.iterdir() if p.name.startswith("live.pre-restore-")]
    assert aside, f"the old data dir was never moved aside\n{out}"
    fail_lines = [ln for ln in out.splitlines() if "✗" in ln]
    assert any(aside[0].name in ln for ln in fail_lines), (
        f"the FAILURE message does not name where the user's data went:\n{out}"
    )


# ── backup: never destroy the previous good archive ──────────────────────────
def test_failed_backup_keeps_the_previous_good_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nightly `akana backup D:/backups/akana.tar.gz` overwrites one filename. The night
    the backup fails must not also be the night last night's archive disappears."""
    src = _seed_data_dir(tmp_path / "src")
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    target = tmp_path / "nightly" / "akana.tar.gz"
    assert backup_cmd.run_backup(target) == 0
    good = target.read_bytes()

    def _boom(path: Path) -> str:
        raise OSError(5, "transient read failure")

    monkeypatch.setattr(backup_cmd, "_sha256", _boom)
    assert backup_cmd.run_backup(target) == 1

    assert target.is_file(), "the failed run destroyed the previous good archive"
    assert target.read_bytes() == good, "the previous good archive was truncated"
    with tarfile.open(target, "r:gz") as tar:  # still a readable archive
        assert "akana-data/manifest.json" in {m.name for m in tar.getmembers()}
    assert not [p for p in target.parent.iterdir() if ".partial" in p.name], (
        "a half-written partial archive was left behind"
    )


def test_backup_partial_is_replaced_not_appended_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The success path must still end with exactly the named file (no *.partial-*)."""
    src = _seed_data_dir(tmp_path / "src")
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    target = tmp_path / "nightly" / "akana.tar.gz"
    assert backup_cmd.run_backup(target) == 0
    assert backup_cmd.run_backup(target) == 0  # overwrite the same name
    assert [p.name for p in target.parent.iterdir()] == ["akana.tar.gz"]


# ── backup: don't claim ciphertext without looking ───────────────────────────
def test_backup_does_not_claim_ciphertext_for_a_plaintext_secrets_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """secret_store still reads (and ships) legacy PLAINTEXT secrets.json. Telling that
    user 'Secrets are encrypted … safe to store off-machine' is how live API keys end up
    in cloud storage."""
    src = _seed_data_dir(tmp_path / "src")
    (src / "secrets.json").write_text('{"openai_api_key": "sk-live-real"}', encoding="utf-8")
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    assert backup_cmd.run_backup(tmp_path / "b") == 0
    out = capsys.readouterr().out

    assert i18n.t("backup.ciphertext_note") not in out, (
        f"backup claimed the archive is ciphertext without checking:\n{out}"
    )
    assert "secrets.json" in out, f"the plaintext secrets were not called out:\n{out}"


def test_backup_keeps_the_ciphertext_note_when_secrets_are_encrypted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src = _seed_data_dir(tmp_path / "src")  # secrets.json is 'vault1:…'
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    assert backup_cmd.run_backup(tmp_path / "b") == 0
    assert i18n.t("backup.ciphertext_note") in capsys.readouterr().out


def test_backup_warns_when_the_active_vault_key_is_not_the_bundled_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--include-vault-key bundles default_keyfile() blindly. On a machine whose ACTIVE
    key comes from AKANA_VAULT_KEYFILE/AKANA_VAULT_KEY, a stale default vault.key gets
    shipped instead — the cross-machine restore this flag exists for then decrypts
    nothing, discovered after the original machine is gone."""
    from akana_server import vault_crypto

    src = _seed_data_dir(tmp_path / "src")
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    stale = tmp_path / "config" / "vault.key"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale-default-key")
    monkeypatch.setattr(vault_crypto, "default_keyfile", lambda: stale)
    active = tmp_path / "elsewhere" / "vault.key"
    active.parent.mkdir(parents=True)
    active.write_bytes(b"the-real-key")
    monkeypatch.setenv("AKANA_VAULT_KEYFILE", str(active))

    assert backup_cmd.run_backup(tmp_path / "b", include_vault_key=True) == 0
    out = capsys.readouterr().out
    assert "AKANA_VAULT_KEYFILE" in out, (
        f"the archive bundled the default keyfile while a different key is active, "
        f"with no warning:\n{out}"
    )


def test_restore_warns_when_the_written_key_will_not_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mirror of the backup-side blindness: _restore_vault_key always writes to
    default_keyfile(), so on a target machine configured with AKANA_VAULT_KEYFILE the
    key lands where the vault never looks — every secret stays undecryptable behind a
    reassuring 'Master key restored to …'."""
    from akana_server import vault_crypto

    src = _seed_data_dir(tmp_path / "src")
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    keyfile = tmp_path / "config" / "vault.key"
    keyfile.parent.mkdir(parents=True)
    keyfile.write_bytes(b"the-key")
    monkeypatch.setattr(vault_crypto, "default_keyfile", lambda: keyfile)
    monkeypatch.delenv("AKANA_VAULT_KEYFILE", raising=False)
    assert backup_cmd.run_backup(tmp_path / "b", include_vault_key=True) == 0
    archive = next((tmp_path / "b").glob("*.tar.gz"))

    # the TARGET machine resolves its key elsewhere
    monkeypatch.setenv("AKANA_VAULT_KEYFILE", str(tmp_path / "elsewhere" / "vault.key"))
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "restored"))
    capsys.readouterr()
    assert backup_cmd.run_restore(archive) == 0
    out = capsys.readouterr().out
    assert "AKANA_VAULT_KEYFILE" in out, (
        f"'Master key restored' was printed for a key nothing will read:\n{out}"
    )


# ── restore: forward-compat on the manifest format ───────────────────────────
def test_restore_refuses_a_newer_archive_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """akana_backup_format exists to enable exactly this refusal; today it is written
    and never read, so a future format-2 archive would restore under format-1 rules."""
    import json

    archive = _make_archive(tmp_path, monkeypatch)
    unpacked = tmp_path / "u"
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(unpacked)
    man = unpacked / "akana-data" / "manifest.json"
    raw = json.loads(man.read_text(encoding="utf-8"))
    raw["akana_backup_format"] = 2
    man.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
    future = tmp_path / "future.tar.gz"
    with tarfile.open(future, "w:gz") as tar:
        tar.add(unpacked / "akana-data", arcname="akana-data")

    dst = tmp_path / "restored"
    monkeypatch.setenv("AKANA_DATA_DIR", str(dst))
    assert backup_cmd.run_restore(future) == 1
    assert not dst.exists(), "a future-format archive was restored under format-1 rules"


# ── doctor: a green line must be backed by a probe ───────────────────────────
def _doctor_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: str):
    from akana_cli import doctor

    env = tmp_path / ".env"
    env.write_text(f"LLM_PROVIDER={provider}\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "ENV_FILE", env)
    monkeypatch.setattr(doctor, "_resolved_provider", lambda: provider)
    monkeypatch.setattr(doctor, "find_system_python", lambda: "python")
    monkeypatch.setattr(doctor, "venv_exists", lambda: True)
    # a non-existent interpreter makes the optional-module probes fail fast (OSError,
    # already swallowed) instead of spawning five real subprocesses.
    monkeypatch.setattr(doctor, "venv_python", lambda: tmp_path / "no-such-python")
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AKANA_HOST", "127.0.0.1")
    monkeypatch.setenv("AKANA_PORT", "8931")
    return doctor


def test_doctor_fails_when_the_ollama_provider_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """'Ollama — local models, no key' is the most attractive row for a first-timer.
    If nothing is installed, doctor and smoke must say so — a green 'Looks ready'
    leaves the user with a 503 on every chat and no next step."""
    doctor = _doctor_sandbox(monkeypatch, tmp_path, "ollama")
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)

    rc = doctor.run_doctor(verbose=True, probe_network=False)
    out = capsys.readouterr().out

    assert rc == 1, f"doctor exited 0 with no ollama on the machine:\n{out}"
    assert i18n.t("doctor.ready") not in out, f"doctor reported ready:\n{out}"
    assert "ollama" in out.lower()


def test_doctor_passes_when_ollama_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    doctor = _doctor_sandbox(monkeypatch, tmp_path, "ollama")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    rc = doctor.run_doctor(verbose=True, probe_network=False)
    assert rc == 0, capsys.readouterr().out


def test_picking_an_absent_ollama_is_not_reported_as_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`external` installers only PRINT a hint, so install_component returned True for a
    machine with no Ollama — and _select_and_install then made it LLM_PROVIDER."""
    from akana_cli import components
    from akana_cli.add_cmd import install_component

    monkeypatch.setattr(components.shutil, "which", lambda _name: None)
    assert install_component(components.REGISTRY["ollama"], interactive=False) is False


# ── setup: record the provider where the server reads it ─────────────────────
def test_setup_records_the_provider_where_the_server_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolve_provider is `llm.provider or settings.llm_provider` — the persisted store
    WINS over .env. Writing only .env made setup print '✓ Active provider: gemini' while
    every chat kept going to cursor."""
    from akana_cli import setup_cmd
    from akana_server.config import load_settings
    from akana_server.llm_settings import load_llm_settings, resolve_provider

    data = tmp_path / "data"
    data.mkdir()
    (data / "llm_settings.json").write_text('{"provider": "cursor"}', encoding="utf-8")
    monkeypatch.setenv("AKANA_DATA_DIR", str(data))
    monkeypatch.setattr(setup_cmd, "ENV_FILE", tmp_path / ".env")

    setup_cmd._configure_after_install(["gemini"])

    settings = load_settings()
    assert resolve_provider(settings, load_llm_settings(data, settings)) == "gemini", (
        "setup said the provider was switched; the server still resolves the old one"
    )


# ── setup --repair: never fall through into 'venv present' ───────────────────
def test_repair_venv_failure_stops_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--repair exists for an already-broken venv. A partially-deleted tree that still
    has Scripts/python.exe reads as 'venv present', so setup skips the rebuild, pip-
    installs into the wreck and prints 'Setup complete'."""
    from akana_cli import setup_cmd

    venv = tmp_path / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(setup_cmd, "VENV_DIR", venv)

    def _boom(path, *a, **k):  # noqa: ANN001, ANN002, ANN003
        raise OSError(13, "The process cannot access the file: it is in use")

    monkeypatch.setattr(setup_cmd.shutil, "rmtree", _boom)

    with pytest.raises(SystemExit) as exc:
        setup_cmd._repair_venv()
    assert exc.value.code != 0
    assert "venv" in capsys.readouterr().out.lower()


# ── the Windows guard that makes a redirected/scheduled run possible ─────────
def test_cli_survives_a_non_utf8_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`akana backup >> backup.log` on Windows gives Python an ANSI-codepage stdout that
    cannot encode '▸'/'✓'. main() reconfigures the stream before any command runs; this
    locks that guard down — without it every unattended nightly backup is a traceback."""
    import io as _pyio
    import sys

    from akana_cli.main import main

    src = _seed_data_dir(tmp_path / "src")
    monkeypatch.setenv("AKANA_DATA_DIR", str(src))
    monkeypatch.setattr(sys, "platform", "win32")
    buf = _pyio.TextIOWrapper(_pyio.BytesIO(), encoding="cp1254", errors="strict")
    monkeypatch.setattr(sys, "stdout", buf)

    rc = main(["backup", "--out", str(tmp_path / "b")])

    assert rc == 0
    assert next((tmp_path / "b").glob("*.tar.gz"), None) is not None
