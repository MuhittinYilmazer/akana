"""Permanently delete the Inbox, learned facts, the memory graph and the vector index.

NOT a cache flush: ``SemanticStore.clear_all`` is ``DELETE FROM facts`` — everything
Akana has learned about the user, unrecoverable without a prior ``akana backup``. Only
the conversation archive and episodic.db survive. Hence the confirmation gate below.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from akana_cli import i18n, io
from akana_cli.env_util import load_repo_dotenv
from akana_cli.paths import default_data_dir, expand_user_path

# Since K11, staging/semantic/graph/vector all share one file,
# `<data_dir>/db/memory.db`, alongside conversations/episodic (which reset
# must NOT touch) — so resetting means clearing specific tables via each
# store's own clear()/clear_all(), not deleting legacy per-store files that
# current code never creates.
#
# `import akana.memory.*` (inside run_reset_memory) resolves to src/akana via the
# central bootstrap wired into akana.py (the launcher entry point). Before that
# bootstrap existed this command raised ModuleNotFoundError, which a blanket
# `except Exception` swallowed into a warn line — so it claimed success while
# clearing nothing on every machine. The import error is now impossible on the
# supported path AND no longer swallowed (see run_reset_memory).


def _resolve_data_dir() -> Path:
    load_repo_dotenv()
    env = os.environ.get("AKANA_DATA_DIR", "").strip()
    if env:
        return expand_user_path(env)
    return default_data_dir()


def _server_might_be_running() -> bool:
    """Cheap check: warn if uvicorn for our app appears in process list."""
    try:
        from akana_cli.stop_cmd import find_pids_on_port

        from akana_cli.env_util import server_host_port

        host, port = server_host_port()
        return bool(find_pids_on_port(port, host))
    except Exception:
        return False


def _stdin_is_interactive() -> bool:
    """True only when a human can actually answer a prompt.

    A scheduled run, a pipeline, or a test has no terminal — prompting there would
    either hang or raise, so the caller refuses with a --yes hint instead.
    """
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError):  # detached/closed stream
        return False


def run_reset_memory(*, assume_yes: bool = False) -> int:
    data_dir = _resolve_data_dir()
    io.step(i18n.t("reset.resetting", path=data_dir))
    print("  " + i18n.t("reset.preserved_note"))

    if _server_might_be_running():
        io.warn(i18n.t("reset.server_running"))

    db_path = data_dir / "db" / "memory.db"
    if not db_path.exists():
        io.ok(i18n.t("reset.nothing"))
        print("  " + i18n.t("reset.restart_hint"))
        print("  " + i18n.t("reset.browser_hint"))
        return 0

    # Ask BEFORE destroying anything. The sibling destructive command (`restore`)
    # already refuses an occupied data dir without --force; this one deletes strictly
    # more (every learned fact, the graph, the Inbox) and used to do it on one keystroke.
    # The check sits AFTER the "nothing to reset" exit so a no-op never prompts.
    if not assume_yes:
        if not _stdin_is_interactive():
            io.fail(i18n.t("reset.needs_yes"))
            return 1
        if not io.ask_yes_no(i18n.t("reset.confirm", path=data_dir), default=False):
            io.warn(i18n.t("reset.aborted"))
            return 0

    # Import at call time (not module scope) so `python akana.py --help` and the
    # other subcommands never pay for the memory stack. This import MUST NOT be
    # wrapped in a swallowing except: if `akana.memory` fails to resolve, that is
    # the exact "dead on arrival" bug this command used to hide — let it surface.
    from akana.memory.graph import GraphStore
    from akana.memory.semantic import SemanticStore
    from akana.memory.staging import StagingStore
    from akana.memory.vector import VectorStore

    # Only genuine data-access failures (a locked DB while the server runs, a
    # filesystem error) are caught here — and they are reported as a FAILURE with
    # a non-zero exit, never as a successful/no-op reset. A narrow (sqlite3.Error,
    # OSError) keeps programming errors (AttributeError, TypeError) loud.
    try:
        removed = StagingStore.for_data_dir(data_dir).clear()
        removed += SemanticStore.for_data_dir(data_dir).clear_all()
        removed += VectorStore.for_data_dir(data_dir).clear()
        GraphStore.for_data_dir(data_dir).clear_all()
        removed += 1  # graph.clear_all() has no return value; count it as one action
    except (sqlite3.Error, OSError) as exc:
        io.fail(i18n.t("reset.db_failed", exc=exc))
        print("  " + i18n.t("reset.db_failed_hint"))
        return 1

    io.ok(i18n.t("reset.cleared", path=db_path))
    print("  " + i18n.t("reset.restart_hint"))
    print("  " + i18n.t("reset.browser_hint"))
    return 0
