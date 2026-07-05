"""CLI entry: python akana.py <command>."""

from __future__ import annotations

import argparse
import subprocess
import sys


def build_parser() -> argparse.ArgumentParser:
    # Help strings go through i18n.t so `python akana.py`/`--help` honour AKANA_LANGUAGE
    # (main() calls i18n.set_lang before build_parser). argparse's own boilerplate
    # (usage:/positional arguments:/options:) stays English — localizing it would need
    # a custom HelpFormatter.
    from akana_cli import i18n

    p = argparse.ArgumentParser(
        prog="akana",
        description=i18n.t("cli.help.description"),
    )
    # Optional so a bare `python akana.py` prints help instead of an argparse error.
    sub = p.add_subparsers(dest="command", required=False)

    sp = sub.add_parser("setup", help=i18n.t("cli.help.setup"))
    sp.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help=i18n.t("cli.help.setup_yes"),
    )
    sp.add_argument(
        "--voice",
        choices=("none", "piper", "full", "xtts"),
        default=None,
        help=i18n.t("cli.help.setup_voice"),
    )
    sp.add_argument(
        "--repair",
        action="store_true",
        help=i18n.t("cli.help.setup_repair"),
    )
    sp.add_argument(
        "--lang",
        choices=("en", "tr"),
        default=None,
        help=i18n.t("cli.help.setup_lang"),
    )

    from akana_cli.components import REGISTRY

    ap = sub.add_parser(
        "add",
        help=i18n.t("cli.help.add"),
    )
    ap.add_argument(
        "component",
        nargs="?",
        default=None,
        choices=tuple(REGISTRY),
        help=i18n.t("cli.help.add_component") + ", ".join(REGISTRY),
    )

    sub.add_parser("smoke", help=i18n.t("cli.help.smoke"))
    sub.add_parser("start", help=i18n.t("cli.help.start"))
    sub.add_parser("stop", help=i18n.t("cli.help.stop"))
    dp = sub.add_parser("doctor", help=i18n.t("cli.help.doctor"))
    dp.add_argument(
        "--mcp",
        action="store_true",
        help=i18n.t("cli.help.doctor_mcp"),
    )
    sub.add_parser("test", help=i18n.t("cli.help.test"))
    ship = sub.add_parser("ship", help=i18n.t("cli.help.ship"))
    ship.add_argument(
        "--out-dir",
        type=str,
        default=None,
        help=i18n.t("cli.help.ship_out"),
    )
    sub.add_parser(
        "reset-memory",
        help=i18n.t("cli.help.reset_memory"),
    )

    return p


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    # WINDOWS (audit M1): a cp1252 console cannot encode the Unicode box/marker
    # characters (═ ▸ ✓ ⚠ ✗ ● ○) or Turkish output → print() blows up setup/doctor/stop
    # with a UnicodeEncodeError. Switch stdout/stderr to utf-8 (Py3.7+ reconfigure);
    # any remaining unencodable char is dropped via replace instead of crashing.
    if sys.platform == "win32":
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001 — silently skip if reconfigure is missing/fails
                pass
    # Bridge legacy AKANA_CURSOR_* names (shell/systemd) to their new AKANA_* counterparts.
    from akana_server.config import apply_legacy_env_aliases

    apply_legacy_env_aliases()

    # Speak the user's language across the whole CLI: read AKANA_LANGUAGE (shell env wins,
    # else .env). `setup` may override this via its own picker / --lang and record the choice.
    import os

    from akana_cli import i18n
    from akana_cli.env_util import EnvDecodeError, read_env_key

    # A non-UTF-8 .env (PS5.1 `>` = UTF-16, an ANSI editor = cp1254) must NOT crash the
    # very command meant to repair it. Read the language best-effort; if .env is undecodable,
    # print a clear one-liner (in the shell-env language if any) and bail cleanly.
    from akana_cli.paths import ENV_FILE

    try:
        _lang_from_env = read_env_key("AKANA_LANGUAGE")
    except EnvDecodeError as exc:
        i18n.set_lang(os.environ.get("AKANA_LANGUAGE") or "en")
        from akana_cli import io

        io.fail(i18n.t("env.not_utf8", error=str(exc), path=str(ENV_FILE)))
        return 1
    i18n.set_lang(os.environ.get("AKANA_LANGUAGE") or _lang_from_env or "en")

    parser = build_parser()
    args = parser.parse_args(argv)

    # A Ctrl+C anywhere in a command (the intro promises "Ctrl+C to abort") should be
    # a clean one-liner, not a Python traceback.
    try:
        if args.command == "setup":
            from akana_cli.setup_cmd import run_setup

            return run_setup(
                non_interactive=bool(args.yes),
                voice_mode=args.voice,
                repair=bool(args.repair),
                lang=args.lang,
            )
        if args.command == "add":
            from akana_cli.add_cmd import run_add

            return run_add(args.component)
        if args.command == "smoke":
            from akana_cli.smoke_cmd import run_smoke

            return run_smoke()
        if args.command == "start":
            from akana_cli.start_cmd import run_start

            return run_start()
        if args.command == "stop":
            from akana_cli.stop_cmd import run_stop

            return run_stop()
        if args.command == "doctor":
            from akana_cli.doctor import run_doctor

            return run_doctor(mcp=bool(args.mcp))
        if args.command == "test":
            from akana_cli.test_cmd import run_test

            return run_test()
        if args.command == "ship":
            from pathlib import Path

            from akana_cli.ship_cmd import run_ship

            out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else None
            return run_ship(out_dir)
        if args.command == "reset-memory":
            from akana_cli.reset_memory_cmd import run_reset_memory

            return run_reset_memory()
        # No subcommand given → show help cleanly (exit 0), not an argparse error.
        parser.print_help()
        return 0
    except KeyboardInterrupt:
        from akana_cli import i18n, io

        print()
        io.warn(i18n.t("io.cancelled"))
        return 130
    except subprocess.CalledProcessError as exc:
        # A child process (venv / pip / npm / server / pytest) exited non-zero. Surface a
        # clean one-liner with its exit code instead of a raw CalledProcessError traceback.
        from akana_cli import i18n, io

        io.fail(i18n.t("io.cmd_failed", code=exc.returncode))
        return exc.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
