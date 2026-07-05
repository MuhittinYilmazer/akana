"""SKILL.md parser — YAML frontmatter + markdown body (SkillEngine F0.1).

The frontmatter is the L1 metadata candidate (name + description, etc.); the body
is the L2 content. Errors are reported descriptively via `SkillParseError` (path +
reason); the caller (registry scan) ensures a single broken skill does not block
the others.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER_DELIMITER = "---"

#: Required fields for frontmatter-based skills (those without manifest.yaml).
REQUIRED_FIELDS = ("name", "description")

#: Fields expected to be plain-text lists (a single string → a one-element list).
LIST_FIELDS = ("triggers", "tags", "cursor_skills", "tools_allowed")

#: SKILL_VISION_PLAN §4.4 — the 5 skill types.
KNOWN_SKILL_TYPES = ("skill", "rule", "role", "playbook", "snippet")


class SkillParseError(ValueError):
    """SKILL.md frontmatter error — path + descriptive reason."""

    def __init__(self, reason: str, *, path: str | Path | None = None) -> None:
        self.reason = reason
        self.path = str(path) if path is not None else None
        super().__init__(f"{self.path}: {reason}" if self.path else reason)


@dataclass(frozen=True, slots=True)
class ParsedSkill:
    """Parse result: normalized frontmatter (L1 candidate) + markdown body (L2)."""

    frontmatter: dict[str, Any]
    body: str
    path: str | None = None

    @property
    def name(self) -> str | None:
        return self.frontmatter.get("name")

    @property
    def description(self) -> str | None:
        return self.frontmatter.get("description")


def split_frontmatter(text: str, *, path: str | Path | None = None) -> tuple[str | None, str]:
    """Returns ``(frontmatter_yaml | None, body)``.

    Raises a descriptive error if the frontmatter opens with ``---`` but does not
    close; if there is no frontmatter at all, returns ``(None, text)``.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_DELIMITER:
            return "".join(lines[1:i]), "".join(lines[i + 1 :])
    raise SkillParseError(
        "frontmatter opened with '---' but no closing '---' line was found",
        path=path,
    )


def _as_str_list(value: Any, key: str, path: str | Path | None) -> list[str]:
    if isinstance(value, str):
        v = value.strip()
        return [v] if v else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, (dict, list)):
                raise SkillParseError(
                    f"'{key}' list must contain only plain-text items "
                    f"({type(item).__name__} found)",
                    path=path,
                )
            s = str(item).strip()
            if s:
                out.append(s)
        return out
    raise SkillParseError(
        f"'{key}' field must be a list or text, got {type(value).__name__}",
        path=path,
    )


def normalize_metadata(raw: dict[str, Any], *, path: str | Path | None = None) -> dict[str, Any]:
    """Normalizes frontmatter/manifest fields (id→name, lists, type)."""
    out = dict(raw)
    name = out.get("name") or out.get("id")
    if name is not None:
        name = str(name).strip()
        if name:
            out["name"] = name
    if out.get("type") is not None:
        t = str(out["type"]).strip().lower()
        if t not in KNOWN_SKILL_TYPES:
            raise SkillParseError(
                f"invalid 'type' value: {out['type']!r} "
                f"(expected: {', '.join(KNOWN_SKILL_TYPES)})",
                path=path,
            )
        out["type"] = t
    for key in LIST_FIELDS:
        if key in out and out[key] is not None:
            out[key] = _as_str_list(out[key], key, path)
    for key in ("title", "description", "risk", "trust_tier"):
        if out.get(key) is not None:
            out[key] = str(out[key]).strip()
    return out


def validate_required(meta: dict[str, Any], *, path: str | Path | None = None) -> None:
    """Validates the required L1 fields; missing ones are listed in a single error."""
    missing = [f for f in REQUIRED_FIELDS if not str(meta.get(f) or "").strip()]
    if missing:
        raise SkillParseError(
            "frontmatter is missing required field(s): " + ", ".join(missing),
            path=path,
        )


def parse_skill_md(
    text: str,
    *,
    path: str | Path | None = None,
    require_frontmatter: bool = False,
) -> ParsedSkill:
    """Parses SKILL.md text: YAML frontmatter + markdown body.

    If ``require_frontmatter=True`` the frontmatter block is mandatory and the
    ``name`` + ``description`` fields are validated (skill without manifest.yaml).
    """
    raw_fm, body = split_frontmatter(text, path=path)
    if raw_fm is None:
        if require_frontmatter:
            raise SkillParseError(
                "SKILL.md does not contain a YAML frontmatter block (--- ... --- expected)",
                path=path,
            )
        return ParsedSkill(frontmatter={}, body=body, path=str(path) if path else None)
    try:
        data = yaml.safe_load(raw_fm)
    except yaml.YAMLError as e:
        raise SkillParseError(f"frontmatter YAML error: {e}", path=path) from e
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SkillParseError(
            f"frontmatter must be a YAML mapping, got {type(data).__name__}",
            path=path,
        )
    meta = normalize_metadata(data, path=path)
    if require_frontmatter:
        validate_required(meta, path=path)
    return ParsedSkill(frontmatter=meta, body=body, path=str(path) if path else None)


def parse_skill_file(path: Path, *, require_frontmatter: bool = True) -> ParsedSkill:
    """Reads and parses SKILL.md from disk; a read error is also a `SkillParseError`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise SkillParseError(f"SKILL.md could not be read: {e}", path=path) from e
    return parse_skill_md(text, path=path, require_frontmatter=require_frontmatter)
