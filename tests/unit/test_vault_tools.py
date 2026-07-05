"""In-process vault tool surface (``orchestrator.vault_tools``) — decls + dispatch.

The in-process surface is what Gemini / OpenAI / Ollama call (string results read by
the model). Tests run over the REAL ``secure_vault`` (autouse ``_isolated_vault_key``
fixture gives each test a throwaway master key). Also verifies the merge into
``gemini_tools`` (declarations appended + dispatch fall-through) and the derived
OpenAI shape, since both are single-sourced from here.
"""

from __future__ import annotations

from types import SimpleNamespace

from akana_server import secure_vault
from akana_server.orchestrator import gemini_tools as gt
from akana_server.orchestrator.llm_tools import OPENAI_TOOL_DECLS, dispatch_llm_tool
from akana_server.orchestrator.vault_tools import (
    VAULT_TOOL_DECLS,
    VAULT_TOOL_NAMES,
    dispatch_vault_tool,
)


def _settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path)


# --- Declaration schema -----------------------------------------------------


_ALL_VAULT_NAMES = {
    "vault_list",
    "vault_get",
    "vault_get_credential",
    "vault_set",
    "vault_set_credential",
    "vault_delete",
    "vault_delete_credential",
}


def test_decls_cover_the_full_usage_surface() -> None:
    names = [d["name"] for d in VAULT_TOOL_DECLS]
    assert names == [
        "vault_list",
        "vault_get",
        "vault_get_credential",
        "vault_set",
        "vault_set_credential",
        "vault_delete",
        "vault_delete_credential",
    ]
    assert VAULT_TOOL_NAMES == set(names)
    for d in VAULT_TOOL_DECLS:
        assert d["description"]
        assert d["parameters"]["type"] == "object"

    get = next(d for d in VAULT_TOOL_DECLS if d["name"] == "vault_get")
    assert get["parameters"]["required"] == ["key"]
    cred = next(d for d in VAULT_TOOL_DECLS if d["name"] == "vault_get_credential")
    assert cred["parameters"]["required"] == ["namespace"]
    sset = next(d for d in VAULT_TOOL_DECLS if d["name"] == "vault_set")
    assert sset["parameters"]["required"] == ["key", "value"]
    scred = next(d for d in VAULT_TOOL_DECLS if d["name"] == "vault_set_credential")
    assert scred["parameters"]["required"] == ["namespace", "field", "value"]
    dele = next(d for d in VAULT_TOOL_DECLS if d["name"] == "vault_delete")
    assert dele["parameters"]["required"] == ["key"]
    dcred = next(d for d in VAULT_TOOL_DECLS if d["name"] == "vault_delete_credential")
    assert dcred["parameters"]["required"] == ["namespace"]


def test_merged_into_gemini_and_openai_surfaces() -> None:
    gem_names = {d["name"] for d in gt.GEMINI_TOOL_DECLS}
    assert _ALL_VAULT_NAMES <= gem_names
    # OpenAI shape is derived single-source from GEMINI_TOOL_DECLS.
    oai_names = {d["function"]["name"] for d in OPENAI_TOOL_DECLS}
    assert _ALL_VAULT_NAMES <= oai_names


# --- vault_list -------------------------------------------------------------


def test_vault_list_formats_inventory(tmp_path) -> None:
    secure_vault.set_scalar(tmp_path, "github_token", "ghp")
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u", "password": "p"})
    out = dispatch_vault_tool(_settings(tmp_path), "c", "vault_list", {})
    assert "github_token" in out
    assert "reddit/default" in out
    assert "username" in out and "password" in out


def test_vault_list_empty(tmp_path) -> None:
    out = dispatch_vault_tool(_settings(tmp_path), "c", "vault_list", {})
    assert "empty" in out.lower()


# --- vault_get --------------------------------------------------------------


def test_vault_get_returns_raw_value(tmp_path) -> None:
    secure_vault.set_scalar(tmp_path, "github_token", "ghp_secret")
    out = dispatch_vault_tool(_settings(tmp_path), "c", "vault_get", {"key": "github_token"})
    assert out == "ghp_secret"


def test_vault_get_empty_key(tmp_path) -> None:
    out = dispatch_vault_tool(_settings(tmp_path), "c", "vault_get", {"key": "  "})
    assert "empty" in out.lower()


def test_vault_get_missing(tmp_path) -> None:
    out = dispatch_vault_tool(_settings(tmp_path), "c", "vault_get", {"key": "nope"})
    assert "no secret named" in out.lower()


# --- vault_get_credential ---------------------------------------------------


def test_vault_get_credential_whole_bundle(tmp_path) -> None:
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u", "password": "p"})
    out = dispatch_vault_tool(
        _settings(tmp_path), "c", "vault_get_credential", {"namespace": "reddit"}
    )
    assert "password: p" in out
    assert "username: u" in out


def test_vault_get_credential_single_field(tmp_path) -> None:
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u", "password": "p"})
    out = dispatch_vault_tool(
        _settings(tmp_path),
        "c",
        "vault_get_credential",
        {"namespace": "reddit", "field": "password"},
    )
    assert out == "p"


def test_vault_get_credential_empty_namespace(tmp_path) -> None:
    out = dispatch_vault_tool(
        _settings(tmp_path), "c", "vault_get_credential", {"namespace": " "}
    )
    assert "namespace is empty" in out.lower()


def test_vault_get_credential_not_found(tmp_path) -> None:
    out = dispatch_vault_tool(
        _settings(tmp_path), "c", "vault_get_credential", {"namespace": "ghost"}
    )
    assert "no credential found" in out.lower()


def test_vault_get_credential_unknown_field(tmp_path) -> None:
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u"})
    out = dispatch_vault_tool(
        _settings(tmp_path),
        "c",
        "vault_get_credential",
        {"namespace": "reddit", "field": "token"},
    )
    assert "not found" in out.lower()


def test_vault_get_credential_invalid_namespace_is_clean_text(tmp_path) -> None:
    # Uppercase fails the ^[a-z]... charset → ValueError → clean "Invalid vault request".
    out = dispatch_vault_tool(
        _settings(tmp_path), "c", "vault_get_credential", {"namespace": "BadNS"}
    )
    assert "invalid vault request" in out.lower()


# --- vault_set / vault_set_credential ---------------------------------------


def test_vault_set_persists_scalar(tmp_path) -> None:
    out = dispatch_vault_tool(
        _settings(tmp_path), "c", "vault_set", {"key": "github_token", "value": "ghp_x"}
    )
    assert "saved" in out.lower() and "github_token" in out
    assert secure_vault.get_scalar(tmp_path, "github_token") == "ghp_x"


def test_vault_set_empty_value(tmp_path) -> None:
    out = dispatch_vault_tool(
        _settings(tmp_path), "c", "vault_set", {"key": "k", "value": "  "}
    )
    assert "empty" in out.lower()


def test_vault_set_rejects_oversize_instead_of_truncating(tmp_path) -> None:
    """VAULT-1: an oversize scalar must be REJECTED (clean error text), never silently
    clipped to a corrupt copy that still reports 'Saved'."""
    big = "x" * (secure_vault.MAX_SECRET_VALUE_LEN + 50)
    out = dispatch_vault_tool(
        _settings(tmp_path), "c", "vault_set", {"key": "github_token", "value": big}
    )
    assert "too long" in out.lower()
    assert "saved" not in out.lower()
    # Nothing (not even a truncated copy) was written.
    assert secure_vault.get_scalar(tmp_path, "github_token") is None


def test_vault_set_credential_rejects_oversize_instead_of_truncating(tmp_path) -> None:
    """VAULT-1: same reject-not-truncate contract for a credential field value."""
    big = "y" * (secure_vault.MAX_SECRET_VALUE_LEN + 50)
    out = dispatch_vault_tool(
        _settings(tmp_path),
        "c",
        "vault_set_credential",
        {"namespace": "reddit", "field": "password", "value": big},
    )
    assert "too long" in out.lower()
    assert "saved" not in out.lower()
    assert secure_vault.load_fields(tmp_path, "reddit", "default") == {}


def test_vault_set_credential_merges(tmp_path) -> None:
    dispatch_vault_tool(
        _settings(tmp_path),
        "c",
        "vault_set_credential",
        {"namespace": "reddit", "field": "username", "value": "u"},
    )
    out = dispatch_vault_tool(
        _settings(tmp_path),
        "c",
        "vault_set_credential",
        {"namespace": "reddit", "field": "password", "value": "p"},
    )
    assert "saved" in out.lower()
    assert secure_vault.load_fields(tmp_path, "reddit", "default") == {
        "username": "u",
        "password": "p",
    }


def test_vault_set_credential_empty_field(tmp_path) -> None:
    out = dispatch_vault_tool(
        _settings(tmp_path),
        "c",
        "vault_set_credential",
        {"namespace": "reddit", "field": " ", "value": "p"},
    )
    assert "field is empty" in out.lower()


# --- vault_delete / vault_delete_credential ---------------------------------


def test_vault_delete_scalar(tmp_path) -> None:
    secure_vault.set_scalar(tmp_path, "github_token", "ghp")
    out = dispatch_vault_tool(
        _settings(tmp_path), "c", "vault_delete", {"key": "github_token"}
    )
    assert "deleted" in out.lower()
    assert secure_vault.get_scalar(tmp_path, "github_token") is None


def test_vault_delete_absent_scalar_says_nothing_to_delete(tmp_path) -> None:
    # Honest signal: the model is told nothing was there, not a phantom success.
    out = dispatch_vault_tool(
        _settings(tmp_path), "c", "vault_delete", {"key": "never_stored"}
    )
    assert "no secret named" in out.lower()
    assert "to delete" in out.lower()


def test_vault_delete_credential_field(tmp_path) -> None:
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u", "password": "p"})
    out = dispatch_vault_tool(
        _settings(tmp_path),
        "c",
        "vault_delete_credential",
        {"namespace": "reddit", "field": "password"},
    )
    assert "deleted" in out.lower()
    assert secure_vault.load_fields(tmp_path, "reddit", "default") == {"username": "u"}


def test_vault_delete_absent_field_says_nothing_to_delete(tmp_path) -> None:
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u"})
    out = dispatch_vault_tool(
        _settings(tmp_path),
        "c",
        "vault_delete_credential",
        {"namespace": "reddit", "field": "password"},
    )
    assert "no field" in out.lower()
    # the existing field is untouched
    assert secure_vault.load_fields(tmp_path, "reddit", "default") == {"username": "u"}


def test_vault_delete_credential_whole_profile(tmp_path) -> None:
    secure_vault.set_fields(tmp_path, "reddit", {"username": "u", "password": "p"})
    out = dispatch_vault_tool(
        _settings(tmp_path), "c", "vault_delete_credential", {"namespace": "reddit"}
    )
    assert "whole credential profile" in out.lower()
    assert secure_vault.load_fields(tmp_path, "reddit", "default") == {}


def test_vault_delete_credential_missing_profile(tmp_path) -> None:
    out = dispatch_vault_tool(
        _settings(tmp_path), "c", "vault_delete_credential", {"namespace": "ghost"}
    )
    assert "no credential profile" in out.lower()


# --- fall-through + defensive ------------------------------------------------


def test_non_vault_name_returns_none(tmp_path) -> None:
    # Lets the shared gemini dispatcher fall through to its own unknown-tool handling.
    assert dispatch_vault_tool(_settings(tmp_path), "c", "memory_search", {}) is None


def test_none_args_safe(tmp_path) -> None:
    out = dispatch_vault_tool(_settings(tmp_path), "c", "vault_get", None)
    assert "empty" in out.lower()


def test_defensive_on_unexpected_error(tmp_path, monkeypatch) -> None:
    def boom(dd):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(secure_vault, "inventory", boom)
    out = dispatch_vault_tool(_settings(tmp_path), "c", "vault_list", {})
    assert "unavailable" in out.lower()


# --- routed through the merged gemini/openai dispatchers --------------------


def test_gemini_dispatch_routes_to_vault(tmp_path) -> None:
    secure_vault.set_scalar(tmp_path, "github_token", "ghp_via_gemini")
    out = gt.dispatch_gemini_tool(
        _settings(tmp_path), "c", "vault_get", {"key": "github_token"}
    )
    assert out == "ghp_via_gemini"


def test_llm_dispatch_routes_to_vault(tmp_path) -> None:
    secure_vault.set_scalar(tmp_path, "github_token", "ghp_via_openai")
    out = dispatch_llm_tool(
        _settings(tmp_path), "c", "vault_get", {"key": "github_token"}
    )
    assert out == "ghp_via_openai"
