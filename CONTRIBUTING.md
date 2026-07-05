# Contributing to Akana

Thanks for your interest in improving Akana — issues and pull requests are welcome. This
guide covers the essentials.

## Development setup

Akana is **clone-and-run** (not a pip-installable package). You need Python **3.11+** (and
Node.js **18+** only if you work on the Cursor provider).

```bash
git clone https://github.com/MuhittinYilmazer/akana.git
cd akana
python akana.py setup      # venv + core deps + your provider (asks for a language first)
python akana.py start      # run the server + web UI → http://127.0.0.1:8766
```

Handy commands:

```bash
python akana.py test       # full test suite (pytest)
python akana.py smoke      # quick pre-flight (doctor + a fast test subset)
python akana.py doctor     # environment checks (add --mcp for stdio-child health)
```

## Before you open a pull request

- **Run the tests** — `python akana.py test` must pass. CI runs the full suite on Linux,
  macOS, and Windows, so keep changes cross-platform: guard OS-specific paths, and let a
  test that needs a platform or privilege skip cleanly rather than fail.
- **Keep it focused** — one logical change per PR, and say *why* in the description.
- **Match the surrounding code** — its naming, structure, and comment density. New
  user-facing behavior should come with a test.

## Code style

- **Write code and comments in English.** Akana is English-first; the Turkish you may see in
  the UI is the optional *interface* language, delivered through the i18n string tables — not
  developer comments.
- **User-facing strings are bilingual (en + tr).** If you add text the user sees:
  - **Web UI** → add the key to the `web_ui/static/akana-i18n-strings*.js` tables (both `en`
    and `tr`) and render it with `AkanaI18n.t(...)`.
  - **CLI / setup** → add the key to [`akana_cli/i18n.py`](akana_cli/i18n.py) (both `en` and
    `tr`) and use `i18n.t(...)`. `tests/unit/test_cli_i18n.py` guards that every key used in
    the CLI is defined and bilingual.
- **Keep files reasonably small.** An architecture test caps module size (see
  `tests/architecture/test_repo_boundaries.py`) — split a file by responsibility before it
  grows past the ceiling.
- **Never commit secrets.** Keys go in the encrypted vault (Settings → Identity) or an
  untracked `.env`, never into the tree. `.env` is gitignored; `.env.example` documents every
  setting.

## Reporting bugs / requesting features

Use the issue templates (**Bug report** / **Feature request**). For **security** issues,
follow [SECURITY.md](SECURITY.md) — please do not open a public issue.

By contributing, you agree that your contributions are licensed under the project's
[MIT License](LICENSE).
