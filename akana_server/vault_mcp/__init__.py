"""SecureVault MCP surface — exposes the vault to the claude CLI as NATIVE tools.

A separate stdio MCP process (like the ``akana_memory`` child) that reads the
``data_dir``-scoped secure vault directly (file/crypto; no ``app.state``, no loopback
REST). Full *usage* tools: discover what secrets exist, fetch them, and store/update or
delete them. Ungated by explicit owner decision — this is the assistant's own vault (see
``vault_mcp.tools``).
"""

from __future__ import annotations

from akana_server.vault_mcp.tools import VaultTools, vault_schemas

__all__ = ["VaultTools", "vault_schemas"]
