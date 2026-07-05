#!/usr/bin/env bash
# Akana bootstrap (Linux/macOS) — the friendly first run.
#
# Finds a Python 3.11+ interpreter (telling you how to install one if it's
# missing), then hands off to the real setup wizard. Any flags you pass are
# forwarded, so `./install.sh --yes` or `./install.sh --repair` work too.
#
#   ./install.sh            # interactive setup
#   ./install.sh --yes      # unattended (CI)
#   ./install.sh --repair   # rebuild a broken virtual environment
#   ./install.sh --lang tr  # Turkish (also forwarded to the wizard)
set -euo pipefail

cd "$(dirname "$0")"

# Language for the bootstrap's OWN messages. A language isn't chosen yet, so: honor an
# explicit --lang (also forwarded to the wizard), otherwise show BOTH languages — the
# same "show both before a choice" rule the wizard's language picker uses.
LANG_SEL=""
_want_lang=0
for _a in "$@"; do
    case "$_a" in
    --lang) _want_lang=1 ;;
    --lang=*) LANG_SEL="${_a#--lang=}" ;;
    *) if [ "$_want_lang" = "1" ]; then LANG_SEL="$_a"; _want_lang=0; fi ;;
    esac
done
LANG_SEL="$(printf '%s' "$LANG_SEL" | tr '[:upper:]' '[:lower:]')"

say() { # say "EN" "TR"
    case "$LANG_SEL" in
    tr) printf '%s\n' "$2" ;;
    en) printf '%s\n' "$1" ;;
    *)
        printf '%s\n' "$1"
        printf '%s\n' "$2"
        ;;
    esac
}

find_python() {
    # Newest first, then generic names; print the first that is >= 3.11.
    # The version check (not a hardcoded ceiling) is what qualifies a candidate,
    # so a machine with only python3.14+ is found and a future 3.15 needs no edit.
    for c in python3.14 python3.13 python3.12 python3.11 python3 python; do
        if command -v "$c" >/dev/null 2>&1 &&
            "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' >/dev/null 2>&1; then
            command -v "$c"
            return 0
        fi
    done
    return 1
}

print_python_help() {
    echo
    say 'Akana needs Python 3.11 or newer, but it was not found on your PATH.' 'Akana için Python 3.11 veya üstü gerekli, ancak PATH'\''te bulunamadı.'
    echo
    case "$(uname -s)" in
    Linux)
        if command -v apt >/dev/null 2>&1; then
            echo "  Debian/Ubuntu:  sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
        elif command -v dnf >/dev/null 2>&1; then
            echo "  Fedora/RHEL:    sudo dnf install -y python3 python3-pip"
        elif command -v pacman >/dev/null 2>&1; then
            echo "  Arch:           sudo pacman -S --needed python python-pip"
        elif command -v zypper >/dev/null 2>&1; then
            echo "  openSUSE:       sudo zypper install -y python3 python3-pip"
        else
            echo "  Install Python 3.11+ with your distro's package manager."
        fi
        ;;
    Darwin)
        echo "  macOS (Homebrew):  brew install python@3.12"
        echo "  …or download from  https://www.python.org/downloads/"
        ;;
    *)
        echo "  Download Python 3.11+ from https://www.python.org/downloads/"
        ;;
    esac
    echo
    say "Then re-run:  ./install.sh" "Sonra tekrar çalıştır:  ./install.sh"
}

PY="$(find_python || true)"
if [ -z "${PY:-}" ]; then
    print_python_help
    exit 1
fi

say "Using Python: $PY" "Python kullanılıyor: $PY"
exec "$PY" akana.py setup "$@"
