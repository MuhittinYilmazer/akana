"""SecureVault tool registry — ``data_dir``-scoped, called from the stdio MCP.

Vault access is pure file/crypto (:mod:`akana_server.secure_vault`); UNLIKE the
capability tools there is NO loopback REST — the MCP child reads the vault directly
(same pattern as the ``akana_memory`` child reading the memory DB).

These tools are the full *usage* surface — discover + fetch a secret so the model
can act with it, plus store/update (vault_set / vault_set_credential) and delete
(vault_delete / vault_delete_credential). Access-gating is intentionally OFF here by
explicit owner decision: vault access is all-or-nothing — this is the ASSISTANT'S OWN
vault, so the model may read OR mutate any secret; the vault audits every access. There
is no per-pack secret scoping.

This module is the SINGLE SOURCE for the vault tool schemas — the in-process
Gemini/OpenAI surface (:mod:`akana_server.orchestrator.vault_tools`) derives its
declarations from here, so the two surfaces never diverge.

Names follow the MCP charset (underscores); descriptions are model-facing
(English-first, per the i18n effort) and tell the model WHEN to call each tool.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from akana_server import secure_vault

log = logging.getLogger(__name__)

#: System-boundary input cap — model-generated args can be arbitrarily long.
_ARG_MAX = 256


def _clip(value: Any, limit: int = _ARG_MAX) -> str:
    return str(value or "").strip()[:limit]


def _clip_value(value: Any) -> str:
    """Secret VALUE normaliser — strips, but does NOT cap (unlike ``_clip``/``_ARG_MAX``).

    Truncating here would silently corrupt a long API key/token and still report
    'saved'; instead reject an oversize value with a ValueError (caught by
    :meth:`VaultTools.handle_tool_call` → ``{"error": ...}``) so the model learns the
    write failed. Mirrors the REST surface's 422 for the same input."""
    text = str(value if value is not None else "").strip()
    if len(text) > secure_vault.MAX_SECRET_VALUE_LEN:
        raise ValueError(
            f"value too long (max {secure_vault.MAX_SECRET_VALUE_LEN} chars)"
        )
    return text


__all__ = ["VaultTools", "vault_schemas"]

#: MCP tool schemas (name, description, JSON Schema ``input_schema``). SINGLE SOURCE
#: — the in-process surface derives ``parameters`` from these.
_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "name": "vault_list",
        "description": (
            "List what is stored in Akana's secure vault: scalar secret key names "
            "and credential profiles (namespace / profile / field names). Returns "
            "NAMES ONLY — never values. Call it first to discover what credentials "
            "exist before fetching one."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "vault_get",
        "description": (
            "Read a scalar secret's value from the secure vault by its key name "
            "(e.g. an API key or token). Use vault_list first if you don't know the "
            "exact key name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Scalar secret key name (e.g. 'github_token').",
                }
            },
            "required": ["key"],
        },
    },
    {
        "name": "vault_get_credential",
        "description": (
            "Read a stored credential profile from the secure vault. Give the "
            "namespace (e.g. 'reddit') and optionally the profile (default "
            "'default') and a single field name. Without a field, returns ALL "
            "fields of the profile so you can use the credential to act on the "
            "user's behalf."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Credential namespace (e.g. 'reddit').",
                },
                "profile": {
                    "type": "string",
                    "description": "Profile name (default 'default').",
                },
                "field": {
                    "type": "string",
                    "description": "Optional single field (e.g. 'password'); omit for all fields.",
                },
            },
            "required": ["namespace"],
        },
    },
    {
        "name": "vault_set",
        "description": (
            "Store or update a scalar secret in Akana's secure vault (e.g. an API key "
            "or token) under a key name. Overwrites any existing value for that key. "
            "Use this when the user gives you a secret to keep; it is encrypted at "
            "rest. To remove one, use vault_delete."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Scalar secret key name (e.g. 'github_token').",
                },
                "value": {"type": "string", "description": "The secret value to store."},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "vault_set_credential",
        "description": (
            "Store or update ONE field of a credential profile in the secure vault "
            "(namespace + profile + field, e.g. reddit / default / password). Merges "
            "with existing fields — call once per field (e.g. username, then "
            "password). Use vault_delete_credential to remove a field or the profile."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Credential namespace (e.g. 'reddit').",
                },
                "profile": {
                    "type": "string",
                    "description": "Profile name (default 'default').",
                },
                "field": {
                    "type": "string",
                    "description": "Field name (e.g. 'username' or 'password').",
                },
                "value": {"type": "string", "description": "The value for this field."},
            },
            "required": ["namespace", "field", "value"],
        },
    },
    {
        "name": "vault_delete",
        "description": (
            "Delete a scalar secret from the secure vault by its key name. Removing a "
            "key that does not exist is a no-op. Use vault_list to see exact key names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Scalar secret key name to delete.",
                }
            },
            "required": ["key"],
        },
    },
    {
        "name": "vault_delete_credential",
        "description": (
            "Delete a stored credential from the secure vault. With a 'field', removes "
            "just that field of the namespace/profile; WITHOUT a 'field', deletes the "
            "WHOLE profile (all its fields). Use vault_list to see what exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Credential namespace (e.g. 'reddit').",
                },
                "profile": {
                    "type": "string",
                    "description": "Profile name (default 'default').",
                },
                "field": {
                    "type": "string",
                    "description": "Optional single field to remove; omit to delete the whole profile.",
                },
            },
            "required": ["namespace"],
        },
    },
)


def vault_schemas() -> list[dict[str, Any]]:
    """Copy of the MCP schemas (name, description, ``input_schema``)."""
    return [dict(s) for s in _SCHEMAS]


class VaultTools:
    """``data_dir``-scoped secure-vault read/write/delete tools.

    Each ``_tool_*`` method takes a single ``args`` dict and returns a
    JSON-serialisable result. An error IS a result: ``{"error": "..."}`` is
    returned (the MCP layer derives ``isError`` from it) — exceptions do not leak.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)

    @property
    def _handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "vault_list": self._tool_vault_list,
            "vault_get": self._tool_vault_get,
            "vault_get_credential": self._tool_vault_get_credential,
            "vault_set": self._tool_vault_set,
            "vault_set_credential": self._tool_vault_set_credential,
            "vault_delete": self._tool_vault_delete,
            "vault_delete_credential": self._tool_vault_delete_credential,
        }

    def handle_tool_call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a tool call; unknown name, bad input or crash → ``{"error": ...}``."""
        handler = self._handlers.get(name)
        if handler is None:
            return {"error": f"unknown tool: {name}"}
        try:
            return handler(args if isinstance(args, dict) else {})
        except ValueError as e:  # secure_vault validation (bad namespace/profile/key)
            return {"error": f"invalid request: {e}"[:300]}
        except Exception as e:  # noqa: BLE001 — a tool error must not break the MCP turn
            log.warning("vault tool failed: %s", name, exc_info=True)
            return {"error": f"{type(e).__name__}: {e}"[:300]}

    def _tool_vault_list(self, args: dict[str, Any]) -> dict[str, Any]:
        return secure_vault.inventory(self._data_dir)

    def _tool_vault_get(self, args: dict[str, Any]) -> dict[str, Any]:
        key = _clip(args.get("key"))
        if not key:
            return {"error": "key is empty"}
        value = secure_vault.get_scalar(self._data_dir, key, consumer="mcp:vault")
        if not value:
            return {"error": f"no secret named '{key}'"}
        return {"key": key, "value": value}

    def _tool_vault_get_credential(self, args: dict[str, Any]) -> dict[str, Any]:
        namespace = _clip(args.get("namespace"))
        if not namespace:
            return {"error": "namespace is empty"}
        profile = _clip(args.get("profile")) or "default"
        field = _clip(args.get("field"))
        fields = secure_vault.load_fields(
            self._data_dir, namespace, profile, consumer="mcp:vault"
        )
        if not fields:
            return {"error": f"no credential for {namespace}/{profile}"}
        if field:
            if field not in fields:
                return {"error": f"field '{field}' not found in {namespace}/{profile}"}
            return {
                "namespace": namespace,
                "profile": profile,
                "field": field,
                "value": fields[field],
            }
        return {"namespace": namespace, "profile": profile, "fields": fields}

    def _tool_vault_set(self, args: dict[str, Any]) -> dict[str, Any]:
        key = _clip(args.get("key"))
        if not key:
            return {"error": "key is empty"}
        value = _clip_value(args.get("value"))
        if not value:
            return {"error": "value is empty (use vault_delete to remove a secret)"}
        secure_vault.set_scalar(self._data_dir, key, value, consumer="mcp:vault")
        return {"key": key, "status": "saved"}

    def _tool_vault_set_credential(self, args: dict[str, Any]) -> dict[str, Any]:
        namespace = _clip(args.get("namespace"))
        if not namespace:
            return {"error": "namespace is empty"}
        profile = _clip(args.get("profile")) or "default"
        field = _clip(args.get("field"))
        if not field:
            return {"error": "field is empty"}
        value = _clip_value(args.get("value"))
        if not value:
            return {"error": "value is empty (use vault_delete_credential to remove a field)"}
        secure_vault.set_fields(
            self._data_dir, namespace, {field: value}, profile, consumer="mcp:vault"
        )
        return {"namespace": namespace, "profile": profile, "field": field, "status": "saved"}

    def _tool_vault_delete(self, args: dict[str, Any]) -> dict[str, Any]:
        key = _clip(args.get("key"))
        if not key:
            return {"error": "key is empty"}
        removed = secure_vault.delete_scalar(self._data_dir, key, consumer="mcp:vault")
        return {"key": key, "removed": removed, "status": "deleted" if removed else "absent"}

    def _tool_vault_delete_credential(self, args: dict[str, Any]) -> dict[str, Any]:
        namespace = _clip(args.get("namespace"))
        if not namespace:
            return {"error": "namespace is empty"}
        profile = _clip(args.get("profile")) or "default"
        field = _clip(args.get("field"))
        if field:
            removed = secure_vault.delete_field(
                self._data_dir, namespace, field, profile, consumer="mcp:vault"
            )
            return {
                "namespace": namespace,
                "profile": profile,
                "field": field,
                "removed": removed,
                "status": "deleted" if removed else "absent",
            }
        result = secure_vault.delete_profile(
            self._data_dir, namespace, profile, consumer="mcp:vault"
        )
        removed = bool(result.get("removed"))
        return {**result, "status": "deleted" if removed else "absent"}
