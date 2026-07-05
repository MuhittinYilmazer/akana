"""FileEngine F0 — Akana's OWN policy-governed file tools.

File operations were previously provider-side only (Cursor/claude used their own
tools); in paths Akana runs itself (ReAct toolbox, workflow steps, future automations)
there was no file capability. This module fills that gap with root-allowlist discipline
(``tasks/project_checks`` pattern):

* :class:`FileService` — safe ``read_text / list_dir / stat / write_text``
  within the ``AKANA_FILE_ROOTS`` allowlist; every path goes through a root check AFTER
  resolve (symlink/``..`` escape → ``PermissionError``).
  Empty allowlist = disabled (every operation raises an explicit :class:`FileEngineNotConfigured`).
* Writes are always permitted (FULL AUTONOMY decision — the old risk/approval
  gate is removed; ``write_text`` reports a fixed ``policy_decision: "allow"``).
* :class:`FileOpLog` — append-only operation ledger ``<data_dir>/db/files.db``;
  writes store the old+new content hash and backup path (undo F1 foundation).

Workflow contract (note for F1): the workflow step type is DELIBERATELY absent in F0;
in F1 ``file_read``/``file_write`` steps will call this service — step input will be
``{path, content?, max_bytes?}`` and FileService signatures are kept stable.
"""

from akana_server.files.oplog import (
    FileOpLog,
    get_file_oplog,
    reset_file_oplogs,
)
from akana_server.files.service import (
    DEFAULT_MAX_READ_BYTES,
    FileEngineNotConfigured,
    FileService,
)

__all__ = [
    "DEFAULT_MAX_READ_BYTES",
    "FileEngineNotConfigured",
    "FileOpLog",
    "FileService",
    "get_file_oplog",
    "reset_file_oplogs",
]
