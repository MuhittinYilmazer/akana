"""PackManifest schema + validation — single source of truth for pack (producer)
and akana (consumer). SYSTEM_PLANNING_NOTES §5.1.

Canonical JSON Schema:  PackManifest.model_json_schema()
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SandboxTier = Literal["host", "container", "gvisor", "microvm", "wasm", "vm"]
CreatedBy = Literal["user-teaching", "manual", "marketplace"]

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ExternalTool(BaseModel):
    """An external tool required by the pack (e.g. ghidra-mcp). Extra fields allowed."""

    model_config = ConfigDict(extra="allow")

    name: str
    kind: str = "mcp_server"
    purpose: str | None = None
    required: bool = False
    probe: str | None = None          # absence detection (shell check / .mcp.json key)
    install_hint: str | None = None   # how to install (human-readable reference)
    setup_skill: str | None = None    # consent-gated install skill to run when missing


class Contains(BaseModel):
    """Content the pack registers with akana registries (reference lists).

    Only the three content types the consumer actually registers are modelled:
    ``skills`` and ``personas`` (auto-discovered from the folder layout, §1) and
    ``tools`` (the external-MCP names, mirrored from ``dependencies.external_tools``
    for listing convenience). A canonical pack omits this block entirely.
    """

    # extra="allow": a legacy pack may still declare removed fields
    # (workflows/ui_cards/plugins/memory_schema_extensions) — they parse and are
    # ignored rather than failing the pack outright.
    model_config = ConfigDict(extra="allow")

    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    personas: list[str] = Field(default_factory=list)


class Dependencies(BaseModel):
    # extra="allow": a shipping pack may still carry advisory-only fields such as
    # ``minimum_akana_version`` (declared but never version-checked in v0.1).
    model_config = ConfigDict(extra="allow")

    packs: list[str] = Field(default_factory=list)
    external_tools: list[ExternalTool] = Field(default_factory=list)


class Permissions(BaseModel):
    """Consent + PolicyEngine enforcement point. Empty network = offline."""

    model_config = ConfigDict(extra="forbid")

    network: list[str] = Field(default_factory=list)
    sandbox: SandboxTier = "container"
    secure_vault_read: list[str] = Field(default_factory=list)
    file_system: list[str] = Field(default_factory=list)


class PackManifest(BaseModel):
    """L2 Capability Pack manifest (the ``pack:`` body in pack.yaml)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    created_by: CreatedBy = "manual"
    created_at: str | None = None
    title: str | None = None
    description: str | None = None
    contains: Contains = Field(default_factory=Contains)
    dependencies: Dependencies = Field(default_factory=Dependencies)
    # v0.1 SIMPLIFICATION: a simple markdown pack (id/title/version/description +
    # skills) is valid WITHOUT a permissions block; sandbox/permissions are NOT
    # ENFORCED in v0.1 (see PACK_INTERFACE.md §5). cli.py reads perms.sandbox →
    # works fine with the default Permissions object. The former ``isolation`` /
    # ``learning`` blocks were never wired to any behaviour and are dropped from
    # the schema (a legacy pack may still declare them — they are ignored, see
    # ``_flatten_simple_form``).
    permissions: Permissions = Field(default_factory=Permissions)

    @model_validator(mode="before")
    @classmethod
    def _flatten_simple_form(cls, data: object) -> object:
        """Simple form: top-level ``skills:``/``tools:``/``personas:`` → ``contains:``.

        v0.1 simple packs (PACK_ARCHITECTURE.md §3) write these fields FLAT;
        legacy packs use ``contains.*``. Both are valid — flat fields are moved
        into ``contains`` (only if absent there; backward-compatible, additive).

        Legacy ``isolation`` / ``learning`` blocks (removed from the v0.1 schema,
        never enforced) are dropped here so an old pack still loads instead of
        failing the strict ``extra="forbid"`` check.
        """
        if isinstance(data, dict):
            for legacy in ("isolation", "learning"):
                data.pop(legacy, None)
            contains = dict(data.get("contains") or {})
            moved = False
            for key in ("skills", "tools", "personas"):
                if key in data and key not in contains:
                    contains[key] = data.pop(key)
                    moved = True
            if moved:
                data["contains"] = contains
        return data

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"pack.id must be in 'namespace/name' format: {v!r}")
        return v

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if not _VERSION_RE.match(v):
            raise ValueError(f"pack.version must be semver (X.Y.Z): {v!r}")
        return v


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_manifest(path: str | Path) -> PackManifest:
    """Read pack.yaml and parse it into a PackManifest (unwraps the ``pack:`` body)."""
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data = raw.get("pack", raw) if isinstance(raw, dict) else {}
    return PackManifest.model_validate(data)


def autodiscover_contents(manifest: PackManifest, root: Path) -> None:
    """Convention over configuration — fill ``contains.skills``/``personas`` from the folder.

    The standard pack layout is the source of truth, so a pack need not declare
    its content in ``contains``:
      skills   → every ``skills/<id>/`` dir that has a ``SKILL.md``
      personas → every ``personas/<id>.yaml`` (and legacy ``plugins/personas/``)
    Explicit ``contains.*`` always wins (override); discovery only fills a gap.
    This is the single source of the auto-discovery rule shared by the consumer
    (pack host, persona resolver) and the validator below.
    """
    if not manifest.contains.skills:
        skills_dir = root / "skills"
        if skills_dir.is_dir():
            manifest.contains.skills = sorted(
                d.name
                for d in skills_dir.iterdir()
                if d.is_dir() and (d / "SKILL.md").is_file()
            )
    if not manifest.contains.personas:
        found: set[str] = set()
        for base in (root / "personas", root / "plugins" / "personas"):
            if base.is_dir():
                for ext in ("*.yaml", "*.yml"):
                    found.update(p.stem for p in base.glob(ext))
        manifest.contains.personas = sorted(found)


def validate_pack_dir(pack_dir: str | Path) -> ValidationResult:
    """Validate a pack directory against the schema and the standard folder layout.

    Checks: pack.yaml is valid; each AUTO-DISCOVERED skill dir has manifest.yaml +
    SKILL.md; each declared persona has a file (``personas/`` or legacy
    ``plugins/personas/``); required external_tools carry a purpose.
    """
    root = Path(pack_dir)
    manifest_path = root / "pack.yaml"
    if not manifest_path.is_file():
        return ValidationResult(False, [f"pack.yaml not found: {manifest_path}"])

    try:
        m = load_manifest(manifest_path)
    except Exception as e:  # includes pydantic ValidationError
        return ValidationResult(False, [f"manifest invalid: {e}"])
    autodiscover_contents(m, root)  # skills/personas from the folder layout

    errors: list[str] = []
    warnings: list[str] = []

    if not m.contains.skills:
        warnings.append("no skills found (skills/<id>/SKILL.md) — pack provides no capability")
    for sid in m.contains.skills:
        d = root / "skills" / sid
        if not (d / "manifest.yaml").is_file() or not (d / "SKILL.md").is_file():
            errors.append(f"skill missing/incomplete (manifest.yaml + SKILL.md expected): {sid}")

    for persona in m.contains.personas:
        hit = any(
            (base / f"{persona}{ext}").is_file()
            for base in (root / "personas", root / "plugins" / "personas")
            for ext in (".yaml", ".yml", ".md")
        )
        if not hit:
            warnings.append(f"persona file not found (optional): {persona}")

    if m.dependencies.external_tools:
        for t in m.dependencies.external_tools:
            if t.required and not t.purpose:
                warnings.append(f"required external_tool '{t.name}' has no purpose")

    return ValidationResult(not errors, errors, warnings)


__all__ = [
    "Contains",
    "Dependencies",
    "ExternalTool",
    "PackManifest",
    "Permissions",
    "ValidationResult",
    "autodiscover_contents",
    "load_manifest",
    "validate_pack_dir",
]
