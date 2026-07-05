"""SecureVault native tools — in-process, provider-neutral (Gemini / OpenAI / Ollama).

Mirrors ``gemini_tools``' memory surface for the vault: a declaration list (merged
into ``GEMINI_TOOL_DECLS`` so every provider surface picks it up) and a
string-returning dispatch (the model reads the result text).

The declarations are DERIVED from the MCP registry (``vault_mcp.tools.vault_schemas``)
— SINGLE SOURCE; the MCP (claude) and native (gemini/openai) surfaces never diverge
(same pattern as ``llm_tools`` deriving the OpenAI shape from ``GEMINI_TOOL_DECLS``).

The full *usage* surface (access-gating is intentionally OFF per owner decision — the
model may read OR mutate any secret; the vault audits every access):
- ``vault_list``: inventory (names only, no values) so the model knows what exists.
- ``vault_get`` / ``vault_get_credential``: read a scalar value / a credential field.
- ``vault_set`` / ``vault_set_credential``: store or update a scalar / a credential field.
- ``vault_delete`` / ``vault_delete_credential``: remove a scalar / a field or whole profile.

DEFENSIVE: every dispatch path converts errors to clean text (never raises) — a tool
error breaks neither the text turn nor the voice session; the model reads the result.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from akana_server.vault_mcp.tools import vault_schemas

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)

#: Names this module handles — dispatch returns ``None`` for anything else so the
#: shared gemini dispatcher can fall through to its own unknown-tool handling.
VAULT_TOOL_NAMES = frozenset(
    {
        "vault_list",
        "vault_get",
        "vault_get_credential",
        "vault_set",
        "vault_set_credential",
        "vault_delete",
        "vault_delete_credential",
    }
)

#: Gemini native function declarations — DERIVED from the MCP schemas (``input_schema``
#: → ``parameters``; the JSON-Schema body is identical, only the key name differs).
VAULT_TOOL_DECLS: list[dict[str, Any]] = [
    {
        "name": s["name"],
        "description": s.get("description", ""),
        "parameters": s["input_schema"],
    }
    for s in vault_schemas()
]


def _format_inventory(inv: dict[str, Any]) -> str:
    """Names-only inventory dict → compact text the model can read."""
    scalars = inv.get("scalars") or []
    credentials = inv.get("credentials") or []
    if not scalars and not credentials:
        return "The secure vault is empty."
    lines: list[str] = []
    if scalars:
        lines.append("Scalar secrets (keys): " + ", ".join(scalars))
    for c in credentials:
        fields = ", ".join(c.get("fields") or []) or "(no fields)"
        lines.append(f"Credential {c.get('namespace')}/{c.get('profile')}: {fields}")
    return "\n".join(lines)


def dispatch_vault_tool(
    settings: Settings, conv_id: str | None, name: str, args: dict[str, Any] | None
) -> str | None:
    """Vault tool → string result, or ``None`` if ``name`` is not a vault tool.

    Returning ``None`` lets the shared gemini dispatcher fall through to its own
    'unknown tool' handling. DEFENSIVE: a vault tool never raises — every error
    (validation, crypto, IO) is converted to clean text the model reads."""
    if name not in VAULT_TOOL_NAMES:
        return None
    args = args or {}
    try:
        from akana_server import secure_vault

        dd = settings.data_dir
        if name == "vault_list":
            return _format_inventory(secure_vault.inventory(dd))
        if name == "vault_get":
            key = str(args.get("key") or "").strip()
            if not key:
                return "Secret key is empty."
            value = secure_vault.get_scalar(dd, key, consumer="mcp:llm")
            if not value:
                return f"No secret named '{key}' found in the vault."
            return value
        if name == "vault_get_credential":
            namespace = str(args.get("namespace") or "").strip()
            if not namespace:
                return "Credential namespace is empty."
            profile = str(args.get("profile") or "default").strip() or "default"
            field = str(args.get("field") or "").strip()
            fields = secure_vault.load_fields(dd, namespace, profile, consumer="mcp:llm")
            if not fields:
                return f"No credential found for {namespace}/{profile}."
            if field:
                if field not in fields:
                    return f"Field '{field}' not found in {namespace}/{profile}."
                return fields[field]
            return "\n".join(f"{k}: {v}" for k, v in sorted(fields.items()))
        if name == "vault_set":
            key = str(args.get("key") or "").strip()
            if not key:
                return "Secret key is empty."
            # Do NOT truncate: an oversize secret is REJECTED by set_scalar (raises
            # ValueError → clean text below), never silently clipped to a corrupt copy
            # that still reports "Saved" (matches the MCP surface's _clip_value).
            value = str(args.get("value") if args.get("value") is not None else "").strip()
            if not value:
                return "Secret value is empty (use vault_delete to remove a secret)."
            secure_vault.set_scalar(dd, key, value, consumer="mcp:llm")
            return f"Saved secret '{key}' to the vault."
        if name == "vault_set_credential":
            namespace = str(args.get("namespace") or "").strip()
            if not namespace:
                return "Credential namespace is empty."
            profile = str(args.get("profile") or "default").strip() or "default"
            field = str(args.get("field") or "").strip()
            if not field:
                return "Credential field is empty."
            # Do NOT truncate: set_fields rejects an oversize value (ValueError → clean
            # text below) rather than storing a silently corrupt credential.
            value = str(args.get("value") if args.get("value") is not None else "").strip()
            if not value:
                return "Credential value is empty (use vault_delete_credential to remove a field)."
            secure_vault.set_fields(dd, namespace, {field: value}, profile, consumer="mcp:llm")
            return f"Saved credential {namespace}/{profile} field '{field}'."
        if name == "vault_delete":
            key = str(args.get("key") or "").strip()
            if not key:
                return "Secret key is empty."
            if not secure_vault.delete_scalar(dd, key, consumer="mcp:llm"):
                return f"No secret named '{key}' to delete."
            return f"Deleted secret '{key}' from the vault."
        if name == "vault_delete_credential":
            namespace = str(args.get("namespace") or "").strip()
            if not namespace:
                return "Credential namespace is empty."
            profile = str(args.get("profile") or "default").strip() or "default"
            field = str(args.get("field") or "").strip()
            if field:
                if not secure_vault.delete_field(dd, namespace, field, profile, consumer="mcp:llm"):
                    return f"No field '{field}' in {namespace}/{profile} to delete."
                return f"Deleted field '{field}' from {namespace}/{profile}."
            result = secure_vault.delete_profile(dd, namespace, profile, consumer="mcp:llm")
            if not result.get("removed"):
                return f"No credential profile {namespace}/{profile} to delete."
            return f"Deleted the whole credential profile {namespace}/{profile}."
    except ValueError as e:  # secure_vault validation (bad namespace/profile/key charset)
        return f"Invalid vault request: {e}"
    except Exception:  # pragma: no cover - a tool error must not break the turn/session
        log.warning("vault tool '%s' dispatch error", name, exc_info=True)
        return "The vault tool is currently unavailable."
    return None


__all__ = ["VAULT_TOOL_DECLS", "VAULT_TOOL_NAMES", "dispatch_vault_tool"]
